# -*- coding: utf-8 -*-
"""data/backfill_st_tushare.py — 用 Tushare `stock_st` 回补 daily_bar.is_st 历史标记。

背景：`fix_st_flags.py` 依赖 baostock 逐股拉取（约 5200 只、1-2 小时，且 baostock 常挂起）；
当前 `daily_bar.is_st=1` 仅 5418 行（2026-09-23 起），C5 审计 FAIL（阈值 1%）。
本脚本按 5 天窗口批量拉全市场 ST 名单（单次接口上限 1000 行），只把命中行置 1，
不覆盖已有 0/1 之外字段，幂等可重跑。

用法：
  python data/backfill_st_tushare.py --start 2019-01-01 --end 2022-12-31
  python data/backfill_st_tushare.py --start 2023-01-01
  python data/backfill_st_tushare.py --status
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _call, _pro
from data.paths import BARS_DB


def _windows(start: str, end: str, days: int = 5):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= e:
        e2 = min(d + timedelta(days=days - 1), e)
        yield d.strftime("%Y%m%d"), e2.strftime("%Y%m%d")
        d = e2 + timedelta(days=1)


def _status():
    con = sqlite3.connect(BARS_DB.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        total = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
        n1 = con.execute("SELECT COUNT(*) FROM daily_bar WHERE is_st=1").fetchone()[0]
        nc = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bar WHERE is_st=1").fetchone()[0]
        rng = con.execute("SELECT MIN(date), MAX(date) FROM daily_bar WHERE is_st=1").fetchone()
    finally:
        con.close()
    print(f"is_st=1 行 {n1} / 总 {total} = {n1 / max(total, 1):.4%}（股票 {nc} 只，区间 {rng[0]} ~ {rng[1]}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.status:
        _status()
        return 0

    pro = _pro()
    con = sqlite3.connect(str(BARS_DB), timeout=30)
    total_rows = updated = 0
    try:
        for s, e in _windows(args.start, args.end, args.days):
            df = _call(pro.stock_st, start_date=s, end_date=e)
            if df is None or df.empty:
                print(f"[{s}~{e}] 无数据", flush=True)
                continue
            rows = []
            for _, r in df.iterrows():
                code = str(r.get("ts_code") or "").upper()
                td = str(r.get("trade_date") or "")
                if not code or len(td) < 8:
                    continue
                rows.append((code, f"{td[:4]}-{td[4:6]}-{td[6:8]}"))
            if not rows:
                continue
            if not args.dry_run:
                cur = con.cursor()
                cur.executemany(
                    "UPDATE daily_bar SET is_st=1 WHERE code=? AND date=? AND adjust='qfq' AND is_st=0",
                    rows)
                updated += cur.rowcount
                con.commit()
            total_rows += len(rows)
            print(f"[{s}~{e}] ST {len(rows)} 条，累计更新 {updated} 行", flush=True)
    finally:
        con.close()
    print(f"完成：接口返回 {total_rows} 条，实际更新 {updated} 行")
    _status()
    return 0


if __name__ == "__main__":
    sys.exit(main())