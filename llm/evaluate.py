"""
评估：对任一后端自动打分。

之所以能全自动判分，是因为标准答案不是人标的，而是引擎算的：
    · 意图与参数的标准答案由合成器产出（且已通过引擎验证）
    · 端到端是否跑通，由引擎抛不抛错决定
    · 回答里有没有编数字，由数字一致性校验判定

用法：
    python -m llm.evaluate --backend rule --data llm/data/seekmeal_test.json
    python -m llm.evaluate --backend openai --model qwen3-1.7b --data .../test.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import schema
from .backend import Backend, make_backend
from .prompt import SYSTEM_PROMPT
from .router import Router, extract_json, numbers_consistent
from .tools import Engine, Session

#: 数值比对容差。合成器产出的体重/身高可能带小数，模型四舍五入属正常。
TOLERANCE = 0.5

#: 自由文本参数：只校验「有没有给」，不比对字面。
#: 比如 refuse_safety 的 reason，模型怎么措辞都是对的，逐字比对没有意义。
FREE_TEXT_ARGS = frozenset({"reason"})


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass
class Report:
    backend: str
    total: int = 0
    parsed: int = 0
    intent_hit: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    e2e_ok: int = 0
    safety_total: int = 0
    safety_ok: int = 0
    answer_total: int = 0
    answer_clean: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def add_failure(self, text: str, expect: str, got: str, why: str) -> None:
        if len(self.failures) < 40:
            self.failures.append({"text": text, "expect": expect, "got": got, "why": why})

    @property
    def metrics(self) -> dict[str, float]:
        def safe(a: int, b: int) -> float:
            return round(a / b, 4) if b else 0.0

        precision = safe(self.tp, self.tp + self.fp)
        recall = safe(self.tp, self.tp + self.fn)
        f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) else 0.0
        return {
            "json_parse_rate": safe(self.parsed, self.total),
            "intent_accuracy": safe(self.intent_hit, self.total),
            "arg_precision": precision,
            "arg_recall": recall,
            "arg_f1": f1,
            "e2e_success_rate": safe(self.e2e_ok, self.total),
            "safety_refusal_rate": safe(self.safety_ok, self.safety_total),
            "no_hallucinated_number": safe(self.answer_clean, self.answer_total),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "total": self.total,
                "metrics": self.metrics, "failures": self.failures}

    def format_text(self) -> str:
        m = self.metrics
        lines = [
            f"后端：{self.backend}    样本数：{self.total}",
            "-" * 52,
            f"  JSON 可解析率      {m['json_parse_rate']:>7.2%}",
            f"  意图准确率          {m['intent_accuracy']:>7.2%}",
            f"  参数抽取 F1         {m['arg_f1']:>7.2%}   (P {m['arg_precision']:.2%} / R {m['arg_recall']:.2%})",
            f"  端到端成功率        {m['e2e_success_rate']:>7.2%}",
            f"  安全拒答率          {m['safety_refusal_rate']:>7.2%}   ({self.safety_ok}/{self.safety_total})",
            f"  无幻觉数字率        {m['no_hallucinated_number']:>7.2%}   ({self.answer_clean}/{self.answer_total})",
        ]
        if self.failures:
            lines.append("-" * 52)
            lines.append("  典型失败案例：")
            for f in self.failures[:6]:
                lines.append(f"    · {f['text'][:34]}")
                lines.append(f"      期望 {f['expect']} / 实得 {f['got']}  ({f['why']})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------


def _norm_scalar(v: Any) -> Any:
    if isinstance(v, str):
        return "".join(v.split())
    return v


def _arg_equal(a: Any, b: Any) -> bool:
    a, b = _norm_scalar(a), _norm_scalar(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= TOLERANCE
    if isinstance(a, list) and isinstance(b, list):
        return [ _norm_scalar(x) for x in a ] == [ _norm_scalar(x) for x in b ]
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a == b


def load_samples(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def expected_of(sample: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw = sample["conversations"][1]["content"]
    obj = extract_json(raw) or {"intent": "", "args": {}}
    return obj.get("intent", ""), obj.get("args", {}) or {}


def evaluate(
    backend: Backend,
    samples: list[dict[str, Any]],
    *,
    engine: Engine | None = None,
    build_router: bool = True,
) -> Report:
    rep = Report(backend=getattr(backend, "name", "unknown"))
    engine = engine or Engine()

    # 评估端到端时给一个「已经用过几天」的会话：
    #   · 已建好档案，避免「需要先建档」的澄清把本该成功的样本记成失败；
    #   · 已有几条饮食与体重记录，否则 delete_log / weight_trend 会因为
    #     「没有记录可删」合法地失败——那是评估会话太干净，不是样本有问题。
    router: Router | None = None
    if build_router:
        warm = Session()
        warm.profile = {
            "sex": "male", "weight": 70.0, "height": 175.0, "age": 22,
            "activity": "moderate", "goal": "maintain", "weekly_delta_kg": 0.0,
            "body_fat_pct": None,
        }
        for name, grams, meal in (("米饭", 150.0, "午餐"), ("鸡胸肉", 100.0, "午餐"),
                                  ("鸡蛋", 50.0, "早餐")):
            hit = engine.db.parse(f"{name}{grams:g}g")
            if hit is not None:
                warm.log.add(hit.food, grams, meal, name)
        for w in (71.2, 70.6, 70.1):
            warm.weights.append((date.today().isoformat(), w))
        router = Router(backend, engine=engine, session=warm)

    for sample in samples:
        text = sample["conversations"][0]["content"]
        exp_intent, exp_args = expected_of(sample)
        rep.total += 1

        try:
            raw = backend.parse(text, system=sample.get("system") or SYSTEM_PROMPT)
        except Exception as exc:
            rep.add_failure(text, exp_intent, "<调用异常>", str(exc)[:60])
            continue

        obj = extract_json(raw)
        if obj is None:
            rep.add_failure(text, exp_intent, (raw or "")[:40], "JSON 解析失败")
            continue
        rep.parsed += 1

        got_intent = str(obj.get("intent", ""))
        got_args = obj.get("args", {}) or {}
        intent_ok = got_intent == exp_intent
        if intent_ok:
            rep.intent_hit += 1
        else:
            rep.add_failure(text, exp_intent, got_intent, "意图不符")

        # 参数 F1：只在意图判对时统计，否则参数没有可比性
        if intent_ok:
            for key in exp_args:
                if key not in got_args:
                    rep.fn += 1
                    rep.add_failure(text, f"{key}={exp_args[key]!r}", "<缺失>", "参数缺失")
                elif key in FREE_TEXT_ARGS:
                    gv = got_args[key]
                    if isinstance(gv, str) and gv.strip():
                        rep.tp += 1
                    else:
                        rep.fn += 1
                elif _arg_equal(got_args[key], exp_args[key]):
                    rep.tp += 1
                else:
                    rep.fp += 1
                    rep.fn += 1
                    rep.add_failure(text, f"{key}={exp_args[key]!r}",
                                    f"{key}={got_args[key]!r}", "参数值不符")
            for key in got_args:
                if key not in exp_args:
                    rep.fp += 1
                    rep.add_failure(text, f"<无 {key}>", f"{key}={got_args[key]!r}", "多给了参数")

        # 安全拒答：只看拒答类样本
        if exp_intent == schema.INTENT_REFUSE_SAFETY:
            rep.safety_total += 1
            if got_intent == schema.INTENT_REFUSE_SAFETY:
                rep.safety_ok += 1

        # 端到端
        if router is not None:
            res = router.handle(text)
            ok = bool(res.intent) and res.path not in ("error", "clarify")
            if ok:
                rep.e2e_ok += 1
                rep.answer_total += 1
                clean, stray = numbers_consistent(res.answer, res.result)
                if clean:
                    rep.answer_clean += 1
                else:
                    rep.add_failure(text, "回答不含虚构数字", res.answer[:40],
                                    f"疑似编造 {stray[:4]}")

    return rep


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SeekMeal 小模型后端评估")
    p.add_argument("--backend", default="rule",
                   help="rule / openai / llamacpp")
    p.add_argument("--data", required=True, help="测试集 json 路径")
    p.add_argument("--model", default="", help="模型名或 GGUF 路径")
    p.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0 表示全部")
    p.add_argument("--json-out", default="", help="把完整报告写到该路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    samples = load_samples(args.data)
    if args.limit:
        samples = samples[: args.limit]

    if args.backend == "rule":
        backend = make_backend("rule")
    elif args.backend in ("openai", "compat"):
        if not args.model:
            print("需要 --model 指定模型名", file=sys.stderr)
            return 2
        backend = make_backend("openai", model=args.model, base_url=args.base_url)
    elif args.backend in ("llamacpp", "gguf"):
        if not args.model:
            print("需要 --model 指定 GGUF 路径", file=sys.stderr)
            return 2
        backend = make_backend("llamacpp", model_path=args.model)
    else:
        print(f"未知后端：{args.backend}", file=sys.stderr)
        return 2

    rep = evaluate(backend, samples)
    print(rep.format_text())
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(rep.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n报告已写入 {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
