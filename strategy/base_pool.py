# -*- coding: utf-8 -*-
"""
strategy/base_pool.py — ★合格底座池（2026-08-07，用户定调：量化策略作底座池，池内个股有统计学意义）

底座池 = 全部实证机制的入口：
  - 配置层 v3 的 universe（等权+择时）
  - 优选层 Pitch 的候选来源（只从池内优中选优）
  - 池内股票 = 通过统计学门槛的"合格标的"（质量/盈利/流动性），而非全市场 5500 只

门槛（一票否决制）：
  H1 非 ST（最新交易日 is_st=0）
  H2 非退市（不在 delisted_list.csv）
  H3 上市满 2 年（stock_basic ipoDate，次新股基本面不可信）
  H4 市值 ≥ 30 亿（快照 circ_mv_map；PIT 版在 hist_mv 就绪后切换）
  H5 流动性：近 20 日均成交额 ≥ 0.3 亿（可真实成交）
  Q1 质量：最新 ROE ≥ 8%（持续盈利能力）
  Q2 成长：单季净利同比 > 0
  Q3 持续盈利：近 4 季单季净利之和 > 0

用法：python strategy/base_pool.py
输出：output/base_pool.json {date, n, stats, codes[], detail}
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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

CACHE = Path(r"data/cache")
OUT = BASE / "output" / "base_pool.json"

MIN_MV_YI = 30.0          # 市值下限 30 亿
MIN_TURNOVER_YI = 0.3     # 20 日均成交额下限 0.3 亿
MIN_ROE = 0.08            # ROE 下限 8%
MIN_IPO_YEARS = 2         # 上市满 2 年


def build() -> dict:
    bars = sqlite3.connect(str(CACHE / "bars.db"))
    bcur = bars.cursor()
    last = bcur.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
    # H1 ST
    st_codes = {r[0] for r in bcur.execute(
        "SELECT code FROM daily_bar WHERE date=? AND is_st=1", (last,)).fetchall()}
    # H5 流动性（近 20 日均成交额，amount=元）
    liq = {r[0]: r[1] for r in bcur.execute(
        """SELECT code, AVG(amount) FROM daily_bar
           WHERE date > (SELECT MAX(date) FROM daily_bar WHERE adjust='qfq')
           GROUP BY code""").fetchall()} if False else {}
    # H5 流动性：近 20 个交易日日均成交额（★2026-08-15 单位归一：tushare 千元×1000→元，
    #   baostock/akshare 元不变；排除指数 SH.000300 行——混源 SUM 会污染口径）
    liq = dict(bcur.execute(
        """SELECT code, AVG(CASE WHEN source IN ('tushare','tushare_backup') THEN amount*1000 ELSE amount END)
           FROM daily_bar WHERE adjust='qfq' AND code NOT LIKE 'SH.%' AND code NOT LIKE 'sh.%'
           AND date >= (SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 1 OFFSET 19)
           GROUP BY code""").fetchall())
    bars.close()

    # H2 退市
    delisted = set()
    df = CACHE / "delisted_list.csv"
    if df.exists():
        import csv
        with open(df, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                c = str(row.get("code", "")).strip().upper()
                if c:
                    delisted.add(c if "." in c else c + (".SH" if c[:2] in ("60", "68") else ".SZ"))
    # H3 上市日期
    ipo = {}
    try:
        con = sqlite3.connect(str(CACHE / "stock_basic.db"))
        ipo = {str(r[0]).upper(): r[1] for r in con.execute(
            "SELECT code, ipo_date FROM stock_basic WHERE ipo_date IS NOT NULL").fetchall()}
        con.close()
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "strategy/base_pool.py:79 build")
    # H4 市值
    mv = {}
    mf = CACHE / "circ_mv_map_full.csv"
    if mf.exists():
        import pandas as pd
        try:
            m = pd.read_csv(mf, encoding="utf-8-sig")
            mv = {str(r.ts_code).upper(): float(r.circ_mv) / 10000 for r in m.itertuples()}
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/base_pool.py:89 build")

    # 基本面（最新完整报告期 + 前 3 期单季净利）
    fin = sqlite3.connect(str(CACHE / "finance.db"))
    fcur = fin.cursor()
    # ★报告期覆盖回退：最新期若覆盖不足（如中报未全量入库）→ 回退到覆盖≥500 的最近期
    periods = fcur.execute(
        "SELECT period, COUNT(DISTINCT code) FROM finance_report GROUP BY period ORDER BY period DESC").fetchall()
    latest = periods[0][0]
    for p, n in periods:
        if n >= 500:
            latest = p
            break
    fin_rows = fcur.execute(
        """SELECT code, period, net_profit, sq_net_profit, sq_net_yoy, roe
           FROM finance_report WHERE period IN (
               SELECT DISTINCT period FROM finance_report ORDER BY period DESC LIMIT 4)""").fetchall()
    # 每只取最新 1 期 + 前 3 期（按 period 排序）
    by_code = {}
    for code, period, np_, sq_np, yoy, roe in fin_rows:
        by_code.setdefault(code, []).append((period, np_, sq_np, yoy, roe))
    fin.close()

    passed = []
    detail = {}
    for code, recs in by_code.items():
        recs.sort(reverse=True)  # 最新在前
        p0, np0, sq0, yoy0, roe0 = recs[0]
        # ★ROE 年化修正：finance.db roe 为当期单季值（Q1 的 8% ≈ 年化 32%）
        # 按报告期类型年化：Q1×4 / 中报×2 / Q3×4/3 / 年报×1
        mult = {"03": 4.0, "06": 2.0, "09": 4 / 3, "12": 1.0}.get(p0[5:7], 1.0)
        roe_ann = float(roe0) * mult if roe0 is not None else None
        if roe_ann is None or roe_ann < MIN_ROE:       # Q1
            continue
        if yoy0 is None or float(yoy0) <= 0:           # Q2
            continue
        sq_ok = [float(r[2]) for r in recs[:4] if r[2] is not None]  # Q3 近4季（可得期）
        if len(sq_ok) < 3 or sum(sq_ok) <= 0:
            continue
        c = code if "." in code else code + (".SH" if code[:2] in ("60", "68") else ".SZ")
        if c in st_codes:                               # H1
            continue
        if c in delisted:                               # H2
            continue
        ipod = ipo.get(c, "")
        if ipod and ipod >= f"{int(last[:4]) - MIN_IPO_YEARS}-01-01":   # H3 上市满2年
            continue
        if mv.get(c, 0) < MIN_MV_YI:                    # H4
            continue
        if (liq.get(c) or 0) < MIN_TURNOVER_YI * 1e8:   # H5
            continue
        passed.append(c)
        detail[c] = {"period": p0, "roe_ann": round(roe_ann * 100, 1),
                     "roe_q": round(float(roe0) * 100, 1),
                     "nyoy": round(float(yoy0) * 100, 1), "mv_yi": round(mv.get(c, 0), 1)}

    # 统计
    roes = [d["roe_ann"] for d in detail.values()]
    mvs = [d["mv_yi"] for d in detail.values()]
    stats = {
        "n": len(passed),
        "n_market": len(by_code),
        "roe_median": round(sorted(roes)[len(roes) // 2], 1) if roes else None,
        "mv_median_yi": round(sorted(mvs)[len(mvs) // 2], 1) if mvs else None,
        "hurdles": {
            "st": len(st_codes), "delisted": len(delisted),
            "min_mv_yi": MIN_MV_YI, "min_turnover_yi": MIN_TURNOVER_YI,
            "min_roe": MIN_ROE, "min_ipo_years": MIN_IPO_YEARS,
            "latest_period": latest, "latest_bar": last,
        },
    }
    return {"date": last, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stats": stats, "codes": passed, "detail": detail}


def main():
    r = build()
    OUT.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    s = r["stats"]
    print(f"底座池已生成：{OUT}")
    print(f"  池规模 {s['n']} 只（全市场 {s['n_market']} 只财报覆盖）")
    print(f"  ROE 中位数 {s['roe_median']}% · 市值中位数 {s['mv_median_yi']} 亿 · 数据截至 {r['date']}")
    print(f"  门槛：非ST({s['hurdles']['st']}只剔除) · 非退市 · 上市满2年 · 市值≥{s['hurdles']['min_mv_yi']:.0f}亿 "
          f"· 流动性≥{s['hurdles']['min_turnover_yi']:.1f}亿 · ROE≥{s['hurdles']['min_roe']*100:.0f}% "
          f"· 单季正增长 · 近4季盈利")
    return 0


if __name__ == "__main__":
    sys.exit(main())
