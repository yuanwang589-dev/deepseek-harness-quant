# -*- coding: utf-8 -*-
"""scripts/smoke_check.py — 核心链路冒烟测试（2026-10-15 P1-B）

背景：评审结论"短板集中在失败要大声说出来"——历史多次出现
静默失败（相对路径 as_uri 抛错被吞 / baostock 0 行 parquet / 假成功步骤），
部署/更新后需要一条命令验证系统可用性。

检查项（全部只读，不写任何数据）：
  1. 缓存目录与关键库文件存在且非空（bars.db / finance.db / parquet）
  2. bars.db 可经 db_uri 只读打开、最新交易日合理（≤7 天前）
  3. 机会池 / Pitch / 三层池 / 择时 输出文件存在且 mtime ≤ 48h
  4. 关键模块可导入（cache/scan/portfolio/timing_system/live_api）
  5. traffic_light parquet 与五福 parquet 存在非空
  6. deck 服务健康（可选：--with-server 时探 8787 端口）

用法：
  python scripts/smoke_check.py            # 全量检查，exit 0=通过 1=失败
  python scripts/smoke_check.py --quiet    # 只输出失败项
"""
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # DSHQuant/
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []
QUIET = "--quiet" in sys.argv


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    if not QUIET or not ok:
        print(("  ✓ " if ok else "  ✗ ") + name + ("  " + detail if detail else ""))


def section(t):
    print("\n[" + t + "]")


# ---------- 1. 缓存文件 ----------
section("1. 缓存目录与库文件")
CACHE = BASE / "data" / "cache"
check("缓存目录存在", CACHE.is_dir(), str(CACHE))
for f, min_mb in [("bars.db", 1), ("finance.db", 0), ("stock_basic.db", 0)]:
    p = CACHE / f
    ok = p.exists() and p.stat().st_size >= min_mb * 1024 * 1024
    size = (p.stat().st_size / 1024 / 1024) if p.exists() else 0
    check(f"{f} 存在且 ≥{min_mb}MB", ok, f"{size:.1f}MB")

# ---------- 2. bars.db 可读 + 最新交易日 ----------
section("2. bars.db 只读连通 + 最新交易日")
try:
    from data.cache import db_uri, DailyCache
    con = sqlite3.connect(db_uri(CACHE / "bars.db"), uri=True, timeout=5)
    mx = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    con.close()
    check("bars.db 只读连通", True, "MAX(date)=" + str(mx))
    if mx:
        try:
            import datetime as _dt
            d = _dt.date(int(mx[:4]), int(mx[5:7]), int(mx[8:10]))
            age = (_dt.date.today() - d).days
            check("最新交易日 ≤7 天", age <= 7, f"距今 {age} 天")
        except Exception as e:
            check("最新交易日解析", False, str(e))
    ltd = DailyCache().latest_trade_date()
    check("DailyCache.latest_trade_date()", bool(ltd), str(ltd))
except Exception as e:
    check("bars.db 只读连通", False, f"{type(e).__name__}: {e}")

# ---------- 3. 管道产物（48h 新鲜度）----------
section("3. 管道产物新鲜度（≤48h）")
NOW = time.time()
ARTIFACTS = [
    ("机会池",   BASE / "logs", "opp_pool_*.json"),
    ("Pitch",    BASE / "logs", "pitch_v2_*.json"),
    ("三层池",   BASE / "output", "pool_layers_*.json"),
    ("择时系统", BASE / "output", "timing_system_*.json"),
]
for label, d, pat in ARTIFACTS:
    files = sorted(d.glob(pat), key=os.path.getmtime)
    if not files:
        check(f"{label} 产物存在", False, f"无匹配 {d.name}/{pat}")
        continue
    age_h = (NOW - files[-1].stat().st_mtime) / 3600
    check(f"{label} 产物 ≤48h", age_h <= 48, f"{files[-1].name}（{age_h:.1f}h 前）")

# 竞价信号：分钟数据是硬依赖（data boundary）——缺数据时降级为警告
min_dir = BASE / "data" / "minute" / "download" / "1m_price_zip"
auc = BASE / "logs" / "auction_signal.json"
if not min_dir.exists():
    print("  ⚠ 竞价信号：分钟数据未下载（data/minute/download/1m_price_zip 缺失）——"
          "竞价信号跳过属数据边界，不算失败")
elif auc.exists():
    age_h = (NOW - auc.stat().st_mtime) / 3600
    check("竞价信号 产物 ≤48h", age_h <= 48, f"{age_h:.1f}h 前")
else:
    check("竞价信号 产物存在", False, "分钟数据在但 auction_signal.json 未生成")

# ---------- 4. 关键模块导入 ----------
section("4. 关键模块导入")
for m in ["data.cache", "factors.opportunities.scan", "factors.opportunities.shortterm_hook",
          "strategy.portfolio", "factors.policy.timing_system", "deck.live_api",
          "risk.stop_monitor", "data.incremental_daily_tushare"]:
    try:
        __import__(m)
        check(f"import {m}", True)
    except Exception as e:
        check(f"import {m}", False, f"{type(e).__name__}: {e}")

# ---------- 5. 择时/五福 parquet ----------
section("5. 择时趋势 / 五福轮动 parquet")
tl_pq = [BASE / "output" / "hs300_monthly.parquet", BASE / "output" / "eq_monthly.parquet"]
tl_pq = [p for p in tl_pq if p.exists()]
check("择时 parquet 存在非空", bool(tl_pq and all(p.stat().st_size > 2 for p in tl_pq)),
      f"{len(tl_pq)}/2 个（output/hs300_monthly, eq_monthly）")
try:
    import pandas as _pd
    df = _pd.read_parquet(tl_pq[-1]) if tl_pq else None
    check("择时 parquet 可读且非空", df is not None and len(df) > 0,
          f"{len(df)} 行" if df is not None else "")
except Exception as e:
    check("择时 parquet 可读", False, f"{type(e).__name__}: {e}（pyarrow 未装?）")

wufu_pq = sorted((BASE / "data" / "factorpool" / "output" / "cache").glob("wufu_*.parquet")) \
    if (BASE / "data" / "factorpool" / "output" / "cache").is_dir() else []
if wufu_pq:
    try:
        df = _pd.read_parquet(wufu_pq[-1])
        check("五福 parquet 可读且非空", len(df) > 0, f"{wufu_pq[-1].name}: {len(df)} 行")
    except Exception as e:
        check("五福 parquet 可读", False, f"{type(e).__name__}: {e}")
else:
    check("五福 parquet 存在", False,
          "未找到 data/factorpool/output/cache/wufu_*.parquet（跑 data/build_wufu_cache.py）")

# ---------- 6. deck 服务（可选）----------
section("6. deck 服务健康（8787）")
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8787/api/live/pools", timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    check("GET /api/live/pools", body.get("ok") is True)
except Exception as e:
    check("GET /api/live/pools", False, f"{type(e).__name__}: {e}（deck 未启动?）")

# ---------- 汇总 ----------
print("\n" + "=" * 56)
print(f"冒烟测试：{len(PASS)} 通过，{len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for n, d in FAIL:
        print("  ✗ " + n + ("  " + d if d else ""))
    sys.exit(1)
print("全部通过 ✓")
sys.exit(0)
