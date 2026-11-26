# -*- coding: utf-8 -*-
"""
data/backfill_hist_bars.py — ★2010-2018 全市场历史行情回填（Tushare 主服务器 · 2026-08-07）

背景：bars.db 2010-2018 每年仅 ~3900 行（16 只/天样本），2019 起才全市场（5200 只/天）
      → 回测被迫从 2019 开始。本次用主服务器按日批量补全 2010-01-01~2018-12-31，
      回测可扩展到 17 年（2010-2026）。

★复权衔接方案（关键，F-6 qfq 漂移实锤后的工程解法）：
- Tushare 与 baostock 前复权基准不同（600519 2010 价差 3.5 倍）→ 直接拼接产生虚假收益断层
- 数学推导（锚点缩放，adj_latest 自动抵消，无需拉最新因子）：
    Tushare 前复权价:  P_ts(day)  = raw(day) × adj(day) / adj_latest
    锚点缩放:          P_final(day) = P_ts(day) × scale
    scale = P_bs(anchor) / P_ts(anchor) = P_bs / (raw_a × adj_a / adj_latest)
    → P_final(day) = raw(day) × adj(day) × [P_bs / (raw_a × adj_a)]   ★adj_latest 抵消
  ⇒ 每只股票只需一个常数 k = P_bs / (raw_a × adj_a)，每日写入价 = raw × adj × k
- 锚点日 = 2019-01-02（baostock 全市场起点）；P_bs 读 bars.db，raw_a/adj_a 拉 Tushare
- 效果：2018-12-31 与 2019-01-02 无缝衔接（仅剩正常隔日涨跌），收益序列正确
- 退市股（2019 后无 baostock 数据）无锚点 → scale=1.0 直接写 Tushare 基准，
  回测用其收益率不受影响（价格水平仅影响买入成本假设，占位可接受）

规模：~2187 交易日 × 2 请求（daily + adj_factor）≈ 4400 请求，2 worker 并发约 4-6 小时
注意：is_st 历史标记暂无数据源（stock_st 仅近期）→ 2010-2018 先置 0，审计 F-1 范围外
用法：python data/backfill_hist_bars.py [--workers 2] [--limit N] [--dry-run] [--status]
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
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import concurrent.futures

BARS_DB = r"data\cache\bars.db"
LOG_FILE = BASE / "logs" / "backfill_hist.log"
START = "20100101"
END = "20181231"
ANCHOR_DATE = "20190102"   # baostock 全市场起点（bars.db 该日有全市场 qfq 数据）


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/backfill_hist_bars.py:57 log")


def _conn():
    return sqlite3.connect(BARS_DB)


def load_trade_days(pro):
    """2010-2018 全部交易日（SSE 日历）"""
    cal = pro.trade_cal(exchange="SSE", start_date=START, end_date=END)
    days = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())
    return days


def _load_data_cfg():
    import yaml
    return yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))["data"]


def _backup_trade_days():
    """备用服务器：从主交易日历源拉 2010-2018 交易日（备用无 trade_cal 权限时用主源缓存）"""
    import requests
    cfg = _load_data_cfg()
    base = cfg["tushare_backup"]["url"].rstrip("/")
    key = cfg["tushare_backup"]["api_key"]
    try:
        r = requests.get(f"{base}/trade-cal", headers={"X-API-Key": key},
                         params={"exchange": "SSE", "start_date": START, "end_date": END}, timeout=30)
        d = r.json()
        if d.get("code") == 0:
            items = d["data"].get("items", [])
            days = [it[1] for it in items if len(it) >= 2 and it[2] == 1]  # [exchange, cal_date, is_open]
            if days:
                return sorted(days)
    except Exception as e:
        log(f"[备用] trade-cal 不可用，改用内置交易日历: {str(e)[:60]}")
    # 回退：主源 trade_cal（已缓存过）
    from data.fetcher_tushare import _pro
    return load_trade_days(_pro())


def load_anchor_k(pro, use_backup=False):
    """每只股票常数 k = P_bs(anchor) / (raw_a × adj_a)，anchor=2019-01-02
    返回 {code: k}；无锚点（退市/新股）k=1.0
    """
    # 1) bars.db 的 baostock 锚点价 P_bs（date 为 YYYY-MM-DD 格式）
    anchor_db = f"{ANCHOR_DATE[:4]}-{ANCHOR_DATE[4:6]}-{ANCHOR_DATE[6:]}"
    con = _conn()
    bs_rows = con.execute(
        "SELECT code, close FROM daily_bar WHERE date=? AND adjust='qfq' AND source='baostock'",
        (anchor_db,)).fetchall()
    con.close()
    bs_anchor = {c: v for c, v in bs_rows if v and v > 0}
    if not bs_anchor:
        log(f"[锚点] bars.db 无 {ANCHOR_DATE} baostock 数据 → 全部 k=1.0（Tushare 基准直写）")
        return {}

    # 2) 锚点日批量 daily + adj_factor（主或备用源）
    if use_backup:
        import requests
        cfg = _load_data_cfg()
        base = cfg["tushare_backup"]["url"].rstrip("/")
        key = cfg["tushare_backup"]["api_key"]
        try:
            r1 = requests.get(f"{base}/daily", headers={"X-API-Key": key},
                              params={"trade_date": ANCHOR_DATE}, timeout=30)
            r2 = requests.get(f"{base}/adj-factor", headers={"X-API-Key": key},
                              params={"trade_date": ANCHOR_DATE}, timeout=30)
            d1, d2 = r1.json(), r2.json()
            if d1.get("code") != 0 or d2.get("code") != 0:
                raise RuntimeError(f"backup code {d1.get('code')}/{d2.get('code')}")
            items1 = d1["data"].get("items", [])
            items2 = d2["data"].get("items", [])
        except Exception as e:
            log(f"[锚点] 备用锚点日批量失败 → 全部 k=1.0: {str(e)[:80]}")
            return {}
        a_map = {it[0]: it[2] for it in items2 if len(it) >= 3}
        k_map = {}
        for it in items1:
            code, raw_a = it[0], it[5]   # close 在第 6 列
            adj_a = a_map.get(code)
            p_bs = bs_anchor.get(code)
            if raw_a and float(raw_a) > 0 and adj_a and float(adj_a) > 0 and p_bs and p_bs > 0:
                k_map[code] = p_bs / (float(raw_a) * float(adj_a))
    else:
        d = pro.daily(trade_date=ANCHOR_DATE)
        a = pro.adj_factor(trade_date=ANCHOR_DATE)
        if d is None or d.empty or a is None or a.empty:
            log("[锚点] Tushare 锚点日批量失败 → 全部 k=1.0")
            return {}
        a_map = dict(zip(a["ts_code"], a["adj_factor"]))
        k_map = {}
        for _, r in d.iterrows():
            code = r["ts_code"]
            raw_a = r["close"]
            adj_a = a_map.get(code)
            p_bs = bs_anchor.get(code)
            if raw_a and raw_a > 0 and adj_a and adj_a > 0 and p_bs and p_bs > 0:
                k_map[code] = p_bs / (raw_a * adj_a)
    log(f"[锚点] k 系数计算完成: {len(k_map)} 只（baostock 锚点 {len(bs_anchor)}，"
        f"k 中位数={sorted(k_map.values())[len(k_map)//2]:.6f}）")
    return k_map


def _backup_fetch_day(trade_date, k_map, raw_only=False):
    """备用服务器（备用HTTP服务器 HTTP API）拉单日全市场 daily
    raw_only=True → 存不复权（adjust='none'，复权因子等主服务器恢复后补）
    返回格式与主服务器一致（实测 daily 0.7s 全市场；adj-factor 限频 1 次/分钟 → raw_only 模式跳过）
    """
    import requests
    cfg = _load_data_cfg()
    base = cfg["tushare_backup"]["url"].rstrip("/")
    key = cfg["tushare_backup"]["api_key"]
    try:
        r1 = requests.get(f"{base}/daily", headers={"X-API-Key": key},
                          params={"trade_date": trade_date}, timeout=30)
        d1 = r1.json()
        if d1.get("code") != 0:
            return trade_date, None, f"backup daily code {d1.get('code')}"
        items1 = d1["data"].get("items", [])
    except Exception as e:
        return trade_date, None, str(e)[:120]
    if not items1:
        return trade_date, [], None
    # daily 列序（实测）：ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
    rows = []
    for it in items1:
        code, dt, o, h, l, c, pc, chg, pct, vol, amt = it[:11]
        date_s = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        # pre_close 可能为 None（新股首日）→ 用当日 open 兜底
        pc_v = float(pc) if pc is not None else (float(o) if o is not None else float(c))
        if raw_only:
            rows.append((
                code.upper(), date_s,
                float(o), float(h), float(l), float(c), pc_v,
                float(vol) * 100 if vol is not None else None,
                float(amt) * 1000 if amt is not None else None,
                None, float(pct) if pct is not None else None, 0,
                "none", "tushare_backup",
            ))
        else:
            rows.append((
                code.upper(), date_s,
                float(o), float(h), float(l), float(c), pc_v,
                float(vol) * 100 if vol is not None else None,
                float(amt) * 1000 if amt is not None else None,
                None, float(pct) if pct is not None else None, 0,
                "qfq", "tushare_backup",
            ))
    return trade_date, rows, None


def fetch_day(pro, trade_date, k_map, use_backup=False):
    """拉单日全市场 daily + adj_factor → 写入价 = raw × adj × k → 标准行列表
    use_backup=True 时走备用服务器（主服务器不可达时容错）
    """
    if use_backup:
        return _backup_fetch_day(trade_date, k_map)
    try:
        d = pro.daily(trade_date=trade_date)
        a = pro.adj_factor(trade_date=trade_date)
    except Exception as e:
        return trade_date, None, str(e)[:120]
    if d is None or d.empty:
        return trade_date, [], None
    a_map = {r["ts_code"]: r["adj_factor"] for _, r in a.iterrows()} if a is not None and not a.empty else {}
    rows = []
    for _, r in d.iterrows():
        code = r["ts_code"]
        f = a_map.get(code)
        if f is None or f <= 0:
            continue
        k = k_map.get(code, 1.0)
        price_k = f * k   # 写入价 = raw × f × k
        date_s = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        rows.append((
            code.upper(), date_s,
            r["open"] * price_k, r["high"] * price_k, r["low"] * price_k, r["close"] * price_k,
            r["pre_close"] * price_k,
            r["vol"] * 100, r["amount"] * 1000, None, r["pct_chg"], 0,
            "qfq", "tushare",
        ))
    return trade_date, rows, None


def run(workers=2, limit=None, dry_run=False, use_backup=False):
    # 已完成检查（source 为 tushare / tushare_backup 且 2019 前；date 为 YYYY-MM-DD → 转 YYYYMMDD）
    # ★2026-11-10：加 adjust 条件——raw 模式写入 none 后，qfq 缺口不能被误判为"已完成"
    con = _conn()
    done_days = {r[0].replace("-", "") for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE source IN ('tushare','tushare_backup') "
        "AND adjust='qfq' AND date<'2019-01-01'").fetchall()}
    con.close()

    if use_backup:
        # 备用模式：直接用 HTTP API，不初始化 pro
        days = _backup_trade_days()
        k_map = load_anchor_k(None, use_backup=True)
    else:
        from data.fetcher_tushare import _pro
        pro = _pro()
        days = load_trade_days(pro)
        k_map = load_anchor_k(pro)

    todo = [d for d in days if d not in done_days]
    if limit:
        todo = todo[:limit]
    log(f"历史回填{'[备用服务器]' if use_backup else '[主服务器]'}: 交易日 {len(days)}, "
        f"已完成 {len(done_days)}, 待拉 {len(todo)}" + (" [DRY-RUN]" if dry_run else ""))

    k_map = load_anchor_k(pro if not use_backup else None, use_backup=use_backup)
    if dry_run:
        d0 = todo[0] if todo else None
        if d0:
            td, rows, err = (_backup_fetch_day(d0, k_map) if use_backup else fetch_day(pro, d0, k_map))
            print(f"  [dry-run] {d0}: {len(rows) if rows else 0} 行")
            if rows:
                print(f"  样本: {rows[0]}")
                con = _conn()
                bs = con.execute(
                    "SELECT close FROM daily_bar WHERE code=? AND date=? AND adjust='qfq' AND source='baostock'",
                    (rows[0][0], "2019-01-02")).fetchone()
                con.close()
                print(f"  衔接检查 {rows[0][0]}: 2018-12-31 tushare 写入 {rows[-1][2]:.2f}  vs  2019-01-02 baostock {bs[0] if bs else 'N/A'}")
        return

    t0 = time.time()
    n_ok = 0
    if use_backup:
        # 备用模式：串行拉 daily raw（adjust='none'，快 0.7s/天）；
        # 复权因子受 adj_factor 限频（1 次/分钟）→ 主服务器恢复后用 convert_backup_raw.py 补
        for i, d in enumerate(todo, 1):
            td, rows, err = _backup_fetch_day(d, k_map, raw_only=True)
            if rows:
                con = _conn()
                con.executemany(
                    "INSERT OR REPLACE INTO daily_bar "
                    "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                con.commit()
                con.close()
                n_ok += 1
            elif err:
                log(f"  [失败] {d}: {err}")
            if i % 50 == 0:
                el = time.time() - t0
                log(f"  进度 {i}/{len(todo)} ({el:.0f}s, 成功 {n_ok}, 均 {el/i:.1f}s/天)")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_day, pro, d, k_map): d for d in todo}
            for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                day, rows, err = fut.result()
                if rows:
                    con = _conn()
                    con.executemany(
                        "INSERT OR REPLACE INTO daily_bar "
                        "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    con.commit()
                    con.close()
                    n_ok += 1
                elif err:
                    log(f"  [失败] {day}: {err}")
                if i % 50 == 0:
                    el = time.time() - t0
                    log(f"  进度 {i}/{len(todo)} ({el:.0f}s, 成功 {n_ok}, 均 {el/i:.1f}s/天)")
    el = time.time() - t0
    log(f"完成: 成功 {n_ok}/{len(todo)} 天, 耗时 {el:.0f}s ({el/60:.1f} 分钟)")


def status():
    con = _conn()
    for y in range(2010, 2019):
        n = con.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE date LIKE ? AND source='tushare'", (f"{y}%",)).fetchone()[0]
        codes = con.execute(
            "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date LIKE ? AND source='tushare'", (f"{y}%",)).fetchone()[0]
        print(f"  {y}: {n:,} 行 / {codes} 只")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backup", action="store_true", help="使用备用服务器（主服务器不可达时）")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(workers=args.workers, limit=args.limit, dry_run=args.dry_run, use_backup=args.backup)
