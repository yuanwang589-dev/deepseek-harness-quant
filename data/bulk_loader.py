# -*- coding: utf-8 -*-
"""全市场批量日线下载器（M2 数据入库）

设计：
- 股票列表：Tushare stock_basic（沪深，排除北交所——baostock 不支持）
- 数据：Baostock 主源，前复权 2010 至今，经 ensure_daily 写本地缓存
- ★断点续传：缓存 covers 判定自动跳过已入库股票（可随时中断/重启）
- 进度日志：每 50 只记录，写入 logs/bulk_load.log（含速率/失败清单）
- 异常隔离：单只失败重试 2 次后跳过记入 failures，不中断整体

用法：
  python data/bulk_loader.py --limit 50     # 小样本测试（速率验证）
  python data/bulk_loader.py                # 全量（断点续传）
  python data/bulk_loader.py --start 3000   # 从第 N 只开始（分片）
  python data/bulk_loader.py --status       # 查看进度
"""
import argparse
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

from data.cache import DailyCache
from data.fetcher_baostock import _ensure_login, ensure_daily

# ---- 单实例锁（PID 锁文件：防止重复启动写库冲突）----
LOCK_FILE = Path(__file__).resolve().parent / "logs" / "bulk_load.lock"


def _pid_alive(pid: int) -> bool:
    """检查 PID 对应的进程是否存活（Windows tasklist 查询）"""
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
            capture_output=True, timeout=15)
        out = r.stdout.decode("gbk", errors="ignore")
        return str(pid) in out
    except Exception:
        return False  # 查询失败时保守认为不存活（允许启动）


def acquire_single_instance_lock() -> bool:
    """获取单实例锁（PID 锁文件）。已有实例在跑返回 False。

    原理：锁文件内容 = 运行中实例的 PID。
    - 锁文件存在且 PID 存活 → 有实例在跑 → 拒绝
    - 锁文件不存在 / PID 已死（进程崩溃）→ 写入自己 PID → 获得锁
    进程正常/异常退出后 PID 失效，锁自动可再获取（不残留）。
    """
    try:
        if LOCK_FILE.exists():
            pid_str = LOCK_FILE.read_text(encoding="utf-8").strip()
            if pid_str.isdigit() and _pid_alive(int(pid_str)):
                return False
        LOCK_FILE.write_text(str(__import__("os").getpid()), encoding="utf-8")
        return True
    except Exception:
        return True  # 锁机制异常时放行（由熔断机制兜底）


def release_single_instance_lock():
    """释放锁：删除锁文件（仅当是自己持有）"""
    try:
        import os
        if LOCK_FILE.exists():
            pid_str = LOCK_FILE.read_text(encoding="utf-8").strip()
            if pid_str == str(os.getpid()):
                LOCK_FILE.unlink()
    except Exception:
        pass

LOG_FILE = BASE / "logs" / "bulk_load.log"
PROGRESS_FILE = BASE / "logs" / "bulk_load_progress.txt"
START_DATE = "2019-01-01"      # P0.5 因子验证只需 2020 起 + 250 日 RPS 缓冲；2010-2018 后续补拉


def latest_market_date():
    """★2026-10-15 断点续传修复：用库内最新交易日作为 end。
    原先 end=None → today；而 bar_meta.end_date 只到上一交易日，
    covers() 判 end_date(t-1) >= today(t) 为假 → 已下载股票被全部重拉。
    （同款 bug 见 data/bulk_loader_akshare.py / data/fetcher_akshare.py 修复说明）"""
    import sqlite3
    db = BASE / "data" / "cache" / "bars.db"
    if not db.exists():
        return time.strftime("%Y-%m-%d")
    try:
        con = sqlite3.connect(str(db))
        d = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        con.close()
        return d or time.strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")
CHECKPOINT_EVERY = 50
BATCH_LOG_EVERY = 500
DEFAULT_WORKERS = 2            # 多进程并行。★Baostock 免费服务对并发连接限流（WinError 10057/10002007），4 进程实测失败率 94%，2 进程+重试最稳（2026-08-06 实测）


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_universe(limit=None, start=0):
    """全市场股票列表：Baostock 主源（免费无频次限制，8880 行含指数/退市）

    注：Tushare stock_basic 免费限频 1 次/小时，仅作备源（缓存命中时可用）。
    """
    import baostock as bs
    _ensure_login()
    rs = bs.query_stock_basic()
    if rs.error_code != "0":
        raise RuntimeError(f"Baostock 股票列表失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    cols = rs.fields  # code, code_name, ipoDate, outDate, type, status
    df = pd.DataFrame(rows, columns=cols)
    df = df[df["type"] == "1"]                                    # 仅股票（排除指数）
    df = df[df["status"] == "1"]                                  # 仅上市（排除退市）
    df = df[df["code"].str.startswith(("sh.", "sz."))]            # 沪深（baostock 无北交所）
    df["ts_code"] = df["code"].str.replace("sh.", "6").str.replace("sz.", "0")
    df["ts_code"] = df.apply(lambda r: r["code"][3:] + (".SH" if r["code"].startswith("sh.") else ".SZ"), axis=1)
    df = df.sort_values("ts_code").reset_index(drop=True)
    total = len(df)
    if start > 0:
        df = df.iloc[start:]
    if limit:
        df = df.iloc[:limit]
    log(f"股票池: 共 {total} 只（沪深上市股），本次处理 {len(df)} 只（start={start}, limit={limit or '全量'}）")
    return df, total


def _worker(code: str):
    """单只下载（多进程 worker：每进程独立 DailyCache + baostock 登录）

    ★重试机制：Baostock 免费服务偶发网络断连（10002007 网络接收错误 / WinError 10057），
    单只失败后等待退避重试，最多 5 次——重试后仍失败才记为 fail。
    """
    cache = DailyCache()
    max_retry = 5
    last_err = None
    end = latest_market_date()
    # ★2026-10-15 断点续传修复：cache.covers() 要求 meta.start_date <= start，
    #   但首个交易日(2019-01-02)晚于请求 start(2019-01-01) → covers 恒为 False →
    #   续传时已下载股票被全部重拉。改为只判"末日期是否已到最新交易日"。
    try:
        _meta = cache.get_meta(code, "qfq")
        if _meta and _meta.get("end_date") and _meta["end_date"] >= end:
            return (code, "ok", None)
    except Exception:
        pass
    for attempt in range(max_retry):
        try:
            df = ensure_daily(code, START_DATE, end, "qfq", cache=cache)
            if df is None or df.empty:
                return (code, "empty", None)
            return (code, "ok", None)
        except Exception as e:
            last_err = str(e)[:60]
            if attempt < max_retry - 1:
                time.sleep(2.0 * (2 ** attempt))   # 退避 2s / 4s / 8s / 16s
    return (code, "fail", last_err)


# ---- 连续失败熔断（2026-08-07 新增，防深夜服务端断连空转卡死）----
CONSEC_FAIL_TRIGGER = 20      # 连续失败 N 只 → 熔断暂停
BREAK_PAUSE_SEC = 300          # 熔断暂停 5 分钟（服务端可能维护中）
MAX_BREAK_ROUNDS = 3           # 连续 3 轮熔断仍全失败 → 保存进度正常退出（不卡死）
FAILED_CSV = BASE / "logs" / "bulk_load_failed.csv"


def _append_failed(code: str, reason: str):
    """失败清单实时导出（供备用源腾讯 stock_zh_a_hist_tx 补拉）"""
    try:
        with open(FAILED_CSV, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S},{code},{reason}\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（测试用）")
    ap.add_argument("--start", type=int, default=0, help="从列表第 N 只开始")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并行进程数")
    ap.add_argument("--status", action="store_true", help="查看进度")
    args = ap.parse_args()

    if args.status:
        if PROGRESS_FILE.exists():
            print(PROGRESS_FILE.read_text(encoding="utf-8"))
        else:
            print("尚无批量下载进度")
        return 0

    # ★单实例锁：已有实例在跑则拒绝启动（避免多实例并发写库）
    # 注：Windows 文件锁在进程退出/崩溃时由系统自动释放，无需显式 finally
    if not acquire_single_instance_lock():
        log("★另一个 bulk_loader 实例正在运行（单实例锁），本次启动被拒绝。"
            "不要重复启动；如需重启先结束旧实例（控制台 → 熔断 或任务管理器结束 python）。")
        print("已有下载任务在运行（单实例锁生效），本次未启动。可用控制台菜单 3 查看进度。")
        return 2

    lst, total = load_universe(args.limit, args.start)
    codes = lst["ts_code"].tolist()

    t0 = time.time()
    done, fail = 0, []
    results = []
    consec_fail = 0            # 连续失败计数（熔断判定）
    break_rounds = 0           # 熔断轮数

    def handle_result(res):
        """统一处理单只结果：计数 + 失败实时导出 + 熔断判定"""
        nonlocal done, consec_fail, break_rounds
        if res[1] == "ok":
            done += 1
            consec_fail = 0
        else:
            fail.append((res[0], res[2] or res[1]))
            _append_failed(res[0], res[2] or res[1])
            consec_fail += 1
        # 连续失败熔断：服务端可能维护/断连，暂停等待恢复
        if consec_fail >= CONSEC_FAIL_TRIGGER:
            break_rounds += 1
            log(f"★连续失败 {consec_fail} 只 → 熔断暂停 {BREAK_PAUSE_SEC//60} 分钟（第 {break_rounds} 轮，"
                f"服务端可能维护中）")
            time.sleep(BREAK_PAUSE_SEC)
            consec_fail = 0
            if break_rounds >= MAX_BREAK_ROUNDS:
                log(f"★连续 {MAX_BREAK_ROUNDS} 轮熔断仍全失败 → 保存进度正常退出（不空转卡死）")
                PROGRESS_FILE.write_text(
                    f"熔断退出 {datetime.now():%H:%M:%S} | 成功 {done} | 失败 {len(fail)}\n"
                    + "\n".join(f"{c}: {r}" for c, r in fail[-20:]), encoding="utf-8")
                release_single_instance_lock()
                return "ABORT"

    if args.workers > 1 and len(codes) > 1:
        with Pool(args.workers) as pool:
            for i, res in enumerate(pool.imap_unordered(_worker, codes, chunksize=4)):
                results.append(res)
                if handle_result(res) == "ABORT":
                    return 1
                if (i + 1) % CHECKPOINT_EVERY == 0:
                    el = time.time() - t0
                    rate = (i + 1) / el
                    eta = (len(codes) - i - 1) / rate if rate > 0 else 0
                    log(f"[进度] {i+1}/{len(codes)} 成功{done} 失败{len(fail)} "
                        f"速率{rate:.2f}只/s 剩余约{eta/60:.0f}分钟")
    else:
        for i, (_, row) in enumerate(lst.iterrows()):
            code = row["ts_code"]
            res = _worker(code)
            results.append(res)
            if handle_result(res) == "ABORT":
                return 1
            if (i + 1) % CHECKPOINT_EVERY == 0:
                el = time.time() - t0
                rate = (i + 1) / el
                eta = (len(codes) - i - 1) / rate if rate > 0 else 0
                log(f"[进度] {i+1}/{len(codes)} 成功{done} 失败{len(fail)} "
                    f"速率{rate:.2f}只/s 剩余约{eta/60:.0f}分钟")

    el = time.time() - t0
    log(f"== 批量下载完成 == 处理 {len(codes)} 只，成功 {done}，失败 {len(fail)}，"
        f"耗时 {el/60:.1f} 分钟，速率 {len(codes)/el:.2f} 只/s")
    if fail:
        log(f"失败清单（前 20）: {fail[:20]}")
    PROGRESS_FILE.write_text(
        f"最终 {datetime.now():%H:%M:%S} | 共 {total} 只 | 本次完成 {len(codes)} | 成功 {done} | 失败 {len(fail)}\n"
        + "\n".join(f"{c}: {r}" for c, r in fail[:20]), encoding="utf-8")
    release_single_instance_lock()
    return 0 if not fail else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断，锁已自动释放（进程退出）。")
        sys.exit(130)
