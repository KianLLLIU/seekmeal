"""
一日三餐排餐与采购清单。

真实的一餐（尤其中式）通常是「主食 + 荤菜 + 素菜」的组合，
单道菜撑不起一顿，因此这里按餐次逐道添加，直到接近该餐热量目标。

完整版应建模为整数规划（PuLP / OR-Tools），约束包括热量区间、宏量区间、
过敏原、忌口、菜品不重复、食材复用率。当前为贪心版本，接口保持不变，
后续可直接替换为求解器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .analyzer import Totals
from .formulas import MacroTarget
from .recommend import Recipe, RecipeBook

#: 三餐热量占比
MEAL_RATIO = {"早餐": 0.30, "午餐": 0.40, "晚餐": 0.30}

#: 单道菜的份量缩放区间，避免份量失真
SCALE_MIN = 0.5
SCALE_MAX = 2.0

#: 每餐最多配几道菜
MAX_DISHES_PER_MEAL = 3

#: 累计热量达到该餐目标的比例即停止加菜
MEAL_FILL_RATIO = 0.85


@dataclass
class PlannedDish:
    """餐单中的一道菜（已按份量缩放）。"""

    recipe: Recipe
    scale: float
    nutrition: Totals
    ingredients: list[tuple[str, float]] = field(default_factory=list)

    def describe(self) -> str:
        s = "" if abs(self.scale - 1.0) < 0.02 else f"（份量 ×{self.scale:.2f}）"
        n = self.nutrition
        return f"{self.recipe.name}{s} → {n.kcal:.0f} kcal｜蛋白 {n.protein:.1f}g"


@dataclass
class PlannedMeal:
    """一餐，可含多道菜。"""

    meal: str
    dishes: list[PlannedDish] = field(default_factory=list)
    target_kcal: float = 0.0

    @property
    def nutrition(self) -> Totals:
        t = Totals()
        for d in self.dishes:
            t.kcal += d.nutrition.kcal
            t.protein += d.nutrition.protein
            t.fat += d.nutrition.fat
            t.carb += d.nutrition.carb
            t.fiber += d.nutrition.fiber
            t.sodium += d.nutrition.sodium
            t.gl += d.nutrition.gl
        return t

    @property
    def kcal(self) -> float:
        return self.nutrition.kcal

    def describe(self) -> str:
        n = self.nutrition
        names = " + ".join(d.recipe.name for d in self.dishes)
        return (f"{self.meal}｜{names}\n    小计 {n.kcal:.0f} kcal"
                f"（目标 {self.target_kcal:.0f}）｜蛋白 {n.protein:.1f}g｜"
                f"脂肪 {n.fat:.1f}g｜碳水 {n.carb:.1f}g")


@dataclass
class DayPlan:
    meals: list[PlannedMeal] = field(default_factory=list)

    def totals(self) -> Totals:
        t = Totals()
        for m in self.meals:
            n = m.nutrition
            t.kcal += n.kcal
            t.protein += n.protein
            t.fat += n.fat
            t.carb += n.carb
            t.fiber += n.fiber
            t.sodium += n.sodium
            t.gl += n.gl
        return t

    def shopping_list(self) -> list[tuple[str, float]]:
        """合并同名食材，输出采购清单（已按份量缩放）。"""
        acc: dict[str, float] = {}
        for m in self.meals:
            for d in m.dishes:
                for name, grams in d.ingredients:
                    acc[name] = acc.get(name, 0.0) + grams
        items = sorted(acc.items(), key=lambda x: -x[1])
        return [(k, round(v, 1)) for k, v in items]

    def describe(self) -> str:
        lines = [m.describe() for m in self.meals]
        t = self.totals()
        lines.append(f"合计：{t.kcal:.0f} kcal｜蛋白 {t.protein:.1f}g｜"
                     f"脂肪 {t.fat:.1f}g｜碳水 {t.carb:.1f}g｜"
                     f"纤维 {t.fiber:.1f}g｜钠 {t.sodium:.0f}mg")
        return "\n".join(lines)


def plan_day(
    book: RecipeBook,
    target: MacroTarget,
    already: Totals | None = None,
    preferences: Sequence[str] = (),
    avoid: Sequence[str] = (),
    meals: Sequence[str] = ("早餐", "午餐", "晚餐"),
    max_dishes: int = MAX_DISHES_PER_MEAL,
) -> DayPlan:
    """生成一日餐单，每餐可含多道菜。

    Args:
        already: 当天已摄入的营养，会从预算中扣除
        preferences: 偏好标签，如 ["高蛋白", "减脂"]
        avoid: 忌口食材名或标签
        max_dishes: 每餐最多菜数
    """
    already = already or Totals()
    remain_total = Totals(
        kcal=max(target.kcal - already.kcal, 0.0),
        protein=max(target.protein_g - already.protein, 0.0),
        fat=max(target.fat_g - already.fat, 0.0),
        carb=max(target.carb_g - already.carb, 0.0),
        fiber=max(target.fiber_g - already.fiber, 0.0),
        sodium=max(target.sodium_mg_limit - already.sodium, 0.0),
        gl=0.0,
    )

    plan = DayPlan()
    used: set[str] = set()

    for meal in meals:
        ratio = MEAL_RATIO.get(meal, 1.0 / max(len(meals), 1))
        meal_target = Totals(
            kcal=remain_total.kcal * ratio,
            protein=remain_total.protein * ratio,
            fat=remain_total.fat * ratio,
            carb=remain_total.carb * ratio,
            fiber=remain_total.fiber * ratio,
            sodium=remain_total.sodium * ratio,
            gl=0.0,
        )
        pm = PlannedMeal(meal=meal, dishes=[], target_kcal=round(meal_target.kcal, 1))
        acc = Totals()

        for _ in range(max_dishes):
            if acc.kcal >= meal_target.kcal * MEAL_FILL_RATIO:
                break
            gap = Totals(
                kcal=max(meal_target.kcal - acc.kcal, 0.0),
                protein=max(meal_target.protein - acc.protein, 0.0),
                fat=max(meal_target.fat - acc.fat, 0.0),
                carb=max(meal_target.carb - acc.carb, 0.0),
                fiber=max(meal_target.fiber - acc.fiber, 0.0),
                sodium=max(meal_target.sodium - acc.sodium, 0.0),
                gl=0.0,
            )
            exclude = [*avoid, *used]
            candidates = book.recommend(
                remaining=gap, preferences=preferences, avoid=exclude, cat=meal, top_k=8,
            )
            if not candidates:
                candidates = book.recommend(
                    remaining=gap, preferences=preferences, avoid=exclude, top_k=8,
                )
            if not candidates:
                break

            recipe = candidates[0][0]
            used.add(recipe.name)

            base_kcal = recipe.nutrition().kcal
            if base_kcal > 0 and gap.kcal > 0:
                # 先定标再计算，保证营养、食材、展示三处份量完全一致
                scale = round(max(SCALE_MIN, min(SCALE_MAX, gap.kcal / base_kcal)), 2)
            else:
                scale = 1.0

            dish = PlannedDish(
                recipe=recipe,
                scale=scale,
                nutrition=recipe.nutrition().scaled(scale),
                ingredients=[(f.name, round(g * scale, 1)) for f, g in recipe.ingredients],
            )
            pm.dishes.append(dish)
            n = dish.nutrition
            acc.kcal += n.kcal
            acc.protein += n.protein
            acc.fat += n.fat
            acc.carb += n.carb
            acc.fiber += n.fiber
            acc.sodium += n.sodium
            acc.gl += n.gl

        if pm.dishes:
            plan.meals.append(pm)

    return plan


def suggest_snack(book: RecipeBook, remaining: Totals, avoid: Sequence[str] = ()) -> Recipe | None:
    """加餐建议：剩余热量在 80-320 kcal 区间时才推荐。"""
    if not (80.0 <= remaining.kcal <= 320.0):
        return None
    hits = book.recommend(remaining=remaining, avoid=avoid, cat="加餐", top_k=3)
    if not hits:
        hits = book.recommend(
            remaining=Totals(kcal=min(remaining.kcal, 200.0)), avoid=avoid, top_k=3,
        )
    return hits[0][0] if hits else None
