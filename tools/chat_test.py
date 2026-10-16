# -*- coding: utf-8 -*-
"""端到端联调：直接调用 /api/chat 的处理函数，验证对话与表单共用同一份状态。

用法（项目根目录）：
    python tools/chat_test.py
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import diet_assistant.api as api  # noqa: E402

BUF = io.StringIO()


def say(line=""):
    print(line)
    BUF.write(line + "\n")


UID = "selftest"
api._state["profiles"].pop(UID, None)
api._state["logs"].pop(f"{UID}:{__import__('datetime').date.today().isoformat()}", None)
api._state["weights"].pop(UID, None)

TURNS = [
    "男 175 厘米 70 公斤 22 岁 中等活动量，想减脂每周 0.5 公斤",
    "中午吃了一碗米饭和 150g 鸡胸肉",
    "我今晚还能吃多少",
    "鸡胸肉 100 克有多少热量",
    "有什么高蛋白的菜推荐",
    "帮我排一下明天的三餐",
    "今早称了 78.5 公斤",
    "我最近体重趋势怎么样",
    "把刚才那条删了",
    "我有糖尿病，该怎么吃",
]

say("=" * 70)
say(f"后端：{api.chat_service.info.as_dict()}")
say("=" * 70)

for t in TURNS:
    res = api.chat(api.ChatIn(text=t, user_id=UID))
    say(f"> {t}")
    say(f"  intent={res['intent']}  path={res['path']}  numbers_ok={res['numbers_ok']}")
    say(f"  {res['answer'][:150]}")
    say()

say("=" * 70)
say("状态共享检查（对话写入的应该出现在 api 层状态里）")
say("=" * 70)
prof = api._state["profiles"].get(UID)
say(f"profile: {prof}")
log = api._log_for(UID)
say(f"饮食记录 {len(log.entries)} 条: " +
    "、".join(f"{e.food.name}{e.grams:g}g/{e.meal}" for e in log.entries))
say(f"体重记录 {len(api._state['weights'].get(UID, []))} 条: "
    f"{api._state['weights'].get(UID, [])}")

# 再从「表单侧」读一次，确认两边确实是同一份数据
say()
say(f"/api/summary 视角：{api.summary(UID)['totals']}")
say(f"/api/weight 视角：{len(api.weight_trend(UID)['records'])} 条记录")

(ROOT / "tools" / "_chat_out.txt").write_text(BUF.getvalue(), encoding="utf-8")
