# -*- coding: utf-8 -*-
"""开发自检：合成数据 -> 规则后端基线评估 -> 输出报告。

用法（在 SeekMeal 根目录下执行）：
    python tools/llm_check.py --total 2000
"""

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm import schema                              # noqa: E402
from llm.backend import RuleBackend                 # noqa: E402
from llm.evaluate import evaluate                   # noqa: E402
from llm.synth import Synthesizer, split_samples    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=2000)
    ap.add_argument("--sessions", type=int, default=40)
    ap.add_argument("--out", default=str(ROOT / "tools" / "_check_out.txt"))
    args = ap.parse_args()

    buf = io.StringIO()

    def say(line=""):
        print(line)
        buf.write(line + "\n")

    syn = Synthesizer(seed=20261016)
    samples = syn.build(total=args.total, sessions=args.sessions)

    dist: dict[str, int] = {}
    for s in samples:
        dist[s["meta"]["intent"]] = dist.get(s["meta"]["intent"], 0) + 1

    say("=" * 64)
    say(f"合成：{len(samples)} 条    引擎否决 {len(syn.rejected)}    一致性否决 {len(syn.inconsistent)}")
    say("=" * 64)
    for name in schema.INTENTS:
        say(f"  {name:<18}{dist.get(name, 0):>6}")

    if syn.inconsistent:
        say("-" * 64)
        say("一致性抽查否决样例（前 10）：")
        for intent, text, why in syn.inconsistent[:10]:
            say(f"  · [{intent}] {text[:40]}")
            say(f"      {why}")

    if syn.rejected:
        say("-" * 64)
        say("引擎否决样例（前 10）：")
        for intent, payload, why in syn.rejected[:10]:
            say(f"  · [{intent}] {payload[:56]}")
            say(f"      {why}")

    parts = split_samples(samples)
    say("-" * 64)
    say(f"切分：train {len(parts['train'])} / valid {len(parts['valid'])} / test {len(parts['test'])}")

    rep = evaluate(RuleBackend(), parts["test"])
    say("=" * 64)
    say(rep.format_text())
    say("=" * 64)

    Path(args.out).write_text(buf.getvalue(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
