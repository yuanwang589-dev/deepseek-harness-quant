# -*- coding: utf-8 -*-
"""deck/system_live.py — 实时系统状态聚合（2026-08-10 用户需求 1）

供 /api/system_live 返回实时 JSON（dashboard_live.html 每 5 秒轮询）：
  1. databases: 各数据库心跳（可连接 + 最近交易日）
  2. dev_auto: 最近一次运行日志尾部（正在干什么）
  3. scheduled: 计划任务下次运行时间
  4. api_health: 各 API 端点存活
  5. deck: 自身状态
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
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DBS = {
    "bars": r"data/cache/bars.db",
    "finance": r"data/cache/finance.db",
    "finance_quality": r"data/cache/finance_quality.db",
    "minute": r"data/cache/minute.db",
    "hist_mv": r"data/cache/hist_mv.db",
}

SCHEDULED_TASKS = [
    ("LWQuant-DevDriver", "每 4h 巡检"),
    ("LWQuant-AfterCloseScan", "19:30 盘后兜底扫描"),
    ("LWQuant-DailyPipeline", "17:30 每日全链"),
    ("LWQuant-DeckGuard", "每 30min 守护"),
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
            except Exception as _dsh_exc:
                _dsh_swallow(_dsh_exc, "deck/system_live.py:92 _dev_auto_tail")
    return []


_sched_cache = {"ts": 0, "data": None}


def _scheduled() -> dict:
    """计划任务下次运行时间（5 分钟缓存，schtasks 慢会阻塞 API）"""
    now = time.time()
    if _sched_cache["data"] is not None and now - _sched_cache["ts"] < 300:
        return _sched_cache["data"]
    out = {}
    for name, desc in SCHEDULED_TASKS:
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
                               capture_output=True, text=True, errors="replace",
                               encoding="gbk", timeout=8)
            m = re.search(r"下次运行时间:\s*(.+)", r.stdout)
            out[name] = {"desc": desc, "next": m.group(1).strip() if m else "—"}
        except Exception as e:
            out[name] = {"desc": desc, "next": f"ERR {str(e)[:30]}"}
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
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "deck/system_live.py:162 _deck_pid")
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
            except Exception as _dsh_exc:
                _dsh_swallow(_dsh_exc, "deck/system_live.py:183 _activity_feed")
    items.sort(key=lambda x: x["age_min"])
    return items[:15]


def _next_schedule() -> list:
    """★2026-08-10 距下次各计划任务倒计时（分钟）
    ★2026-08-14 修复：schtasks 输出"下次运行时间"为本地化格式（如 2026/8/14 10:00:00，
    月份/日期不补零），strptime 在部分环境解析失败或类型异常导致 portal_dash 间歇 500。
    改为容错解析：手写拆分（年/月/日/时/分/秒）→ datetime；任何失败返回 None 不抛错。"""
    out = []
    try:
        import re as _re
        from datetime import datetime as _dt
        for name, desc in SCHEDULED_TASKS:
            try:
                r = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
                                   capture_output=True, text=True, errors="replace",
                                   encoding="gbk", timeout=8)
                # ★2026-08-25 兼容英文系统（schtasks 输出 "Next Run Time"，中文正则匹配不到 → 永远"暂无计划任务"）
                m = (_re.search(r"下次运行时间:\s*(.+)", r.stdout)
                     or _re.search(r"Next Run Time:\s*(.+)", r.stdout))
                if not m:
                    continue
                nxt = m.group(1).strip()
                # ★容错解析：拆分数字（兼容 2026/8/14 10:00:00 和 2026-08-14 10:00:00）
                parts = _re.findall(r"\d+", nxt)
                if len(parts) >= 5:
                    y, mo, d, h, mi = (int(parts[0]), int(parts[1]), int(parts[2]),
                                       int(parts[3]), int(parts[4]))
                    s = int(parts[5]) if len(parts) >= 6 else 0
                    try:
                        nxt_dt = _dt(y, mo, d, h, mi, s)
                        mins = round((nxt_dt - _dt.now()).total_seconds() / 60, 0)
                        out.append({"name": name, "desc": desc, "next": nxt,
                                    "mins_left": int(mins)})
                    except Exception:
                        out.append({"name": name, "desc": desc, "next": nxt,
                                    "mins_left": None})
            except Exception:
                continue
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "deck/system_live.py:225 _next_schedule")
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
