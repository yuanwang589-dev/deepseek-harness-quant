# -*- coding: utf-8 -*-
"""deck/health.py — 存活 / 就绪 探针（借 Kubernetes 探针语义，零第三方依赖）

## 为什么要拆成两个端点

原实现只有"健康检查"一个概念，`scripts/watchdog.py` 用 `/api/live/opp` 当探针。
那是个**业务接口**：要 glob 产物、要读库。于是出现这样的因果链（2026-10-18 实测）：

    /api/live/funnel 慢查询 72s → 请求线程占满 → 探针也超时
    → watchdog 判定"不健康" → **立刻 kill 子进程**
    → 全站 30~60s 不可用 → 重启后再来一遍

Kubernetes 官方文档对这种情况有明确警告（concepts/workloads/pods/probes）：

> "Liveness probes can be a powerful way to recover from application failures, but they
> should be used with caution." —— 错误实现会导致 "cascading failures"、高负载下容器被重启。
>
> "When your app has a strict dependency on back-end services, you can implement both a
> liveness and a readiness probe. The liveness probe passes when the app itself is healthy,
> but the readiness probe additionally checks that each required back-end service is
> available."

结论照抄即可：**存活探针只反映"进程自己还能不能干活"，绝不查下游依赖；依赖检查放就绪探针。**
本模块即按此拆分：

| 端点 | 语义 | 做什么 | 耗时 | 失败后果 |
|---|---|---|---|---|
| `/healthz` | liveness | 只读进程内状态（uptime/线程数），**零 I/O** | <1ms | 进程已死，该重启 |
| `/readyz` | readiness | 检查 bars.db 可读 + 产物新鲜度 | ~5ms | **不重启**，只标记 not-ready 并列出原因 |

## 三态而不是二态

Prometheus / Grafana 的健康端点用 ok / degraded / failed 三态，因为"能服务但数据陈旧"
和"根本服务不了"是两种完全不同的处置。这里沿用：

    ok        = 依赖可用、产物新鲜              → 200 {"status":"ok"}
    degraded  = 依赖可用、但有产物陈旧/缺失      → 200 {"status":"degraded"} + warnings
    failed    = 依赖不可用（bars.db 打不开）     → 503 {"status":"failed"} + errors

degraded 返 200 是刻意的：陈旧数据仍然能渲染看板，把它判成"不可用"只会让告警疲劳。
"""
import glob
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "data" / "cache"

_START_TS = time.time()

# 产物新鲜度契约（单一出口）：名称 / glob / 期望最大小时数
# 与 data/health_check.py 的 check_chains() 保持同一口径，但这里只保留
# "看板真正会读"的那几项——探针不该因为无关产物变红。
ARTIFACTS = [
    ("opportunities", "logs/opp_pool_*.json", 48),
    ("pitch", "logs/pitch_v2_*.json", 48),
    ("pool_layers", "logs/pool_layers_*.json", 48),
    ("timing", "logs/timing_system_*.json", 48),
    ("daily_signal", "output/daily_signal_*.json", 48),
]


def _latest(pattern: str):
    fs = glob.glob(str(BASE / pattern))
    if not fs:
        return None
    try:
        return max((Path(p) for p in fs), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def liveness() -> dict:
    """存活：只读进程内状态，不碰磁盘/数据库。必须永远能秒回。"""
    return {
        "status": "ok",
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _START_TS, 1),
        "threads": threading.active_count(),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _check_bars() -> dict:
    """bars.db 可读 + 最新交易日。走 (adjust, date) 索引取 MAX，O(log n)。"""
    t0 = time.time()
    p = CACHE / "bars.db"
    try:
        if not p.exists():
            return {"name": "bars_db", "ok": False, "detail": "文件不存在",
                    "ms": round((time.time() - t0) * 1000, 1)}
        # 一律走 data.cache.db_uri()：仓库路径含中文，手拼 f"file:{path}" 是非法 URI
        # （见 references/repo-map.md §5.1/5.2，曾导致"行情面板为空"的假成功）。
        # 不用 immutable=1：下载期可能正在写，immutable 会读不到表（data/cache.py 有注释）。
        from data.cache import db_uri
        con = sqlite3.connect(db_uri(p), uri=True, timeout=3)
        try:
            d = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
        finally:
            con.close()
        return {"name": "bars_db", "ok": bool(d), "detail": f"最新交易日 {d}",
                "ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:
        return {"name": "bars_db", "ok": False, "detail": f"{type(e).__name__}: {str(e)[:80]}",
                "ms": round((time.time() - t0) * 1000, 1)}


def _check_artifacts() -> list:
    """产物新鲜度。缺失/陈旧都记为 warning，不判 failed。"""
    out = []
    now = time.time()
    for name, pat, max_h in ARTIFACTS:
        f = _latest(pat)
        if f is None:
            out.append({"name": name, "ok": False, "issue": "产物缺失",
                        "hint": f"无匹配 {pat}"})
            continue
        age_h = (now - f.stat().st_mtime) / 3600
        ok = age_h <= max_h
        out.append({"name": name, "ok": ok,
                    "issue": "" if ok else f"陈旧 {age_h:.0f}h（>期望 {max_h}h）",
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")})
    return out


def readiness() -> dict:
    """就绪：依赖 + 产物。failed 只由"依赖不可用"触发。"""
    t0 = time.time()
    bars = _check_bars()
    arts = _check_artifacts()
    errors = [] if bars["ok"] else [bars]
    warnings = [a for a in arts if not a["ok"]]
    status = "failed" if errors else ("degraded" if warnings else "ok")
    return {
        "status": status,
        "ok": not errors,                       # 兼容老前端：ok 只表示"能服务"
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": round(time.time() - _START_TS, 1),
        "checks": [bars] + arts,
        "errors": errors,
        "warnings": warnings,
        "ms": round((time.time() - t0) * 1000, 1),
    }
