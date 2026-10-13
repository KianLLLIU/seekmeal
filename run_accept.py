# -*- coding: utf-8 -*-
import io
import os
import sys
import glob
import json
import time
import subprocess
import py_compile
import unittest

ROOT = r"E:\Kevin L\2026-10-13-16-40-43\SeekMeal"
PY = r"C:\Users\Kevin L\.workbuddy\binaries\python\versions\3.13.12\python.exe"
OUT = os.path.join(ROOT, "accept_results.txt")

lines = []
def log(*a):
    s = " ".join(str(x) for x in a)
    lines.append(s)

# ----------------------------------------------------------------------------
log("=" * 70)
log("SeekMeal 端到端验收输出")
log("=" * 70)
os.chdir(ROOT)

# ---------- 1. 单元测试 ----------
log("\n--- [1] 单元测试 ---")
buf = io.StringIO()
suite = unittest.defaultTestLoader.discover(start_dir="diet_assistant/tests", top_level_dir=".")
runner = unittest.TextTestRunner(stream=buf, verbosity=2)
res = runner.run(suite)
log(buf.getvalue())
log(f"[汇总] 共运行 {res.testsRun} 项 | 失败 {len(res.failures)} | 错误 {len(res.errors)}")
for f in res.failures:
    log("FAIL: " + f[0].id())
    log(f[1].strip().splitlines()[-3:])
for e in res.errors:
    log("ERROR: " + e[0].id())
    log(e[1].strip().splitlines()[-3:])

# ---------- 2. 全量编译 ----------
log("\n--- [2] 全量 py_compile ---")
py_files = glob.glob(os.path.join(ROOT, "diet_assistant", "**", "*.py"), recursive=True)
compile_errors = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as ex:
        compile_errors.append((f, str(ex)))
log(f"扫描 {len(py_files)} 个 .py 文件")
if compile_errors:
    for f, msg in compile_errors:
        log("编译错误: " + f)
        log(msg)
else:
    log("全部编译通过，无语法错误。")

# ---------- 4. 端到端实跑 ----------
log("\n--- [4] 端到端实跑 (uvicorn 127.0.0.1:8123) ---")
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
proc = subprocess.Popen(
    [PY, "-m", "uvicorn", "diet_assistant.api:app", "--host", "127.0.0.1", "--port", "8123"],
    cwd=ROOT, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
)
log("启动 uvicorn (pid=%s)，等待 5 秒就绪..." % proc.pid)
time.sleep(5)

try:
    import httpx
    base = "http://127.0.0.1:8123"
    got_starter = False
    try:
        # /health
        r = httpx.get(base + "/health", timeout=10)
        log("\n[GET /health] status=%s" % r.status_code)
        log("  response: " + r.text[:300])

        # /api/profile
        body = {
            "user_id": "default", "sex": "male", "weight": 70.0, "height": 172.0,
            "age": 20, "activity": "moderate", "goal": "maintain",
            "weekly_delta_kg": 0.0, "body_fat_pct": None,
        }
        r = httpx.post(base + "/api/profile", json=body, timeout=10)
        log("\n[POST /api/profile] status=%s" % r.status_code)
        pj = r.json()
        log("  bmi=%s bmi_category=%s bmr=%s tdee=%s target_kcal=%s kcal_floor_applied=%s"
            % (pj.get("bmi"), pj.get("bmi_category"), pj.get("bmr"),
               pj.get("tdee"), pj.get("target_kcal"), pj.get("kcal_floor_applied")))
        log("  targets=%s" % json.dumps(pj.get("targets"), ensure_ascii=False))

        # /api/logs (中文解析+记录)
        body = {"text": "一碗米饭 鸡胸肉100g", "user_id": "default", "meal": "午餐"}
        r = httpx.post(base + "/api/logs", json=body, timeout=10)
        log("\n[POST /api/logs] status=%s" % r.status_code)
        lj = r.json()
        log("  added=%s count=%s" % (json.dumps(lj.get("added"), ensure_ascii=False), lj.get("count")))

        # /api/summary
        r = httpx.get(base + "/api/summary/default", timeout=10)
        log("\n[GET /api/summary/default] status=%s" % r.status_code)
        sj = r.json()
        log("  totals=%s" % json.dumps(sj.get("totals"), ensure_ascii=False))
        ev = sj.get("evaluations", [])
        log("  evaluations[0]=%s" % (json.dumps(ev[0], ensure_ascii=False) if ev else "[]"))
        log("  risks=%s" % json.dumps(sj.get("risks"), ensure_ascii=False))

        # /api/plan
        body = {"user_id": "default", "preferences": ["高蛋白"], "avoid": []}
        r = httpx.post(base + "/api/plan", json=body, timeout=10)
        log("\n[POST /api/plan] status=%s" % r.status_code)
        plj = r.json()
        meals = plj.get("meals", [])
        log("  meals=%d 首个餐次=%s" % (len(meals), meals[0].get("meal") if meals else None))
        if meals:
            m0 = meals[0]
            log("  meal0 target_kcal=%s dishes=%d" % (m0.get("target_kcal"), len(m0.get("dishes", []))))
            if m0.get("dishes"):
                d0 = m0["dishes"][0]
                log("  dish0 recipe=%s scale=%s kcal=%s" % (d0.get("recipe"), d0.get("scale"), d0.get("nutrition", {}).get("kcal")))
        log("  shopping_list 项数=%d snack=%s" % (len(plj.get("shopping_list", [])), (plj.get("snack") or {}).get("name")))
        got_starter = True
    except Exception as e:
        log("端到端请求异常: " + repr(e))
    finally:
        log("\n[关闭] 终止 uvicorn 进程...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
            log("uvicorn 已停止 (returncode=%s)" % proc.returncode)
        except Exception:
            proc.kill()
            log("uvicorn 被强制 kill")
    if not got_starter:
        # 若接口未起，把启动日志打印出来便于排查
        try:
            out = proc.stdout.read().decode("utf-8", "replace")
            log("[uvicorn 启动日志]\n" + out[:1500])
        except Exception as e:
            log("读取启动日志失败: " + repr(e))
except ImportError:
    log("环境中未安装 httpx，跳过端到端（但 fastapi/uvicorn 应可用）。")
    proc.terminate()

# ---------- 6. 启动入口编码 ----------
log("\n--- [6] 启动入口编码检查 ---")
def check_bat(path):
    with open(path, "rb") as fp:
        raw = fp.read()
    if raw[:3] == b"\xef\xbb\xbf":
        enc = "UTF-8 BOM"
    else:
        try:
            raw.decode("gbk")
            enc = "GBK(无BOM，可正常解码)"
        except UnicodeDecodeError:
            enc = "GBK 解码失败"
    log("  %s -> %s" % (os.path.basename(path), enc))
    log("    前6字节: %s" % raw[:6].hex())

check_bat(os.path.join(ROOT, "start.bat"))
check_bat(os.path.join(ROOT, "start_web.bat"))
check_bat(r"C:\Users\Kevin L\Desktop\SeekMeal 启动器.bat")

with open(OUT, "w", encoding="utf-8") as fp:
    fp.write("\n".join(lines))
print("DONE")
