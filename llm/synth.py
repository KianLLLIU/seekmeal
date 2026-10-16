"""
训练数据合成：用项目已有的确定性引擎自动产出「问题 -> 意图与参数」样本。

这是本项目做领域微调的最大优势。别人要人工标注几千条问答，而这里：

    随机采样参数  ->  套口语模板  ->  真引擎验证  ->  (用户话, 目标 JSON) 对
                                        ↑
                             验证不通过的直接丢弃，样本天然干净

更重要的一点：样本的「答案」是 **意图 + 参数**，不是营养数字。
营养数字永远由引擎在推理时算，因此数据集里不含任何一个需要模型记住的数值——
从源头堵死了「模型背数字背错」这条路。

## 标注一致性（最容易出错的地方）

合成数据最大的坑不是数量不够，而是**模板文本和标注参数对不上**。
例如模板写死「想减脂」，参数却随机成 `goal: gain`；文本说「140 斤」，
参数却记 140 公斤。这种样本会把错误的对应关系教给模型，而且比没数据更糟——
因为它看起来是对的。

因此本文档遵守一条硬规则：

    标注里的每一个字段，都必须能在生成的文本里找到明确出处。

具体落实为：
    · 模板里出现 `{goal}` 才标注 goal；文本没提活动量就不标注 activity；
    · 文本文本用「斤」表述时，参数值按斤换算回公斤（值来自 weight_jin 占位符）；
    · 只有模板真的用了 `{delta_text}`，才标注 weekly_delta_kg；
    · 句子里没出现数字量词（如「三道」）就不标注 top_k，交给 schema 默认值。

`Synthesizer.check_consistency` 在生成时二次抽查这条规则，违反即丢弃。

产出格式兼容 LLaMA-Factory 的 sharegpt 数据集：

    {"conversations": [{"role": "user", ...}, {"role": "assistant", ...}], "meta": {...}}

system prompt 不逐条重复（15000 条会白占 20MB），而是单独落一份
`system_prompt.txt` 并在 `dataset_info.json` 里用 default_system 引用。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import schema
from .prompt import SYSTEM_PROMPT
from .tools import Engine, Session

# ---------------------------------------------------------------------------
# 语料素材
# ---------------------------------------------------------------------------

#: 中文数字，用于生成「一碗」「两个」这类自然说法
_CN_DIGITS = "零一二三四五六七八九"


def cn_num(n: float) -> str:
    """把小数转成口语里的中文数字（只处理日常会用到的范围）。"""
    if n == 0.5:
        return "半"
    if n == 1.5:
        return "一个半"
    i = int(n)
    if i == 1:
        return "一"
    if i == 2:
        return "两"
    if i < 10:
        return _CN_DIGITS[i]
    if i == 10:
        return "十"
    if i < 20:
        return "十" + _CN_DIGITS[i % 10]
    return str(i)


# ---------------------------------------------------------------------------
# 词表：每个词表都必须与 backend.RuleBackend 的触发词表对齐
# ---------------------------------------------------------------------------
#
# 这不是「为了迁就规则后端」。合成语料的作用是教模型「什么样的说法对应什么意图」，
# 如果同一句话连确定性解析器都读不出唯一意图，那它就是歧义样本，不该进数据集。
# 两个词表对齐 = 语料无歧义。

ACTIVITY_PHRASES = {
    "sedentary": ["久坐不动", "基本不运动", "办公室一族", "平时很少动"],
    "light": ["偶尔运动", "每周运动一两次", "轻度活动"],
    "moderate": ["中等活动量", "每周运动三四次", "活动量正常"],
    "high": ["经常运动", "每周练五六次", "运动量比较大"],
    "very_high": ["每天两练", "体力劳动者", "专业运动员"],
}

GOAL_PHRASES = {
    "lose": ["减脂", "减肥", "瘦一点", "降低体重"],
    "maintain": ["保持", "维持现状", "体重不变"],
    "gain": ["增肌", "增重", "长肌肉"],
}

SEX_PHRASES = {
    "male": ["男生", "男的", "男性"],
    "female": ["女生", "女的", "女性"],
}

#: 推荐用的关键词，同样对齐 RuleBackend._recommend_query
QUALITY_WORDS = ["清淡", "快手", "低脂", "高蛋白", "低卡"]

MEAL_WORDS = {
    "早餐": "早餐", "早饭": "早餐",
    "午餐": "午餐", "午饭": "午餐",
    "晚餐": "晚餐", "晚饭": "晚餐",
    "加餐": "加餐", "宵夜": "加餐", "夜宵": "加餐",
}

# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
#
# 占位符约定（与 check_consistency 严格对应）：
#   {sex} {height} {weight} {age} {activity} {goal} {delta_text}  建档
#   {weight_jin}   以「斤」表述的体重（参数值仍是公斤）
#   {food_phrase}  食物说法，一条模板里可出现多次
#   {meal}         餐次，可省略不写
#   {food} {grams} 问食物营养
#   {taste}        推荐的关键词
#   {n}            序号

TEMPLATES: dict[str, list[str]] = {
    schema.INTENT_SET_PROFILE: [
        "我是{sex}，{height}厘米，{weight}公斤，{age}岁，{activity}，想{goal}",
        "帮我建个档案：{sex}，{height}，{weight}公斤，{age}岁，{activity}{delta_text}",
        "{sex}，{age}岁，身高{height}，体重{weight}，{activity}，目标是{goal}",
        "我想让你帮我算每天该吃多少。{sex}，{height}厘米，{weight}公斤，{age}岁，{activity}",
        "更新一下我的资料：{weight}公斤，{height}厘米，{age}岁，{sex}，{activity}，{goal}",
        "我是{sex}的，身高{height}，体重{weight}，{age}岁了，平时{activity}，想{goal}{delta_text}",
        "身高{height}，体重{weight}，{sex}，{age}岁，{activity}，{goal}",
        "{goal}的话，我{weight}公斤{height}厘米{age}岁，{activity}，该怎么吃",
        "帮我建档。{sex}，{age}岁，{height}厘米，{weight}公斤，{activity}{delta_text}",
        "我{age}岁{sex}，体重{weight_jin}斤，身高{height}厘米，{activity}，想{goal}",
        "资料：{sex} {height} {weight}kg {age}岁 {activity} {goal}",
        "{goal}，我{sex}，{weight}公斤，身高{height}，{age}岁，{activity}{delta_text}",
    ],
    schema.INTENT_LOG_MEAL: [
        "中午吃了{food_phrase}",
        "早餐是{food_phrase}，还有{food_phrase}",
        "刚吃了{food_phrase}",
        "晚饭{food_phrase}",
        "帮我记一下，{food_phrase}",
        "{meal}吃了{food_phrase}和{food_phrase}",
        "记录：{food_phrase}，{food_phrase}",
        "今天{meal}吃了{food_phrase}",
        "我又吃了{food_phrase}",
        "加餐{food_phrase}",
        "吃了{food_phrase}，再来{food_phrase}",
        "{meal}：{food_phrase}",
        "刚吃完{food_phrase}，还有点{food_phrase}",
        "记一笔，{food_phrase}",
        "{food_phrase}、{food_phrase}、{food_phrase}",
        "我刚才{meal}的时候吃了{food_phrase}",
        "今天中午就吃了{food_phrase}",
        "宵夜是{food_phrase}",
    ],
    schema.INTENT_QUERY_REMAINING: [
        "我今晚还能吃多少",
        "今天还剩多少额度",
        "还能吃多少千卡",
        "我还能摄入多少热量",
        "剩下的热量还有多少",
        "额度还剩多少",
        "还剩多少可以吃",
        "晚上还能不能吃点东西，还能吃多少",
        "我今天的剩余配额是多少",
        "还能吃多少卡路里",
        "还有多少热量额度",
        "我现在还能吃多少",
    ],
    schema.INTENT_QUERY_SUMMARY: [
        "我今天一共吃了多少卡",
        "今天摄入多少热量了",
        "帮我看看今天的营养汇总",
        "我今天吃的达标了吗",
        "今天吃了多少，蛋白质够不够",
        "统计一下我今天吃的东西",
        "今天总共多少千卡",
        "看一下今天的营养情况",
        "我今天热量超了吗",
        "现在为止吃了多少",
        "今天的饮食记录汇总一下",
        "我的营养摄入达标情况怎么样",
    ],
    schema.INTENT_QUERY_FOOD: [
        "{food}{grams}克有多少热量",
        "{food}每100克多少卡",
        "{food}的热量是多少",
        "请问{food}每100克多少千卡",
        "{food}多少卡路里",
        "{food}{grams}克的热量和蛋白质是多少",
        "我想知道{food}的营养成分",
        "{food}含多少蛋白质",
        "{grams}克{food}是多少热量",
        "{food}的热量高不高",
        "每100克{food}多少卡",
        "{food}的营养价值怎么样",
    ],
    schema.INTENT_RECOMMEND: [
        "推荐点高蛋白的菜",
        "有什么清淡的菜推荐",
        "给我推荐几道低卡的",
        "今天想吃点什么好",
        "推荐{taste}的菜谱",
        "有什么快手的菜",
        "帮我推荐几道{taste}的",
        "晚饭吃什么好，推荐一下",
        "想吃{taste}的东西，推荐几道",
        "有没有适合清淡口味的菜谱",
        "推荐三道菜",
        "给我来几个{taste}的菜谱",
    ],
    schema.INTENT_PLAN_DAY: [
        "帮我排一下明天的三餐",
        "给我安排一天的食谱",
        "今天该怎么安排三餐",
        "排一下一日三餐",
        "帮我规划明天吃什么",
        "按我的目标安排一下今天的饮食",
        "给我做个一天的配餐方案",
        "三餐怎么安排，帮我列一下",
        "帮我搭配今天的食谱，顺带列个采购清单",
        "安排一下明天的饭",
    ],
    schema.INTENT_LOG_WEIGHT: [
        "今早称了{weight}公斤",
        "今天体重{weight}",
        "记录体重{weight}公斤",
        "我体重{weight}公斤了",
        "称重{weight}",
        "晨起体重{weight}公斤",
        "记一下体重，{weight}公斤",
        "今天量了{weight}公斤",
        "刚称完，{weight}公斤",
    ],
    schema.INTENT_WEIGHT_TREND: [
        "我最近体重趋势怎么样",
        "看一下我的体重变化",
        "我体重有下降吗",
        "体重曲线给我看看",
        "这段时间掉秤了吗",
        "我的体重趋势是个什么情况",
        "最近体重有什么变化",
        "帮我看看体重记录的趋势",
    ],
    schema.INTENT_DELETE_LOG: [
        "把刚才那条删了",
        "删除最后一条记录",
        "我记错了，去掉刚才那条",
        "撤销上一条",
        "把第{n}条删掉",
        "最后那条不要了",
        "删了刚才记的",
    ],
}

#: 安全拒答的提问模板。占比必须够，且覆盖各类触发场景。
SAFETY_TEMPLATES: list[str] = [
    "我有{condition}，该怎么吃",
    "{condition}患者饮食要注意什么",
    "我在吃{medicine}，饮食上有什么禁忌",
    "我{stage}，能按这个方案吃吗",
    "{group}每天该吃多少热量",
    "这个方案能治{condition}吗",
    "我体检报告说{condition}，帮我看看该怎么调整饮食",
    "我想{extreme}，有什么办法",
    "吃{medicine}的时候能不能吃这些",
    "{condition}能不能靠饮食治好",
]

CONDITIONS = ["糖尿病", "高血压", "痛风", "脂肪肝", "甲亢", "慢性胃炎", "肾功能不全", "高血脂"]
MEDICINES = ["二甲双胍", "降压药", "胰岛素", "甲状腺素", "抗凝药"]
STAGES = ["怀孕了", "在哺乳期", "刚做完手术", "在备孕"]
GROUPS = ["八个月大的宝宝", "上小学的孩子", "七十多岁的老人"]
EXTREMES = ["三天不吃饭减肥", "吃减肥药快速瘦", "催吐控制体重", "断食一周"]

#: 安全样本的标注。reason 是自由文本，评估时只校验「非空」，不比对字面。
SAFETY_REASON = "涉及疾病、用药或特殊人群"


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    sex: str
    weight: float
    height: float
    age: int
    activity: str
    goal: str
    weekly_delta_kg: float

    @property
    def weight_jin(self) -> float:
        return round(self.weight * 2, 1)


def sample_profile(rng: random.Random) -> Profile:
    sex = rng.choice(["male", "female"])
    if sex == "male":
        weight = round(rng.uniform(50, 105), rng.choice([0, 1]))
        height = round(rng.uniform(160, 195))
    else:
        weight = round(rng.uniform(42, 90), rng.choice([0, 1]))
        height = round(rng.uniform(150, 180))
    return Profile(
        sex=sex,
        weight=weight,
        height=float(height),
        age=rng.randint(16, 62),
        activity=rng.choice(list(ACTIVITY_PHRASES)),
        goal=rng.choice(["lose", "maintain", "gain"]),
        weekly_delta_kg=rng.choice([0, 0.25, 0.5, 0.75, 1.0]),
    )


# ---------------------------------------------------------------------------
# 合成器
# ---------------------------------------------------------------------------


class Synthesizer:
    """用模板 + 真引擎生成训练样本。"""

    def __init__(self, engine: Engine | None = None, seed: int = 20261016) -> None:
        self.engine = engine or Engine()
        self.rng = random.Random(seed)
        self.db = self.engine.db
        self.stats: dict[str, int] = {}
        self.rejected: list[tuple[str, str, str]] = []
        self.inconsistent: list[tuple[str, str, str]] = []

    # -- 词表 ---------------------------------------------------------------

    def _foods(self) -> list[Any]:
        return list(self.db.foods) if hasattr(self.db, "foods") else list(self.db._foods)

    def portion_phrase(self, food: Any) -> str:
        """生成「一碗米饭」「两个鸡蛋」这类自然说法。"""
        portions = getattr(food, "portions", None) or {}
        if portions and self.rng.random() < 0.55:
            key = self.rng.choice(list(portions))
            m = re.match(r"(\d+(?:\.\d+)?)(.*)", key)
            if m:
                n = float(m.group(1))
                unit = m.group(2).strip()
                qty = cn_num(n)
                if unit:
                    return f"{qty}{unit}{food.name}"
                return f"{qty}份{food.name}"
        if self.rng.random() < 0.6:
            grams = self.rng.choice([50, 80, 100, 120, 150, 200, 250, 300])
            return f"{food.name}{grams}克"
        grams = self.rng.choice([50, 100, 150, 200])
        return f"{grams}克{food.name}"

    # -- 建档 ---------------------------------------------------------------

    def make_set_profile(self, p: Profile) -> tuple[str, dict[str, Any]]:
        tpl = self.rng.choice(TEMPLATES[schema.INTENT_SET_PROFILE])
        has_delta = "{delta_text}" in tpl
        delta = p.weekly_delta_kg if has_delta else 0.0
        text = tpl.format(
            sex=self.rng.choice(SEX_PHRASES[p.sex]),
            height=int(p.height),
            weight=p.weight,
            weight_jin=p.weight_jin,
            age=p.age,
            activity=self.rng.choice(ACTIVITY_PHRASES[p.activity]),
            goal=self.rng.choice(GOAL_PHRASES[p.goal]),
            delta_text=f"，每周{delta}公斤" if delta else "",
        )

        # 关键：只标注文本里真的有出处的字段。
        args: dict[str, Any] = {}
        if "{sex}" in tpl:
            args["sex"] = p.sex
        if "{weight}" in tpl or "{weight_jin}" in tpl:
            args["weight"] = p.weight
        if "{height}" in tpl:
            args["height"] = p.height
        if "{age}" in tpl:
            args["age"] = p.age
        if "{activity}" in tpl:
            args["activity"] = p.activity
        if "{goal}" in tpl:
            args["goal"] = p.goal
        if has_delta and delta:
            args["weekly_delta_kg"] = delta
        return text, args

    # -- 记饮食 -------------------------------------------------------------

    def make_log_meal(self) -> tuple[str, dict[str, Any]]:
        tpl = self.rng.choice(TEMPLATES[schema.INTENT_LOG_MEAL])
        foods = self._foods()
        n_slots = tpl.count("{food_phrase}")
        count = self.rng.choice([1, 1, 2, 2, 3])

        meat = self.rng.choice(["早餐", "午餐", "晚餐", "加餐"])
        text = tpl.replace("{meal}", meat)
        for _ in range(n_slots):
            text = text.replace("{food_phrase}", self.portion_phrase(self.rng.choice(foods)), 1)
        text = text.replace("{food_phrase}", "")  # 理论上不会剩下

        # 用户说的原句原样照抄。模型不需要做任何裁剪——纯复制是最不容易出错的行为。
        args: dict[str, Any] = {"text": text}
        meal = next((v for k, v in MEAL_WORDS.items() if k in text), None)
        if meal:
            args["meal"] = meal
        return text, args

    # -- 问食物 -------------------------------------------------------------

    def make_query_food(self) -> tuple[str, dict[str, Any]]:
        food = self.rng.choice(self._foods())
        grams = self.rng.choice([50, 100, 100, 150, 200, 250])
        tpl = self.rng.choice(TEMPLATES[schema.INTENT_QUERY_FOOD])
        text = tpl.format(food=food.name, grams=grams)

        args: dict[str, Any] = {"name": food.name}
        if "{grams}" in tpl:
            args["grams"] = float(grams)
        elif "100克" in text:
            # 「每 100 克」这类固定表述
            args["grams"] = 100.0
        return text, args

    # -- 推荐 ---------------------------------------------------------------

    def make_recommend(self) -> tuple[str, dict[str, Any]]:
        tpl = self.rng.choice(TEMPLATES[schema.INTENT_RECOMMEND])
        taste = self.rng.choice(QUALITY_WORDS)
        text = tpl.format(taste=taste)

        args: dict[str, Any] = {}
        hit = next((w for w in QUALITY_WORDS if w in text), None)
        if hit:
            args["query"] = hit
        if "三道" in text:
            args["top_k"] = 3
        return text, args

    # -- 其余 ---------------------------------------------------------------

    def make_others(self, intent: str) -> tuple[str, dict[str, Any]]:
        if intent == schema.INTENT_LOG_WEIGHT:
            w = round(self.rng.uniform(45, 105), 1)
            tpl = self.rng.choice(TEMPLATES[intent])
            return tpl.format(weight=w), {"weight": w}
        if intent == schema.INTENT_DELETE_LOG:
            tpl = self.rng.choice(TEMPLATES[intent])
            if "{n}" in tpl:
                n = self.rng.randint(1, 3)
                return tpl.format(n=n), {"index": n - 1}
            return tpl, {}
        tpl = self.rng.choice(TEMPLATES.get(intent, ["你好"]))
        return tpl, {}

    def make_safety(self) -> tuple[str, dict[str, Any]]:
        tpl = self.rng.choice(SAFETY_TEMPLATES)
        text = tpl.format(
            condition=self.rng.choice(CONDITIONS),
            medicine=self.rng.choice(MEDICINES),
            stage=self.rng.choice(STAGES),
            group=self.rng.choice(GROUPS),
            extreme=self.rng.choice(EXTREMES),
        )
        return text, {"reason": SAFETY_REASON}

    # -- 校验 ---------------------------------------------------------------

    def check_consistency(self, intent: str, text: str, args: dict[str, Any]) -> str:
        """抽查「标注字段在文本里有出处」这条硬规则，返回空串表示通过。

        这是对上面那段设计说明的代码化落实。它挡住的是合成数据最隐蔽的
        一类错误：样本看起来语法流畅，但标注与文本语义不符。
        """
        if intent == schema.INTENT_SET_PROFILE:
            if "goal" in args:
                words = GOAL_PHRASES[args["goal"]]
                if not any(w in text for w in words):
                    return f"文本未出现 {args['goal']} 的目标说法"
            if "activity" in args:
                words = ACTIVITY_PHRASES[args["activity"]]
                if not any(w in text for w in words):
                    return f"文本未出现 {args['activity']} 的活动说法"
            if "sex" in args:
                words = SEX_PHRASES[args["sex"]]
                if not any(w in text for w in words):
                    return f"文本未出现 {args['sex']} 的性别说法"
            if "weekly_delta_kg" in args and "每周" not in text:
                return "文本未出现每周变化幅度"
        if intent == schema.INTENT_RECOMMEND:
            if "query" in args and args["query"] not in text:
                return "文本未出现推荐关键词"
            if "top_k" in args and "三道" not in text:
                return "文本未说明条数"
        if intent == schema.INTENT_QUERY_FOOD:
            if args.get("name") and args["name"] not in text:
                return "文本未出现该食物名"
        if intent == schema.INTENT_LOG_WEIGHT:
            if "weight" in args:
                tail = f"{args['weight']:g}"
                if tail not in text.replace(" ", ""):
                    return "文本未出现该体重数值"
        # log_meal 的 meal 本身就是从文本里反推出来的，无需再查
        return ""

    def verify(self, intent: str, args: dict[str, Any], session: Session) -> bool:
        """把样本丢给真引擎跑一遍。跑不通的样本不进数据集。

        这是数据质量的第二道闸：**由引擎自己判定对错**。
        """
        try:
            self.engine.run(intent, args, session)
            return True
        except Exception as exc:
            self.rejected.append((intent, json.dumps(args, ensure_ascii=False), str(exc)[:80]))
            return False

    # -- 主入口 -------------------------------------------------------------

    #: 各意图的配比（合计 100）。refuse_safety 必须足量，这是合规底线。
    MIX: dict[str, int] = {
        schema.INTENT_LOG_MEAL: 23,
        schema.INTENT_QUERY_REMAINING: 12,
        schema.INTENT_SET_PROFILE: 11,
        schema.INTENT_QUERY_SUMMARY: 10,
        schema.INTENT_QUERY_FOOD: 10,
        schema.INTENT_RECOMMEND: 8,
        schema.INTENT_REFUSE_SAFETY: 8,
        schema.INTENT_PLAN_DAY: 6,
        schema.INTENT_LOG_WEIGHT: 5,
        schema.INTENT_WEIGHT_TREND: 4,
        schema.INTENT_DELETE_LOG: 3,
    }

    def seed_session(self) -> Session:
        """造一个「已经用过几天」的会话。

        不预置状态的话，weight_trend / delete_log / query_* 这几类样本会因为
        「没有记录可查」被引擎合法地拒绝，白白丢掉 20% 的数据。
        """
        session = Session()
        base = self.rng.uniform(50, 95)
        for _ in range(self.rng.randint(1, 3)):
            w = round(base + self.rng.uniform(-1.5, 1.5), 1)
            try:
                self.engine.log_weight({"weight": w}, session)
            except Exception:  # pragma: no cover - 采样值一定合法
                pass
        for _ in range(self.rng.randint(1, 2)):
            text, args = self.make_log_meal()
            try:
                self.engine.log_meal(args, session)
            except Exception:
                pass
        return session

    def build(self, total: int = 15000, sessions: int = 300) -> list[dict[str, Any]]:
        """生成样本。按会话组织，保证「需要档案」的意图有合法上下文。"""
        out: list[dict[str, Any]] = []
        per_session = max(1, total // max(1, sessions))

        for si in range(sessions):
            session = self.seed_session()
            profile = sample_profile(self.rng)

            # 每个会话以建档开头，之后才有资格问剩余/汇总/推荐/排餐
            text, args = self.make_set_profile(profile)
            if self._accept(schema.INTENT_SET_PROFILE, text, args, session):
                out.append(self._wrap(schema.INTENT_SET_PROFILE, text, args))

            for _ in range(per_session):
                intent = self._pick_intent()
                if intent == schema.INTENT_REFUSE_SAFETY:
                    text, args = self.make_safety()
                    out.append(self._wrap(intent, text, args))
                    continue

                if intent == schema.INTENT_SET_PROFILE:
                    text, args = self.make_set_profile(sample_profile(self.rng))
                elif intent == schema.INTENT_LOG_MEAL:
                    text, args = self.make_log_meal()
                elif intent == schema.INTENT_QUERY_FOOD:
                    text, args = self.make_query_food()
                elif intent == schema.INTENT_RECOMMEND:
                    text, args = self.make_recommend()
                else:
                    text, args = self.make_others(intent)

                if self._accept(intent, text, args, session):
                    out.append(self._wrap(intent, text, args))

            if (si + 1) % 100 == 0:
                print(f"  已生成 {len(out)} 条（会话 {si + 1}/{sessions}）", flush=True)

        self.rng.shuffle(out)
        return out

    def _accept(self, intent: str, text: str, args: dict[str, Any], session: Session) -> bool:
        """过一致性抽查 + 引擎验证，两道都过才收。"""
        why = self.check_consistency(intent, text, args)
        if why:
            self.inconsistent.append((intent, text, why))
            return False
        return self.verify(intent, args, session)

    def _pick_intent(self) -> str:
        r = self.rng.randint(1, 100)
        acc = 0
        for intent, weight in self.MIX.items():
            acc += weight
            if r <= acc:
                return intent
        return schema.INTENT_LOG_MEAL

    @staticmethod
    def _wrap(intent: str, text: str, args: dict[str, Any]) -> dict[str, Any]:
        """包成 sharegpt 一行。system 不逐条存，见 dataset_info.json。"""
        target = json.dumps({"intent": intent, "args": args}, ensure_ascii=False)
        return {
            "conversations": [
                {"role": "user", "content": text},
                {"role": "assistant", "content": target},
            ],
            "meta": {"intent": intent},
        }


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def split_samples(
    samples: list[dict[str, Any]],
    *,
    train: float = 0.8,
    valid: float = 0.1,
    seed: int = 7,
) -> dict[str, list[dict[str, Any]]]:
    """按意图分层切分。

    分层是必要的：安全拒答样本只占 8%，纯随机切分时测试集里可能一条都没有，
    导致最关键的安全指标无法度量。
    """
    rng = random.Random(seed)
    by_intent: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        by_intent.setdefault(s["meta"]["intent"], []).append(s)

    out: dict[str, list[dict[str, Any]]] = {"train": [], "valid": [], "test": []}
    for _, items in by_intent.items():
        rng.shuffle(items)
        n = len(items)
        n_train = int(n * train)
        n_valid = int(n * valid)
        out["train"].extend(items[:n_train])
        out["valid"].extend(items[n_train:n_train + n_valid])
        out["test"].extend(items[n_train + n_valid:])

    for key in out:
        rng.shuffle(out[key])
    return out


DATASET_INFO = {
    "seekmeal": {
        "file_name": "seekmeal_train.json",
        "formatting": "sharegpt",
        "columns": {"messages": "conversations"},
        "tags": {
            "role_tag": "role",
            "content_tag": "content",
            "user_tag": "user",
            "assistant_tag": "assistant",
        },
    },
    "seekmeal_valid": {
        "file_name": "seekmeal_valid.json",
        "formatting": "sharegpt",
        "columns": {"messages": "conversations"},
        "tags": {
            "role_tag": "role",
            "content_tag": "content",
            "user_tag": "user",
            "assistant_tag": "assistant",
        },
    },
}


def write_dataset(samples: list[dict[str, Any]], out_dir: str | Path,
                  prefix: str = "seekmeal") -> dict[str, int]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parts = split_samples(samples)
    counts: dict[str, int] = {}
    for name, items in parts.items():
        path = out / f"{prefix}_{name}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=1)
        counts[name] = len(items)

    (out / "system_prompt.txt").write_text(SYSTEM_PROMPT, encoding="utf-8")

    info = json.loads(json.dumps(DATASET_INFO))
    for key, meta in info.items():
        meta["default_system"] = SYSTEM_PROMPT
    (out / "dataset_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SeekMeal 小模型训练数据合成")
    p.add_argument("--total", type=int, default=15000, help="样本总数")
    p.add_argument("--sessions", type=int, default=300, help="会话数")
    p.add_argument("--seed", type=int, default=20261016)
    p.add_argument("--out", default="", help="输出目录，留空则只打印统计不落盘")
    p.add_argument("--prefix", default="seekmeal")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    syn = Synthesizer(seed=args.seed)
    samples = syn.build(total=args.total, sessions=args.sessions)

    dist: dict[str, int] = {}
    for s in samples:
        dist[s["meta"]["intent"]] = dist.get(s["meta"]["intent"], 0) + 1

    print("-" * 56)
    print(f"生成样本：{len(samples)} 条")
    print(f"引擎否决：{len(syn.rejected)} 条")
    print(f"一致性抽查否决：{len(syn.inconsistent)} 条")
    for name in schema.INTENTS:
        print(f"  {name:<18}{dist.get(name, 0):>6}")

    if syn.inconsistent[:5]:
        print("一致性抽查样例：")
        for intent, text, why in syn.inconsistent[:5]:
            print(f"  · [{intent}] {text[:36]} -> {why}")
    if syn.rejected[:5]:
        print("引擎否决样例：")
        for intent, payload, why in syn.rejected[:5]:
            print(f"  · [{intent}] {payload[:48]} -> {why}")

    if args.out:
        counts = write_dataset(samples, args.out, args.prefix)
        print("-" * 56)
        for k, v in counts.items():
            print(f"  {args.prefix}_{k}.json  {v}")
        print(f"输出目录：{Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
