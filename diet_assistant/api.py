"""
FastAPI 接口层，供 Web 前端与微信小程序共用。

启动：
    pip install fastapi uvicorn
    uvicorn diet_assistant.api:app --reload --port 8000

接口文档自动生成于 /docs。
前端（含小程序）只需按 README 把域名配成合法域名即可直连，
无需改动本文件——这也是当初坚持前后端分离的收益。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .analyzer import DayLog, Totals
from .food_db import FoodDB
from .formulas import (
    ACTIVITY_FACTORS,
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

DISCLAIMER = "本工具仅做营养记录与参考，不构成医疗建议，不能替代医生诊疗。"

app = FastAPI(
    title="智能饮食健康助手 API",
    version="0.1.0",
    description="营养计算与中文菜谱推荐。所有数值均由确定性代码计算。",
)

# 网页既可由本 API 的根路径提供，也可作为 file:// 本地文件直接打开。
# 后一种方式的浏览器 Origin 为 null，因此仅放行本机页面来源。
app.add_middleware(
    CORSMiddleware,
    # "null" 对应直接双击打开的 file:// 页面；正则覆盖本机任意端口
    allow_origins=["null"],
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

WEB_INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"

db = FoodDB.load()
book = RecipeBook.load(db)

#: MVP 阶段用内存态保存，生产环境替换为数据库
_state: dict[str, Any] = {"logs": {}, "weights": {}, "profiles": {}}


def _log_for(user_id: str) -> DayLog:
    key = f"{user_id}:{date.today().isoformat()}"
    if key not in _state["logs"]:
        _state["logs"][key] = DayLog()
    return _state["logs"][key]


def _targets_of(profile: dict) -> MacroTarget:
    b = compute_energy_budget(
        sex=profile["sex"],
        weight_kg=profile["weight"],
        height_cm=profile["height"],
        age=profile["age"],
        activity=profile.get("activity", "moderate"),
        goal=profile.get("goal", "maintain"),
        weekly_delta_kg=profile.get("weekly_delta_kg", 0.0),
        body_fat_pct=profile.get("body_fat_pct"),
    )
    return macro_targets(profile["weight"], b.target_kcal, profile.get("goal", "maintain"))


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ProfileIn(BaseModel):
    user_id: str = "default"
    sex: str = Field("male", pattern="^(male|female)$")
    weight: float = Field(70.0, gt=20, lt=300)
    height: float = Field(172.0, gt=100, lt=250)
    age: int = Field(20, ge=10, le=100)
    activity: str = "moderate"
    goal: str = Field("maintain", pattern="^(lose|maintain|gain)$")
    weekly_delta_kg: float = 0.0
    body_fat_pct: float | None = None


class ParseIn(BaseModel):
    text: str
    user_id: str = "default"
    meal: str = "午餐"


class RecommendIn(BaseModel):
    user_id: str = "default"
    query: str = ""
    preferences: list[str] = []
    avoid: list[str] = []
    top_k: int = 5


class PlanIn(BaseModel):
    user_id: str = "default"
    preferences: list[str] = []
    avoid: list[str] = []


class WeightIn(BaseModel):
    user_id: str = "default"
    weight: float
    day: str | None = None


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def web_index() -> FileResponse:
    """返回零构建网页入口。"""
    if not WEB_INDEX.is_file():
        raise HTTPException(404, "网页文件不存在")
    return FileResponse(WEB_INDEX)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "foods": len(db), "recipes": len(book), "disclaimer": DISCLAIMER}


@app.get("/api/activity-levels")
def activity_levels() -> dict:
    return {"levels": ACTIVITY_FACTORS}


@app.post("/api/profile")
def set_profile(p: ProfileIn) -> dict:
    if p.activity not in ACTIVITY_FACTORS:
        raise HTTPException(400, f"未知活动水平：{p.activity}")
    _state["profiles"][p.user_id] = p.model_dump()
    budget = compute_energy_budget(
        sex=p.sex, weight_kg=p.weight, height_cm=p.height, age=p.age,
        activity=p.activity, goal=p.goal, weekly_delta_kg=p.weekly_delta_kg,
        body_fat_pct=p.body_fat_pct,
    )
    targets = macro_targets(p.weight, budget.target_kcal, p.goal)
    bmi_v = bmi(p.weight, p.height)
    return {
        "user_id": p.user_id,
        "bmi": round(bmi_v, 1),
        "bmi_category": bmi_category(bmi_v),
        "bmr": budget.bmr,
        "bmr_formula": budget.formula,
        "tdee": budget.tdee,
        "target_kcal": budget.target_kcal,
        "daily_deficit": budget.daily_deficit,
        "weekly_delta_kg": budget.weekly_delta_kg,
        "kcal_floor_applied": budget.clamped,
        "targets": {
            "kcal": targets.kcal, "protein_g": targets.protein_g,
            "fat_g": targets.fat_g, "carb_g": targets.carb_g, "fiber_g": targets.fiber_g,
            "ratios": {k: round(v, 3) for k, v in targets.ratios().items()},
        },
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/foods/search")
def search_foods(q: str, top_k: int = 5) -> dict:
    hits = db.search(q, top_k=top_k)
    return {
        "query": q,
        "results": [
            {
                "id": f.id, "name": f.name, "cat": f.cat, "score": round(s, 3), "matched_by": how,
                "per_100g": {"kcal": f.kcal, "protein": f.protein, "fat": f.fat,
                             "carb": f.carb, "fiber": f.fiber, "sodium": f.sodium, "gi": f.gi},
                "portions": f.portions,
            }
            for f, s, how in hits
        ],
    }


@app.post("/api/parse")
def parse_text(body: ParseIn) -> dict:
    """解析一句中文食物描述，返回候选（含克重），由前端确认后再入库。"""
    results = db.parse_many(body.text)
    if not results:
        raise HTTPException(404, "未能识别食物，请换一种说法")
    return {
        "text": body.text,
        "items": [
            {
                "food_id": r.food.id, "name": r.food.name, "grams": round(r.grams, 1),
                "score": round(r.score, 3), "matched_by": r.matched_by, "portion": r.portion_used,
                "kcal": round(r.food.per_gram(r.grams)["kcal"], 1),
            }
            for r in results
        ],
    }


@app.post("/api/logs")
def add_log(body: ParseIn) -> dict:
    log = _log_for(body.user_id)
    results = db.parse_many(body.text)
    if not results:
        raise HTTPException(404, "未能识别食物")
    added = []
    for r in results:
        e = log.add(r.food, r.grams, body.meal, body.text)
        added.append({"name": e.food.name, "grams": e.grams, "kcal": round(e.kcal, 1)})
    return {"added": added, "count": len(log.entries)}


@app.get("/api/logs/{user_id}")
def get_logs(user_id: str) -> dict:
    log = _log_for(user_id)
    return {
        "entries": [
            {"name": e.food.name, "grams": e.grams, "meal": e.meal, "kcal": round(e.kcal, 1)}
            for e in log.entries
        ]
    }


@app.delete("/api/logs/{user_id}/{index}")
def delete_log(user_id: str, index: int) -> dict:
    log = _log_for(user_id)
    if log.remove(index) is None:
        raise HTTPException(404, "索引不存在")
    return {"count": len(log.entries)}


@app.get("/api/summary/{user_id}")
def summary(user_id: str) -> dict:
    profile = _state["profiles"].get(user_id)
    if not profile:
        raise HTTPException(400, "请先通过 /api/profile 建立档案")
    log = _log_for(user_id)
    targets = _targets_of(profile)
    totals = log.totals()
    return {
        "totals": totals.as_dict(),
        "targets": {
            "kcal": targets.kcal, "protein_g": targets.protein_g, "fat_g": targets.fat_g,
            "carb_g": targets.carb_g, "fiber_g": targets.fiber_g,
        },
        "evaluations": [
            {"key": e.key, "label": e.label, "actual": e.actual, "target": e.target,
             "ratio": e.ratio, "status": e.status, "status_zh": e.status_zh, "advice": e.advice}
            for e in log.evaluate(targets)
        ],
        "risks": log.risks(targets),
        "by_meal": {k: v.as_dict() for k, v in log.by_meal().items()},
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/recommend")
def recommend(body: RecommendIn) -> dict:
    profile = _state["profiles"].get(body.user_id)
    if not profile:
        raise HTTPException(400, "请先建立档案")
    targets = _targets_of(profile)
    remaining = _log_for(body.user_id).remaining(targets)
    hits = book.recommend(
        remaining=remaining, query=body.query,
        preferences=body.preferences, avoid=body.avoid, top_k=body.top_k,
    )
    return {
        "remaining": remaining.as_dict(),
        "recipes": [
            {
                "id": r.id, "name": r.name, "cat": r.cat, "minutes": r.minutes, "tags": r.tags,
                "nutrition": r.nutrition().as_dict(),
                "ingredients": [{"name": f.name, "grams": g} for f, g in r.ingredients],
                "steps": r.steps, "score": score, "detail": detail,
            }
            for r, score, detail in hits
        ],
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/plan")
def plan(body: PlanIn) -> dict:
    profile = _state["profiles"].get(body.user_id)
    if not profile:
        raise HTTPException(400, "请先建立档案")
    targets = _targets_of(profile)
    plan_obj = plan_day(
        book, targets, already=_log_for(body.user_id).totals(),
        preferences=body.preferences, avoid=body.avoid,
    )
    snack = suggest_snack(book, _log_for(body.user_id).remaining(targets), avoid=body.avoid)
    return {
        "meals": [
            {
                "meal": m.meal,
                "target_kcal": m.target_kcal,
                "subtotal": m.nutrition.as_dict(),
                "dishes": [
                    {
                        "recipe": d.recipe.name,
                        "scale": d.scale,
                        "minutes": d.recipe.minutes,
                        "tags": d.recipe.tags,
                        "nutrition": d.nutrition.as_dict(),
                        "ingredients": [{"name": n, "grams": g} for n, g in d.ingredients],
                        "steps": d.recipe.steps,
                    }
                    for d in m.dishes
                ],
            }
            for m in plan_obj.meals
        ],
        "totals": plan_obj.totals().as_dict(),
        "shopping_list": [{"name": n, "grams": g} for n, g in plan_obj.shopping_list()],
        "snack": ({"name": snack.name, "nutrition": snack.nutrition().as_dict()} if snack else None),
        "disclaimer": DISCLAIMER,
    }


@app.post("/api/weight")
def add_weight(body: WeightIn) -> dict:
    key = body.user_id
    _state["weights"].setdefault(key, [])
    day = body.day or date.today().isoformat()
    _state["weights"][key].append((day, body.weight))
    return {"count": len(_state["weights"][key])}


@app.get("/api/weight/{user_id}")
def weight_trend(user_id: str) -> dict:
    records = _state["weights"].get(user_id, [])
    vals = [w for _, w in records]
    if not vals:
        return {"records": [], "trend": [], "message": "暂无记录"}
    return {
        "records": [{"day": d, "weight": w} for d, w in records],
        "trend_ema": [round(v, 2) for v in ema_series(vals, 0.15)],
        "trend_kalman": [round(v, 2) for v in kalman_trend(vals)],
        "latest": vals[-1],
        "note": "体重日波动可达 ±1kg，请以趋势线为准，不要看单点。",
    }


@app.get("/api/totals/{user_id}")
def totals_now(user_id: str) -> dict:
    return {"totals": _log_for(user_id).totals().as_dict(), "empty": Totals().as_dict()}
