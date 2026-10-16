"""
理解层的提示词。

目标模型是 0.6B–1.7B 级别，能力有限，因此：
    · 只要求输出一行 JSON，不给它留任何自由发挥的余地；
    · 参数表用紧凑格式，few-shot 只放最典型的几例，控制 token；
    · 数量词一律要求原样保留（让它把「一碗米饭」整句抄给 text），
      换算交给引擎——模型一旦开始换算就会开始编数字。
"""

from __future__ import annotations

from . import schema

SYSTEM_PROMPT = f"""你是 SeekMeal 的意图解析器。把用户的一句话解析成一行 JSON。

输出规则（必须严格遵守）：
1. 只输出一行 JSON，不要解释、不要 markdown 代码块、不要任何多余文字
2. 格式固定为：{{"intent": "<意图>", "args": {{<参数>}}}}
3. 数字写阿拉伯数字，不要带单位
4. 不要自己计算任何数值，也不要换算克重
5. log_meal 的 text 把用户整句话原样抄进去，一个字都不要改
6. 可选参数没提到就省略，不要填默认值、更不要编
7. 拿不准时选最接近的意图

可用意图与参数（带 * 为必填）：
{schema.describe_for_prompt()}

示例：
用户：帮我建档案，男 175 厘米 70 公斤 22 岁，中等活动，想减脂每周 0.5 公斤
输出：{{"intent": "set_profile", "args": {{"sex": "male", "height": 175, "weight": 70, "age": 22, "activity": "moderate", "goal": "lose", "weekly_delta_kg": 0.5}}}}

用户：中午吃了一碗米饭和 150g 鸡胸肉
输出：{{"intent": "log_meal", "args": {{"text": "中午吃了一碗米饭和 150g 鸡胸肉", "meal": "午餐"}}}}

用户：我今晚还能吃多少
输出：{{"intent": "query_remaining", "args": {{}}}}

用户：鸡胸肉 100 克有多少热量
输出：{{"intent": "query_food", "args": {{"name": "鸡胸肉", "grams": 100}}}}

用户：有什么高蛋白的菜推荐
输出：{{"intent": "recommend", "args": {{"query": "高蛋白"}}}}

用户：推荐三道菜
输出：{{"intent": "recommend", "args": {{"top_k": 3}}}}

用户：帮我排一下明天的三餐
输出：{{"intent": "plan_day", "args": {{}}}}

用户：今早称了 78.5 公斤
输出：{{"intent": "log_weight", "args": {{"weight": 78.5}}}}

用户：我有糖尿病，该怎么吃
输出：{{"intent": "refuse_safety", "args": {{"reason": "涉及糖尿病"}}}}
"""

def build_chatml(system: str = "", user: str = "", assistant: str | None = None,
                 history: list[dict[str, str]] | None = None) -> str:
    """手工拼出 Qwen3 的 ChatML 输入。

    为什么不直接用 tokenizer.apply_chat_template：

      1. **返回类型不稳定**。transformers 5.x 下 `tokenize=True` 给回的是
         ``Encoding`` 对象而不是 ``list[int]``，调用方 ``len()`` / 切片会静默算错——
         实测表现为「训练样本 0 条，跳过 12233 条」，很难一眼看出是模板的锅。
      2. **会塞进空思考块**。官方模板渲染 assistant 消息时会补上
         ```` 这样的空块，而我们要的恰恰是「直接吐 JSON、不要思考」；
         带着空思考块训练等于教模型先写一段推理，与目标相反。

    手工拼的输出与官方模板逐字符一致（已实测比对），但完全确定，
    而且训练与推理共用这一段代码，不会出现两边格式漂移。

    user 传空串则不加 user 轮（做纯 prompt 用）；assistant 传 None 表示
    只拼到 `<|im_start|>assistant\\n`，等待模型续写。
    """
    parts: list[str] = []
    if system:
        parts.append(f"<|im_start|>system\n{system}<|im_end|>\n")
    for turn in history or []:
        role = str(turn.get("role") or "user")
        parts.append(f"<|im_start|>{role}\n{turn.get('content', '')}<|im_end|>\n")
    if user:
        parts.append(f"<|im_start|>user\n{user}<|im_end|>\n")
    text = "".join(parts) + "<|im_start|>assistant\n"
    if assistant is not None:
        text += f"{assistant}<|im_end|>\n"
    return text


#: 表达层（可选润色）用的提示词。默认不启用，见 router.Router.paraphrase。
PARAPHRASE_PROMPT = """把下面这段饮食记录改写得更口语、更亲切一点。

硬性要求：
- 数字一个都不能改，不能新增，也不能删减
- 不要添加任何原始文本里没有的信息
- 长度控制在两句话以内
- 直接输出改写后的文字，不要解释

原始文本：
{text}
"""
