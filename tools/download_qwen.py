"""
下载 Qwen3-0.6B 权重到本地（走 ModelScope，不依赖 modelscope 包）。

为什么不用 huggingface-cli：huggingface.co 在本机不可达（超时），
而 modelscope.cn 直连正常，且提供等价的公开下载 API。

支持断点续传：中断后重跑会从 .part 文件已下载的字节继续。

用法：
    python tools/download_qwen.py --model Qwen/Qwen3-0.6B --out E:/seekmeal_llm/models/Qwen3-0.6B
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_FILES = [
    "config.json", "configuration.json", "generation_config.json",
    "tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt",
    "model.safetensors", "LICENSE", "README.md",
]

UA = {"User-Agent": "Mozilla/5.0"}


def api_files(model_id: str, revision: str = "master") -> list[dict]:
    url = f"https://modelscope.cn/api/v1/models/{model_id}/repo/files?Revision={revision}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("Data", {}).get("Files", []) or []


def file_url(model_id: str, name: str, revision: str = "master") -> str:
    return (f"https://modelscope.cn/api/v1/models/{model_id}/repo"
            f"?Revision={revision}&FilePath={name}")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(model_id: str, name: str, dest: Path, revision: str = "master") -> None:
    target = dest / name
    if target.exists() and target.stat().st_size > 0:
        print(f"  跳过 {name}（已存在 {human(target.stat().st_size)}）", flush=True)
        return

    tmp = dest / (name + ".part")
    pos = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(file_url(model_id, name, revision), headers=dict(UA))
    if pos:
        req.add_header("Range", f"bytes={pos}-")
        print(f"  续传 {name}（已有 {human(pos)}）", flush=True)
    else:
        print(f"  下载 {name}", flush=True)

    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "ab") as fh:
        total = int(r.headers.get("Content-Length") or 0) + pos
        got = pos
        t0 = time.time()
        last = 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            now = time.time()
            if now - last > 3:
                last = now
                sp = (got - pos) / max(1e-6, now - t0)
                pct = f"{got / total:.1%}" if total else "?"
                print(f"    {pct}  {human(got)}/{human(total)}  {human(sp)}/s", flush=True)

    os.replace(tmp, target)
    print(f"  完成 {name}  {human(target.stat().st_size)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ModelScope 模型下载器")
    ap.add_argument("--model", default="Qwen/Qwen3-0.6B")
    ap.add_argument("--out", required=True)
    ap.add_argument("--revision", default="master")
    ap.add_argument("--files", default="", help="逗号分隔，留空则自动取仓库全部文件")
    args = ap.parse_args(argv)

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)

    if args.files:
        names = [x.strip() for x in args.files.split(",") if x.strip()]
    else:
        names = [f.get("Name", "") for f in api_files(args.model, args.revision)]
        names = [n for n in names if n]
        if not names:
            names = DEFAULT_FILES

    print(f"模型 {args.model} -> {dest}", flush=True)
    print(f"待下载 {len(names)} 个文件：{names}", flush=True)

    t0 = time.time()
    for name in names:
        download(args.model, name, dest, args.revision)
    print(f"全部完成，用时 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
