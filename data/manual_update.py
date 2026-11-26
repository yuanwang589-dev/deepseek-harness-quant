# -*- coding: utf-8 -*-
"""data/manual_update.py — 手动全域更新执行器（2026-08-10 用户需求）

★背景：系统不 24 小时在线（会关机），自动程序（dev_auto 每 4h / daily_pipeline 18:30）失效后
  数据会缺 → 给用户"手动全域更新"能力（实时面板一键触发完整管道）。

★安全设计（不误伤自动程序）：
  1. 双重忙检查：① 最近手动更新状态 running 且 pid 存活 → 拒绝重复触发
                  ② psutil 扫描进程命令行含 dev_auto.py / daily_pipeline.py → 拒绝（自动程序运行中）
  2. 状态文件时间戳命名（写保护免疫，每次唯一）：
     logs/manual_update_{ts}.json       = running（worker 写）
     logs/manual_update_{ts}_done.json  = done/failed（worker 完成时写）
  3. 日志 logs/manual_update_{ts}.log   = 管道实时输出（每次唯一）

用法（Deck 路由调用）：
  POST /api/manual_update → start() → {"ok": true/false, ...}
  GET  /api/update_status → status() → {busy, reason, last, log_tail, ...}
"""
# ★2026-11-26 静默异常统一记录（自动注入）
try:
    from dsh_log import swallow as _dsh_swallow
except ImportError:
    import sys as _dsh_sys
    from pathlib import Path as _dsh_Path
    for _d in [_dsh_Path(__file__).resolve().parent] + list(_dsh_Path(__file__).resolve().parents):
        if (_d / 'dsh_log.py').exists():
            _dsh_sys.path.insert(0, str(_d))
            break
    from dsh_log import swallow as _dsh_swallow
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
PY = sys.executable

# 自动程序标识（命中即拒绝手动更新）
AUTO_SCRIPTS = ("dev_auto.py", "daily_pipeline.py")


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def _auto_running() -> bool:
    """自动程序（dev_auto / daily_pipeline）是否在运行（psutil 命令行扫描）

    ★2026-08-10 误伤防护：① 排除自身 pid ② 排除 python -c 调试进程
      （-c 命令行的字符串里可能包含关键词，不是真实管道）——只认脚本文件方式运行的管道
    """
    try:
        import psutil
        me = os.getpid()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if p.pid == me:
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                parts = p.info.get("cmdline") or []
                if any(part.strip() == "-c" for part in parts):
                    continue  # 调试/内联脚本，非管道
                cmd = " ".join(parts)
                if any(s in cmd for s in AUTO_SCRIPTS):
                    return True
            except Exception:
                continue
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/manual_update.py:70 _auto_running")
    return False


def _latest_state() -> dict:
    """最新状态文件（running 与 done 按 mtime 取最新——
    ★#349 修复：原只 glob *_done.json，旧 done（如 17:28）会盖掉新 running（18:19）
    → worker 运行中 update_status 误判空闲 → 重复触发风险）"""
    files = sorted(glob.glob(str(LOGS / "manual_update_2*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/manual_update.py:83 _latest_state")
    return {}


def check_busy():
    """→ (busy: bool, reason: str, detail: dict)"""
    # 0) 最近触发锁（5 分钟内存在 → 有任务刚启动或 worker 初始化中）
    trig = sorted(glob.glob(str(LOGS / "mu_trigger_*.lock")), key=os.path.getmtime)
    if trig:
        age = time.time() - Path(trig[-1]).stat().st_mtime
        if age < 300:
            return True, "手动更新刚触发（初始化中）", {}
    # 1) 手动更新运行中（状态 running + pid 存活）
    st = _latest_state()
    if st.get("status") == "running" and _pid_alive(st.get("pid")):
        return True, f"手动更新运行中（{st.get('started_at', '')} 启动）", st
    # 2) 自动程序在跑
    if _auto_running():
        return True, "自动程序运行中（dev_auto / daily_pipeline），手动更新已自动禁用", {}
    return False, "", {}


def start() -> dict:
    """触发手动全域更新（busy 则拒绝；★O_EXCL 原子触发锁防并发双击/双实例）"""
    busy, reason, _ = check_busy()
    if busy:
        return {"ok": False, "reason": reason}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ★2026-08-10 加固：O_EXCL 原子创建触发锁——并发调用只有一个能成功（双实例/双击防护）
    lock = LOGS / f"mu_trigger_{ts}.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"ts": ts, "pid": os.getpid()}).encode())
        os.close(fd)
    except FileExistsError:
        return {"ok": False, "reason": "触发冲突：另一触发锁已存在（防并发双实例）"}
    except Exception as e:
        return {"ok": False, "reason": f"触发锁创建失败: {str(e)[:60]}"}
    try:
        p = subprocess.Popen(
            [PY, "-X", "utf8", str(BASE / "data" / "manual_update_worker.py"), ts],
            cwd=str(BASE),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "ts": ts, "pid": p.pid,
                "note": "手动全域更新已启动（后台完整管道：7z→parquet→日线→巡检→竞价信号→扫描→Pitch）"}
    except Exception as e:
        return {"ok": False, "reason": f"启动失败: {str(e)[:80]}"}


def status() -> dict:
    """状态聚合（供 /api/update_status）"""
    busy, reason, detail = check_busy()
    # 最近完成结果
    done_files = sorted(glob.glob(str(LOGS / "manual_update_*_done.json")))
    last = {}
    if done_files:
        try:
            last = json.loads(Path(done_files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/manual_update.py:143 status")
    # 最近日志尾部（running 任务的日志或最新日志）
    log_tail = []
    logs = sorted(glob.glob(str(LOGS / "manual_update_2*.log")), key=os.path.getmtime)
    if logs:
        try:
            log_tail = Path(logs[-1]).read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/manual_update.py:151 status")
    return {
        "busy": busy,
        "reason": reason,
        "running_since": detail.get("started_at") if detail and detail.get("status") == "running" else None,
        "last": last,
        "log_tail": log_tail,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(status(), ensure_ascii=False, indent=1)[:600])
