# -*- coding: utf-8 -*-
"""deck/system_live.py — 实时系统状态聚合（2026-08-10 用户需求 1）

供 /api/system_live 返回实时 JSON（dashboard_live.html 每 5 秒轮询）：
  1. databases: 各数据库心跳（可连接 + 最近交易日）
  2. dev_auto: 最近一次运行日志尾部（正在干什么）
  3. scheduled: 计划任务下次运行时间
  4. api_health: 各 API 端点存活
  5. deck: 自身状态
"""
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DBS = {
    "bars": str(BASE / "data" / "cache" / "bars.db"),          # ★2026-10-15 P1-A 绝对路径
    "finance": str(BASE / "data" / "cache" / "finance.db"),
    "finance_quality": str(BASE / "data" / "cache" / "finance_quality.db"),
    "minute": str(BASE / "data" / "cache" / "minute.db"),
    "hist_mv": str(BASE / "data" / "cache" / "hist_mv.db"),
}

# ★2026-10-18：原表硬编码 "LWQuant-*"（历史前缀），实际注册为 "DSHQuant-*" → 页面全部显示异常。
#   现改为「后缀 + 说明」，真实任务名运行时由 data/task_registry 解析（前缀无关）。
SCHEDULED_TASKS = [
    ("DevDriver", "每 4h 巡检"),
    ("AfterCloseScan", "17:35 盘后机会扫描"),
    ("DailyPipeline", "18:30 每日全链"),
    ("TushareInc", "17:30 日线增量"),
    ("BreakoutMon", "18:50 突破监控"),
    ("DeckGuard", "每 30min 守护"),
]


_db_cache = {"ts": 0, "data": None}


def _probe_one(name, path):
    """单个库探测：只读+immutable 连接（不等待写锁，立即返回）"""
    import sqlite3
    t0 = time.time()
    try:
        # ★2026-08-10：mode=ro&immutable=1 → 只读快照连接，不等写锁（库被占用也能即时探测）
        uri = f"file:{path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=0.2)
        tabs = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tname = tabs[0][0] if tabs else "?"
        try:
            n = con.execute(f"SELECT MAX(rowid) FROM {tname}").fetchone()[0]
        except Exception:
            n = None
        con.close()
        return {name: {"online": True, "rows": n, "tables": len(tabs),
                       "ms": round((time.time() - t0) * 1000)}}
    except Exception as e:
        return {name: {"online": False, "error": str(e)[:50],
                       "ms": round((time.time() - t0) * 1000)}}


def _db_health() -> dict:
    """数据库心跳（★2026-08-10：5 库并行探测 + 0.8s 超时 + 30s 缓存，API 毫秒级响应；
    库被占锁时标记"占用中"而非阻塞等待）"""
    from concurrent.futures import ThreadPoolExecutor
    now = time.time()
    if _db_cache["data"] is not None and now - _db_cache["ts"] < 30:
        return _db_cache["data"]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_probe_one, name, path): name
                   for name, path in DBS.items()}
        for fut in futures:
            try:
                out.update(fut.result(timeout=1.5))
            except Exception as e:
                out[futures[fut]] = {"online": False, "error": "探测超时",
                                     "ms": 1500}
    _db_cache["ts"] = now
    _db_cache["data"] = out
    return out


def _dev_auto_tail(n=8) -> list:
    """dev_auto 最近运行日志尾部（显示系统在干什么）"""
    for p in (BASE / "logs" / "dev_auto.log", BASE / "logs" / "dev_auto_console.log"):
        if p.exists():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                return lines[-n:]
            except Exception:
                pass
    return []


_sched_cache = {"ts": 0, "data": None}


def _resolve_task(suffix: str) -> str | None:
    """后缀 → 真实注册任务名（查不到返回 None，页面据此显示「未注册」而不是假装正常）"""
    try:
        from data.task_registry import resolve
        return resolve(suffix)
    except Exception:
        return None


def _scheduled() -> dict:
    """计划任务下次运行时间（5 分钟缓存）

    ★2026-10-18：改走 data/task_registry.query_next_runs()（PowerShell Get-ScheduledTaskInfo）。
      原因：本机安全策略把 schtasks.exe 列入程序黑名单，原实现全部抛 [WinError 5] 拒绝访问，
      页面显示 ERR 看起来像调度层坏了。PowerShell 通路一次调用拿全部任务，也不慢。
    """
    now = time.time()
    if _sched_cache["data"] is not None and now - _sched_cache["ts"] < 300:
        return _sched_cache["data"]
    out = {}
    runs = None
    try:
        from data.task_registry import query_next_runs
        runs = query_next_runs()
    except Exception:
        runs = None
    for suffix, desc in SCHEDULED_TASKS:
        name = _resolve_task(suffix)
        if not name:
            out[suffix] = {"desc": desc, "next": "未注册", "registered": False, "name": None}
            continue
        if runs is None:
            out[suffix] = {"desc": desc, "next": "查询失败", "registered": True, "name": name}
            continue
        out[suffix] = {"desc": desc, "next": runs.get(name) or "—",
                       "registered": True, "name": name}
    _sched_cache["ts"] = now
    _sched_cache["data"] = out
    return out


def _api_health() -> dict:
    """对 Deck 自身各 API 端点做存活探测（本进程直接读文件更快）
    ★#351 每数据源自定义滞后阈值 stale_h（低频源如 beneish/sim_tracks 用大阈值，
    高频源用 24-48h）——前端按 stale_h 判断"滞后"，不再一刀切 24h 误报低频源"""
    import glob
    checks = {
        "pitch_v2": ("logs/pitch_v2_*.json", 48),
        "opp_pool": ("logs/opp_pool_*.json", 48),
        "beneish": ("logs/beneish_report*.json", 24 * 7),      # 月度报告，7 天内都新鲜
        "sim_tracks": ("logs/sim_tracks*.json", 24 * 7),        # 样本跟踪低频
        "pitch_track": ("logs/pitch_track_pool_*.json", 48),
        "regime": ("output/dynamic_regime.json", 72),
        "traffic_light": ("output/traffic_light.json", 72),   # ★2026-08-14 择时红绿灯
        "signal": ("output/daily_signal_*.json", 48),          # ★#351 时间戳文件（无戳旧版残留勿用）
        "audit": ("report/data_audit_report_*.json", 72),       # ★F2：时间戳文件名 glob
        "factor_pool": ("report/factor_pool_report.json", 72),
        "pool_layers": ("output/pool_layers_*.json", 48),       # ★#351 时间戳文件
    }
    out = {}
    for name, (pat, stale_h) in checks.items():
        full = str(BASE / pat)
        if "*" in pat:
            files = sorted(glob.glob(full))
            ok = bool(files)
            age = time.time() - Path(files[-1]).stat().st_mtime if ok else None
        else:
            p = Path(full)
            ok = p.exists()
            age = time.time() - p.stat().st_mtime if ok else None
        out[name] = {"online": ok,
                     "age_h": round(age / 3600, 1) if age is not None else None,
                     "stale_h": stale_h,
                     "stale": (not ok) or (age is not None and age > stale_h * 3600)}
    return out


def _deck_pid() -> int:
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=15)
        for line in r.stdout.splitlines():
            if ":8787" in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:
        pass
    return 0


def _activity_feed(minutes: int = 60) -> list:
    """★2026-08-10 实时活动流：logs/ 目录最近 N 分钟修改的产出文件（显示系统在干活）"""
    import glob
    now = time.time()
    items = []
    for pat in ("logs/*.json", "logs/*.md", "logs/*.csv", "output/*.json", "report/*.json"):
        for p_str in glob.glob(str(BASE / pat)):
            try:
                p = Path(p_str)
                mt = p.stat().st_mtime
                age = now - mt
                if age <= minutes * 60:
                    items.append({
                        "file": p.name, "age_min": round(age / 60, 1),
                        "ts": time.strftime("%H:%M:%S", time.localtime(mt)),
                    })
            except Exception:
                pass
    items.sort(key=lambda x: x["age_min"])
    return items[:15]


def _next_schedule() -> list:
    """距下次各计划任务倒计时（分钟）

    ★2026-10-18：改走 data/task_registry.query_next_runs()，不再依赖 schtasks.exe
      （本机该程序在黑名单 → 原实现全部 WinError 5，倒计时永远空）。
    """
    out = []
    try:
        from datetime import datetime as _dt
        from data.task_registry import query_next_runs
        runs = query_next_runs()
        for suffix, desc in SCHEDULED_TASKS:
            name = _resolve_task(suffix)
            if not name:
                # 未注册：诚实列出，倒计时留空（不要凭空给个时间）
                out.append({"name": suffix, "desc": desc, "next": "未注册",
                            "mins_left": None, "registered": False})
                continue
            nxt = (runs or {}).get(name)
            if not nxt:
                out.append({"name": name, "desc": desc, "next": "—",
                            "mins_left": None, "registered": True})
                continue
            mins = None
            try:
                nxt_dt = _dt.strptime(nxt, "%Y-%m-%d %H:%M:%S")
                mins = int(round((nxt_dt - _dt.now()).total_seconds() / 60, 0))
            except Exception:
                mins = None
            out.append({"name": name, "desc": desc, "next": nxt,
                        "mins_left": mins, "registered": True})
    except Exception:
        pass
    return out


_cache = {"ts": 0.0, "data": None}


def collect() -> dict:
    """系统实时状态聚合（★2026-08-10 性能：10s 短缓存——页面 5s 轮询降频，避免每次 1.6s 全量探测）"""
    import time as _t
    now = _t.time()
    if _cache["data"] is not None and now - _cache["ts"] < 10:
        _cache["data"]["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return _cache["data"]
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "databases": _db_health(),
        "dev_auto_tail": _dev_auto_tail(),
        "scheduled": _scheduled(),
        "api_health": _api_health(),
        "deck_pid": _deck_pid(),
        "activity_feed": _activity_feed(),      # ★实时活动流
        "next_schedule": _next_schedule(),      # ★倒计时
    }
    _cache.update({"ts": now, "data": out})
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=1))
