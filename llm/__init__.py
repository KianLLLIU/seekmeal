"""
SeekMeal 领域小模型模块。

用法（在项目根目录下）：

    from llm import Router, make_backend

    r = Router(make_backend("rule"))          # 先不接模型也能跑
    print(r.handle("男 175 厘米 70 公斤 22 岁，中等活动，想减脂").answer)
    print(r.handle("中午吃了一碗米饭和 150g 鸡胸肉").answer)
    print(r.handle("我今晚还能吃多少").answer)

架构（三层职责分离，模型不碰数字）：

    理解层  llm/backend.py + llm/prompt.py   中文 -> {"intent", "args"}
    计算层  llm/tools.py -> diet_assistant/*  全部数值在这里产生
    表达层  llm/router.py 的模板 + 可选润色    数值一致性校验，编数字即回退
"""

from __future__ import annotations

from .backend import (
    Backend,
    LlamaCppBackend,
    OpenAICompatBackend,
    RuleBackend,
    ScriptedBackend,
    make_backend,
)
from .router import ChatResult, Router, collect_numbers, extract_json, numbers_consistent
from .schema import INTENTS, TOOLS, ArgError
from .tools import Engine, Session

__all__ = [
    "Backend",
    "ChatResult",
    "Engine",
    "INTENTS",
    "LlamaCppBackend",
    "OpenAICompatBackend",
    "Router",
    "RuleBackend",
    "ScriptedBackend",
    "Session",
    "TOOLS",
    "ArgError",
    "collect_numbers",
    "extract_json",
    "make_backend",
    "numbers_consistent",
]
