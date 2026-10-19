# -*- coding: utf-8 -*-
"""data/bulk_loader_akshare.py — AkShare 备源全市场日线下载器（新增，2026-10-15）

背景：2026-10-15 实测 baostock 服务端不可用（单次登录亦报 10002007 网络接收错误），
      改用项目内置备源 AkShare（新浪）补完剩余股票。
      与 data/bulk_loader.py 同语义：缓存优先、断点续传、多进程、失败重试。
      参数来源见 config/params.yaml data.sources.daily_backup = "akshare"。

差异：
- 股票池来自 data/cache/stock_basic.db（baostock 已入库 5562 只）——不依赖 baostock 在线
- 写入 source='akshare'；preclose/pct_chg 由 fetcher_akshare 自算，is_st 置 0

用法：
  python data/bulk_loader_akshare.py --limit 20      # 小样本验证
  python data/bulk_loader_akshare.py --workers 4     # 全量（断点续传，可随时中断重启）
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

START_DATE = "2019-01-01"
LOG_FILE = BASE / "logs" / "bulk_load_akshare.log"
FAILED_FILE = BASE / "logs" / "bulk_failed_akshare.csv"
STOCK_BASIC = BASE / "data" / "cache" / "stock_basic.db"
BARS_DB = BASE / "data" / "cache" / "bars.db"


def latest_market_date():
    """★断点续传关键：用库内最新交易日作为 end。
    若用 today，covers() 因 meta.end_date(=昨日) < today 而判未覆盖 →
    已下载股票会被全部重拉（实测 000001.SZ 被 akshare 重下 1885 行）。"""
    if not BARS_DB.exists():
        return time.strftime("%Y-%m-%d")
    con = sqlite3.connect(str(BARS_DB))
    try:
        d = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    except Exception:
        d = None
    finally:
        con.close()
    return d or time.strftime("%Y-%m-%d")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_universe(limit=None, start=0):
    """股票池：stock_basic.db（沪深 A 股，按代码排序稳定）"""
    con = sqlite3.connect(str(STOCK_BASIC))
    codes = [r[0] for r in con.execute(
        "SELECT code FROM stock_basic WHERE code LIKE '%.SH' OR code LIKE '%.SZ' ORDER BY code")]
    con.close()
    total = len(codes)
    if start > 0:
        codes = codes[start:]
    if limit:
        codes = codes[:limit]
    log(f"股票池: 共 {total} 只（沪深），本次处理 {len(codes)} 只（start={start}, limit={limit or '全量'}）")
    return codes, total


def _worker(code: str):
    """单只下载（子进程独立 DailyCache）。缓存已覆盖则自动跳过（断点续传）。"""
    from data.cache import DailyCache
    from data.fetcher_akshare import ensure_daily
    cache = DailyCache()
    end = latest_market_date()
    # 同 bulk_loader.py：covers() 因 start_date(首个交易日) > start 恒为 False，
    # 改用"末日期是否已达最新交易日"判定续传
    try:
        _meta = cache.get_meta(code, "qfq")
        if _meta and _meta.get("end_date") and _meta["end_date"] >= end:
            return (code, "ok", None)
    except Exception:
        pass
    last_err = None
    for attempt in range(4):
        try:
            df = ensure_daily(code, START_DATE, end, "qfq", cache=cache, retries=2)
            if df is None or df.empty:
                return (code, "empty", None)
            return (code, "ok", None)
        except Exception as e:
            last_err = str(e)[:90]
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    return (code, "fail", last_err)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if not STOCK_BASIC.exists():
        log(f"缺少 {STOCK_BASIC}，请先运行 data/fetch_stock_basic.py")
        return 1

    codes, total = load_universe(args.limit, args.start)
    t0 = time.time()
    ok = empty = 0
    fails = []
    step = 50

    with Pool(args.workers) as pool:
        for i, (code, status, err) in enumerate(pool.imap_unordered(_worker, codes), 1):
            if status == "ok":
                ok += 1
            elif status == "empty":
                empty += 1
            else:
                fails.append((code, err or ""))
                with open(FAILED_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S},{code},{err}\n")
            if i % step == 0 or i == len(codes):
                el = time.time() - t0
                rate = i / el if el > 0 else 0
                left = (len(codes) - i) / rate / 60 if rate > 0 else 0
                log(f"[进度] {i}/{len(codes)} 成功{ok} 空{empty} 失败{len(fails)} "
                    f"速率{rate:.2f}只/s 剩余约{left:.0f}分钟")

    el = time.time() - t0
    log(f"== 完成 == 处理 {len(codes)} 只，成功 {ok}，空 {empty}，失败 {len(fails)}，"
        f"耗时 {el/60:.1f} 分钟")
    if fails:
        log(f"失败清单（前 10）：{fails[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
