# -*- coding: utf-8 -*-
"""
data/fetch_quality_tushare.py — ★质量因子全市场补拉（主服务器版 · 2026-08-07）

背景：baostock 版 fetch_quality.py 三接口多进程拉取，全市场 5400 只进度慢（1761 只）。
      主服务器 fina_indicator 单接口即含全部所需指标 → 并发 8 线程全市场 ~10 分钟。

与 baostock 版同表（finance_quality.db / quality），字段映射：
  roe_avg          ← roe（年化 ROE，fina_indicator 已年化口径）
  gp_margin        ← grossprofit_margin
  np_margin        ← netprofit_margin
  current_ratio    ← current_ratio
  liability_to_asset ← debt_to_assets
  cfo_to_np        ← ocfps/eps 近似（无直接字段；eps>0 才有意义）
  cfo_to_or        ← ocfps/revenue_ps 近似
  pub_date         ← ann_date

拉取区间：2024Q1-2026Q2（10 个报告期，覆盖 F-Score 同比判断）
用法：python data/fetch_quality_tushare.py [--workers 8] [--limit N] [--status]
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
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

os_env_safe = True
import os
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
import concurrent.futures

QD_DB = Path(r"data/cache/finance_quality.db")
LOG_FILE = BASE / "logs" / "quality_tushare.log"
START_QUARTER = "20240101"
END_QUARTER = "20260630"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/fetch_quality_tushare.py:53 log")


def _conn():
    con = sqlite3.connect(str(QD_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS quality (
        code TEXT, period TEXT, roe_avg REAL, gp_margin REAL, np_margin REAL,
        current_ratio REAL, liability_to_asset REAL, cfo_to_np REAL, cfo_to_or REAL,
        pub_date TEXT, PRIMARY KEY(code, period))""")
    con.commit()
    return con


def load_codes():
    con = sqlite3.connect(r"data/cache/stock_basic.db")
    codes = [r[0] for r in con.execute(
        "SELECT code FROM stock_basic WHERE code LIKE '%.SH' OR code LIKE '%.SZ'").fetchall()]
    con.close()
    return codes


def _done_codes():
    con = _conn()
    done = {r[0] for r in con.execute("SELECT DISTINCT code FROM quality").fetchall()}
    con.close()
    return done


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN → None
    except (TypeError, ValueError):
        return None


def _fetch_one(pro, code):
    """拉单股 fina_indicator → 质量指标行列表（含近似现金流比）
    主服务器对并发敏感（8 线程会 Read timeout）→ 单股内部自带 2 次重试
    """
    for attempt in range(3):
        try:
            d = pro.fina_indicator(ts_code=code, start_date=START_QUARTER, end_date=END_QUARTER)
            break
        except Exception as e:
            if attempt == 2:
                return code, None, str(e)[:120]
            time.sleep(2.0 * (attempt + 1))
    if d is None or d.empty:
        return code, [], None
    rows = []
    for _, r in d.iterrows():
        period = str(r["end_date"])
        if len(period) != 8:
            continue
        period = f"{period[:4]}-{period[4:6]}-{period[6:]}"
        eps = _num(r.get("eps"))
        ocfps = _num(r.get("ocfps"))
        rev_ps = _num(r.get("revenue_ps"))
        cfo_np = (ocfps / eps) if (eps and eps > 0 and ocfps is not None) else None
        cfo_or = (ocfps / rev_ps) if (rev_ps and rev_ps > 0 and ocfps is not None) else None
        # ★口径：Tushare 返回百分数（2.8165=2.82%），baostock 版存小数（0.028165）→ 统一 /100
        #   current_ratio 两源都是倍率（6.07 倍），不除
        pct = lambda v: (v / 100.0) if v is not None else None
        rows.append((code, period,
                     pct(_num(r.get("roe"))), pct(_num(r.get("grossprofit_margin"))),
                     pct(_num(r.get("netprofit_margin"))), _num(r.get("current_ratio")),
                     pct(_num(r.get("debt_to_assets"))), cfo_np, cfo_or,
                     str(r.get("ann_date")) if r.get("ann_date") else None))
    return code, rows, None


def run(workers=8, limit=None):
    from data.fetcher_tushare import _pro, _rate_limit
    pro = _pro()
    codes = load_codes()
    done = _done_codes()
    todo = [c for c in codes if c not in done]
    if limit:
        todo = todo[:limit]
    log(f"开始全市场质量补拉: 总 {len(codes)} 只, 已完成 {len(done)}, 待拉 {len(todo)} 只, workers={workers}")
    if not todo:
        log("无需补拉")
        return

    con = _conn()
    t0 = time.time()
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, pro, c): c for c in todo}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            code, rows, err = fut.result()
            if rows is not None and rows:
                con.executemany(
                    """INSERT OR REPLACE INTO quality
                       (code, period, roe_avg, gp_margin, np_margin, current_ratio,
                        liability_to_asset, cfo_to_np, cfo_to_or, pub_date)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
                con.commit()
                ok += 1
            elif err:
                log(f"  [失败] {code}: {err}")
            if i % 200 == 0:
                el = time.time() - t0
                log(f"  进度 {i}/{len(todo)} ({el:.0f}s, 成功 {ok}, 速度 {i/el:.1f}只/s)")
    con.close()
    el = time.time() - t0
    log(f"完成: 成功 {ok}/{len(todo)} 只, 耗时 {el:.0f}s ({el/60:.1f} 分钟)")


def status():
    con = _conn()
    n = con.execute("SELECT COUNT(DISTINCT code) FROM quality").fetchone()[0]
    rows = con.execute("SELECT COUNT(*) FROM quality").fetchone()[0]
    print(f"quality 表: {n} 只股票 / {rows} 行")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(workers=args.workers, limit=args.limit)
