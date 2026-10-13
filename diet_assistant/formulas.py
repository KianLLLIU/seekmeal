"""
营养与代谢计算核心。

设计原则：这里的每一个数字都是确定性计算，不允许由大模型生成。
公式出处参考开源项目 simonoppowa/OpenNutriTracker 的 Sources & References 做法，
每个公式都在 docstring 中标注来源，便于追溯。

参考：
  - Mifflin-St Jeor (1990)
  - Harris-Benedict (1919, Roza & Shizgal 1984 修订)
  - Katch-McArdle (基于去脂体重)
  - IOM 2005 Dietary Reference Intake
  - WHO TRS 916 (宏量营养素)
  - 中国营养学会《中国居民膳食营养素参考摄入量》
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

Sex = Literal["male", "female"]
Goal = Literal["lose", "maintain", "gain"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: Atwater 系数，单位 kcal/g
ATWATER = {"protein": 4.0, "fat": 9.0, "carb": 4.0, "alcohol": 7.0}

#: 身体活动水平系数（TDEE = BMR × 系数）
ACTIVITY_FACTORS = {
    "sedentary": 1.2,     # 久坐，几乎不运动
    "light": 1.375,       # 每周轻度运动 1-3 天
    "moderate": 1.55,     # 每周中等运动 3-5 天
    "high": 1.725,        # 每周高强度运动 6-7 天
    "very_high": 1.9,     # 体力劳动或每日双练
}

ACTIVITY_LABELS_ZH = {
    "sedentary": "久坐（几乎不运动）",
    "light": "轻度活动（每周运动 1-3 天）",
    "moderate": "中度活动（每周运动 3-5 天）",
    "high": "高度活动（每周运动 6-7 天）",
    "very_high": "极高活动（体力劳动 / 每日双练）",
}

#: 1 kg 体脂约等价的能量，kcal（用于减重进度换算）
KCAL_PER_KG_BODY_FAT = 7700.0

#: 热量安全下限，kcal/日。低于此值不建议，防止极端节食
MIN_DAILY_KCAL = {"male": 1500.0, "female": 1200.0}

#: 蛋白质目标系数，g/kg 体重。减脂期取高值以保留肌肉
PROTEIN_PER_KG = {"lose": 1.8, "maintain": 1.4, "gain": 1.6}

#: 脂肪目标系数，g/kg 体重
FAT_PER_KG = {"lose": 0.8, "maintain": 0.9, "gain": 1.0}

#: 脂肪供能占比下限（长期低于此值会影响激素与脂溶性维生素吸收）
MIN_FAT_KCAL_RATIO = 0.20

#: 膳食纤维推荐量，g per 1000 kcal
FIBER_PER_1000KCAL = 14.0

#: 中国成人 BMI 分级（中国标准，与 WHO 国际标准的切点不同）
BMI_BANDS_CN = [
    (0.0, 18.5, "偏瘦"),
    (18.5, 24.0, "正常"),
    (24.0, 28.0, "超重"),
    (28.0, math.inf, "肥胖"),
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroTarget:
    """每日宏量营养素目标。"""

    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    fiber_g: float
    sodium_mg_limit: float = 2000.0

    @property
    def protein_kcal(self) -> float:
        return self.protein_g * ATWATER["protein"]

    @property
    def fat_kcal(self) -> float:
        return self.fat_g * ATWATER["fat"]

    @property
    def carb_kcal(self) -> float:
        return self.carb_g * ATWATER["carb"]

    def ratios(self) -> dict[str, float]:
        """返回三大营养素供能比。"""
        total = self.protein_kcal + self.fat_kcal + self.carb_kcal
        if total <= 0:
            return {"protein": 0.0, "fat": 0.0, "carb": 0.0}
        return {
            "protein": self.protein_kcal / total,
            "fat": self.fat_kcal / total,
            "carb": self.carb_kcal / total,
        }


@dataclass(frozen=True)
class EnergyBudget:
    """能量预算计算结果。"""

    bmr: float
    tdee: float
    target_kcal: float
    daily_deficit: float
    formula: str
    clamped: bool
    weekly_delta_kg: float


# ---------------------------------------------------------------------------
# 体格指标
# ---------------------------------------------------------------------------


def bmi(weight_kg: float, height_cm: float) -> float:
    """体质指数。WHO 定义：体重(kg) / 身高(m)^2。"""
    if height_cm <= 0:
        raise ValueError("身高必须为正数")
    h = height_cm / 100.0
    return weight_kg / (h * h)


def bmi_category(value: float, standard: str = "CN") -> str:
    """BMI 分级。默认使用中国标准（切点 18.5 / 24 / 28）。"""
    if standard == "WHO":
        bands = [(0.0, 18.5, "偏瘦"), (18.5, 25.0, "正常"),
                 (25.0, 30.0, "超重"), (30.0, math.inf, "肥胖")]
    else:
        bands = BMI_BANDS_CN
    for low, high, label in bands:
        if low <= value < high:
            return label
    return "未知"


def waist_hip_ratio(waist_cm: float, hip_cm: float) -> float:
    """腰臀比。男 >0.90、女 >0.85 提示中心性肥胖。"""
    if hip_cm <= 0:
        raise ValueError("臀围必须为正数")
    return waist_cm / hip_cm


# ---------------------------------------------------------------------------
# 基础代谢
# ---------------------------------------------------------------------------


def bmr_mifflin(sex: Sex, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor 公式（1990），当前主流首选。

    男: 10W + 6.25H - 5A + 5
    女: 10W + 6.25H - 5A - 161
    """
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + 5.0 if sex == "male" else base - 161.0


def bmr_harris_benedict(sex: Sex, weight_kg: float, height_cm: float, age: int) -> float:
    """Harris-Benedict 公式（Roza & Shizgal 1984 修订）。与旧文献对齐时使用。"""
    if sex == "male":
        return 88.362 + 13.397 * weight_kg + 4.799 * height_cm - 5.677 * age
    return 447.593 + 9.247 * weight_kg + 3.098 * height_cm - 4.330 * age


def bmr_katch_mcardle(lean_mass_kg: float) -> float:
    """Katch-McArdle 公式：370 + 21.6 × 去脂体重(kg)。

    已知体脂率时比 Mifflin 更准确，适合健身人群。
    """
    return 370.0 + 21.6 * lean_mass_kg


def bmr(
    sex: Sex,
    weight_kg: float,
    height_cm: float,
    age: int,
    body_fat_pct: float | None = None,
) -> tuple[float, str]:
    """自动选择 BMR 公式。有体脂率时用 Katch-McArdle，否则用 Mifflin-St Jeor。

    返回 (数值, 公式名)。
    """
    if body_fat_pct is not None:
        if not 0 < body_fat_pct < 100:
            raise ValueError("体脂率应在 0-100 之间")
        lean = weight_kg * (1 - body_fat_pct / 100.0)
        return bmr_katch_mcardle(lean), "Katch-McArdle"
    return bmr_mifflin(sex, weight_kg, height_cm, age), "Mifflin-St Jeor"


# ---------------------------------------------------------------------------
# 总消耗与目标
# ---------------------------------------------------------------------------


def tdee(bmr_value: float, activity: str = "moderate") -> float:
    """总每日能量消耗 = BMR × 活动系数。"""
    if activity not in ACTIVITY_FACTORS:
        raise ValueError(f"未知活动水平: {activity}，可选 {list(ACTIVITY_FACTORS)}")
    return bmr_value * ACTIVITY_FACTORS[activity]


def activity_kcal(met: float, weight_kg: float, hours: float) -> float:
    """单项运动消耗 = MET × 体重(kg) × 时长(h)。"""
    return met * weight_kg * hours


def compute_energy_budget(
    sex: Sex,
    weight_kg: float,
    height_cm: float,
    age: int,
    activity: str = "moderate",
    goal: Goal = "maintain",
    weekly_delta_kg: float = 0.0,
    body_fat_pct: float | None = None,
) -> EnergyBudget:
    """计算完整能量预算。

    Args:
        weekly_delta_kg: 期望每周体重变化，正数增重、负数减重。
            减脂建议 -0.5 ~ -0.7 kg/周，一般不超过体重的 1%。
    """
    bmr_value, formula = bmr(sex, weight_kg, height_cm, age, body_fat_pct)
    tdee_value = tdee(bmr_value, activity)

    # 由周变化目标反推每日热量缺口：1 kg 体脂 ≈ 7700 kcal
    daily_deficit = -weekly_delta_kg * KCAL_PER_KG_BODY_FAT / 7.0
    target = tdee_value - daily_deficit

    clamped = False
    floor = MIN_DAILY_KCAL[sex]
    if target < floor:
        target = floor
        clamped = True

    actual_deficit = tdee_value - target
    actual_weekly = -actual_deficit * 7.0 / KCAL_PER_KG_BODY_FAT

    return EnergyBudget(
        bmr=round(bmr_value, 1),
        tdee=round(tdee_value, 1),
        target_kcal=round(target, 1),
        daily_deficit=round(actual_deficit, 1),
        formula=formula,
        clamped=clamped,
        weekly_delta_kg=round(actual_weekly, 3),
    )


def macro_targets(weight_kg: float, target_kcal: float, goal: Goal = "maintain") -> MacroTarget:
    """由目标热量反推三大营养素目标。

    顺序固定：先定蛋白质（保肌肉），再定脂肪（保激素），碳水吃剩余热量。
    """
    protein_g = weight_kg * PROTEIN_PER_KG[goal]
    fat_g = weight_kg * FAT_PER_KG[goal]

    # 保证脂肪供能比不低于下限
    min_fat_g = target_kcal * MIN_FAT_KCAL_RATIO / ATWATER["fat"]
    if fat_g < min_fat_g:
        fat_g = min_fat_g

    carb_kcal = target_kcal - protein_g * ATWATER["protein"] - fat_g * ATWATER["fat"]
    if carb_kcal < 0:
        # 目标热量过低，先压脂肪到下限，仍不足则压蛋白质到基础量
        fat_g = min_fat_g
        carb_kcal = target_kcal - protein_g * ATWATER["protein"] - fat_g * ATWATER["fat"]
        if carb_kcal < 0:
            protein_g = max(weight_kg * 1.0, 0.0)
            carb_kcal = target_kcal - protein_g * ATWATER["protein"] - fat_g * ATWATER["fat"]
        carb_kcal = max(carb_kcal, 0.0)

    carb_g = carb_kcal / ATWATER["carb"]
    fiber_g = FIBER_PER_1000KCAL * target_kcal / 1000.0

    return MacroTarget(
        kcal=round(target_kcal, 1),
        protein_g=round(protein_g, 1),
        fat_g=round(fat_g, 1),
        carb_g=round(carb_g, 1),
        fiber_g=round(fiber_g, 1),
    )


# ---------------------------------------------------------------------------
# 换算与评估
# ---------------------------------------------------------------------------


def atwater_kcal(protein_g: float, fat_g: float, carb_g: float, alcohol_g: float = 0.0) -> float:
    """由宏量营养素反算热量，用于校验成分表自洽性。"""
    return (
        protein_g * ATWATER["protein"]
        + fat_g * ATWATER["fat"]
        + carb_g * ATWATER["carb"]
        + alcohol_g * ATWATER["alcohol"]
    )


def glycemic_load(gi: float, carb_g: float) -> float:
    """血糖负荷 GL = GI × 碳水(g) / 100。

    GL ≥ 20 高，11-19 中，≤ 10 低。比单独看 GI 更有实际意义。
    """
    if gi <= 0:
        return 0.0
    return gi * carb_g / 100.0


def gl_level(gl: float) -> str:
    if gl <= 10:
        return "低"
    if gl < 20:
        return "中"
    return "高"


def ideal_body_weight(height_cm: float, sex: Sex) -> float:
    """理想体重简易估算（Devine 公式），仅作参考。"""
    inches = height_cm / 2.54
    base = 50.0 if sex == "male" else 45.5
    return base + 2.3 * (inches - 60.0)


# ---------------------------------------------------------------------------
# 体重趋势处理
# ---------------------------------------------------------------------------


def ema_series(values: Sequence[float], alpha: float = 0.15) -> list[float]:
    """指数移动平均，抹平体重日波动（水分、食物残留可达 ±1 kg）。"""
    if not values:
        return []
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1 - alpha) * out[-1])
    return out


def kalman_trend(
    values: Sequence[float],
    process_var: float = 1e-4,
    obs_var: float = 0.5,
    init_var: float = 1.0,
) -> list[float]:
    """一维卡尔曼滤波估计真实体重趋势。

    把真实体重当隐状态、日波动当观测噪声。比 EMA 更稳，且能给出收敛速度控制。
    """
    if not values:
        return []
    x = float(values[0])
    p = init_var
    out = [x]
    for z in values[1:]:
        p += process_var
        k = p / (p + obs_var)
        x += k * (float(z) - x)
        p *= (1 - k)
        out.append(x)
    return out


def predict_weeks_to_goal(current_kg: float, target_kg: float, weekly_rate_kg: float) -> float | None:
    """按当前速率预测达成目标所需周数。速率为 0 时返回 None。"""
    if weekly_rate_kg == 0:
        return None
    weeks = (target_kg - current_kg) / weekly_rate_kg
    return round(max(weeks, 0.0), 1)


def adherence_score(logged_days: int, total_days: int) -> float:
    """记录依从性（0-1）。"""
    if total_days <= 0:
        return 0.0
    return round(min(logged_days / total_days, 1.0), 3)
