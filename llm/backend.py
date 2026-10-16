"""
模型后端抽象。

理解层需要的能力只有一个：给一段中文，吐出一行 JSON。
不同后端都实现同一个接口，因此：

    · 规则后端（RuleBackend）不依赖任何模型，用来验证链路本身；
    · OpenAI 兼容后端（OpenAICompatBackend）零依赖，可接 LM Studio / Ollama /
      vLLM 等任何暴露 OpenAI 接口的本地服务；
    · llama.cpp 后端（LlamaCppBackend）直接吃 GGUF 文件，最省事的是量化后的 0.6B。

后端只负责「文本 -> 文本」，不做校验、不碰引擎。校验在 schema，执行在 tools。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from . import schema


class Backend(Protocol):
    """理解层后端协议。"""

    name: str

    def parse(self, text: str, *, system: str = "", history: list[dict[str, str]] | None = None) -> str:
        """返回模型原始输出，期望是一行 JSON。"""
        ...


# ---------------------------------------------------------------------------
# 规则后端：不依赖模型
# ---------------------------------------------------------------------------

_NUM = r"\d+(?:\.\d+)?"

# 「公斤」「千克」里含「斤」字，必须让 kg 系优先匹配。
# 否则「70 公斤」会先被斤规则命中，被除以 2 变成 35 公斤——这是个真实踩过的坑。
_WEIGHT_KG = re.compile(rf"({_NUM})\s*(?:kg|KG|Kg|公斤|千克)")
_WEIGHT_JIN = re.compile(rf"({_NUM})\s*斤")
_CM = re.compile(rf"({_NUM})\s*(?:cm|CM|厘米|公分)")
_METER = re.compile(rf"([12])[.\s](\d{{1,2}})\s*(?:m|M|米)")
_AGE = re.compile(rf"({_NUM})\s*(?:岁|周岁)")
_GRAMS = re.compile(rf"({_NUM})\s*(?:g|G|克)")

#: 裸数字（不带单位）的匹配，避开小数点的前后位
_BARE_NUM = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")


def _find_weight(raw: str) -> float | None:
    """从文本里抽体重（公斤）。

    两个真实踩过的坑：
      · 「公斤」里含「斤」字，必须让 kg 系优先，否则 70 公斤会变成 35 公斤；
      · 「每周 0.25 公斤」是减重幅度，不是体重。前缀是「每周」的一律跳过，
        否则「想减脂每周0.25公斤」会被读成体重 0.25。
    """
    for m in _WEIGHT_KG.finditer(raw):
        if raw[max(0, m.start() - 2):m.start()] == "每周":
            continue
        return float(m.group(1))
    for m in _WEIGHT_JIN.finditer(raw):
        if raw[max(0, m.start() - 2):m.start()] == "每周":
            continue
        return round(float(m.group(1)) / 2, 1)
    return None

_MEAL_WORDS = {
    "早餐": "早餐", "早饭": "早餐",
    "午餐": "午餐", "午饭": "午餐", "中饭": "午餐",
    "晚餐": "晚餐", "晚饭": "晚餐",
    "加餐": "加餐", "宵夜": "加餐", "夜宵": "加餐",
}

_ACTIVITY_WORDS = (
    (("久坐", "办公室", "几乎不运动", "不运动", "躺", "很少动"), "sedentary"),
    (("轻度", "偶尔运动", "每周一两次", "一两次"), "light"),
    (("中等", "适度", "正常", "每周三", "三四次"), "moderate"),
    (("高强度", "经常运动", "天天练", "每周五", "每周六", "五六次", "比较大"), "high"),
    (("运动员", "体力劳动", "专业训练", "每天两练"), "very_high"),
)

_GOAL_WORDS = (
    (("减脂", "减肥", "瘦", "掉秤", "降低体重", "降下来"), "lose"),
    (("增肌", "增重", "长肌肉", "变壮"), "gain"),
    (("保持", "维持", "不变"), "maintain"),
)

#: 记饮食的触发词
_EAT_WORDS = ("吃了", "喝了", "刚吃", "刚喝", "record", "记一下", "记录", "加一条", "来了一份")
#: 问剩余的触发词
_REMAIN_WORDS = ("还能吃", "还剩", "剩下多少", "还有多少", "剩余", "额度", "还能摄入")
#: 问汇总的触发词
_SUMMARY_WORDS = ("吃了多少", "总共", "汇总", "今天摄入", "达标", "统计", "一共吃",
                  "营养情况", "营养状况", "热量超", "饮食记录", "摄入情况")
#: 问食物营养的触发词。「千卡」「热量」这类词必须列全，
#: 否则「每100克多少千卡」会被当成在记录饮食，这是个真实踩过的坑。
_FOOD_WORDS = ("多少卡", "卡路里", "千卡", "热量", "多少蛋白", "蛋白质", "营养成分",
               "营养价值", "营养成")
#: 推荐触发词
_RECOMMEND_WORDS = ("推荐", "有什么好吃", "吃点什么", "推荐菜", "有什么菜", "有什么",
                    "菜谱", "有没有适合", "来几个", "想吃点")
#: 排餐触发词
_PLAN_WORDS = ("排一下", "安排", "一日三餐", "明天吃", "今天吃啥", "配餐", "食谱",
               "三餐", "采购清单", "规划")
#: 体重记录触发词。注意「体重」本身很宽，但第 3 步要求同时抽到数值才会命中，
#: 因此「我体重有下降吗」不会被误判成记录。
_WEIGHT_LOG_WORDS = ("称了", "称重", "称完", "体重记", "记体重", "今早称", "体重", "量了", "量一下")
#: 体重趋势触发词
_WEIGHT_TREND_WORDS = ("体重趋势", "体重变化", "掉秤", "掉称", "体重曲线", "瘦了多少",
                       "趋势", "变化", "下降")
#: 删除触发词
_DELETE_WORDS = ("删了", "删掉", "删除", "去掉", "撤销", "记错了", "不要了")

#: 中文数目字，用于抽出「推荐三道菜」里的条数
_CN_COUNT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT_RE = re.compile(r"([0-9]+|[一两二三四五六七八九十])\s*(?:道|个|条|份|样)")

#: 问句里要剥掉的营养词与疑问说法。长的写在前面，避免「多少卡路里」被「多少」截断。
_FOOD_NOISE = (
    r"多少卡路里|多少千卡|多少卡|多少热量|多少蛋白质|多少蛋白|多少|"
    r"卡路里|千卡|热量|蛋白质|蛋白|营养成分|营养价值|营养|成分|含量|"
    r"高不高|怎么样|是什么|是多少|有多少|"
    r"每100克|每百克|每100g|100克|100g|一百克|"
    r"请问|我想知道|帮我|查一下|看看|大概|每"
)
#: 连接字。它们只在问句里出现，剥掉不会伤到食物名。
#:
#: 这里必须用「整字」而不是「词」——曾经把「大概」拆成单字塞进来，
#: 结果「大米」被削成「米」、「大白菜」被削成「白菜」，食物名直接抽错。
#: 单字表里只放绝对不会出现在食物名里的字。
_CONNECT_CHARS = "的有是含和以及跟"




class RuleBackend:
    """纯规则后端。

    它不是「模型」，而是链路的验证器与兜底：
        · 在下载任何权重之前，先证明「理解 -> 计算 -> 表达」是通的；
        · 模型不可用（离线、显存不足、加载失败）时，整个功能不会瘫掉；
        · 作为评估里的第二组对照数据，说明模型到底带来了多少增量。
    """

    name = "rule"

    def parse(self, text: str, *, system: str = "", history: list[dict[str, str]] | None = None) -> str:
        return json.dumps(self.analyze(text), ensure_ascii=False)

    # -- 分析 ---------------------------------------------------------------

    def analyze(self, text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {"intent": schema.INTENT_LOG_MEAL, "args": {"text": ""}}

        # 1) 安全优先：命中医疗相关词汇直接拒答，不再往下判断
        for kw in schema.SAFETY_KEYWORDS:
            if kw in raw:
                return {
                    "intent": schema.INTENT_REFUSE_SAFETY,
                    "args": {"reason": f"涉及「{kw}」，属于医疗范畴"},
                }

        # 2) 建档：能抽齐体重/身高/年龄才算
        prof = self._extract_profile(raw)
        if prof and len(prof) >= 3 and any(w in raw for w in
                                          ("档案", "资料", "我是", "我男", "我女", "cm", "厘米",
                                           "公斤", "kg", "岁")) and not any(
                w in raw for w in _EAT_WORDS):
            return {"intent": schema.INTENT_SET_PROFILE, "args": prof}

        # 3) 记体重（要在体重趋势之前，避免「称了」被趋势规则吞掉）
        if any(w in raw for w in _WEIGHT_LOG_WORDS):
            w = _find_weight(raw)
            if w is not None:
                return {"intent": schema.INTENT_LOG_WEIGHT, "args": {"weight": w}}
            for token in _BARE_NUM.findall(raw):
                v = float(token)
                if 20 < v < 300:
                    return {"intent": schema.INTENT_LOG_WEIGHT, "args": {"weight": v}}

        # 4) 体重趋势
        if any(w in raw for w in _WEIGHT_TREND_WORDS):
            return {"intent": schema.INTENT_WEIGHT_TREND, "args": {}}

        # 5) 删除记录
        if any(w in raw for w in _DELETE_WORDS):
            m = re.search(r"第\s*(\d+)\s*条", raw)
            args = {"index": int(m.group(1)) - 1} if m else {}
            return {"intent": schema.INTENT_DELETE_LOG, "args": args}

        # 6) 排餐
        if any(w in raw for w in _PLAN_WORDS):
            return {"intent": schema.INTENT_PLAN_DAY, "args": {}}

        # 7) 推荐
        if any(w in raw for w in _RECOMMEND_WORDS):
            rargs: dict[str, Any] = {}
            q = self._recommend_query(raw)
            if q:
                rargs["query"] = q
            n = self._count_of(raw)
            if n:
                rargs["top_k"] = n
            return {"intent": schema.INTENT_RECOMMEND, "args": rargs}

        # 8) 问剩余
        if any(w in raw for w in _REMAIN_WORDS):
            return {"intent": schema.INTENT_QUERY_REMAINING, "args": {}}

        # 9) 问汇总
        if any(w in raw for w in _SUMMARY_WORDS):
            return {"intent": schema.INTENT_QUERY_SUMMARY, "args": {}}

        # 10) 问单个食物营养（要有问热量/营养的词，且不是记录语气）
        if any(w in raw for w in _FOOD_WORDS) and not any(w in raw for w in _EAT_WORDS):
            fargs: dict[str, Any] = {"name": self._food_name(raw)}
            g = _GRAMS.search(raw)
            if g:
                fargs["grams"] = float(g.group(1))
            return {"intent": schema.INTENT_QUERY_FOOD, "args": fargs}

        # 11) 记饮食（兜底：当成在说吃了什么）
        #     text 一律「原句照抄」：引擎自己会跳过「中午吃了」这类引导语，
        #     而让模型做纯复制，比让它学裁剪要可靠得多。
        meal = next((v for k, v in _MEAL_WORDS.items() if k in raw), None)
        args: dict[str, Any] = {"text": raw}
        if meal:
            args["meal"] = meal
        return {"intent": schema.INTENT_LOG_MEAL, "args": args}

    # -- 抽取辅助 -----------------------------------------------------------

    @staticmethod
    def _extract_profile(raw: str) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if re.search(r"(男|男生|男性|男的)", raw):
            args["sex"] = "male"
        elif re.search(r"(女|女生|女性|女的)", raw):
            args["sex"] = "female"

        # 已被单位规则消费的字符区间，裸数字扫描时要跳过。
        # 不跳过的话，「140 斤」里的 140（体重值）会被当成身高。
        consumed: list[tuple[int, int]] = []
        for regex in (_WEIGHT_KG, _WEIGHT_JIN, _CM, _METER, _AGE):
            for mt in regex.finditer(raw):
                consumed.append(mt.span())

        m = _CM.search(raw)
        if m:
            args["height"] = float(m.group(1))
        else:
            m = _METER.search(raw)
            if m:
                args["height"] = float(f"{m.group(1)}.{m.group(2)}")

        m = _AGE.search(raw)
        if m:
            args["age"] = int(float(m.group(1)))

        w = _find_weight(raw)
        if w is not None:
            args["weight"] = w

        # 没带单位的裸数字：三位数且在身高区间 -> 身高；两位数 -> 体重
        if "height" not in args or "weight" not in args:
            for mt in _BARE_NUM.finditer(raw):
                if any(a <= mt.start() < b for a, b in consumed):
                    continue
                v = float(mt.group())
                if "height" not in args and 140 <= v <= 210 and v == int(v):
                    args["height"] = v
                elif "weight" not in args and 35 <= v <= 150:
                    args["weight"] = v

        for words, level in _ACTIVITY_WORDS:
            if any(w in raw for w in words):
                args["activity"] = level
                break

        for words, goal in _GOAL_WORDS:
            if any(w in raw for w in words):
                args["goal"] = goal
                break

        m = re.search(rf"每周\s*({_NUM})\s*(?:kg|KG|Kg|公斤|千克)", raw)
        if m:
            args["weekly_delta_kg"] = round(min(float(m.group(1)), 1.5), 2)
        else:
            m = re.search(rf"每周\s*({_NUM})\s*斤", raw)
            if m:
                args["weekly_delta_kg"] = round(min(float(m.group(1)) / 2, 1.5), 2)

        m = re.search(rf"体脂\s*({_NUM})", raw)
        if m:
            args["body_fat_pct"] = float(m.group(1))
        return args

    @staticmethod
    def _recommend_query(raw: str) -> str:
        for kw in ("高蛋白", "低卡", "低脂", "清淡", "快手", "简单", "汤", "主食",
                   "鸡", "牛", "鱼", "虾", "蛋", "素", "蔬菜"):
            if kw in raw:
                return kw
        return ""

    @staticmethod
    def _count_of(raw: str) -> int:
        """抽出「推荐三道菜」里的 3。没说数量就返回 0（交给 schema 默认值）。"""
        m = _COUNT_RE.search(raw)
        if not m:
            return 0
        token = m.group(1)
        if token.isdigit():
            return int(token)
        return _CN_COUNT.get(token, 0)

    @staticmethod
    def _food_name(raw: str) -> str:
        """从问句里剥出食物名。

        分三步剥，顺序不能乱：
          1. 先去掉「150克」这类克重（不能只删「克」字，否则「巧克力」会变成「巧力」）
          2. 再去掉问句里的营养词与疑问说法
          3. 最后去掉「的 / 有 / 是 / 含」这类连接字与标点
        """
        cleaned = re.sub(rf"{_NUM}\s*(?:g|G|克)", "", raw)
        cleaned = re.sub(_FOOD_NOISE, "", cleaned)
        cleaned = re.sub(rf"[{_CONNECT_CHARS}]", "", cleaned)
        cleaned = re.sub(r"[?？。，、！；：\s]", "", cleaned)
        return cleaned or raw


# ---------------------------------------------------------------------------
# OpenAI 兼容后端：零依赖，接本地服务
# ---------------------------------------------------------------------------


class OpenAICompatBackend:
    """对接任何暴露 OpenAI /chat/completions 的本地服务。

    适用：LM Studio、Ollama（/v1 兼容口）、vLLM、llama.cpp server。
    只用标准库，不引入 openai 包。
    """

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:1234/v1",
        api_key: str = "not-needed",
        temperature: float = 0.0,
        max_tokens: int = 128,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.name = f"openai-compat:{model}"

    def parse(self, text: str, *, system: str = "", history: list[dict[str, str]] | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for turn in history or []:
            messages.append(turn)
        messages.append({"role": "user", "content": text})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"模型服务返回 {exc.code}：{exc.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(
                f"连不上模型服务 {self.base_url}（{exc}）。"
                "请确认本地服务已启动，或用 --backend rule 走规则模式。"
            )
        return body["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# llama.cpp 后端：直接吃 GGUF
# ---------------------------------------------------------------------------


class LlamaCppBackend:
    """本地 GGUF 推理。需要 `pip install llama-cpp-python`。"""

    def __init__(self, model_path: str, n_ctx: int = 2048, n_gpu_layers: int = -1,
                 chat_format: str | None = None) -> None:
        try:
            from llama_cpp import Llama  # 延迟导入：没装也不影响其它后端
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 llama-cpp-python。请执行：pip install llama-cpp-python"
            ) from exc
        self._llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            chat_format=chat_format,
            verbose=False,
        )
        self.name = f"llamacpp:{model_path.rsplit('/', 1)[-1]}"

    def parse(self, text: str, *, system: str = "", history: list[dict[str, str]] | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for turn in history or []:
            messages.append(turn)
        messages.append({"role": "user", "content": text})
        out = self._llm.create_chat_completion(
            messages=messages, temperature=0.0, max_tokens=128,
        )
        return out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 脚本后端：测试用
# ---------------------------------------------------------------------------


class ScriptedBackend:
    """按输入精确匹配返回预设输出。用于单元测试与回归。"""

    def __init__(self, mapping: dict[str, str], default: str | None = None) -> None:
        self.mapping = mapping
        self.default = default
        self.name = "scripted"

    def parse(self, text: str, *, system: str = "", history: list[dict[str, str]] | None = None) -> str:
        if text in self.mapping:
            return self.mapping[text]
        if self.default is not None:
            return self.default
        return json.dumps({"intent": schema.INTENT_LOG_MEAL, "args": {"text": text}},
                          ensure_ascii=False)


def make_backend(kind: str = "rule", **kw: Any) -> Backend:
    """按名称构造后端，供命令行与脚本统一使用。"""
    kind = (kind or "rule").lower()
    if kind == "rule":
        return RuleBackend()
    if kind in ("openai", "compat", "lmstudio", "ollama"):
        return OpenAICompatBackend(**kw)
    if kind in ("llamacpp", "gguf"):
        return LlamaCppBackend(**kw)
    raise ValueError(f"未知后端 {kind!r}，可选：rule / openai / llamacpp")
