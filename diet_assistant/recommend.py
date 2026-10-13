"""
中文菜谱推荐。

混合排序 = 营养缺口适配度 + 文本相似度 + 偏好标签加成。

文本相似度用无依赖的 TF-IDF（字符 bigram）+ cosine 实现，等价于
CheeChoong14/Food-Recipe-Recommender-System 中 scikit-learn 的
TfidfVectorizer + cosine_similarity 思路，但避免引入第三方依赖。
接入全量数据后可换成 sklearn / jieba 提升效果。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .analyzer import Totals
from .food_db import Food, FoodDB

DEFAULT_RECIPES = Path(__file__).parent / "data" / "recipes_zh.json"

#: 排序权重
W_NUTRITION = 0.60
W_SIMILARITY = 0.25
W_PREFERENCE = 0.15


def _norm(text: str) -> str:
    t = re.sub(r"\s+", "", text or "")
    return t.lower()


def _bigrams(text: str) -> list[str]:
    t = _norm(text)
    if len(t) < 2:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


def _tfidf(docs: Sequence[Sequence[str]]) -> list[dict[str, float]]:
    """极简 TF-IDF。docs 为每个文档的词表。"""
    n = len(docs)
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    out: list[dict[str, float]] = []
    for d in docs:
        tf: dict[str, int] = {}
        for w in d:
            tf[w] = tf.get(w, 0) + 1
        vec: dict[str, float] = {}
        for w, c in tf.items():
            idf = math.log((n + 1) / (df.get(w, 0) + 1)) + 1.0
            vec[w] = (c / len(d)) * idf
        out.append(vec)
    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    common = set(a) & set(b)
    num = sum(a[w] * b[w] for w in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return num / (na * nb) if na and nb else 0.0


@dataclass
class Recipe:
    """一道菜谱。营养由食材自动推导，不硬编码。"""

    id: str
    name: str
    cat: str = "午餐"
    minutes: int = 15
    tags: list[str] = field(default_factory=list)
    ingredients: list[tuple[Food, float]] = field(default_factory=list)
    steps: str = ""
    unresolved: list[str] = field(default_factory=list)

    @property
    def total_grams(self) -> float:
        return sum(g for _, g in self.ingredients)

    def nutrition(self) -> Totals:
        t = Totals()
        for food, grams in self.ingredients:
            n = food.per_gram(grams)
            t.kcal += n["kcal"]
            t.protein += n["protein"]
            t.fat += n["fat"]
            t.carb += n["carb"]
            t.fiber += n["fiber"]
            t.sodium += n["sodium"]
            t.gl += food.glycemic_load(grams)
        return t

    def nutrition_per_100g(self) -> dict[str, float]:
        g = self.total_grams
        if g <= 0:
            return {}
        t = self.nutrition()
        return {
            "kcal": t.kcal * 100 / g,
            "protein": t.protein * 100 / g,
            "fat": t.fat * 100 / g,
            "carb": t.carb * 100 / g,
            "fiber": t.fiber * 100 / g,
            "sodium": t.sodium * 100 / g,
        }

    @property
    def text(self) -> str:
        parts = [self.name, self.cat, *self.tags, *(f.name for f, _ in self.ingredients)]
        return " ".join(parts)

    def ingredient_names(self) -> list[str]:
        return [f.name for f, _ in self.ingredients]


class RecipeBook:
    """菜谱库：加载、向量化、推荐。"""

    def __init__(self, recipes: Iterable[Recipe]):
        self.recipes: list[Recipe] = list(recipes)
        self._docs = [_bigrams(r.text) for r in self.recipes]
        self._vecs = _tfidf(self._docs) if self._docs else []
        self._by_id = {r.id: r for r in self.recipes}

    @classmethod
    def load(cls, db: FoodDB, path: str | Path = DEFAULT_RECIPES) -> "RecipeBook":
        p = Path(path)
        with p.open(encoding="utf-8") as fp:
            raw = json.load(fp)
        recipes: list[Recipe] = []
        for item in raw.get("recipes", []):
            ing: list[tuple[Food, float]] = []
            unresolved: list[str] = []
            for ref in item.get("ingredients", []):
                grams = float(ref.get("grams", 0))
                name = str(ref.get("food", ""))
                if grams <= 0:
                    continue
                hit = db.parse(name, default_grams=0)
                if hit is None:
                    unresolved.append(name)
                    continue
                ing.append((hit.food, grams))
            recipes.append(
                Recipe(
                    id=item["id"],
                    name=item["name"],
                    cat=item.get("cat", "午餐"),
                    minutes=int(item.get("minutes", 15)),
                    tags=list(item.get("tags", [])),
                    ingredients=ing,
                    steps=item.get("steps", ""),
                    unresolved=unresolved,
                )
            )
        return cls(recipes)

    def get(self, rid: str) -> Recipe | None:
        return self._by_id.get(rid)

    def __len__(self) -> int:
        return len(self.recipes)

    # ------------------------------------------------------------------

    def similarity(self, query: str) -> list[float]:
        """每个菜谱与查询的文本相似度。"""
        if not self._vecs:
            return []
        qv = _tfidf([_bigrams(query)])[0]
        return [_cosine(qv, v) for v in self._vecs]

    def nutrition_fit(self, recipe: Recipe, remaining: Totals) -> float:
        """菜谱营养与「剩余缺口」的适配度，0-1。

        剩余为负（已超标）时，目标压到很低，从而偏好低热量选项。
        """
        n = recipe.nutrition()
        pairs = [
            (n.kcal, remaining.kcal, 0.40),
            (n.protein, remaining.protein, 0.30),
            (n.fat, remaining.fat, 0.15),
            (n.carb, remaining.carb, 0.15),
        ]
        total_w = 0.0
        acc = 0.0
        for actual, need, w in pairs:
            target = need if need > 0 else max(abs(need) * 0.1, 50.0)
            diff = abs(actual - target)
            fit = max(0.0, 1.0 - diff / max(target, 1.0))
            acc += fit * w
            total_w += w
        return acc / total_w if total_w else 0.0

    def recommend(
        self,
        remaining: Totals,
        query: str = "",
        preferences: Sequence[str] = (),
        avoid: Sequence[str] = (),
        cat: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[Recipe, float, dict]]:
        """推荐菜谱。

        Args:
            remaining: 当日营养缺口
            query: 用户描述，如「想吃清淡高蛋白的」
            preferences: 偏好标签，如 ["高蛋白", "减脂"]
            avoid: 忌口食材名或标签，命中即过滤
            cat: 限定餐次（早餐/午餐/晚餐/加餐）
        """
        sims = self.similarity(query) if query else [0.0] * len(self.recipes)
        pref = {_norm(p) for p in preferences}
        avoid_set = {_norm(a) for a in avoid}

        scored: list[tuple[Recipe, float, dict]] = []
        for i, r in enumerate(self.recipes):
            if cat and r.cat != cat and r.cat != "加餐":
                continue
            names = {_norm(x) for x in r.ingredient_names()}
            tags = {_norm(t) for t in r.tags}
            if avoid_set & (names | tags | {_norm(r.name)}):
                continue

            fit = self.nutrition_fit(r, remaining)
            sim = sims[i] if i < len(sims) else 0.0
            pref_bonus = 1.0 if pref & tags else 0.0

            # 未解析食材越多，营养越不准，轻微降权
            penalty = 0.95 ** len(r.unresolved)

            score = (W_NUTRITION * fit + W_SIMILARITY * sim + W_PREFERENCE * pref_bonus) * penalty
            detail = {"nutrition_fit": round(fit, 3), "similarity": round(sim, 3),
                      "preference_hit": bool(pref & tags)}
            scored.append((r, round(score, 4), detail))

        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]
