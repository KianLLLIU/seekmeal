"""
饮食记录与营养核算。

把「吃了什么」变成「营养摄入是多少」，并与目标比对给出状态判定。
状态判定用固定阈值，不依赖模型，保证可解释、可测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from .food_db import Food
from .formulas import MacroTarget, atwater_kcal

MEALS = ["早餐", "午餐", "晚餐", "加餐"]

#: 判定为「达标」的容差
TOLERANCE = {
    "kcal": 0.10,
    "protein": 0.10,
    "fat": 0.15,
    "carb": 0.15,
    "fiber": 0.15,
}

STATUS_LABEL = {"low": "偏低", "ok": "达标", "high": "偏高"}


@dataclass
class Entry:
    """一条饮食记录。"""

    food: Food
    grams: float
    meal: str = "午餐"
    raw_text: str = ""

    @property
    def nutrients(self) -> dict[str, float]:
        return self.food.per_gram(self.grams)

    @property
    def kcal(self) -> float:
        return self.food.per_gram(self.grams)["kcal"]

    def describe(self) -> str:
        n = self.nutrients
        return (f"{self.food.name} {self.grams:.0f}g → "
                f"{n['kcal']:.0f} kcal｜蛋白 {n['protein']:.1f}g｜"
                f"脂肪 {n['fat']:.1f}g｜碳水 {n['carb']:.1f}g")


@dataclass
class Totals:
    """营养汇总。"""

    kcal: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carb: float = 0.0
    fiber: float = 0.0
    sodium: float = 0.0
    gl: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self.__dict__.items()}

    def scaled(self, factor: float) -> "Totals":
        """按比例缩放，用于把菜谱份量调整到目标热量。"""
        if factor <= 0:
            return Totals()
        return Totals(
            kcal=self.kcal * factor,
            protein=self.protein * factor,
            fat=self.fat * factor,
            carb=self.carb * factor,
            fiber=self.fiber * factor,
            sodium=self.sodium * factor,
            gl=self.gl * factor,
        )

    def __mul__(self, factor: float) -> "Totals":
        return self.scaled(factor)

    @property
    def kcal_from_macros(self) -> float:
        return atwater_kcal(self.protein, self.fat, self.carb)

    @property
    def ratios(self) -> dict[str, float]:
        total = self.kcal_from_macros
        if total <= 0:
            return {"protein": 0.0, "fat": 0.0, "carb": 0.0}
        return {
            "protein": self.protein * 4 / total,
            "fat": self.fat * 9 / total,
            "carb": self.carb * 4 / total,
        }


@dataclass
class ItemEval:
    """单项营养素评估。"""

    key: str
    label: str
    actual: float
    target: float
    ratio: float
    status: str
    advice: str = ""

    @property
    def status_zh(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)


@dataclass
class DayLog:
    """一天的记录。"""

    day: date = field(default_factory=date.today)
    entries: list[Entry] = field(default_factory=list)

    def add(self, food: Food, grams: float, meal: str = "午餐", raw_text: str = "") -> Entry:
        e = Entry(food=food, grams=grams, meal=meal, raw_text=raw_text)
        self.entries.append(e)
        return e

    def remove(self, index: int) -> Entry | None:
        if 0 <= index < len(self.entries):
            return self.entries.pop(index)
        return None

    def totals(self) -> Totals:
        t = Totals()
        for e in self.entries:
            n = e.nutrients
            t.kcal += n["kcal"]
            t.protein += n["protein"]
            t.fat += n["fat"]
            t.carb += n["carb"]
            t.fiber += n["fiber"]
            t.sodium += n["sodium"]
            t.gl += e.food.glycemic_load(e.grams)
        return t

    def by_meal(self) -> dict[str, Totals]:
        out = {m: Totals() for m in MEALS}
        for e in self.entries:
            n = e.nutrients
            meal = e.meal if e.meal in out else "加餐"
            t = out[meal]
            t.kcal += n["kcal"]
            t.protein += n["protein"]
            t.fat += n["fat"]
            t.carb += n["carb"]
            t.fiber += n["fiber"]
            t.sodium += n["sodium"]
            t.gl += e.food.glycemic_load(e.grams)
        return out

    def remaining(self, target: MacroTarget) -> Totals:
        """距目标还差多少（可为负，表示已超）。"""
        t = self.totals()
        return Totals(
            kcal=target.kcal - t.kcal,
            protein=target.protein_g - t.protein,
            fat=target.fat_g - t.fat,
            carb=target.carb_g - t.carb,
            fiber=target.fiber_g - t.fiber,
            sodium=target.sodium_mg_limit - t.sodium,
            gl=0.0,
        )

    def evaluate(self, target: MacroTarget) -> list[ItemEval]:
        """逐项评估当日摄入。"""
        t = self.totals()
        items = [
            ("kcal", "热量", t.kcal, target.kcal, "kcal"),
            ("protein", "蛋白质", t.protein, target.protein_g, "g"),
            ("fat", "脂肪", t.fat, target.fat_g, "g"),
            ("carb", "碳水", t.carb, target.carb_g, "g"),
            ("fiber", "膳食纤维", t.fiber, target.fiber_g, "g"),
        ]
        evals: list[ItemEval] = []
        for key, label, actual, tgt, unit in items:
            ratio = actual / tgt if tgt else 0.0
            tol = TOLERANCE[key]
            if ratio < 1 - tol:
                status, advice = "low", f"还差 {tgt - actual:.1f}{unit}"
            elif ratio > 1 + tol:
                status, advice = "high", f"超出 {actual - tgt:.1f}{unit}"
            else:
                status, advice = "ok", "在目标区间内"
            evals.append(ItemEval(key, label, round(actual, 1), round(tgt, 1),
                                  round(ratio, 3), status, advice))
        return evals

    def risks(self, target: MacroTarget) -> list[str]:
        """规则化风险提示。纯确定性规则，不做医疗判断。"""
        t = self.totals()
        out: list[str] = []
        if t.sodium > target.sodium_mg_limit:
            out.append(f"钠摄入 {t.sodium:.0f} mg，已超 {target.sodium_mg_limit:.0f} mg 建议上限，"
                       "注意酱油与加工食品用量")
        elif t.sodium > target.sodium_mg_limit * 0.8:
            out.append(f"钠摄入 {t.sodium:.0f} mg，接近建议上限")
        if t.fiber < target.fiber_g * 0.7:
            out.append(f"膳食纤维仅 {t.fiber:.1f} g，建议增加蔬菜、全谷物与豆类")
        if t.gl > 100:
            out.append(f"全天血糖负荷约 {t.gl:.0f}，偏高，建议减少精制主食与含糖饮料")
        r = t.ratios
        if r["fat"] > 0.35:
            out.append(f"脂肪供能比 {r['fat'] * 100:.0f}%，高于建议的 20-30%")
        if r["protein"] < 0.10 and t.kcal > 0:
            out.append("蛋白质供能比低于 10%，注意优质蛋白摄入")
        if not out:
            out.append("未发现明显异常")
        return out


def summarize_days(logs: Sequence[DayLog], target: MacroTarget) -> dict[str, float]:
    """多日平均摄入，用于周报。"""
    if not logs:
        return {}
    keys = ["kcal", "protein", "fat", "carb", "fiber", "sodium"]
    acc = {k: 0.0 for k in keys}
    for log in logs:
        t = log.totals()
        for k in keys:
            acc[k] += getattr(t, k)
    n = len(logs)
    return {k: round(v / n, 1) for k, v in acc.items()}


def top_contributors(entries: Iterable[Entry], key: str = "kcal", top_n: int = 3) -> list[tuple[str, float]]:
    """找出某营养素的主要来源，便于给出针对性建议。"""
    vals: list[tuple[str, float]] = []
    for e in entries:
        n = e.nutrients
        vals.append((f"{e.food.name} {e.grams:.0f}g", n.get(key, 0.0)))
    vals.sort(key=lambda x: -x[1])
    return [(k, round(v, 1)) for k, v in vals[:top_n]]
