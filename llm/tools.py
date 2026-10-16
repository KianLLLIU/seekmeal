"""
工具层：把 SeekMeal 已有的确定性引擎包装成模型可调用的工具。

这一层是「计算层」的全部实现。它只调用 diet_assistant 里的纯逻辑，
**不依赖 FastAPI**，因此小模型模块可以在不启动服务的情况下独立运行、
也能在没有装 fastapi 的环境里做数据合成。

关键约定：
    · 每个工具的返回值都是普通 dict，且**所有数值都在里面**。
      表达层与数字校验都以它为唯一依据。
    · 模型不参与任何运算。它只提供参数，参数由 schema.validate 收口。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from diet_assistant.analyzer import DayLog
from diet_assistant.food_db import FoodDB
from diet_assistant.formulas import (
    ACTIVITY_FACTORS,
    compute_energy_budget,
    ema_series,
    kalman_trend,
    macro_targets,
)
from diet_assistant.planner import plan_day, suggest_snack
from diet_assistant.recommend import RecipeBook

from . import schema
from .schema import ArgError

#: 中文餐次 -> engine 默认值
DEFAULT_MEAL = "午餐"


class Session:
    """一次对话会话的状态。

    与 api.py 的内存态不同，这里不做多用户分桶——对话场景下
    一个 Session 就代表一个正在说话的人。
    """

    def __init__(self, user_id: str = "chat") -> None:
        self.user_id = user_id
        self.profile: dict[str, Any] | None = None
        self.log = DayLog()
        self.weights: list[tuple[str, float]] = []

    def reset_day(self) -> None:
        self.log = DayLog()

    def snapshot(self) -> dict[str, Any]:
        return {
            "has_profile": self.profile is not None,
            "entries": len(self.log.entries),
            "weights": len(self.weights),
        }


class Engine:
    """工具执行器。"""

    def __init__(self, db: FoodDB | None = None, book: RecipeBook | None = None) -> None:
        self.db = db or FoodDB.load()
        self.book = book or RecipeBook.load(self.db)

    # -- 内部 ---------------------------------------------------------------

    def _require_profile(self, session: Session) -> dict[str, Any]:
        if session.profile is None:
            raise ArgError("请先建立身体档案（需要体重、身高、年龄）。")
        return session.profile

    def _targets(self, session: Session):
        p = self._require_profile(session)
        budget = compute_energy_budget(
            sex=p["sex"],
            weight_kg=p["weight"],
            height_cm=p["height"],
            age=p["age"],
            activity=p.get("activity", "moderate"),
            goal=p.get("goal", "maintain"),
            weekly_delta_kg=p.get("weekly_delta_kg", 0.0),
            body_fat_pct=p.get("body_fat_pct"),
        )
        return budget, macro_targets(p["weight"], budget.target_kcal, p.get("goal", "maintain"))

    # -- 工具 ---------------------------------------------------------------

    def set_profile(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        from diet_assistant.formulas import bmi, bmi_category

        activity = args.get("activity", "moderate")
        # 模型可能给中文活动水平，做一次归一
        activity = ACTIVITY_ALIASES.get(activity, activity)
        if activity not in ACTIVITY_FACTORS:
            activity = "moderate"

        profile = {
            "sex": args.get("sex", "male"),
            "weight": args["weight"],
            "height": args["height"],
            "age": args["age"],
            "activity": activity,
            "goal": args.get("goal", "maintain"),
            "weekly_delta_kg": args.get("weekly_delta_kg", 0.0),
            "body_fat_pct": args.get("body_fat_pct"),
        }
        session.profile = profile

        budget = compute_energy_budget(
            sex=profile["sex"],
            weight_kg=profile["weight"],
            height_cm=profile["height"],
            age=profile["age"],
            activity=profile["activity"],
            goal=profile["goal"],
            weekly_delta_kg=profile["weekly_delta_kg"],
            body_fat_pct=profile["body_fat_pct"],
        )
        targets = macro_targets(profile["weight"], budget.target_kcal, profile["goal"])
        bmi_v = bmi(profile["weight"], profile["height"])
        return {
            "bmi": round(bmi_v, 1),
            "bmi_category": bmi_category(bmi_v),
            "bmr": budget.bmr,
            "bmr_formula": budget.formula,
            "tdee": budget.tdee,
            "target_kcal": budget.target_kcal,
            "daily_deficit": budget.daily_deficit,
            "weekly_delta_kg": abs(budget.weekly_delta_kg),
            "goal": profile["goal"],
            "activity": profile["activity"],
            "targets": {
                "kcal": targets.kcal,
                "protein_g": targets.protein_g,
                "fat_g": targets.fat_g,
                "carb_g": targets.carb_g,
                "fiber_g": targets.fiber_g,
            },
        }

    def log_meal(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        text = args["text"]
        meal = args.get("meal") or DEFAULT_MEAL
        results = self.db.parse_many(text)
        if not results:
            raise ArgError(
                "没能从这句话里识别出食物。可以说具体一点，例如「一碗米饭 150g鸡胸肉」。"
            )
        added = []
        for r in results:
            e = session.log.add(r.food, r.grams, meal, text)
            added.append(
                {"name": e.food.name, "grams": round(e.grams, 1), "kcal": round(e.kcal, 1)}
            )
        totals = session.log.totals()
        return {
            "added": added,
            "count": len(session.log.entries),
            "meal": meal,
            "day_kcal": round(totals.kcal, 1),
        }

    def query_remaining(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        budget, targets = self._targets(session)
        totals = session.log.totals()
        remaining = session.log.remaining(targets)
        # 注意字段名差异：Totals 用 protein/fat/carb，MacroTarget 才用 protein_g/fat_g/carb_g
        rem = remaining.as_dict()
        return {
            "totals_kcal": round(totals.kcal, 1),
            "target_kcal": targets.kcal,
            "remaining_kcal": round(remaining.kcal, 1),
            "remaining_protein_g": round(rem["protein"], 1),
            "remaining_fat_g": round(rem["fat"], 1),
            "remaining_carb_g": round(rem["carb"], 1),
            "over": remaining.kcal < 0,
        }

    def query_summary(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        budget, targets = self._targets(session)
        totals = session.log.totals()
        return {
            "entries": len(session.log.entries),
            "totals": {k: round(v, 1) for k, v in totals.as_dict().items()},
            "targets": {
                "kcal": targets.kcal,
                "protein_g": targets.protein_g,
                "fat_g": targets.fat_g,
                "carb_g": targets.carb_g,
                "fiber_g": targets.fiber_g,
            },
            "evaluations": [
                {
                    "label": e.label,
                    "actual": round(e.actual, 1),
                    "target": round(e.target, 1),
                    "status": e.status,
                    "status_zh": e.status_zh,
                }
                for e in session.log.evaluate(targets)
            ],
            "risks": session.log.risks(targets),
            "by_meal": {
                k: round(v.kcal, 1) for k, v in session.log.by_meal().items()
            },
        }

    def query_food(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        name = args["name"]
        grams = args.get("grams")
        if grams:
            hit = self.db.parse(f"{name}{grams:g}g")
            if hit is None:
                raise ArgError(f"食物库里没有找到「{name}」。")
            per = hit.food.per_gram(grams)
            return {
                "name": hit.food.name,
                "cat": hit.food.cat,
                "grams": round(grams, 1),
                "kcal": round(per["kcal"], 1),
                "protein": round(per["protein"], 1),
                "fat": round(per["fat"], 1),
                "carb": round(per["carb"], 1),
                "fiber": round(per["fiber"], 1),
                "sodium": round(per["sodium"], 1),
                "per_100g_kcal": hit.food.kcal,
            }
        hits = self.db.search(name, top_k=1)
        if not hits:
            raise ArgError(f"食物库里没有找到「{name}」。")
        f, score, how = hits[0]
        return {
            "name": f.name,
            "cat": f.cat,
            "grams": 100.0,
            "kcal": f.kcal,
            "protein": f.protein,
            "fat": f.fat,
            "carb": f.carb,
            "fiber": f.fiber,
            "sodium": f.sodium,
            "per_100g_kcal": f.kcal,
            "per_100g": True,
        }

    def recommend(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        budget, targets = self._targets(session)
        remaining = session.log.remaining(targets)
        hits = self.book.recommend(
            remaining=remaining,
            query=args.get("query", "") or "",
            preferences=args.get("preferences", []) or [],
            avoid=args.get("avoid", []) or [],
            top_k=int(args.get("top_k", 5)),
        )
        if not hits:
            raise ArgError("没有符合条件的菜谱，换个关键词试试。")
        return {
            "remaining_kcal": round(remaining.kcal, 1),
            "recipes": [
                {
                    "name": r.name,
                    "cat": r.cat,
                    "minutes": r.minutes,
                    "kcal": round(r.nutrition().kcal, 1),
                    "protein_g": round(r.nutrition().as_dict()["protein"], 1),
                    "score": round(score, 3),
                }
                for r, score, _ in hits
            ],
        }

    def plan_day(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        budget, targets = self._targets(session)
        already = session.log.totals()
        plan = plan_day(
            self.book,
            targets,
            already=already,
            preferences=args.get("preferences", []) or [],
            avoid=args.get("avoid", []) or [],
        )
        snack = suggest_snack(self.book, session.log.remaining(targets),
                              avoid=args.get("avoid", []) or [])
        return {
            "target_kcal": targets.kcal,
            "meals": [
                {
                    "meal": m.meal,
                    "target_kcal": m.target_kcal,
                    "kcal": round(m.nutrition.kcal, 1),
                    "dishes": [
                        {
                            "recipe": d.recipe.name,
                            "scale": d.scale,
                            "kcal": round(d.nutrition.kcal, 1),
                            "minutes": d.recipe.minutes,
                        }
                        for d in m.dishes
                    ],
                }
                for m in plan.meals
            ],
            "totals_kcal": round(plan.totals().kcal, 1),
            "shopping_list": [
                {"name": n, "grams": round(g, 0)} for n, g in plan.shopping_list()
            ],
            "snack": (
                {"name": snack.name, "kcal": round(snack.nutrition().kcal, 1)}
                if snack
                else None
            ),
        }

    def log_weight(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        w = args["weight"]
        session.weights.append((date.today().isoformat(), w))
        vals = [v for _, v in session.weights]
        return {
            "weight": w,
            "count": len(session.weights),
            "latest": vals[-1],
            "first": vals[0],
            "delta": round(vals[-1] - vals[0], 2),
        }

    def weight_trend(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        vals = [v for _, v in session.weights]
        if not vals:
            raise ArgError("还没有体重记录，先说一句「今天称了 78.5」吧。")
        return {
            "count": len(vals),
            "latest": vals[-1],
            "first": vals[0],
            "delta": round(vals[-1] - vals[0], 2),
            "trend_ema_last": round(ema_series(vals, 0.15)[-1], 2),
            "trend_kalman_last": round(kalman_trend(vals)[-1], 2),
            "note": "体重日波动可达 ±1kg，看趋势不要看单点。",
        }

    def delete_log(self, args: dict[str, Any], session: Session) -> dict[str, Any]:
        if not session.log.entries:
            raise ArgError("今天还没有记录可以删。")
        index = int(args.get("index", len(session.log.entries) - 1))
        removed = session.log.remove(index)
        if removed is None:
            raise ArgError(f"没有第 {index} 条记录。")
        return {
            "removed": {"name": removed.food.name, "grams": round(removed.grams, 1),
                        "kcal": round(removed.kcal, 1)},
            "count": len(session.log.entries),
            "index": index,
        }

    # -- 分发 ---------------------------------------------------------------

    def run(self, intent: str, args: dict[str, Any], session: Session) -> dict[str, Any]:
        """执行一个工具。refuse_safety 不走引擎，由 router 直接处理。"""
        if intent == schema.INTENT_REFUSE_SAFETY:
            return {"reason": args.get("reason", "涉及医疗相关话题")}
        fn = getattr(self, intent, None)
        if fn is None or intent not in schema.TOOLS:
            raise ArgError(f"没有名为 {intent!r} 的工具。")
        return fn(args, session)


#: 模型可能吐中文活动水平，做一次别名归一
ACTIVITY_ALIASES = {
    "久坐": "sedentary",
    "几乎不动": "sedentary",
    "轻度": "light",
    "偶尔运动": "light",
    "中等": "moderate",
    "适度": "moderate",
    "正常": "moderate",
    "高强度": "high",
    "经常运动": "high",
    "很高": "very_high",
    "运动员": "very_high",
    "体力劳动": "very_high",
}


def default_session() -> Session:
    return Session()
