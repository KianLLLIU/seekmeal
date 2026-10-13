"""
中文食物成分库与食物名解析。

设计要点：
  1. 成分数据默认随包附带（data/foods_zh.json），可一行替换为《中国食物成分表》全量数据。
  2. 中文食物名歧义大（"宫保鸡丁"/"宫爆鸡丁"、"土豆"/"马铃薯"），
     因此匹配采用「精确 → 别名 → 子串 → 模糊」四级降级，而不是只靠向量召回。
     参考 CheeChoong14/Food-Recipe-Recommender-System 的思路，但这里用无依赖实现。

数据来源替换建议（见 README）：
  - github.com/Sanotsu/china-food-composition-data  第 6 版 OCR 转 JSON / SQLite
  - Open Food Facts / USDA FoodData Central（CC0，可商用）
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_DATA = Path(__file__).parent / "data" / "foods_zh.json"

#: 中文数字与量词
CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

UNIT_WORDS = ["克", "g", "毫升", "ml", "千克", "kg"]

GRAM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(千克|kg|克|g|毫升|ml)?")
PORTION_PATTERN = re.compile(
    r"([0-9零一两二三四五六七八九十半]*)\s*"
    r"(汤匙|碗|个|份|块|根|片|杯|勺|把|串|盒|瓶|罐|包|盘|只|条|袋)"
)

#: 独立成段的纯数量片段，如 '100g'、'200 克'、'0.5kg'
QUANTITY_TOKEN = re.compile(
    r"[0-9０-９]+(?:\.[0-9０-９]+)?\s*(?:千克|kg|克|g|毫升|ml)?", re.IGNORECASE
)


def _norm(text: str) -> str:
    """归一化：全角转半角、去空格、小写。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace(" ", "").replace("\u3000", "")
    return t.lower()


def _char_bigrams(text: str) -> set[str]:
    t = _norm(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similarity(a: str, b: str) -> float:
    """混合相似度：序列比 + 字符 bigram Jaccard，取加权。对中文短名更稳。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ba, bb = _char_bigrams(na), _char_bigrams(nb)
    jac = len(ba & bb) / len(ba | bb) if (ba or bb) else 0.0
    return 0.5 * seq + 0.5 * jac


def _cn_number(s: str) -> float:
    """解析中文数字串，支持 '一'、'十'、'十五'、'两'、'半'。"""
    s = (s or "").strip()
    if not s:
        return 1.0
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if s == "十":
        return 10.0
    if "十" in s:
        left, _, right = s.partition("十")
        l = CN_DIGITS.get(left, 1) if left else 1
        r = CN_DIGITS.get(right, 0) if right else 0
        return float(l) * 10 + float(r)
    total = 0.0
    for ch in s:
        if ch in CN_DIGITS:
            total += CN_DIGITS[ch]
    return total if total else 1.0


@dataclass
class Food:
    """一条食物成分记录（每 100 克）。"""

    id: str
    name: str
    alias: list[str] = field(default_factory=list)
    cat: str = ""
    kcal: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carb: float = 0.0
    fiber: float = 0.0
    sodium: float = 0.0
    gi: float = 0.0
    portions: dict[str, float] = field(default_factory=dict)

    def per_gram(self, grams: float) -> dict[str, float]:
        """按克重换算营养素。"""
        k = grams / 100.0
        return {
            "kcal": self.kcal * k,
            "protein": self.protein * k,
            "fat": self.fat * k,
            "carb": self.carb * k,
            "fiber": self.fiber * k,
            "sodium": self.sodium * k,
        }

    def glycemic_load(self, grams: float) -> float:
        if self.gi <= 0:
            return 0.0
        return self.gi * self.carb * grams / 100.0 / 100.0

    @property
    def keywords(self) -> list[str]:
        return [self.name, *self.alias]


@dataclass
class MatchResult:
    """食物匹配结果。"""

    food: Food
    grams: float
    score: float
    matched_by: str       # exact / alias / substring / fuzzy
    portion_used: str = ""


class FoodDB:
    """食物库：加载、检索、解析自然语言食物串。"""

    def __init__(self, foods: Iterable[Food]):
        self.foods: list[Food] = list(foods)
        self._by_id: dict[str, Food] = {f.id: f for f in self.foods}
        self._exact: dict[str, Food] = {}
        for f in self.foods:
            for kw in f.keywords:
                self._exact.setdefault(_norm(kw), f)

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = DEFAULT_DATA) -> "FoodDB":
        """从 JSON 加载。字段缺失时用 0 填充。"""
        p = Path(path)
        with p.open(encoding="utf-8") as fp:
            raw = json.load(fp)
        foods: list[Food] = []
        for item in raw.get("foods", []):
            foods.append(
                Food(
                    id=item["id"],
                    name=item["name"],
                    alias=list(item.get("alias", [])),
                    cat=item.get("cat", ""),
                    kcal=float(item.get("kcal", 0)),
                    protein=float(item.get("protein", 0)),
                    fat=float(item.get("fat", 0)),
                    carb=float(item.get("carb", 0)),
                    fiber=float(item.get("fiber", 0)),
                    sodium=float(item.get("sodium", 0)),
                    gi=float(item.get("gi", 0)),
                    portions=dict(item.get("portions", {})),
                )
            )
        return cls(foods)

    @classmethod
    def from_open_food_facts(cls, records: Sequence[dict]) -> "FoodDB":
        """预留：把 Open Food Facts / USDA 的记录转成内部 Food。

        records 需包含 name / kcal / protein / fat / carb 等键，键名兼容两种来源。
        """
        foods = []
        for i, r in enumerate(records):
            foods.append(
                Food(
                    id=str(r.get("id") or r.get("code") or f"off_{i}"),
                    name=str(r.get("name") or r.get("product_name") or ""),
                    alias=list(r.get("alias", [])),
                    cat=str(r.get("cat", "")),
                    kcal=float(r.get("kcal") or r.get("energy-kcal_100g") or 0),
                    protein=float(r.get("protein") or r.get("proteins_100g") or 0),
                    fat=float(r.get("fat") or 0),
                    carb=float(r.get("carb") or r.get("carbohydrates_100g") or 0),
                    fiber=float(r.get("fiber") or 0),
                    sodium=float(r.get("sodium") or 0),
                )
            )
        return cls(foods)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def get(self, food_id: str) -> Food | None:
        return self._by_id.get(food_id)

    def search(self, query: str, top_k: int = 5, threshold: float = 0.35) -> list[tuple[Food, float, str]]:
        """多级匹配，返回 [(food, score, matched_by)]，按分数降序。"""
        q = _norm(query)
        if not q:
            return []

        hits: dict[str, tuple[Food, float, str]] = {}

        def offer(food: Food, score: float, how: str) -> None:
            cur = hits.get(food.id)
            if cur is None or score > cur[1]:
                hits[food.id] = (food, score, how)

        # 1) 精确
        if q in self._exact:
            offer(self._exact[q], 1.0, "exact")
        # 2) 别名已在 _exact 中覆盖，此处再走一遍包含关系
        for f in self.foods:
            for kw in f.keywords:
                nk = _norm(kw)
                if not nk:
                    continue
                if nk == q:
                    offer(f, 0.98, "alias" if kw != f.name else "exact")
                elif nk in q:
                    # 子串命中：越长越可信
                    offer(f, 0.75 + 0.2 * (len(nk) / max(len(q), 1)), "substring")
                elif q in nk and len(q) >= 2:
                    offer(f, 0.70, "substring")
        # 3) 模糊
        if not any(h[2] in ("exact", "alias") for h in hits.values()):
            for f in self.foods:
                best = 0.0
                for kw in f.keywords:
                    best = max(best, _similarity(q, kw))
                if best >= threshold:
                    offer(f, best * 0.9, "fuzzy")

        ranked = sorted(hits.values(), key=lambda x: -x[1])
        return ranked[:top_k]

    # ------------------------------------------------------------------
    # 自然语言解析
    # ------------------------------------------------------------------

    def parse(self, text: str, default_grams: float = 100.0) -> MatchResult | None:
        """解析一句中文食物描述，如 '一碗米饭'、'150g 鸡胸肉'、'两个鸡蛋'。

        返回 MatchResult，无法匹配时返回 None。
        """
        raw = (text or "").strip()
        if not raw:
            return None
        # 名称与数量之间的空格不影响语义：'鸡胸肉 100g' → '鸡胸肉100g'
        raw = re.sub(r"(?<=\D)\s+(?=[\d０-９])", "", raw)
        raw = re.sub(r"(?<=[\d０-９])\s+(?=g|G|克|kg|KG|千克|ml|ML|毫升)", "", raw)
        raw = raw.strip()

        grams: float | None = None
        portion_used = ""

        # 1) 显式克重：150g / 150 克 / 0.5kg
        m = GRAM_PATTERN.search(_norm(raw))
        if m:
            num = float(m.group(1))
            unit = m.group(2) or "克"
            if unit in ("千克", "kg"):
                num *= 1000.0
            grams = num
            remainder = raw.replace(m.group(0), " ").strip()

        # 2) 份量词：一碗 / 两个 / 1份
        if grams is None:
            pm = PORTION_PATTERN.search(raw)
            if pm:
                count = _cn_number(pm.group(1))
                portion_word = pm.group(2)
                remainder = PORTION_PATTERN.sub(" ", raw, count=1).strip()
            else:
                remainder = raw
        else:
            remainder = remainder if remainder else raw

        # 去掉残留数字，避免干扰匹配
        query = re.sub(r"[\d\.]+", "", remainder).strip()
        if not query:
            query = re.sub(r"[\d\.]+", "", raw).strip()

        hits = self.search(query, top_k=1)
        if not hits:
            return None
        food, score, how = hits[0]

        if grams is None:
            if pm:
                key = f"{int(count) if float(count).is_integer() else count}{portion_word}"
                if key in food.portions:
                    grams = food.portions[key]
                    portion_used = key
                else:
                    # 份量词在该食物上没定义，退而用同量词的其他定义或默认值
                    for k, v in food.portions.items():
                        if portion_word in k:
                            grams = v * count
                            portion_used = f"{key}(按 {k} 折算)"
                            break
                    if grams is None:
                        grams = default_grams
            else:
                grams = default_grams

        return MatchResult(food=food, grams=float(grams), score=score,
                           matched_by=how, portion_used=portion_used)

    def _split_items(self, text: str) -> list[str]:
        """把多食物输入切成独立片段。

        先按标点与任意空白切分，再把「纯数量」片段（如 ``100g``、``200 克``）
        并回相邻的食物片段，避免把 ``米饭 100g`` / ``150g 鸡胸肉`` 拆成两段后
        丢失克重或只剩一个空数字。
        """
        raw_parts = [p.strip() for p in re.split(r"[、,，;；+＋]|和|加上|再加|还有|\s+", text or "")]
        parts = [p for p in raw_parts if p]
        merged: list[str] = []
        pending: str = ""
        for part in parts:
            if QUANTITY_TOKEN.fullmatch(part):
                if merged:
                    # 数量跟在食物后面：米饭 + 100g → 米饭100g
                    merged[-1] = f"{merged[-1]}{part}"
                else:
                    # 数量写在食物前面：先攒着，并到下一个食物片段
                    pending = part
                continue
            if pending:
                part = f"{pending} {part}"
                pending = ""
            merged.append(part)
        if pending:
            if merged:
                merged[-1] = f"{merged[-1]} {pending}"
            else:
                merged.append(pending)
        return merged

    def parse_many(self, text: str) -> list[MatchResult]:
        """解析多食物输入，支持顿号、逗号、分号、'和'、'加'、'+' 与空白分隔。

        数量片段会与相邻食物片段合并，因此 ``米饭 100g`` 与 ``150g 鸡胸肉``
        两种写法都能得到正确克重，不会因为切分导致食物被静默丢弃。
        """
        out: list[MatchResult] = []
        for part in self._split_items(text):
            r = self.parse(part)
            if r:
                out.append(r)
        return out

    def by_category(self, cat: str) -> list[Food]:
        return [f for f in self.foods if f.cat == cat]

    def __len__(self) -> int:
        return len(self.foods)
