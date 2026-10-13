"""核心计算与解析的单元测试。运行：python -m unittest discover -s diet_assistant/tests"""

import unittest
from pathlib import Path

from diet_assistant.analyzer import DayLog, Totals
from diet_assistant.food_db import FoodDB
from diet_assistant.formulas import (
    atwater_kcal,
    bmi,
    bmi_category,
    bmr_mifflin,
    compute_energy_budget,
    ema_series,
    glycemic_load,
    kalman_trend,
    macro_targets,
    tdee,
)
from diet_assistant.planner import plan_day
from diet_assistant.recommend import RecipeBook

ROOT = Path(__file__).resolve().parents[1]


class TestFormulas(unittest.TestCase):
    def test_bmi(self):
        self.assertAlmostEqual(bmi(70, 172), 23.66, places=1)
        self.assertEqual(bmi_category(23.66), "正常")
        self.assertEqual(bmi_category(25.0), "超重")
        self.assertEqual(bmi_category(29.0), "肥胖")
        self.assertEqual(bmi_category(17.0), "偏瘦")

    def test_bmr_mifflin_known_values(self):
        # 男 70kg 172cm 20岁: 10*70 + 6.25*172 - 5*20 + 5 = 1680
        self.assertAlmostEqual(bmr_mifflin("male", 70, 172, 20), 1680.0, places=2)
        # 女 60kg 165cm 30岁: 600 + 1031.25 - 150 - 161 = 1320.25
        self.assertAlmostEqual(bmr_mifflin("female", 60, 165, 30), 1320.25, places=2)

    def test_tdee(self):
        self.assertAlmostEqual(tdee(1680, "moderate"), 1680 * 1.55, places=2)
        self.assertAlmostEqual(tdee(1680, "sedentary"), 1680 * 1.2, places=2)

    def test_energy_budget_floor(self):
        # 极端缺口时应触发安全下限
        b = compute_energy_budget("male", 55, 165, 25, "sedentary", "lose", -3.0)
        self.assertTrue(b.clamped)
        self.assertGreaterEqual(b.target_kcal, 1500.0)

    def test_calorie_deficit_math(self):
        b = compute_energy_budget("male", 80, 178, 30, "moderate", "lose", -0.5)
        # 每周 -0.5kg => 每日缺口 7700*0.5/7 = 550
        self.assertAlmostEqual(b.daily_deficit, 550.0, places=0)

    def test_macro_targets_sum(self):
        t = macro_targets(70, 2200, "lose")
        recovered = atwater_kcal(t.protein_g, t.fat_g, t.carb_g)
        self.assertAlmostEqual(recovered, 2200.0, delta=15.0)

    def test_macro_fat_floor(self):
        # 极低热量下脂肪不应低于供能比 20%
        t = macro_targets(50, 1300, "maintain")
        self.assertGreaterEqual(t.fat_g * 9 / 1300, 0.19)

    def test_glycemic_load(self):
        # GI 83, 碳水 25.9g => GL 21.5
        self.assertAlmostEqual(glycemic_load(83, 25.9), 21.5, places=1)

    def test_ema_smoothing(self):
        vals = [70, 71.2, 69.8, 70.5, 70.0]
        out = ema_series(vals, 0.15)
        self.assertEqual(len(out), len(vals))

        def roughness(seq):
            return sum(abs(seq[i] - seq[i - 1]) for i in range(1, len(seq)))

        # 平滑后序列的抖动应明显小于原始序列
        self.assertLess(roughness(out), roughness(vals))
        # 且趋势值不应偏离原始区间
        self.assertGreaterEqual(out[-1], min(vals))
        self.assertLessEqual(out[-1], max(vals))

    def test_kalman_converges(self):
        vals = [70 + (0.5 if i % 2 else -0.5) for i in range(10)]
        out = kalman_trend(vals)
        self.assertEqual(len(out), len(vals))
        self.assertAlmostEqual(out[-1], 70.0, delta=1.0)


class TestFoodDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = FoodDB.load(ROOT / "data" / "foods_zh.json")

    def test_loaded(self):
        self.assertGreater(len(self.db), 80)

    def test_exact_and_alias(self):
        hit = self.db.parse("米饭")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.food.name, "米饭")

        hit = self.db.parse("西红柿")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.food.name, "番茄")

    def test_portion_parsing(self):
        hit = self.db.parse("一碗米饭")
        self.assertEqual(hit.food.name, "米饭")
        self.assertAlmostEqual(hit.grams, 150.0, places=1)

        hit = self.db.parse("两个鸡蛋")
        self.assertEqual(hit.food.name, "鸡蛋")
        self.assertAlmostEqual(hit.grams, 100.0, places=1)

    def test_gram_parsing(self):
        hit = self.db.parse("150g 鸡胸肉")
        self.assertEqual(hit.food.name, "鸡胸肉")
        self.assertAlmostEqual(hit.grams, 150.0, places=1)

        hit = self.db.parse("鸡蛋 60克")
        self.assertAlmostEqual(hit.grams, 60.0, places=1)

    def test_fuzzy(self):
        hit = self.db.parse("宫保鸡丁")
        self.assertIsNotNone(hit)
        self.assertIn("鸡丁", hit.food.name)

    def test_parse_many(self):
        results = self.db.parse_many("一碗米饭、150g鸡胸肉、两个鸡蛋")
        self.assertEqual(len(results), 3)

    def test_unknown(self):
        self.assertIsNone(self.db.parse("火星进口能量棒"))


class TestAnalyzer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = FoodDB.load(ROOT / "data" / "foods_zh.json")
        cls.targets = macro_targets(70, 2200, "lose")

    def test_totals(self):
        log = DayLog()
        rice = self.db.get("rice_steamed")
        chicken = self.db.get("chicken_breast")
        log.add(rice, 150, "午餐")
        log.add(chicken, 150, "午餐")
        t = log.totals()
        self.assertAlmostEqual(t.kcal, 116 * 1.5 + 133 * 1.5, places=1)
        self.assertAlmostEqual(t.protein, 2.6 * 1.5 + 19.4 * 1.5, places=1)

    def test_evaluate_status(self):
        log = DayLog()
        log.add(self.db.get("rice_steamed"), 150, "午餐")
        evals = log.evaluate(self.targets)
        kcal_eval = next(e for e in evals if e.key == "kcal")
        self.assertEqual(kcal_eval.status, "low")

    def test_risks_include_sodium(self):
        log = DayLog()
        log.add(self.db.get("salt"), 10, "午餐")  # 10g 盐 ≈ 3900mg 钠
        risks = log.risks(self.targets)
        self.assertTrue(any("钠" in r for r in risks))

    def test_remaining(self):
        log = DayLog()
        log.add(self.db.get("rice_steamed"), 150, "午餐")
        rem = log.remaining(self.targets)
        self.assertAlmostEqual(rem.kcal, self.targets.kcal - 174.0, places=0)


class TestRecommendAndPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = FoodDB.load(ROOT / "data" / "foods_zh.json")
        cls.book = RecipeBook.load(cls.db, ROOT / "data" / "recipes_zh.json")
        cls.targets = macro_targets(70, 2200, "lose")

    def test_recipes_loaded(self):
        self.assertGreater(len(self.book), 15)

    def test_recipe_nutrition_derived(self):
        r = self.book.get("r_tomato_egg")
        self.assertIsNotNone(r)
        n = r.nutrition()
        self.assertGreater(n.kcal, 0)
        # 番茄 200g -> 40kcal; 鸡蛋 100g -> 144kcal; 油 12g -> 108kcal
        self.assertAlmostEqual(n.kcal, 40 + 144 + 108, delta=5.0)

    def test_recommend_returns_ranked(self):
        remaining = Totals(kcal=700, protein=40, fat=20, carb=60, fiber=8, sodium=800)
        hits = self.book.recommend(remaining, query="高蛋白", preferences=["高蛋白"], top_k=5)
        self.assertTrue(hits)
        scores = [h[1] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_avoid_filter(self):
        remaining = Totals(kcal=700, protein=40, fat=20, carb=60, fiber=8, sodium=800)
        hits = self.book.recommend(remaining, avoid=["鸡蛋"], top_k=20)
        for r, _, _ in hits:
            self.assertNotIn("鸡蛋", r.ingredient_names())

    def test_plan_day(self):
        plan = plan_day(self.book, self.targets, already=Totals())
        self.assertEqual(len(plan.meals), 3)
        total = plan.totals()
        self.assertGreater(total.kcal, 0)
        # 份量缩放后，三餐热量应落在目标 70%-130% 区间内
        self.assertGreater(total.kcal, self.targets.kcal * 0.70)
        self.assertLess(total.kcal, self.targets.kcal * 1.30)
        self.assertTrue(plan.shopping_list())

    def test_plan_scales_portions(self):
        plan = plan_day(self.book, self.targets, already=Totals())
        dish_count = 0
        for m in plan.meals:
            self.assertTrue(m.dishes)
            for d in m.dishes:
                dish_count += 1
                self.assertTrue(0.5 <= d.scale <= 2.0)
                self.assertTrue(d.ingredients)
                # 缩放后的营养应等于原营养 × scale
                base = d.recipe.nutrition()
                self.assertAlmostEqual(d.nutrition.kcal, base.kcal * d.scale, places=1)
        self.assertGreaterEqual(dish_count, 3)
        # 采购清单总量应与缩放后一致
        self.assertGreater(sum(g for _, g in plan.shopping_list()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
