"""
智能饮食健康助手 · 第一版（Python）

模块：
  formulas  营养与代谢计算（确定性，不允许模型参与）
  food_db   中文食物成分库与自然语言解析
  analyzer  饮食记录与营养核算
  recommend 中文菜谱推荐（TF-IDF + 营养缺口适配）
  planner   一日三餐排餐与采购清单
  cli       命令行中文交互
  api       FastAPI 接口（供 Web / 微信小程序接入）

设计参考的开源项目见 README.md。
"""

__version__ = "0.1.0"

from .analyzer import DayLog, Entry, ItemEval, Totals
from .food_db import Food, FoodDB, MatchResult
from .formulas import (
    EnergyBudget,
    MacroTarget,
    bmi,
    bmi_category,
    compute_energy_budget,
    ema_series,
    glycemic_load,
    kalman_trend,
    macro_targets,
    tdee,
)
from .planner import DayPlan, plan_day
from .recommend import Recipe, RecipeBook

__all__ = [
    "Food", "FoodDB", "MatchResult",
    "DayLog", "Entry", "Totals", "ItemEval",
    "EnergyBudget", "MacroTarget",
    "bmi", "bmi_category", "compute_energy_budget", "macro_targets",
    "tdee", "glycemic_load", "ema_series", "kalman_trend",
    "Recipe", "RecipeBook", "DayPlan", "plan_day",
]
