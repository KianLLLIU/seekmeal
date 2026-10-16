# SeekMeal 领域小模型模块

用 0.6B–1.7B 的开源小模型，把「说一句话就能记录饮食」接到 SeekMeal 上。

核心设计只有一句话：**模型负责翻译，代码负责计算。**

```
用户一句话
   │
   ├─ 理解层   backend.py + prompt.py      中文 → {"intent": "...", "args": {...}}
   │                                        （模型只做这件事，只输出一行 JSON）
   ├─ 计算层   tools.py → diet_assistant/  热量、营养素、推荐、排餐全部由确定性公式算出
   │
   └─ 表达层   router.py 的模板 + 可选润色   数字一致性校验；出现引擎没算过的数字就丢弃重来
```

第 5 步那层校验让「小模型胡说数字」在结构上不可能发生：回答里每个数字都必须能在
引擎返回的 JSON 里找到出处，找不到就回退模板。所以哪怕模型只有 0.6B，数值也不会错。

---

## 目录

| 文件 | 职责 |
| --- | --- |
| `schema.py` | 意图与工具 schema 的**单一真相**。输出协议、参数区间、安全词表都在这。改这一处，训练标注、校验、评估自动同步 |
| `prompt.py` | 理解层提示词（含 few-shot） |
| `backend.py` | 四种模型后端：规则 / OpenAI 兼容 / llama.cpp / 脚本 |
| `tools.py` | 把 `diet_assistant` 的纯逻辑包装成工具，**不依赖 FastAPI** |
| `router.py` | 调度器。理解 → 校验 → 计算 → 表达 → 数字校验 |
| `synth.py` | 训练数据合成器（用引擎自己造样本） |
| `evaluate.py` | 自动评估，六项指标全部可自动判分 |
| `service.py` | 接到 Web 服务上（供 `diet_assistant/api.py` 使用） |
| `data/` | 已生成的数据集：train 12233 / valid 1525 / test 1540 |

---

## 三种跑法

**1. 不接模型（默认，推荐先用这个）**

```bash
python -m llm.evaluate --backend rule --data llm/data/seekmeal_test.json
```

规则后端不是「模型」，它是链路的验证器与兜底。先证明「理解 → 计算 → 表达」是通的，
再接模型，这样模型一旦表现异常，你能立刻分清是模型的问题还是链路的问题。

**2. 接本地 OpenAI 兼容服务**（LM Studio / Ollama / vLLM / llama.cpp server）

```bash
SEEKMEAL_LLM_BACKEND=openai \
SEEKMEAL_LLM_MODEL=qwen3-0.6b \
SEEKMEAL_LLM_BASE_URL=http://127.0.0.1:1234/v1 \
python -m llm.evaluate --backend openai --model qwen3-0.6b --data llm/data/seekmeal_test.json
```

**3. 直接吃 GGUF 文件**

```bash
pip install llama-cpp-python
python -m llm.evaluate --backend llamacpp --model D:/models/qwen3-0.6b-q4_k_m.gguf \
    --data llm/data/seekmeal_test.json
```

第一次接模型时**不要跳过第 1 步**：先用规则后端把数据与评估跑通，再对比模型分数。
后面所有「模型有没有带来增量」的判断，都要跟规则基线比。

---

## 数据是怎么来的

不需要人工标注。参数随机采样 → 套口语模板 → 真引擎跑一遍 → 通过就收，不通过就丢：

```bash
python -m llm.synth --total 15000 --sessions 300 --out llm/data
```

样本的「答案」是 **意图 + 参数**，不是营养数字。数据集里不含任何一个需要模型记住的数值，
从源头堵死了「模型背数字背错」这条路。

合成器有一条硬规则，落在 `Synthesizer.check_consistency` 里：

> 标注里的每一个字段，都必须能在生成的文本里找到明确出处。

这条规则挡的是合成数据最隐蔽的一类错误——句子读起来通顺，但标注和文本语义不符
（比如模板写死「想减脂」，参数却随机成 `goal: gain`）。这种样本比没数据更糟，
因为它看起来是对的。生成了 15298 条，一致性抽查否决 0 条，引擎否决 2 条。

`text` 字段的约定是**整句原文照抄**。实测整句与只给食物短语在引擎侧解析能力完全一致
（都是 16/17，唯一失败是食物库里没有「沙拉」），那就选更简单的那个：纯复制，
对 0.6B 这种小模型是最不容易出错的行为。

---

## 评估指标与当前基线

六项指标全部由引擎判定，不依赖人工打分：

| 指标 | 含义 | 规则后端 |
| --- | --- | --- |
| JSON 可解析率 | 输出能不能被解析成 JSON | 100% |
| 意图准确率 | 意图判对的比例 | 100% |
| 参数抽取 F1 | 参数值的精确率 / 召回率 | 100% |
| 端到端成功率 | 整条链路跑通（不落在 clarify / error） | 100% |
| 安全拒答率 | 医疗类问题正确拒答的比例 | 100% |
| 无幻觉数字率 | 回答里的数字全都能在引擎结果里找到出处 | 100% |

**怎么读这个 100%**：语料本来就是规则生成的，所以规则后端拿满分说明的是
「语料无歧义 + 标注与文本严格一致 + 链路自洽」，不是「规则解析器很强」。
它的价值在于给出一个干净的参照系：接入模型后如果低于 100%，差距就纯粹是模型能力问题，
而不是数据脏或链路有 bug。

规则后端只在合成语料上满分，真实用户的说法千奇百怪，它会明显掉分——
那正是要上模型的原因。

---

## QLoRA 微调配方（8GB 显存）

基座选 **Qwen3-0.6B**（轻松）或 **Qwen3-1.7B**（舒适）。3B 勉强，7B 在这个显存下不要碰。

**环境准备（本机实测）**

- RTX 5060 Laptop 8GB，Blackwell 架构（`sm_120`），**必须 PyTorch 2.7+ / cu128**，
  老版本 CUDA 内核编译不出来。
- C 盘只剩 19GB，而模型权重与训练缓存动辄几十 GB。**必须先把缓存重定向到 E 盘**，
  否则会在下载到一半时磁盘写满：

```bat
set HF_HOME=E:\hf
set HF_HUB_CACHE=E:\hf\hub
set TRANSFORMERS_CACHE=E:\hf\transformers
set MODELSCOPE_CACHE=E:\hf\modelscope
set TORCH_HOME=E:\hf\torch
set TEMP=E:\tmp
set TMP=E:\tmp
```

**训练（LLaMA-Factory 路线）**

数据集已经按 LLaMA-Factory 的 sharegpt 格式落盘，`llm/data/dataset_info.json`
里带好了 `default_system`，把它拷到 LLaMA-Factory 的 `data/` 下即可直接用
`seekmeal` / `seekmeal_valid` 两个名字。

```yaml
# llm/lora_sft.yaml
model_name_or_path: Qwen/Qwen3-0.6B
stage: sft
do_train: true
finetuning_type: lora
lora_target: all
quantization_bit: 4            # QLoRA
dataset: seekmeal
eval_dataset: seekmeal_valid
template: qwen3
cutoff_len: 1024               # 本任务的输入输出都很短，1024 足够
learning_rate: 1.0e-4
num_train_epochs: 3
per_device_train_batch_size: 8
gradient_accumulation_steps: 4
lr_scheduler_type: cosine
warmup_ratio: 0.05
bf16: true
logging_steps: 20
save_steps: 500
output_dir: E:\hf\seekmeal-lora
```

```bash
llamafactory-cli train llm/lora_sft.yaml
```

**要不要微调，先看提示词路线的分数。** 先用第 2 种方式接上未微调的 Qwen3-0.6B 跑一遍
`evaluate.py`：

- 如果意图准确率已经 ≥ 95%，微调的收益主要在**长尾说法**和**参数抽取**上，可以做，但别期待翻倍；
- 如果低于 90%，先微调，这是投入产出比最高的一步；
- **安全拒答率低于 100% 必须处理**。这条是合规底线，优先于其他所有指标。

训练完把 LoRA 合并导出成 GGUF，就能走第 3 种方式直接挂到服务上。

---

## 接到服务上

`diet_assistant/api.py` 已经暴露了 `POST /api/chat`，后端由环境变量决定：

```bash
SEEKMEAL_LLM_BACKEND=rule          # 默认。不接模型也能用
SEEKMEAL_LLM_BACKEND=openai        # 接本地 OpenAI 兼容服务
SEEKMEAL_LLM_MODEL=qwen3-1.7b
SEEKMEAL_LLM_BASE_URL=http://127.0.0.1:1234/v1
SEEKMEAL_LLM_BACKEND=llamacpp      # 直接吃 GGUF
SEEKMEAL_LLM_MODEL=E:/models/seekmeal-qwen3-0.6b-q4_k_m.gguf
```

模型后端初始化失败会自动降级到规则后端，并在 `/health` 的 `llm.note` 里说明原因——
模型是加分项，不该是命门。服务起不来这件事，比模型不工作严重得多。

对话状态与表单状态**共用同一份数据**：`Session.log` / `Session.weights` 直接引用
`api.py` 里的那两个对象，所以聊天里记的一餐会立刻出现在「饮食分析」面板里，
反之亦然，不需要任何同步代码。

---

## 常用命令

```bash
# 数据 + 规则基线的自检（开发时最常用）
python tools/llm_check.py --total 3000

# 只评估，不重新合成
python -m llm.evaluate --backend rule --data llm/data/seekmeal_test.json

# 对话链路端到端（不经 HTTP）
python tools/chat_test.py
```
