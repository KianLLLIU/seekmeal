"""
调度器：理解 -> 计算 -> 表达 -> 校验。

这是整个模块的中枢，也是「模型不许碰数字」这条红线的执行者：

    1. 理解层（后端）把中文变成 {"intent", "args"}
    2. schema.validate 校验并归一化参数
    3. tools.Engine 调用确定性引擎算出全部数值
    4. 模板按引擎结果渲染一份「标准答案」
    5. 若启用了润色，模型改写后必须通过数字一致性校验；
       一旦出现引擎结果里没有的数字，直接丢弃模型输出，回退标准答案

第 5 步让「模型胡说数字」在结构上不可能发生。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import schema
from .backend import Backend, RuleBackend
from .prompt import SYSTEM_PROMPT
from .tools import Engine, Session

# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class ChatResult:
    text: str
    intent: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    answer: str = ""
    path: str = ""              # model / rule / fallback / clarify / error
    raw: str = ""               # 后端原始输出，便于排查
    numbers_ok: bool = True
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "intent": self.intent,
            "args": self.args,
            "result": self.result,
            "answer": self.answer,
            "path": self.path,
            "numbers_ok": self.numbers_ok,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 数字处理
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def collect_numbers(obj: Any, out: set[float] | None = None) -> set[float]:
    """递归收集结果里的全部数值，并展开成多种展示形式。

    引擎返回 174.0，模板可能写「174」也可能写「174.0」，两种都算合法。
    字符串里的数字同样计入：风险提示「钠 2415 毫克，超过上限 2000 毫克」
    这类文案也是引擎自己产出的，不是模型编的。
    """
    if out is None:
        out = set()
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        v = float(obj)
        out.add(round(v, 2))
        out.add(round(v, 1))
        if abs(v - round(v)) < 0.05:
            out.add(float(int(round(v))))
        return out
    if isinstance(obj, str):
        out.update(extract_numbers(obj))
        return out
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_numbers(v, out)
    return out


def extract_numbers(text: str) -> list[float]:
    return [float(m) for m in _NUM_RE.findall(text or "")]


def numbers_consistent(text: str, result: dict[str, Any] | None,
                       tolerance: float = 0.06) -> tuple[bool, list[float]]:
    """检查文本里的每个数字是否都能在引擎结果中找到出处。

    比绝对值：模板会把「剩余 -5.3 克」渲染成负数，而正则抽出来的是 5.3。
    符号属于排版，不是编造，因此忽略正负。

    返回 (是否一致, 找不到出处的数字列表)。
    """
    if result is None:
        return True, []
    allowed = collect_numbers(result)
    if not allowed:
        return True, []
    mags = {abs(a) for a in allowed}
    stray: list[float] = []
    for v in extract_numbers(text):
        if not any(abs(abs(v) - m) <= tolerance for m in mags):
            stray.append(v)
    return (not stray), stray


# ---------------------------------------------------------------------------
# 输出解析（容错）
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any] | None:
    """从后端输出里捞出 JSON 对象。

    小模型常见的三种毛病这里都覆盖：
        1. 用 markdown 代码块包起来
        2. 前后带解释性文字
        3. 用单引号或带尾逗号
    """
    if not raw:
        return None
    text = raw.strip()

    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    body = text[start:end + 1]

    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 兜底修复：单引号 -> 双引号，去掉尾逗号
    repaired = body.replace("'", '"')
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    try:
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 表达层：模板渲染
# ---------------------------------------------------------------------------


def _n(v: Any, nd: int = 1) -> str:
    """格式化数字：接近整数就不显示小数位。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 0.05:
        return str(int(round(f)))
    return f"{f:.{nd}f}"


def render_set_profile(r: dict[str, Any], args: dict[str, Any]) -> str:
    t = r["targets"]
    goal_zh = {"lose": "减脂", "maintain": "保持", "gain": "增肌"}.get(r.get("goal"), "保持")
    head = f"档案已建好。你的 BMI 是 {_n(r['bmi'])}，属于{r['bmi_category']}。"
    body = (
        f"基础代谢 {_n(r['bmr'])} 千卡，日常消耗 {_n(r['tdee'])} 千卡，"
        f"按{goal_zh}目标每天吃 {_n(r['target_kcal'])} 千卡。"
    )
    macros = (
        f"营养素目标：蛋白质 {_n(t['protein_g'])} 克、脂肪 {_n(t['fat_g'])} 克、"
        f"碳水 {_n(t['carb_g'])} 克、膳食纤维 {_n(t['fiber_g'])} 克。"
    )
    return head + body + macros


def render_log_meal(r: dict[str, Any], args: dict[str, Any]) -> str:
    items = "、".join(f"{a['name']} {_n(a['grams'])} 克（{_n(a['kcal'])} 千卡）" for a in r["added"])
    return (
        f"已记到{r['meal']}：{items}。"
        f"今天共 {r['count']} 条记录，累计 {_n(r['day_kcal'])} 千卡。"
    )


def render_query_remaining(r: dict[str, Any], args: dict[str, Any]) -> str:
    if r.get("over"):
        return (
            f"今天已经吃了 {_n(r['totals_kcal'])} 千卡，超过目标 {_n(r['target_kcal'])} 千卡 "
            f"{_n(abs(r['remaining_kcal']))} 千卡。接下来以清淡为主就好。"
        )
    return (
        f"今天已摄入 {_n(r['totals_kcal'])} 千卡，目标 {_n(r['target_kcal'])} 千卡，"
        f"还能吃 {_n(r['remaining_kcal'])} 千卡。"
        f"剩余配额里还可以安排蛋白质 {_n(r['remaining_protein_g'])} 克、"
        f"脂肪 {_n(r['remaining_fat_g'])} 克、碳水 {_n(r['remaining_carb_g'])} 克。"
    )


def render_query_summary(r: dict[str, Any], args: dict[str, Any]) -> str:
    tot, tgt = r["totals"], r["targets"]
    parts = [
        f"今天共 {r['entries']} 条记录，合计 {_n(tot['kcal'])} 千卡"
        f"（目标 {_n(tgt['kcal'])} 千卡）。",
        f"蛋白质 {_n(tot['protein'])}/{_n(tgt['protein_g'])} 克，"
        f"脂肪 {_n(tot['fat'])}/{_n(tgt['fat_g'])} 克，"
        f"碳水 {_n(tot['carb'])}/{_n(tgt['carb_g'])} 克。",
    ]
    off = [e for e in r["evaluations"] if e["status"] != "ok"]
    if off:
        parts.append("偏得比较多的是：" + "、".join(
            f"{e['label']}（{e['status_zh']}）" for e in off[:3]) + "。")
    if r["risks"]:
        parts.append("提醒：" + r["risks"][0])
    return "".join(parts)


def render_query_food(r: dict[str, Any], args: dict[str, Any]) -> str:
    scope = f"{_n(r['grams'])} 克" if not r.get("per_100g") else "每 100 克"
    return (
        f"{r['name']}（{r['cat']}）{scope}：{_n(r['kcal'])} 千卡，"
        f"蛋白质 {_n(r['protein'])} 克、脂肪 {_n(r['fat'])} 克、碳水 {_n(r['carb'])} 克、"
        f"膳食纤维 {_n(r['fiber'])} 克、钠 {_n(r['sodium'])} 毫克。"
    )


def render_recommend(r: dict[str, Any], args: dict[str, Any]) -> str:
    # 刻意不用「1. 2. 3.」编号：序号本身是数字，会被数字一致性校验当成
    # 引擎结果里查无出处的编造值。渲染层只允许输出结果里有的数字。
    lines = [f"按你目前还剩 {_n(r['remaining_kcal'])} 千卡的额度，推荐这几道："]
    for rec in r["recipes"]:
        lines.append(
            f"· {rec['name']}（{_n(rec['kcal'])} 千卡，约 {rec['minutes']} 分钟）"
        )
    return " ".join(lines)


def render_plan_day(r: dict[str, Any], args: dict[str, Any]) -> str:
    lines = [f"今天目标 {_n(r['target_kcal'])} 千卡，安排如下："]
    for m in r["meals"]:
        dishes = "、".join(f"{d['recipe']}（{_n(d['kcal'])} 千卡）" for d in m["dishes"])
        lines.append(f"{m['meal']}：{dishes}，小计 {_n(m['kcal'])} 千卡。")
    if r.get("snack"):
        lines.append(f"加餐可加 {r['snack']['name']}（{_n(r['snack']['kcal'])} 千卡）。")
    lines.append(f"全天合计 {_n(r['totals_kcal'])} 千卡。")
    if r["shopping_list"]:
        items = "、".join(f"{s['name']} {_n(s['grams'])} 克" for s in r["shopping_list"])
        lines.append(f"采购清单：{items}。")
    return " ".join(lines)


def render_log_weight(r: dict[str, Any], args: dict[str, Any]) -> str:
    tail = ""
    if r["count"] > 1:
        direction = "增加" if r["delta"] > 0 else "减少"
        tail = f"和第一次相比{direction}了 {_n(abs(r['delta']))} 公斤。"
    return f"已记录 {_n(r['weight'])} 公斤，这是第 {r['count']} 次记录。{tail}"


def render_weight_trend(r: dict[str, Any], args: dict[str, Any]) -> str:
    return (
        f"共 {r['count']} 次记录，最新 {_n(r['latest'])} 公斤，"
        f"较首次变化 {_n(r['delta'])} 公斤。"
        f"趋势线目前 EMA 为 {_n(r['trend_ema_last'])} 公斤、"
        f"卡尔曼为 {_n(r['trend_kalman_last'])} 公斤。"
        f"{r['note']}"
    )


def render_delete_log(r: dict[str, Any], args: dict[str, Any]) -> str:
    rm = r["removed"]
    return f"已删除：{rm['name']} {_n(rm['grams'])} 克（{_n(rm['kcal'])} 千卡）。今天还剩 {r['count']} 条记录。"


def render_refuse_safety(r: dict[str, Any], args: dict[str, Any]) -> str:
    return schema.SAFETY_REPLY


RENDERERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str]] = {
    schema.INTENT_SET_PROFILE: render_set_profile,
    schema.INTENT_LOG_MEAL: render_log_meal,
    schema.INTENT_QUERY_REMAINING: render_query_remaining,
    schema.INTENT_QUERY_SUMMARY: render_query_summary,
    schema.INTENT_QUERY_FOOD: render_query_food,
    schema.INTENT_RECOMMEND: render_recommend,
    schema.INTENT_PLAN_DAY: render_plan_day,
    schema.INTENT_LOG_WEIGHT: render_log_weight,
    schema.INTENT_WEIGHT_TREND: render_weight_trend,
    schema.INTENT_DELETE_LOG: render_delete_log,
    schema.INTENT_REFUSE_SAFETY: render_refuse_safety,
}


def render(intent: str, result: dict[str, Any], args: dict[str, Any]) -> str:
    fn = RENDERERS.get(intent)
    if fn is None:
        return "（没有对应的回答模板）"
    return fn(result, args)


# 缺参数时的追问话术
_CLARIFY: dict[str, str] = {
    schema.INTENT_SET_PROFILE: "还请告诉我你的体重、身高和年龄，我才能算出目标热量。",
    schema.INTENT_LOG_MEAL: "说一下你吃了什么吧，例如「一碗米饭 100g 鸡胸肉」。",
    schema.INTENT_QUERY_FOOD: "想问哪种食物？例如「鸡胸肉 100 克多少热量」。",
    schema.INTENT_LOG_WEIGHT: "体重记多少？例如「今天称了 78.5」。",
    schema.INTENT_DELETE_LOG: "想删哪一条？我按你说的位置删。",
    schema.INTENT_REFUSE_SAFETY: schema.SAFETY_REPLY,
}

_NO_PROFILE = "我这边还没有你的身体档案。先告诉我性别、体重、身高、年龄，比如「男 175 厘米 70 公斤 22 岁」。"


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class Router:
    """把一句话变成回答。"""

    def __init__(
        self,
        backend: Backend | None = None,
        engine: Engine | None = None,
        session: Session | None = None,
        *,
        fallback: RuleBackend | None = None,
        paraphraser: Callable[[str], str] | None = None,
    ) -> None:
        self.backend: Backend = backend or RuleBackend()
        self.engine = engine or Engine()
        self.session = session or Session()
        #: 模型输出无法解析时的兜底后端
        self.fallback = fallback if fallback is not None else RuleBackend()
        #: 可选的表达层润色函数（输入标准答案，输出改写结果）
        self.paraphraser = paraphraser
        self.history: list[dict[str, str]] = []

    # -- 主流程 -------------------------------------------------------------

    def handle(self, text: str) -> ChatResult:
        res = ChatResult(text=text)
        if not (text or "").strip():
            res.path = "clarify"
            res.answer = "说点什么吧，比如「中午吃了一碗米饭」。"
            return res

        parsed, res.raw, res.path, res.error = self._understand(text)

        if parsed is None:
            res.answer = res.error or "我没能理解这句话，换个说法试试。"
            return res

        intent = str(parsed.get("intent", "")).strip()
        try:
            args = schema.validate(intent, parsed.get("args"))
        except schema.ArgError as exc:
            res.intent = intent
            res.path = "clarify"
            res.error = str(exc)
            res.answer = _CLARIFY.get(intent, f"我还需要一些信息：{exc}")
            return res

        res.intent = intent
        res.args = args

        # 需要档案但没有 -> 直接引导，不去碰引擎
        if intent in schema.NEEDS_PROFILE and self.session.profile is None:
            res.path = "clarify"
            res.answer = _NO_PROFILE
            return res

        try:
            result = self.engine.run(intent, args, self.session)
        except schema.ArgError as exc:
            res.path = "error"
            res.error = str(exc)
            res.answer = str(exc)
            return res

        res.result = result
        draft = render(intent, result, args)
        res.answer = draft

        # 表达层润色（默认关闭）。启用时数字必须与引擎结果一致，否则丢弃。
        if self.paraphraser is not None and intent != schema.INTENT_REFUSE_SAFETY:
            try:
                polished = self.paraphraser(draft)
                ok, stray = numbers_consistent(polished, result)
                res.numbers_ok = ok
                if ok and polished.strip():
                    res.answer = polished.strip()
                    res.path = res.path + "+paraphrase"
                else:
                    res.error = f"润色结果含未知数字 {stray}，已回退模板"
            except Exception as exc:  # 润色失败绝不影响主流程
                res.error = f"润色失败（{exc}），已回退模板"

        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": res.answer})
        return res

    # -- 理解层 -------------------------------------------------------------

    def _understand(self, text: str) -> tuple[dict[str, Any] | None, str, str, str]:
        """返回 (解析结果, 原始输出, 路径标记, 错误信息)。"""
        raw = ""
        error = ""
        try:
            raw = self.backend.parse(text, system=SYSTEM_PROMPT, history=self.history[-4:])
            parsed = extract_json(raw)
            if parsed is not None:
                # 规则后端自己就是规则，标记为 rule；其余标记为 model
                path = "rule" if isinstance(self.backend, RuleBackend) else "model"
                return parsed, raw, path, ""
            error = f"模型输出无法解析为 JSON：{raw[:120]}"
        except Exception as exc:
            error = f"模型调用失败：{exc}"

        # 兜底：规则后端再试一次
        if not isinstance(self.backend, RuleBackend) and self.fallback is not None:
            try:
                parsed = self.fallback.analyze(text)
                return parsed, raw, "fallback", error
            except Exception as exc:  # pragma: no cover
                return None, raw, "error", f"{error}；兜底也失败：{exc}"

        return None, raw, "error", error

    # -- 便捷 ---------------------------------------------------------------

    def reset(self) -> None:
        self.session = Session()
        self.history.clear()
