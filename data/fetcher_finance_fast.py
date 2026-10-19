# -*- coding: utf-8 -*-
"""data/fetcher_finance_fast.py — 财报并行补拉器（新增，2026-10-15）

背景：fetcher_finance.py 为单线程 + 写死 time.sleep(0.30) 限流，
      全量 5223 只约 104 分钟；且不跳过已入库股票（重跑=全量重拉）。
      本脚本：多进程并行 + 断点续传（跳过 finance.db 已有 code），只补缺口。

复用 fetcher_finance 的 fetch_one / parse_finance（同一套解析，不另起一套逻辑），
入库也在主进程单点写入，避免多进程并发写 SQLite。

用法：
  python data/fetcher_finance_fast.py --workers 4            # 补拉缺口
  python data/fetcher_finance_fast.py --workers 4 --limit 30 # 小样本验证
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

FIN_DB = BASE / "data" / "cache" / "finance.db"
BARS_DB = BASE / "data" / "cache" / "bars.db"
FAILED_FILE = BASE / "logs" / "finance_failed_codes_fast.json"
LOG_FILE = BASE / "logs" / "finance_fast.log"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def all_codes():
    con = sqlite3.connect(str(BARS_DB))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    return sorted({c.split(".")[0] for c in codes})


def done_codes():
    if not FIN_DB.exists():
        return set()
    con = sqlite3.connect(str(FIN_DB))
    try:
        s = {r[0] for r in con.execute("SELECT DISTINCT code FROM finance_report")}
    except Exception:
        s = set()
    con.close()
    return s


def _worker(code6):
    """子进程：拉取+解析，返回行列表（不写库）

    ★2026-10-15：同花顺接口对科创板(688xxx)存在**间歇性解析失败**
      （akshare 报 'NoneType' object has no attribute 'string'），
      实测重试即恢复（4/5 成功）→ 内置重试，不当作永久失败。
    """
    from data.fetcher_finance import fetch_one, parse_finance
    last = None
    for attempt in range(4):
        time.sleep(0.15)                  # 每进程限流
        try:
            df = fetch_one(code6)
            rows = parse_finance(code6, df)
            return (code6, rows, None)
        except Exception as e:
            last = str(e)[:90]
            if attempt < 3:
                time.sleep(0.8 * (attempt + 1))
    return (code6, None, last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="不跳过已入库（全量重拉）")
    args = ap.parse_args()

    codes = all_codes()
    if not args.all:
        have = done_codes()
        todo = [c for c in codes if c not in have]
    else:
        have = set()
        todo = codes
    # ★先记录真实缺口，再应用 --limit（否则日志里的"已入库"会被 limit 虚高）
    gap = len(todo)
    if args.limit:
        todo = todo[:args.limit]

    log(f"股票池 {len(codes)} 只，已入库 {len(have)} 只，缺口 {gap} 只，本次补拉 {len(todo)} 只，{args.workers} 进程")
    if not todo:
        log("无缺口，跳过")
        return 0

    con = sqlite3.connect(str(FIN_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS finance_report (
        code TEXT, period TEXT, net_profit REAL, revenue REAL,
        sq_net_profit REAL, sq_net_yoy REAL, sq_revenue REAL, sq_rev_yoy REAL,
        eps REAL, roe REAL, source TEXT,
        PRIMARY KEY (code, period))""")
    con.commit()

    t0 = time.time()
    ok = empty = 0
    fails = []
    with Pool(args.workers) as pool:
        for i, (code, rows, err) in enumerate(pool.imap_unordered(_worker, todo), 1):
            if err:
                fails.append(code)
            elif not rows:
                empty += 1
            else:
                if rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO finance_report VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                ok += 1
            if i % 50 == 0 or i == len(todo):
                con.commit()
                el = time.time() - t0
                rate = i / el if el > 0 else 0
                left = (len(todo) - i) / rate / 60 if rate > 0 else 0
                log(f"[进度] {i}/{len(todo)} 成功{ok} 空{empty} 失败{len(fails)} "
                    f"速率{rate:.2f}只/s 剩余约{left:.0f}分钟")
    con.commit()
    con.close()

    if fails:
        import json
        FAILED_FILE.write_text(json.dumps(fails, ensure_ascii=False), encoding="utf-8")
        log(f"失败 {len(fails)} 只已落盘 {FAILED_FILE}")
    el = time.time() - t0
    log(f"== 完成 == 处理 {len(todo)} 只，成功 {ok}，空 {empty}，失败 {len(fails)}，耗时 {el/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
