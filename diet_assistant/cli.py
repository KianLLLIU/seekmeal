"""
命令行中文交互入口。

运行：python -m diet_assistant.cli
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .analyzer import DayLog, Totals
from .food_db import FoodDB
from .formulas import (
    ACTIVITY_LABELS_ZH,
    MacroTarget,
    bmi,
    bmi_category,
    compute_energy_budget,
    ema_series,
    kalman_trend,
    macro_targets,
)
from .planner import plan_day, suggest_snack
from .recommend import RecipeBook

STATE_FILE = Path("diet_state.json")

SEX_OPTIONS = {"1": "male", "2": "female"}
GOAL_OPTIONS = {"1": "lose", "2": "maintain", "3": "gain"}
ACTIVITY_KEYS = list(ACTIVITY_LABELS_ZH.keys())


def _ask(prompt: str, default: str = "") -> str:
    tail = f"（默认 {default}）" if default else ""
    v = input(f"{prompt}{tail}：").strip()
    return v or default


def _ask_float(prompt: str, default: float) -> float:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print("  请输入数字")


def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return int(float(raw))
        except ValueError:
            print("  请输入整数")


class Assistant:
    """命令行助手，持有画像、记录与体重历史。"""

    def __init__(self) -> None:
        self.db = FoodDB.load()
        self.book = RecipeBook.load(self.db)
        self.profile: dict | None = None
        self.today = DayLog()
        self.weights: list[tuple[str, float]] = []
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self) -> None:
        data = {
            "profile": self.profile,
            "weights": self.weights,
            "entries": [
                {"food_id": e.food.id, "grams": e.grams, "meal": e.meal, "raw": e.raw_text}
                for e in self.today.entries
            ],
        }
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self.profile = data.get("profile")
        self.weights = [(str(d), float(w)) for d, w in data.get("weights", [])]
        for item in data.get("entries", []):
            food = self.db.get(item["food_id"])
            if food:
                self.today.add(food, item["grams"], item.get("meal", "午餐"), item.get("raw", ""))

    # ------------------------------------------------------------------
    # 画像
    # ------------------------------------------------------------------

    def ensure_profile(self) -> dict:
        if self.profile:
            return self.profile
        print("\n先建立你的身体档案（用于计算热量与营养素目标）")
        sex = SEX_OPTIONS[_ask("性别 1=男 2=女", "1")]
        weight = _ask_float("体重 kg", 70.0)
        height = _ask_float("身高 cm", 172.0)
        age = _ask_int("年龄", 20)
        print("活动水平：")
        for i, key in enumerate(ACTIVITY_KEYS, 1):
            print(f"  {i}. {ACTIVITY_LABELS_ZH[key]}")
        activity = ACTIVITY_KEYS[_ask_int("选择", 3) - 1]
        goal = GOAL_OPTIONS[_ask("目标 1=减脂 2=维持 3=增肌", "1")]
        weekly = -0.5 if goal == "lose" else (0.25 if goal == "gain" else 0.0)
        self.profile = {
            "sex": sex, "weight": weight, "height": height, "age": age,
            "activity": activity, "goal": goal, "weekly_delta_kg": weekly,
        }
        self.save()
        return self.profile

    def budget(self):
        p = self.profile
        return compute_energy_budget(
            sex=p["sex"], weight_kg=p["weight"], height_cm=p["height"], age=p["age"],
            activity=p["activity"], goal=p["goal"], weekly_delta_kg=p["weekly_delta_kg"],
        )

    def targets(self) -> MacroTarget:
        b = self.budget()
        return macro_targets(self.profile["weight"], b.target_kcal, self.profile["goal"])

    # ------------------------------------------------------------------
    # 功能
    # ------------------------------------------------------------------

    def show_profile(self) -> None:
        p = self.ensure_profile()
        b = self.budget()
        t = self.targets()
        bmi_v = bmi(p["weight"], p["height"])
        r = t.ratios()
        print("\n===== 我的档案 =====")
        print(f"性别 {('男' if p['sex'] == 'male' else '女')}｜年龄 {p['age']}｜"
              f"身高 {p['height']:.0f}cm｜体重 {p['weight']:.1f}kg")
        print(f"BMI {bmi_v:.1f}（{bmi_category(bmi_v)}）｜活动水平：{ACTIVITY_LABELS_ZH[p['activity']]}")
        print(f"BMR {b.bmr:.0f} kcal（{b.formula}）")
        print(f"TDEE {b.tdee:.0f} kcal｜每日缺口 {b.daily_deficit:.0f} kcal"
              f"｜预期每周 {b.weekly_delta_kg:+.2f} kg")
        if b.clamped:
            print("  ⚠ 目标热量已触及安全下限，已自动上调")
        print(f"目标热量 {t.kcal:.0f} kcal")
        print(f"  蛋白质 {t.protein_g:.0f} g｜脂肪 {t.fat_g:.0f} g｜碳水 {t.carb_g:.0f} g｜"
              f"膳食纤维 {t.fiber_g:.0f} g")
        print(f"  供能比 蛋白 {r['protein'] * 100:.0f}% / 脂肪 {r['fat'] * 100:.0f}% / "
              f"碳水 {r['carb'] * 100:.0f}%")

    def search_food(self) -> None:
        q = _ask("\n输入食物名（支持模糊，如 鸡胸 / 米饭 / 番茄）")
        hits = self.db.search(q, top_k=5)
        if not hits:
            print("  没找到，试试更常见的说法")
            return
        print("\n匹配结果：")
        for i, (f, score, how) in enumerate(hits, 1):
            print(f"  {i}. {f.name}（{f.cat}）  匹配置信 {score:.2f} [{how}]")
            if f.portions:
                pv = " / ".join(f"{k}={v:.0f}g" for k, v in f.portions.items())
                print(f"     每100g：{f.kcal:.0f}kcal 蛋白{f.protein:.1f}g "
                      f"脂肪{f.fat:.1f}g 碳水{f.carb:.1f}g｜常见份量 {pv}")
            else:
                print(f"     每100g：{f.kcal:.0f}kcal 蛋白{f.protein:.1f}g "
                      f"脂肪{f.fat:.1f}g 碳水{f.carb:.1f}g")

    def log_meal(self) -> None:
        print("\n餐次：1=早餐 2=午餐 3=晚餐 4=加餐")
        meal = {"1": "早餐", "2": "午餐", "3": "晚餐", "4": "加餐"}[_ask("选择", "2")]
        text = _ask("吃了什么（可用顿号分隔，如：一碗米饭、150g鸡胸肉、两个鸡蛋）")
        results = self.db.parse_many(text)
        if not results:
            print("  没识别出食物，换个说法试试")
            return
        for r in results:
            e = self.today.add(r.food, r.grams, meal, text)
            tag = f"［{r.portion_used}］" if r.portion_used else ""
            conf = f"（置信 {r.score:.2f}）" if r.score < 0.9 else ""
            print(f"  ✓ {e.describe()}{tag}{conf}")
        self.save()

    def show_today(self) -> None:
        if not self.today.entries:
            print("\n今天还没有记录")
            return
        t = self.targets()
        totals = self.today.totals()
        print("\n===== 今日记录 =====")
        for i, e in enumerate(self.today.entries, 1):
            print(f"  {i}. [{e.meal}] {e.describe()}")
        print("\n===== 营养核算 =====")
        print(f"热量 {totals.kcal:.0f} / {t.kcal:.0f} kcal｜"
              f"蛋白 {totals.protein:.1f} / {t.protein_g:.1f} g｜"
              f"脂肪 {totals.fat:.1f} / {t.fat_g:.1f} g｜"
              f"碳水 {totals.carb:.1f} / {t.carb_g:.1f} g")
        print(f"膳食纤维 {totals.fiber:.1f} g｜钠 {totals.sodium:.0f} mg｜血糖负荷 {totals.gl:.0f}")
        print("\n达标情况：")
        for ev in self.today.evaluate(t):
            bar = "█" * max(1, min(int(ev.ratio * 10), 12))
            print(f"  {ev.label:<6} {ev.actual:>7.1f} / {ev.target:<7.1f} "
                  f"{bar:<12} {ev.status_zh}｜{ev.advice}")
        print("\n提示：")
        for msg in self.today.risks(t):
            print(f"  · {msg}")

    def recommend_recipes(self) -> None:
        t = self.targets()
        remaining = self.today.remaining(t)
        q = _ask("\n想吃点什么（可直接回车跳过，如：清淡高蛋白）")
        pref_raw = _ask("偏好标签（逗号分隔，如：高蛋白,减脂）")
        prefs = [p.strip() for p in pref_raw.split(",") if p.strip()]
        avoid_raw = _ask("忌口食材或标签（逗号分隔）")
        avoid = [a.strip() for a in avoid_raw.split(",") if a.strip()]
        print(f"\n剩余额度：热量 {remaining.kcal:.0f} kcal｜蛋白 {remaining.protein:.1f} g｜"
              f"脂肪 {remaining.fat:.1f} g｜碳水 {remaining.carb:.1f} g")
        hits = self.book.recommend(remaining, query=q, preferences=prefs, avoid=avoid, top_k=5)
        if not hits:
            print("  没有符合条件的菜谱")
            return
        print("\n推荐菜谱：")
        for i, (r, score, detail) in enumerate(hits, 1):
            n = r.nutrition()
            print(f"  {i}. {r.name}｜{r.cat}｜{r.minutes}分钟｜标签 {','.join(r.tags)}")
            print(f"     {n.kcal:.0f} kcal｜蛋白 {n.protein:.1f}g｜脂肪 {n.fat:.1f}g｜"
                  f"碳水 {n.carb:.1f}g｜纤维 {n.fiber:.1f}g｜钠 {n.sodium:.0f}mg")
            print(f"     综合分 {score:.3f}（营养适配 {detail['nutrition_fit']}｜"
                  f"文本相似 {detail['similarity']}）")
        self._maybe_show_steps(hits)

    def _maybe_show_steps(self, hits) -> None:
        choice = _ask("\n查看某道菜的做法（输入序号，回车跳过）")
        if not choice:
            return
        try:
            idx = int(choice) - 1
            r = hits[idx][0]
        except (ValueError, IndexError):
            return
        print(f"\n【{r.name}】食材：")
        for food, grams in r.ingredients:
            print(f"  · {food.name} {grams:.0f}g")
        if r.unresolved:
            print(f"  （未收录：{'、'.join(r.unresolved)}）")
        print(f"做法：{r.steps}")

    def plan_today(self) -> None:
        t = self.targets()
        avoid_raw = _ask("\n忌口（逗号分隔，可留空）")
        avoid = [a.strip() for a in avoid_raw.split(",") if a.strip()]
        pref_raw = _ask("偏好标签（逗号分隔，可留空）")
        prefs = [p.strip() for p in pref_raw.split(",") if p.strip()]
        plan = plan_day(self.book, t, already=self.today.totals(), preferences=prefs, avoid=avoid)
        if not plan.meals:
            print("  没能生成餐单")
            return
        print("\n===== 今日餐单 =====")
        print(plan.describe())
        print("\n采购清单：")
        for name, grams in plan.shopping_list():
            print(f"  · {name} {grams:.0f}g")
        snack = suggest_snack(self.book, self.today.remaining(t), avoid=avoid)
        if snack:
            print(f"\n加餐建议：{snack.name}（{snack.nutrition().kcal:.0f} kcal）")

    def weight_track(self) -> None:
        print("\n1=记录体重  2=查看趋势")
        if _ask("选择", "1") == "1":
            w = _ask_float("今日体重 kg", self.weights[-1][1] if self.weights else 70.0)
            self.weights.append((date.today().isoformat(), w))
            self.save()
            print(f"  已记录 {w:.1f} kg")
        if len(self.weights) < 3:
            print(f"  已有 {len(self.weights)} 条记录，满 3 条后显示趋势")
            return
        vals = [w for _, w in self.weights]
        ema_vals = ema_series(vals, alpha=0.15)
        kal = kalman_trend(vals)
        print("\n日期        实测     EMA趋势   卡尔曼")
        for (d, w), e, k in zip(self.weights, ema_vals, kal):
            print(f"  {d}  {w:6.1f}  {e:8.2f}  {k:7.2f}")
        delta = kal[-1] - kal[0]
        print(f"\n趋势变化 {delta:+.2f} kg（共 {len(vals)} 天）。"
              f"日波动 ±1kg 属正常，看趋势不要看单点。")

    # ------------------------------------------------------------------

    def run(self) -> None:
        print("智能饮食健康助手 v0.1 · 命令行版")
        print("提示：本工具仅做营养记录与参考，不能替代医生诊疗。")
        while True:
            print("\n" + "=" * 46)
            print("1 我的档案与目标    2 查食物营养    3 记一餐")
            print("4 今日汇总与评估    5 推荐菜谱      6 生成今日餐单")
            print("7 体重与趋势        8 清空今日记录  0 退出")
            print("=" * 46)
            choice = input("选择：").strip()
            try:
                if choice == "1":
                    self.show_profile()
                elif choice == "2":
                    self.search_food()
                elif choice == "3":
                    self.log_meal()
                elif choice == "4":
                    if self.profile is None:
                        self.ensure_profile()
                    self.show_today()
                elif choice == "5":
                    if self.profile is None:
                        self.ensure_profile()
                    self.recommend_recipes()
                elif choice == "6":
                    if self.profile is None:
                        self.ensure_profile()
                    self.plan_today()
                elif choice == "7":
                    self.weight_track()
                elif choice == "8":
                    self.today.entries.clear()
                    self.save()
                    print("  已清空今日记录")
                elif choice == "0":
                    self.save()
                    print("已保存，再见")
                    break
                else:
                    print("  无效选择")
            except KeyboardInterrupt:
                print("\n已中断")
                break


def main() -> None:
    Assistant().run()


if __name__ == "__main__":
    main()
