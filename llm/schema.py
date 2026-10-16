"""
意图与工具 schema —— 整个小模型模块的「单一真相」。

理解层的输出、数据合成的标注、评估脚本的判分标准，全部读取这里的定义。
只改这一处，三处自动同步。

设计约束（来自 8GB 显存与 0.6B–1.7B 模型的能力上限）：
    · 输出格式刻意保持「单层、两字段」，不用完整的 function-calling JSON。
      字段越少、层级越浅，小模型的可解析率越高。
    · 所有数值字段都带合法区间，便于参数抽取时做硬校验。
    · 数值一律由确定性引擎产生，模型只负责搬运参数。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 意图标签
# ---------------------------------------------------------------------------

INTENT_SET_PROFILE = "set_profile"
INTENT_LOG_MEAL = "log_meal"
INTENT_QUERY_REMAINING = "query_remaining"
INTENT_QUERY_SUMMARY = "query_summary"
INTENT_QUERY_FOOD = "query_food"
INTENT_RECOMMEND = "recommend"
INTENT_PLAN_DAY = "plan_day"
INTENT_LOG_WEIGHT = "log_weight"
INTENT_WEIGHT_TREND = "weight_trend"
INTENT_DELETE_LOG = "delete_log"
INTENT_REFUSE_SAFETY = "refuse_safety"

#: 全部意图。顺序即提示词与报表中的展示顺序。
INTENTS: tuple[str, ...] = (
    INTENT_SET_PROFILE,
    INTENT_LOG_MEAL,
    INTENT_QUERY_REMAINING,
    INTENT_QUERY_SUMMARY,
    INTENT_QUERY_FOOD,
    INTENT_RECOMMEND,
    INTENT_PLAN_DAY,
    INTENT_LOG_WEIGHT,
    INTENT_WEIGHT_TREND,
    INTENT_DELETE_LOG,
    INTENT_REFUSE_SAFETY,
)

#: 需要先建立档案才能执行的意图
NEEDS_PROFILE: frozenset[str] = frozenset(
    {
        INTENT_QUERY_REMAINING,
        INTENT_QUERY_SUMMARY,
        INTENT_RECOMMEND,
        INTENT_PLAN_DAY,
    }
)

#: 与医疗相关、必须拒答并引导就医的触发词。
#: 命中任意一个即判为 refuse_safety，不进入营养建议流程。
SAFETY_KEYWORDS: tuple[str, ...] = (
    # 疾病
    "糖尿病", "高血压", "高血脂", "痛风", "尿酸", "肾病", "肾衰", "肾功能", "肝功能",
    "肝炎", "脂肪肝", "甲状腺", "甲亢", "甲减", "癌症", "肿瘤", "胃炎",
    "胃溃疡", "肠炎", "贫血", "多囊", "胰岛素抵抗", "冠心病", "心脏病",
    # 用药与治疗
    "吃什么药", "用药", "服药", "药物", "胰岛素", "打针", "处方", "治疗",
    "能不能治", "怎么治", "停药", "禁忌",
    # 具体药名。只写「用药」这类泛词会漏掉「我在吃二甲双胍」这种最常见的说法，
    # 而漏掉一条用药咨询的代价，比误判一条普通问句高得多。
    "二甲双胍", "降压药", "抗凝药", "甲状腺素", "降糖药", "他汀",
    # 特殊人群
    "怀孕", "孕妇", "孕期", "哺乳", "备孕", "坐月子", "婴儿", "宝宝",
    "小孩", "孩子", "儿童", "青少年发育", "老人病", "老人",
    "手术", "术后", "刚做完",
    # 进食障碍与极端手段
    "厌食", "暴食", "催吐", "进食障碍", "泻药", "减肥药", "绝食", "断食",
    "不吃饭",
    # 求诊断
    "我是不是得", "确诊", "化验单", "体检报告", "指标异常",
)

#: 安全拒答时的固定引导话术（不由模型自由发挥）
SAFETY_REPLY = (
    "这个问题涉及疾病、用药或特殊人群，超出饮食记录工具的范围，我不能给出建议。"
    "请咨询医生或注册营养师，由专业人员结合你的具体情况判断。"
    "如果你只是想做日常饮食记录，我可以继续帮你记录吃了什么、算算营养素。"
)


# ---------------------------------------------------------------------------
# 工具 schema
# ---------------------------------------------------------------------------

#: 字段类型标记 -> 中文说明，用于生成提示词
FIELD_TYPES = {
    "str": "文本",
    "float": "小数",
    "int": "整数",
    "str_list": "文本数组",
    "bool": "真假",
}


def _f(type_: str, desc: str, **kw: Any) -> dict[str, Any]:
    """声明一个参数字段。"""
    spec: dict[str, Any] = {"type": type_, "desc": desc}
    spec.update(kw)
    return spec


#: 工具定义。key = 工具名（等于意图名，refuse_safety 除外）。
#: args 中 required=True 的字段必须由模型抽出；其余有默认值。
TOOLS: dict[str, dict[str, Any]] = {
    INTENT_SET_PROFILE: {
        "desc": "建立或更新身体档案，之后才能计算目标热量与营养素目标。",
        "args": {
            "weight": _f("float", "体重公斤", required=True, lo=20, hi=300),
            "height": _f("float", "身高厘米", required=True, lo=100, hi=250),
            "age": _f("int", "年龄", required=True, lo=10, hi=100),
            "sex": _f("str", "性别，male 或 female", enum=["male", "female"]),
            "activity": _f(
                "str", "活动水平",
                enum=["sedentary", "light", "moderate", "high", "very_high"],
            ),
            "goal": _f("str", "目标", enum=["lose", "maintain", "gain"]),
            "weekly_delta_kg": _f("float", "每周体重变化幅度，只填正数", lo=0, hi=1.5),
            "body_fat_pct": _f("float", "体脂率百分比，可选", lo=3, hi=60),
        },
        "example": {"weight": 70, "height": 175, "age": 22, "sex": "male",
                    "activity": "moderate", "goal": "lose", "weekly_delta_kg": 0.5},
    },
    INTENT_LOG_MEAL: {
        "desc": "记录吃了什么。食物原文照抄给 text，不要自己换算克重。",
        "args": {
            "text": _f("str", "用户整句话原样照抄，不要换算克重、不要删减", required=True, max_len=500),
            "meal": _f("str", "餐次", enum=["早餐", "午餐", "晚餐", "加餐"]),
        },
        "example": {"text": "一碗米饭 150g鸡胸肉", "meal": "午餐"},
    },
    INTENT_QUERY_REMAINING: {
        "desc": "询问今天还能吃多少。不需要参数。",
        "args": {},
        "example": {},
    },
    INTENT_QUERY_SUMMARY: {
        "desc": "询问今天已经吃了多少、营养是否达标。不需要参数。",
        "args": {},
        "example": {},
    },
    INTENT_QUERY_FOOD: {
        "desc": "询问某一种食物的营养成分。",
        "args": {
            "name": _f("str", "食物名称", required=True, max_len=50),
            "grams": _f("float", "克重，没说就省略", lo=1, hi=5000),
        },
        "example": {"name": "鸡胸肉", "grams": 100},
    },
    INTENT_RECOMMEND: {
        "desc": "请求推荐菜谱，可带口味或关键词。",
        "args": {
            "query": _f("str", "关键词，如「高蛋白」「清淡」，可选", max_len=100),
            "top_k": _f("int", "要几条，默认 5", lo=1, hi=20),
            "avoid": _f("str_list", "要避开的东西，可选"),
        },
        "example": {"query": "高蛋白", "top_k": 3},
    },
    INTENT_PLAN_DAY: {
        "desc": "请求安排一整天三餐与采购清单。不需要参数。",
        "args": {
            "preferences": _f("str_list", "偏好标签，可选"),
            "avoid": _f("str_list", "要避开的东西，可选"),
        },
        "example": {},
    },
    INTENT_LOG_WEIGHT: {
        "desc": "记录一次体重。",
        "args": {
            "weight": _f("float", "体重公斤", required=True, lo=20, hi=300),
        },
        "example": {"weight": 78.5},
    },
    INTENT_WEIGHT_TREND: {
        "desc": "查看体重变化趋势。不需要参数。",
        "args": {},
        "example": {},
    },
    INTENT_DELETE_LOG: {
        "desc": "删除今天的一条饮食记录，省略 index 表示删最后一条。",
        "args": {
            "index": _f("int", "记录序号，从 0 开始，可省略", lo=0, hi=1000),
        },
        "example": {},
    },
    INTENT_REFUSE_SAFETY: {
        "desc": "涉及疾病、用药、孕期、儿童或极端减肥手段的问题，必须拒答并引导就医。",
        "args": {
            "reason": _f("str", "简短说明触发了哪类安全问题", max_len=60),
        },
        "example": {"reason": "涉及糖尿病饮食治疗"},
    },
}


def tool_names() -> list[str]:
    return list(TOOLS)


def required_args(tool: str) -> list[str]:
    return [k for k, v in TOOLS[tool]["args"].items() if v.get("required")]


def optional_args(tool: str) -> list[str]:
    return [k for k, v in TOOLS[tool]["args"].items() if not v.get("required")]


# ---------------------------------------------------------------------------
# 输出协议
# ---------------------------------------------------------------------------

#: 理解层的输出：单层 JSON，两个字段。解析与生成都按这个来。
OUTPUT_TEMPLATE = '{"intent": "<意图>", "args": {<参数>}}'


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


class ArgError(ValueError):
    """参数不合法。"""


def coerce(value: Any, spec: dict[str, Any]) -> Any:
    """按 schema 把一个参数值转成合法类型与取值，非法则抛 ArgError。

    模型抽出来的值经常是字符串（`"70"`、`"175cm"`），这里负责收口。
    """
    t = spec["type"]
    if t == "float":
        try:
            v = float(str(value).strip().rstrip("kg公斤克斤磅"))
        except (TypeError, ValueError):
            raise ArgError(f"需要数字，收到 {value!r}")
    elif t == "int":
        try:
            v = int(float(str(value).strip()))
        except (TypeError, ValueError):
            raise ArgError(f"需要整数，收到 {value!r}")
    elif t == "str":
        v = str(value).strip()
    elif t == "str_list":
        if isinstance(value, str):
            v = [s for s in value.replace("、", ",").replace("，", ",").split(",") if s.strip()]
        else:
            v = [str(s).strip() for s in value]
    elif t == "bool":
        v = bool(value)
    else:  # pragma: no cover - schema 自己写错才会到这
        raise ArgError(f"未知类型 {t}")

    enum = spec.get("enum")
    if enum and v not in enum:
        raise ArgError(f"取值必须是 {enum} 之一，收到 {v!r}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        lo, hi = spec.get("lo"), spec.get("hi")
        if lo is not None and v < lo:
            raise ArgError(f"{v} 小于下限 {lo}")
        if hi is not None and v > hi:
            raise ArgError(f"{v} 超过上限 {hi}")
    if t == "str":
        ml = spec.get("max_len")
        if ml and len(v) > ml:
            v = v[:ml]
    return v


def validate(intent: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """校验一次理解层的输出，返回归一化后的 args。"""
    if intent not in TOOLS:
        raise ArgError(f"未知意图 {intent!r}")
    spec = TOOLS[intent]["args"]
    raw = args or {}
    if not isinstance(raw, dict):
        raise ArgError("args 必须是对象")

    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in spec:
            continue  # 模型多给的字段直接丢弃，不报错
        if value is None or value == "":
            continue
        cleaned[key] = coerce(value, spec[key])

    missing = [k for k in required_args(intent) if k not in cleaned]
    if missing:
        raise ArgError("缺少必填参数：" + "、".join(missing))
    return cleaned


def describe_for_prompt() -> str:
    """生成给模型看的工具清单（紧凑格式，省 token）。"""
    lines: list[str] = []
    for name, spec in TOOLS.items():
        parts: list[str] = []
        for arg, meta in spec["args"].items():
            mark = "*" if meta.get("required") else ""
            enum = meta.get("enum")
            tail = f"({ '/'.join(enum) })" if enum else ""
            parts.append(f"{arg}{mark}{tail}")
        arg_txt = ", ".join(parts) if parts else "无参数"
        lines.append(f"- {name}: {spec['desc']} 参数：{arg_txt}")
    lines.append("* 表示必填。")
    return "\n".join(lines)
