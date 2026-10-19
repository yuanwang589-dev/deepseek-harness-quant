# -*- coding: utf-8 -*-
"""scripts/run_task.py — 计划任务统一入口（2026-10-18）

【解决什么问题】
Windows 计划任务用 `pythonw.exe -X utf8 D:\...\factors\opportunities\scan.py` 启动时，
Python 把 **脚本所在目录** 放进 sys.path[0]（即 factors/opportunities），而不是项目根。
于是脚本里的 `from data.io_utils import ...` / `import factors.xxx` 全部 ModuleNotFoundError
→ 进程立刻退出 → 计划任务显示"成功"但什么也没做（Result=0 的假成功）。

watchdog 之前就是因为同一个原因，deck_server 起不来 → /api/live/* 全 500 → 每 30s 重启死循环。

【做法】
所有计划任务统一改为：
    pythonw.exe -X utf8 scripts/run_task.py <相对脚本路径> [脚本参数...]

本入口负责：
  1. cwd 切到项目根
  2. sys.path / PYTHONPATH 注入项目根
  3. 子进程跑目标脚本，透传退出码与输出（输出追加到 logs/task_<name>.log，便于事后排查）
  4. 失败时把 stderr 尾部写进日志，不静默吞掉
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def log_line(logf, msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/run_task.py <相对脚本路径> [参数...]", file=sys.stderr)
        return 2

    rel = sys.argv[1]
    args = sys.argv[2:]
    target = (BASE / rel).resolve()
    if not target.exists():
        print(f"目标脚本不存在: {target}", file=sys.stderr)
        return 2

    logdir = BASE / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logf = logdir / f"task_{target.stem}.log"

    env = dict(os.environ)
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(BASE) + (os.pathsep + pp if pp else "")
    env["PYTHONIOENCODING"] = "utf-8"

    log_line(logf, f"▶ 启动 {rel} {' '.join(args)}")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", str(target)] + args,
                           cwd=str(BASE), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5400)
    except subprocess.TimeoutExpired:
        log_line(logf, f"✗ 超时（>5400s）: {rel}")
        return 124
    except Exception as e:
        log_line(logf, f"✗ 异常: {type(e).__name__}: {e}")
        return 1

    dt = time.time() - t0
    out_tail = (r.stdout or "").strip().splitlines()[-5:]
    err_tail = (r.stderr or "").strip().splitlines()[-8:]
    for ln in out_tail:
        log_line(logf, f"  | {ln}")
    if r.returncode != 0:
        for ln in err_tail:
            log_line(logf, f"  ! {ln}")
        log_line(logf, f"✗ 失败 rc={r.returncode} ({dt:.0f}s): {rel}")
    else:
        log_line(logf, f"✓ 完成 ({dt:.0f}s): {rel}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
