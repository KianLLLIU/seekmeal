"""
LoRA 微调：让 Qwen3-0.6B 学会「一句话 -> 一行 JSON」。

设计取舍（都是针对本机 8GB 显存 + Windows 做的决定）：

  · **不用 4-bit 量化**。0.6B 的 bf16 权重只有 1.2GB，8GB 显存绰绰有余；
    而 bitsandbytes 在 Windows 原生环境下要额外折腾。既然量化是负担不是收益，
    就直接 bf16 + LoRA。LLaMA-Factory 那份 lora_sft.yaml 用的是 4bit，
    两者都能跑，这份脚本是更轻的版本（只依赖 torch/transformers/peft）。

  · **只对 assistant 部分算 loss**。system 与 user 是输入，让模型去拟合它们
    是纯浪费，还会把「复述用户的话」这种坏习惯训进去。

  · **enable_thinking=False**。Qwen3 是混合推理模型，默认会先吐一段思考过程，
    但我们要的只有一行 JSON，必须显式关掉；模型侧一旦开了思考，
    JSON 解析率会直接崩掉。

  · **cutoff 1024**。本任务的 system prompt 约 600 token，加上用户句与答案
    约 700 token，1024 留了充足余量。

用法：
    python -m llm.train_lora --model E:/seekmeal_llm/models/Qwen3-0.6B \
        --train llm/data/seekmeal_train.json --valid llm/data/seekmeal_valid.json \
        --system llm/data/system_prompt.txt --out E:/seekmeal_llm/out/seekmeal-lora
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from pathlib import Path
from typing import Any

import torch
from torch.utils.data import DataLoader, Dataset

from .prompt import build_chatml

#: Qwen 系列的注意力/FFN 线性层名，LoRA 挂在这些地方。
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj",
                  "gate_proj", "up_proj", "down_proj"]


def load_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


class SFTDataset(Dataset):
    """把 sharegpt 格式（conversations）转成「带 labels mask 的 token 序列」。

    mask 的做法：把 prompt 部分（system + user + 生成提示）的 label 置为 -100，
    只有 assistant 的回答参与 loss 计算。
    """

    def __init__(self, path: str | Path, tokenizer, system: str, cutoff: int) -> None:
        self.rows = load_json(path)
        self.tok = tokenizer
        self.system = system
        self.cutoff = cutoff
        self.items: list[dict[str, list[int]]] = []
        self.skipped = 0
        for row in self.rows:
            convs = row.get("conversations") or []
            if len(convs) < 2:
                self.skipped += 1
                continue
            ex = self._encode(convs[0]["content"], convs[1]["content"])
            if ex is None:
                self.skipped += 1
                continue
            self.items.append(ex)

    def _encode(self, user: str, assistant: str) -> dict[str, list[int]] | None:
        # 手工拼 ChatML，不走 apply_chat_template：后者在 transformers 5.x 下
        # 返回的是 Encoding 对象（不是 list[int]），长度与切片都会静默算错；
        # 而且会给 assistant 补一个空思考块，与「直接吐 JSON」的目标相反。
        full_text = build_chatml(self.system, user, assistant)
        prompt_text = build_chatml(self.system, user)
        ids_full = list(self.tok(full_text, add_special_tokens=False).input_ids)
        ids_prompt = list(self.tok(prompt_text, add_special_tokens=False).input_ids)
        if len(ids_full) > self.cutoff:
            return None
        labels = [-100] * len(ids_prompt) + ids_full[len(ids_prompt):]
        if all(l == -100 for l in labels):
            return None  # 答案被截没了，这条样本没有学习价值
        return {"input_ids": ids_full, "labels": labels}

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx: int) -> dict[str, list[int]]:
        return self.items[idx]


def make_collate(pad_id: int):
    def collate(batch: list[dict[str, list[int]]]) -> dict[str, torch.Tensor]:
        width = max(len(b["input_ids"]) for b in batch)
        ii, ll, am = [], [], []
        for b in batch:
            pad = width - len(b["input_ids"])
            ii.append(list(b["input_ids"]) + [pad_id] * pad)
            ll.append(list(b["labels"]) + [-100] * pad)
            am.append([1] * len(b["input_ids"]) + [0] * pad)
        return {
            "input_ids": torch.tensor(ii, dtype=torch.long),
            "labels": torch.tensor(ll, dtype=torch.long),
            "attention_mask": torch.tensor(am, dtype=torch.long),
        }
    return collate


# ---------------------------------------------------------------------------
# 训练
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="SeekMeal LoRA 微调")
    ap.add_argument("--model", required=True, help="基座模型目录")
    ap.add_argument("--train", required=True, help="训练集 json")
    ap.add_argument("--valid", default="", help="验证集 json（可选）")
    ap.add_argument("--system", default="", help="system prompt 文本文件（可选）")
    ap.add_argument("--out", required=True, help="LoRA 适配器输出目录")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--bs", type=int, default=2)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--cutoff", type=int, default=1024)
    ap.add_argument("--warmup-ratio", type=float, default=0.03)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--grad-ckpt", action="store_true", help="开启梯度检查点（省显存、慢 20%）")
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--save-every", type=int, default=0, help="每 N 步存一次，0 表示只在 epoch 末存")
    ap.add_argument("--max-steps", type=int, default=0, help="只跑前 N 步（冒烟测试用）")
    ap.add_argument("--seed", type=int, default=42)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, TaskType, get_peft_model

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    if not torch.cuda.is_available():
        print("!! CUDA 不可用，训练会慢到不可接受，先确认 torch cu128 装好了。")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"设备：{dev}" + (f"  {torch.cuda.get_device_name(0)}" if dev == "cuda" else ""))

    system = ""
    if args.system and Path(args.system).exists():
        system = Path(args.system).read_text(encoding="utf-8").strip()
    print(f"system prompt 长度：{len(system)} 字符")

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def _load_model():
        kw = dict(trust_remote_code=True)
        try:
            return AutoModelForCausalLM.from_pretrained(
                args.model, torch_dtype=torch.bfloat16, **kw)
        except TypeError:
            return AutoModelForCausalLM.from_pretrained(
                args.model, dtype=torch.bfloat16, **kw)

    model = _load_model()
    model.config.use_cache = False
    if args.grad_ckpt:
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()

    lora = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=TARGET_MODULES,
        bias="none",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    train_ds = SFTDataset(args.train, tok, system, args.cutoff)
    print(f"训练样本 {len(train_ds)} 条（跳过 {train_ds.skipped} 条）")
    valid_ds = None
    if args.valid:
        valid_ds = SFTDataset(args.valid, tok, system, args.cutoff)
        print(f"验证样本 {len(valid_ds)} 条")

    collate = make_collate(tok.pad_token_id)
    loader = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                        collate_fn=collate, num_workers=0, drop_last=False)

    steps_per_epoch = math.ceil(len(loader) / args.accum)
    total_steps = steps_per_epoch * args.epochs
    if args.max_steps:
        total_steps = min(total_steps, args.max_steps)
    warmup = max(1, int(total_steps * args.warmup_ratio))

    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=args.lr, betas=(0.9, 0.999), weight_decay=0.0)

    def lr_at(step: int) -> float:
        if step < warmup:
            return args.lr * (step + 1) / warmup
        prog = (step - warmup) / max(1, total_steps - warmup)
        return args.lr * 0.5 * (1.0 + math.cos(math.pi * min(1.0, prog)))

    model.to(dev)
    model.train()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    gstep = 0
    micro = 0
    t0 = time.time()
    running = 0.0
    running_n = 0
    opt.zero_grad(set_to_none=True)
    stop = False

    for epoch in range(args.epochs):
        if stop:
            break
        for batch in loader:
            batch = {k: v.to(dev) for k, v in batch.items()}
            out = model(**batch)
            loss = out.loss
            (loss / args.accum).backward()
            running += float(loss.detach())
            running_n += 1
            micro += 1

            if micro % args.accum == 0:
                lr = lr_at(gstep)
                for g in opt.param_groups:
                    g["lr"] = lr
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                opt.zero_grad(set_to_none=True)
                gstep += 1

                if gstep % args.log_every == 0 or gstep == 1:
                    avg = running / max(1, running_n)
                    el = time.time() - t0
                    eta = el / gstep * (total_steps - gstep)
                    print(f"[ep{epoch + 1}] {gstep}/{total_steps}  loss={avg:.4f}  "
                          f"lr={lr:.2e}  {el:.0f}s  ETA {eta / 60:.1f}min", flush=True)
                    running = 0.0
                    running_n = 0

                if args.save_every and gstep % args.save_every == 0:
                    model.save_pretrained(out_dir)
                    print(f"  · 已保存 step {gstep} -> {out_dir}", flush=True)

                if args.max_steps and gstep >= args.max_steps:
                    stop = True
                    break

        if not stop and valid_ds is not None:
            model.eval()
            vloader = DataLoader(valid_ds, batch_size=args.bs, shuffle=False,
                                 collate_fn=collate, num_workers=0)
            tot, n = 0.0, 0
            with torch.no_grad():
                for batch in vloader:
                    batch = {k: v.to(dev) for k, v in batch.items()}
                    tot += float(model(**batch).loss)
                    n += 1
            print(f"  >> epoch {epoch + 1} 完，验证 loss={tot / max(1, n):.4f}", flush=True)
            model.train()

    model.save_pretrained(out_dir)
    tok.save_pretrained(out_dir)
    print(f"完成：{gstep} 步，{time.time() - t0:.0f}s，适配器已存到 {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
