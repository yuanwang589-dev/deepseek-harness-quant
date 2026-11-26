# -*- coding: utf-8 -*-
"""
data/convert_backup_raw.py — ★备用 raw 历史数据 → 前复权转换（v2 · 2026-08-09 本地版）

★v2 重大升级：不再依赖主服务器！
  用户下载了 Tushare 15000 积分全量历史包（本地 parquet）：
    data/minute/download/tushare_15000_history_by_api_packages_20260627/adj_factor/
  含 2000-2026 全部交易日 adj_factor（6428 个文件，按 trade_date 分片）

  转换公式（标准前复权，与 baostock 2019+ 基准一致，此前已实证 600519 精确吻合）：
    qfq(day) = raw(day) × adj(day) / adj_latest
  其中 adj_latest = 每只股票最新交易日（2026-08）的复权因子。

背景：2010-2018 段备用服务器只拉了不复权（adjust='none', source='tushare_backup'），
  本脚本把这些 raw 转成 qfq 写入（source='tushare_backup', adjust='qfq'）。

用法：
  python data/convert_backup_raw.py --status   # 查看待转换天数
  python data/convert_backup_raw.py --limit 5  # 试转 5 天（验证）
  python data/convert_backup_raw.py            # 全量转换
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
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

BARS_DB = r"data\cache\bars.db"
LOG_FILE = BASE / "logs" / "convert_raw.log"
# ★本地 adj_factor parquet 目录（用户下载的 Tushare 历史包）
ADJ_DIR = Path(r"data/minute/download/tushare_15000_history_by_api_packages_20260627/adj_factor/adj_factor/data")
START = "20100101"
END = "20181231"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/convert_backup_raw.py:48 log")


def _conn():
    return sqlite3.connect(BARS_DB)


def get_raw_days():
    """2010-2018 中已有 raw（tushare_backup）的交易日"""
    con = _conn()
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE source='tushare_backup' AND adjust='none' "
        "AND date>='2010-01-01' AND date<'2019-01-01'").fetchall()]
    con.close()
    return sorted(days)


def get_qfq_done():
    """已转换（有 qfq 且 source=tushare_backup）的日期"""
    con = _conn()
    days = {r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE source='tushare_backup' AND adjust='qfq' "
        "AND date>='2010-01-01' AND date<'2019-01-01'").fetchall()}
    con.close()
    return days


def load_adj_day(trade_date):
    """读某交易日本地 adj_factor → {code: adj_factor}"""
    p = ADJ_DIR / f"adj_factor__trade_date={trade_date}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return dict(zip(df["ts_code"], df["adj_factor"]))


def load_anchor_k():
    """锚点系数 k（v3 锚点法，2026-08-09 修正）：
    k = P_bs(anchor) / (raw_a × adj_a)
      P_bs  = bars.db baostock 2019-01-02 qfq 价
      raw_a = 本地 daily parquet 2019-01-02 不复权价
      adj_a = 本地 adj_factor parquet 2019-01-02 因子
    ★为何不用 adj_latest 除法：本地包因子最新仅 2026-07-15，若此后发生除权
      （分红/送转）则 qfq 整体比例漂移 → 与 baostock 2019+ 基准断层。
      锚点法在锚点日精确对齐 baostock，其他日期只依赖 adj 比例（同源一致）→ 无漂移。
    """
    ANCHOR = "20190102"
    daily_p = Path(r"data/minute/download/tushare_15000_history_by_api_packages_20260627/daily/daily/data") / f"daily__trade_date={ANCHOR}.parquet"
    adj_p = ADJ_DIR / f"adj_factor__trade_date={ANCHOR}.parquet"
    if not daily_p.exists() or not adj_p.exists():
        log(f"[锚点] 本地锚点文件缺失 daily={daily_p.exists()} adj={adj_p.exists()}")
        return {}
    d = pd.read_parquet(daily_p)
    a = pd.read_parquet(adj_p)
    d_map = dict(zip(d["ts_code"], d["close"]))
    a_map = dict(zip(a["ts_code"], a["adj_factor"]))
    anchor_db = f"{ANCHOR[:4]}-{ANCHOR[4:6]}-{ANCHOR[6:]}"
    con = _conn()
    bs_rows = con.execute(
        "SELECT code, close FROM daily_bar WHERE date=? AND adjust='qfq' AND source='baostock'",
        (anchor_db,)).fetchall()
    con.close()
    k_map = {}
    for code, p_bs in bs_rows:
        if not p_bs or p_bs <= 0:
            continue
        raw_a = d_map.get(code)
        adj_a = a_map.get(code)
        if raw_a and raw_a > 0 and adj_a and adj_a > 0:
            k_map[code] = p_bs / (raw_a * adj_a)
    log(f"[锚点] k 系数 {len(k_map)} 只（锚点日 {ANCHOR} 精确对齐 baostock）")
    return k_map


def convert_day(trade_date, k_map):
    """单日转换：本地 adj_factor → qfq = raw × adj × k（锚点法）→ 写 qfq 行"""
    date_s = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    a_map = load_adj_day(trade_date)
    if a_map is None:
        return trade_date, 0, "本地因子文件缺失"
    if not a_map:
        return trade_date, 0, "本地因子空"
    con = _conn()
    raws = con.execute(
        "SELECT code, open, high, low, close, preclose, volume, amount, pct_chg "
        "FROM daily_bar WHERE date=? AND source='tushare_backup' AND adjust='none'",
        (date_s,)).fetchall()
    rows = []
    for code, o, h, l, c, pc, vol, amt, pct in raws:
        f = a_map.get(code)
        if f is None or f <= 0:
            continue
        k = k_map.get(code, 1.0)   # 无锚点股票 k=1.0（新股/退市，raw 直写）
        pk = f * k                 # ★锚点法前复权系数
        rows.append((
            code.upper(), date_s,
            o * pk, h * pk, l * pk, c * pk, pc * pk,
            vol, amt, None, pct, 0, "qfq", "tushare_backup",
        ))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO daily_bar "
            "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    con.close()
    return trade_date, len(rows), None


def run(limit=None):
    if not ADJ_DIR.exists():
        log(f"[错误] 本地 adj_factor 目录不存在: {ADJ_DIR}")
        return
    days = get_raw_days()
    done = get_qfq_done()
    todo = [d for d in days if d not in done]
    if limit:
        todo = todo[:limit]
    log(f"转换(v3锚点法): raw 天数 {len(days)}, 已完成 {len(done)}, 待转 {len(todo)}")
    if not todo:
        log("无需转换")
        return
    k_map = load_anchor_k()
    t0 = datetime.now()
    n_ok = n_rows = 0
    for i, d in enumerate(todo, 1):
        td, nr, err = convert_day(d.replace("-", ""), k_map)
        if nr:
            n_ok += 1
            n_rows += nr
        elif err:
            log(f"  [失败] {d}: {err}")
        if i % 50 == 0:
            el = (datetime.now() - t0).total_seconds()
            log(f"  进度 {i}/{len(todo)} ({el:.0f}s, 成功 {n_ok}, 均 {el/i:.1f}s/天)")
    el = (datetime.now() - t0).total_seconds()
    log(f"完成: {n_ok}/{len(todo)} 天, {n_rows} 行, 耗时 {el:.0f}s ({el/60:.1f} 分钟)")


def status():
    days = get_raw_days()
    done = get_qfq_done()
    print(f"raw 天数: {len(days)} | qfq 已转换: {len(done)} | 待转: {len(days) - len(done)}")
    print(f"本地 adj_factor 目录: {ADJ_DIR} ({'存在' if ADJ_DIR.exists() else '不存在'})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(limit=args.limit)
