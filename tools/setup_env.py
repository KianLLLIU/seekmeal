#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SeekMeal 专用 Python 环境 —— 创建 / 修复 / 自检（幂等）。

环境位置（按优先级）：
    1. 环境变量 SEEKMEAL_ENV_DIR 指定的目录
    2. %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\seekmeal

之所以放在这里：该路径正是 start.bat / start_web.bat 探测列表中的第 3 顺位，
项目目录与桌面副本共用同一份环境，装一次两边都能用；同时不往 git 仓库里
塞一个 200MB 的 .venv。

用法：
    python tools\\setup_env.py            创建或修复（默认）
    python tools\\setup_env.py --check     只体检，不做任何改动
    python tools\\setup_env.py --force     删除后重装
    python tools\\setup_env.py --tests     装完顺带跑一遍项目单测
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_DIR / "requirements.txt"

# 网页服务必需 / 接口测试需要
REQUIRED_MODULES = ("fastapi", "uvicorn", "pydantic")
OPTIONAL_MODULES = ("httpx",)

MIN_MINOR = (3, 10)  # 最低 Python 版本


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def head(title: str) -> None:
    print(f"\n{'=' * 66}\n  {title}\n{'=' * 66}")


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def info(msg: str) -> None:
    print(f"  ...    {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def default_env_dir() -> Path:
    override = os.environ.get("SEEKMEAL_ENV_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "seekmeal"


def env_python(env_dir: Path) -> Path:
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(args, timeout: int = 900, cwd: Path | None = None):
    return subprocess.run(
        [str(a) for a in args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def imports_ok(python: Path, modules) -> bool:
    code = ";".join(f"import {m}" for m in modules) + ";print(1)"
    try:
        return run([python, "-c", code], timeout=120).returncode == 0
    except Exception:
        return False


def python_version(python) -> tuple[int, int] | None:
    try:
        r = run([python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        major, minor = r.stdout.strip().split(".")
        return int(major), int(minor)
    except Exception:
        return None


def describe(python: Path) -> str:
    ver = python_version(python)
    return f"{ver[0]}.{ver[1]}" if ver else "?"


# --------------------------------------------------------------------------
# 查找基础解释器
# --------------------------------------------------------------------------
def base_candidates(env_dir: Path):
    """候选基础解释器：(显示名, 可执行文件, 前置参数)"""
    cands = []

    here = Path(sys.executable).resolve()
    if env_dir not in here.parents:
        cands.append(("当前解释器", here, []))

    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local.is_dir():
        for d in sorted(local.glob("Python3*"), reverse=True):
            exe = d / "python.exe"
            if exe.exists():
                cands.append((f"系统 {d.name}", exe, []))

    bundled = (
        Path.home() / ".workbuddy" / "binaries" / "python" / "versions" / "3.13.12" / "python.exe"
    )
    cands.append(("随附解释器", bundled, []))

    cands.append(("py -3", shutil.which("py") or "py", ["-3"]))
    cands.append(("py", shutil.which("py") or "py", []))
    cands.append(("python", shutil.which("python") or "python", []))
    cands.append(("python3", shutil.which("python3") or "python3", []))

    return cands


def find_base_python(env_dir: Path):
    """返回第一个版本达标、且能执行的基础解释器。"""
    tried = []
    for label, exe, prefix in base_candidates(env_dir):
        resolved = Path(exe)
        if not resolved.exists() and not shutil.which(str(exe)):
            continue
        ver = python_version(exe) if not prefix else None
        if prefix:
            try:
                r = run([exe, *prefix, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=60)
                ver = None
                if r.returncode == 0:
                    maj, mn = r.stdout.strip().split(".")
                    ver = (int(maj), int(mn))
            except Exception:
                ver = None
        if ver is None:
            continue
        if ver < MIN_MINOR:
            tried.append(f"{label} {ver[0]}.{ver[1]}（版本过低）")
            continue
        return label, exe, prefix, ver, tried
    return None, None, None, None, tried


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def do_check(env_dir: Path) -> int:
    head("环境体检")
    print(f"  项目目录 : {PROJECT_DIR}")
    print(f"  环境目录 : {env_dir}")
    print(f"  依赖清单 : {REQUIREMENTS}")

    if not REQUIREMENTS.exists():
        fail("找不到 requirements.txt")
        return 1

    py = env_python(env_dir)
    print(f"  环境解释器: {py}")

    problems = 0
    if not py.exists():
        fail("专用环境不存在")
        problems += 1
    else:
        ok(f"解释器存在（Python {describe(py)}）")
        if imports_ok(py, REQUIRED_MODULES):
            ok("必需依赖可导入：" + ", ".join(REQUIRED_MODULES))
        else:
            fail("必需依赖不可导入：" + ", ".join(REQUIRED_MODULES))
            problems += 1
        if imports_ok(py, OPTIONAL_MODULES):
            ok("测试依赖可导入：" + ", ".join(OPTIONAL_MODULES))
        else:
            warn("测试依赖缺失：" + ", ".join(OPTIONAL_MODULES))
        r = run([py, "-c", "from diet_assistant.api import app"], cwd=PROJECT_DIR, timeout=120)
        if r.returncode == 0:
            ok("项目可正常导入（diet_assistant.api:app）")
        else:
            fail("项目导入失败： " + (r.stderr or "").strip().splitlines()[-1:][0] if r.stderr else "未知错误")
            problems += 1

    print()
    if problems == 0:
        print("  结论：环境可用。")
    else:
        print(f"  结论：发现 {problems} 个问题 —— 运行  python tools\\setup_env.py  修复。")
    return 0 if problems == 0 else 2


def do_install(env_dir: Path, force: bool, run_tests: bool) -> int:
    head("SeekMeal 专用 Python 环境配置")
    print(f"  项目目录 : {PROJECT_DIR}")
    print(f"  环境目录 : {env_dir}")

    if not REQUIREMENTS.exists():
        fail(f"找不到依赖清单：{REQUIREMENTS}")
        return 1

    # 0) 已就绪则直接返回
    py = env_python(env_dir)
    if py.exists() and not force and imports_ok(py, REQUIRED_MODULES):
        ok(f"环境已就绪（Python {describe(py)}），无需重装")
        for line in _versions(py):
            print(f"         {line}")
        return 0

    if force and env_dir.exists():
        head("0. 清理旧环境")
        shutil.rmtree(env_dir, ignore_errors=True)
        info(f"已删除 {env_dir}")

    # 1) 找基础解释器
    head("1. 选择基础解释器")
    label, exe, prefix, ver, tried = find_base_python(env_dir)
    for t in tried:
        warn(f"跳过 {t}")
    if exe is None:
        fail("没有找到可用的 Python（需要 3.10 及以上）")
        print("       请先安装 Python：https://www.python.org/downloads/")
        print("       安装时记得勾选 “Add python.exe to PATH”。")
        return 1
    ok(f"选用 {label}：{exe} {' '.join(prefix)}  (Python {ver[0]}.{ver[1]})")

    # 2) 创建虚拟环境
    head("2. 创建虚拟环境")
    try:
        env_dir.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        fail(f"无法创建父目录 {env_dir.parent}：{e}")
        return 1

    r = run([exe, *prefix, "-m", "venv", env_dir], timeout=600)
    if r.returncode != 0 or not env_python(env_dir).exists():
        fail("虚拟环境创建失败")
        print("       " + (r.stderr or r.stdout or "").strip()[-500:])
        print("       若是 ensurepip 缺失，请重装 Python 并保留 pip 组件。")
        return 1
    ok(f"已创建 {env_dir}")
    py = env_python(env_dir)

    # 3) 升级 pip
    head("3. 升级 pip")
    r = run([py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], timeout=600)
    if r.returncode != 0:
        warn("pip 升级失败，继续用现有版本安装")
    else:
        ok("pip / setuptools / wheel 已是最新")

    # 4) 安装依赖
    head("4. 安装项目依赖")
    cmd = [py, "-m", "pip", "install", "-r", REQUIREMENTS]
    mirror = os.environ.get("SEEKMEAL_PIP_INDEX")
    if mirror:
        cmd += ["-i", mirror]
        info(f"使用镜像源 {mirror}")
    r = run(cmd, timeout=1800)
    if r.returncode != 0:
        fail("依赖安装失败")
        tail = "\n       ".join((r.stderr or r.stdout or "").strip().splitlines()[-12:])
        print("       " + tail)
        print("       若为网络问题，可设置镜像后重试：")
        print("         set SEEKMEAL_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1
    ok("依赖安装完成")
    for line in _versions(py):
        print(f"         {line}")

    # 5) 导入自检
    head("5. 导入自检")
    if not imports_ok(py, REQUIRED_MODULES):
        fail("依赖装上了但导入失败，请把上面的安装日志发我看看")
        return 1
    ok("fastapi / uvicorn / pydantic 导入正常")
    r = run([py, "-c", "from diet_assistant.api import app;print(type(app).__name__)"], cwd=PROJECT_DIR, timeout=120)
    if r.returncode != 0:
        fail("项目导入失败： " + (r.stderr or "").strip()[-400:])
        return 1
    ok("项目可正常导入（diet_assistant.api:app）")

    # 6) 单测（可选）
    if run_tests:
        head("6. 运行项目单元测试")
        r = run([py, "-m", "unittest", "discover", "-s", "diet_assistant/tests"],
                cwd=PROJECT_DIR, timeout=600)
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        if r.returncode == 0:
            ok("全部单测通过")
        else:
            fail("单测未全通过：" + tail[0])
            return 1

    head("完成")
    ok("环境就绪，可以双击 start.bat 使用了。")
    print()
    print("  想在终端里直接用 uvicorn / pytest，先激活环境：")
    print(f"      {env_dir}\\Scripts\\activate.bat")
    print("  或在项目目录执行：  call env.bat")
    return 0


def _versions(py: Path):
    code = (
        "import fastapi,uvicorn,pydantic\n"
        "print('fastapi  ' + fastapi.__version__)\n"
        "print('uvicorn  ' + uvicorn.__version__)\n"
        "print('pydantic ' + pydantic.VERSION)"
    )
    r = run([py, "-c", code], timeout=120)
    return [l for l in r.stdout.splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="SeekMeal 专用 Python 环境配置")
    ap.add_argument("--check", action="store_true", help="只体检，不做改动")
    ap.add_argument("--force", action="store_true", help="删除后重装")
    ap.add_argument("--tests", action="store_true", help="装完顺带跑单测")
    ap.add_argument("--env-dir", default=None, help="自定义环境目录")
    args = ap.parse_args()

    if args.env_dir:
        env_dir = Path(args.env_dir).expanduser().resolve()
    else:
        env_dir = default_env_dir()

    try:
        if args.check:
            return do_check(env_dir)
        return do_install(env_dir, args.force, args.tests)
    except KeyboardInterrupt:
        print("\n  已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
