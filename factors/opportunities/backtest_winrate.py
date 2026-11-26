# -*- coding: utf-8 -*-
"""factors/opportunities/backtest_winrate.py — 每类机会滚动回测（胜率精化 · 外包 AI 2026-08-08）

★目的：替换 score.py / scan.py 中的硬编码 winrate_approx，让"概率分"有真实历史依据。

方法（PIT 严格）：
  1. 月度滚动：2019-01 ~ 2026-06 每月最后交易日 = 回测日 t
  2. 因子面板：技术因子用截至 t 的 320 日窗口（与 scan.py 一致）；财务因子按"披露窗口对齐"——
     报告期 3/31→4-30、6/30→8-31、9/30→10-31、12/31→次年 4-30 之后才视为可得（无未来函数）
  3. 触发：import scan.triggers()（★复用机会引擎同一触发逻辑）
  4. 收益：T+1 开盘买入（PIT 纪律）→ 持有 1/3/6 个自然月（到期最近交易日收盘卖出）
  5. 指标：胜率 / 平均收益 / 中位收益 / 最大回撤（持有期路径）/ 盈亏比 / 样本数

输出：logs/opportunity_winrates.json
  {otype: {horizon: {winrate, avg_ret, med_ret, max_dd, pl_ratio, n}}}
  meta: {period, horizons, n_months, note(数据限制说明), hardcoded_prev(原硬编码对比)}

用法：
  python factors/opportunities/backtest_winrate.py [--months-start 2019-01]
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.opportunities.scan import triggers

BARS_DB = r"data\cache\bars.db"
FIN_DB = r"data\cache\finance.db"
QD_DB = r"data\cache\finance_quality.db"
BASIC_DB = r"data\cache\stock_basic.db"
OUT = BASE / "logs" / "opportunity_winrates.json"

HORIZONS = {1: 1, 3: 3, 6: 6}          # 持有月数
WIN_HARDCODED = {                       # 原硬编码（对比基准）
    "reversal": 0.62, "value": 0.65, "breakout": 0.58,
    "revalue": 0.60, "event": 0.55, "quality_gap": 0.63,
}
ORDER = ["reversal", "value", "breakout", "revalue", "event", "quality_gap"]


# ==================== PIT 数据层 ====================

def _disclose_date(period: str) -> date:
    """财报披露窗口近似（PIT 对齐）：按报告期所在季度映射披露截止
    3-31→4-30，6-30→8-31，9-30→10-31，12-31→次年 4-30；
    非季度末 period（个别异常）归入其所属季度区间"""
    d = datetime.strptime(period, "%Y-%m-%d").date()
    if d.month <= 3:
        return date(d.year, 4, 30)
    if d.month <= 6:
        return date(d.year, 8, 31)
    if d.month <= 9:
        return date(d.year, 10, 31)
    return date(d.year + 1, 4, 30)


def _load_all() -> tuple:
    """一次加载：日线（2018-06 起，窗口预留）、财报全量、质量全量、基础信息"""
    con = sqlite3.connect(BARS_DB)
    bars = pd.read_sql(
        "SELECT code, date, open, close, volume, turn, is_st FROM daily_bar "
        "WHERE adjust='qfq' AND date>='2009-06-01' "
        "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'", con)
    con.close()
    bars["date"] = pd.to_datetime(bars["date"])
    bars["code"] = bars["code"].astype("category")

    con = sqlite3.connect(FIN_DB)
    fin = pd.read_sql(
        "SELECT code, period, roe, sq_net_yoy, sq_rev_yoy FROM finance_report", con)
    con.close()
    fin["disclose"] = fin["period"].map(_disclose_date)
    fin["period"] = pd.to_datetime(fin["period"])
    fin["disclose"] = pd.to_datetime(fin["disclose"])

    con = sqlite3.connect(QD_DB)
    q = pd.read_sql(
        "SELECT code, period, pub_date, roe_avg, gp_margin, liability_to_asset, cfo_to_np "
        "FROM quality", con)
    con.close()
    pub = q["pub_date"].fillna(q["period"])
    q["pub"] = pd.to_datetime(pub, errors="coerce").fillna(pd.to_datetime(q["period"]))
    q["period"] = pd.to_datetime(q["period"])

    con = sqlite3.connect(BASIC_DB)
    basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", con)
    con.close()
    basic = basic.set_index("code")

    return bars, fin, q, basic


def _load_st_on(bars: pd.DataFrame, t: pd.Timestamp) -> set:
    """t 当日 ST 名单（PIT：用当日 is_st 标记）"""
    mask = (bars["date"] == t) & (bars["is_st"] == 1)
    return set(bars.loc[mask, "code"])


def _pit_finance(fin: pd.DataFrame, t: pd.Timestamp) -> dict:
    """截至 t 的最新可得财报（按披露日对齐）→ {code6: {roe, nyoy, ryoy}}"""
    avail = fin[fin["disclose"] <= t]
    if avail.empty:
        return {}
    idx = avail.groupby("code")["period"].idxmax()
    sub = avail.loc[idx]
    return {str(c): {"roe": roe, "nyoy": nyoy, "ryoy": ryoy}
            for c, roe, nyoy, ryoy in zip(sub["code"], sub["roe"],
                                          sub["sq_net_yoy"], sub["sq_rev_yoy"])}


def _pit_quality(q: pd.DataFrame, t: pd.Timestamp) -> dict:
    """截至 t 的最新可用质量期 → {code(带后缀): {roe, gp, liab, cfo}}"""
    avail = q[q["pub"] <= t]
    if avail.empty:
        return {}
    idx = avail.groupby("code")["period"].idxmax()
    sub = avail.loc[idx]
    return {c: {"roe": roe, "gp": gp, "liab": liab, "cfo": cfo}
            for c, roe, gp, liab, cfo in zip(sub["code"], sub["roe_avg"],
                                             sub["gp_margin"], sub["liability_to_asset"],
                                             sub["cfo_to_np"])}


# ==================== PIT 因子面板（与 scan.compute_factors 同逻辑） ====================

def precompute_tech(close_full: pd.DataFrame, volume_full: pd.DataFrame) -> dict:
    """★性能优化（2026-08-09 外包 AI-1）：全市场滚动因子全量预计算（一次），每期取行
    与 compute_factors_pit 的逐期滚动在数学上等价（rolling 值只依赖截至 t 的数据），
    把 92 期 × 2.9s 的重复滚动计算压缩为一次 ~30s。
    返回 {factor_name: DataFrame(index=date, columns=code)}，每期取 .loc[t] 即为该期因子行。
    """
    close = close_full.astype(float)
    ret = close.pct_change()
    up = ret.clip(lower=0).rolling(14).mean()
    dn = (-ret.clip(upper=0)).rolling(14).mean()
    v20 = volume_full.rolling(20, min_periods=10).mean()
    v60 = volume_full.rolling(60, min_periods=30).mean()
    return {
        "close": close,
        "mom120": close / close.shift(120) - 1,
        "mom20": close / close.shift(20) - 1,
        "vol60": ret.rolling(60, min_periods=40).std() * np.sqrt(252),
        "high252": close.rolling(250, min_periods=150).max(),
        "low252": close.rolling(250, min_periods=150).min(),
        "drawdown_60d": close / close.rolling(60, min_periods=40).max() - 1,
        "rsi14": 100 - 100 / (1 + up / dn.replace(0, np.nan)),
        "vol_ratio": v20 / v60.replace(0, np.nan),
        "vol_contract": ret.rolling(20, min_periods=10).std() / ret.rolling(60, min_periods=30).std(),
        "ma50": close.rolling(50, min_periods=40).mean(),
        "ma200": close.rolling(200, min_periods=120).mean(),
    }


def panel_from_pre(t: pd.Timestamp, pre: dict, fin_pit: dict, q_pit: dict,
                   st_codes: set, basic: pd.DataFrame) -> pd.DataFrame:
    """★预计算模式取面板：pre 全序列在 t 行取值 + 截面因子（财务/质量/估值/ST），
    与 compute_factors_pit 等价（等价性已验证 2026-08-09，触发数量一致）"""
    f = pd.DataFrame(index=pre["close"].columns)
    row = {k: v.loc[t] for k, v in pre.items()}
    f["close"] = row["close"]
    f["mom120"] = row["mom120"]
    f["mom20"] = row["mom20"]
    f["vol60"] = row["vol60"]
    f["high252"] = row["high252"]
    f["low252"] = row["low252"]
    f["near_high_250"] = f["close"] / f["high252"] - 1
    f["drawdown_60d"] = row["drawdown_60d"]
    f["rsi14"] = row["rsi14"]
    f["vol_ratio"] = row["vol_ratio"]
    f["vol_contract"] = row["vol_contract"]
    f["ma50_up"] = (row["ma50"] > pre["ma50"].shift(5).loc[t]).astype(int)
    f["ma200_up"] = (row["ma200"] > pre["ma200"].shift(5).loc[t]).astype(int)

    f["code6"] = f.index.str[:6]
    fr = pd.DataFrame.from_dict(fin_pit, orient="index")
    fr.index = fr.index.astype(str)
    if not fr.empty:
        f["roe"] = f["code6"].map(fr["roe"]).astype(float)
        f["sq_nyoy"] = f["code6"].map(fr["nyoy"]).astype(float)
        f["sq_rev_yoy"] = f["code6"].map(fr["ryoy"]).astype(float)
    else:
        f["roe"], f["sq_nyoy"], f["sq_rev_yoy"] = np.nan, np.nan, np.nan

    qr = pd.DataFrame.from_dict(q_pit, orient="index")
    if not qr.empty:
        f["liability"] = f.index.map(lambda c: qr["liab"].get(c))
        f["cfo_to_np"] = f.index.map(lambda c: qr["cfo"].get(c))
        f["cfo_health"] = (f["cfo_to_np"] > 0).astype(int)
    else:
        f["liability"], f["cfo_to_np"], f["cfo_health"] = np.nan, np.nan, 0

    f["roe"] = pd.to_numeric(f["roe"], errors="coerce")
    f["sq_nyoy"] = pd.to_numeric(f["sq_nyoy"], errors="coerce")
    f["non_st"] = (~f.index.isin(st_codes)).astype(int)
    f["pb"] = np.nan
    f["pe_ttm"] = 1.0
    f["pb_pct"] = f["close"].rank(pct=True)
    f["pe_pct"] = f["close"].rank(pct=True)
    f["div_yield"] = 0.0
    f = f.join(basic[["name", "industry"]], how="left")
    f["industry"] = f["industry"].fillna("未知")
    return f


def compute_factors_pit(px: pd.DataFrame, vx: pd.DataFrame,
                        fin_pit: dict, q_pit: dict, st_codes: set,
                        basic: pd.DataFrame) -> pd.DataFrame:
    """PIT 版因子面板（输入为截至回测日的窗口数据；触发逻辑与 scan.py 完全一致）"""
    close = px.astype(float)
    ret = close.pct_change()
    f = pd.DataFrame(index=close.columns)

    f["close"] = close.iloc[-1]
    f["mom120"] = (close.iloc[-1] / close.shift(120).iloc[-1] - 1)
    f["mom20"] = (close.iloc[-1] / close.shift(20).iloc[-1] - 1)
    f["vol60"] = ret.rolling(60, min_periods=40).std().iloc[-1] * np.sqrt(252)
    f["high252"] = close.rolling(250, min_periods=150).max().iloc[-1]
    f["low252"] = close.rolling(250, min_periods=150).min().iloc[-1]
    f["near_high_250"] = f["close"] / f["high252"] - 1
    f["drawdown_60d"] = f["close"] / close.rolling(60, min_periods=40).max().iloc[-1] - 1
    up = ret.clip(lower=0).rolling(14).mean()
    dn = (-ret.clip(upper=0)).rolling(14).mean()
    f["rsi14"] = 100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1].replace(0, np.nan))
    v20 = vx.rolling(20, min_periods=10).mean().iloc[-1]
    v60 = vx.rolling(60, min_periods=30).mean().iloc[-1]
    f["vol_ratio"] = v20 / v60.replace(0, np.nan)
    f["vol_contract"] = (ret.rolling(20, min_periods=10).std().iloc[-1] /
                         ret.rolling(60, min_periods=30).std().iloc[-1])
    f["ma50_up"] = (close.rolling(50, min_periods=40).mean().iloc[-1] >
                    close.rolling(50, min_periods=40).mean().iloc[-6]).astype(int)
    f["ma200_up"] = (close.rolling(200, min_periods=120).mean().iloc[-1] >
                     close.rolling(200, min_periods=120).mean().iloc[-6]).astype(int)

    f["code6"] = f.index.str[:6]
    fr = pd.DataFrame.from_dict(fin_pit, orient="index")
    fr.index = fr.index.astype(str)
    if not fr.empty:
        f["roe"] = f["code6"].map(fr["roe"]).astype(float)
        f["sq_nyoy"] = f["code6"].map(fr["nyoy"]).astype(float)
        f["sq_rev_yoy"] = f["code6"].map(fr["ryoy"]).astype(float)
    else:
        f["roe"], f["sq_nyoy"], f["sq_rev_yoy"] = np.nan, np.nan, np.nan

    qr = pd.DataFrame.from_dict(q_pit, orient="index")
    if not qr.empty:
        f["liability"] = f.index.map(lambda c: qr["liab"].get(c))
        f["cfo_to_np"] = f.index.map(lambda c: qr["cfo"].get(c))
        f["cfo_health"] = (f["cfo_to_np"] > 0).astype(int)
    else:
        f["liability"], f["cfo_to_np"], f["cfo_health"] = np.nan, np.nan, 0

    f["roe"] = pd.to_numeric(f["roe"], errors="coerce")
    f["sq_nyoy"] = pd.to_numeric(f["sq_nyoy"], errors="coerce")
    f["non_st"] = (~f.index.isin(st_codes)).astype(int)
    # ★2026-11-10 修复：scan.triggers() 依赖 non_lowprice，本函数此前未生成该列 →
    #   任何股票通过前置条件即抛 KeyError('non_lowprice')，胜率回测从未成功。
    f["non_lowprice"] = (f["close"] >= 1.5).astype(int) if "close" in f.columns else 1
    # ★估值列（PIT 回测近似，2026-08-08 主程序 v1.1 引入真实估值后对齐）：
    #   历史 daily_basic 估值无本地缓存 → 用价格分位近似（与 scan.py 无估值数据时的
    #   降级语义一致）：pb 置 NaN（触发兜底不阻塞）、pe_ttm 置 1.0（正数满足 >0 检查）
    f["pb"] = np.nan
    f["pe_ttm"] = 1.0
    f["pb_pct"] = f["close"].rank(pct=True)
    f["pe_pct"] = f["close"].rank(pct=True)
    f["div_yield"] = 0.0
    f = f.join(basic[["name", "industry"]], how="left")
    f["industry"] = f["industry"].fillna("未知")
    return f


# ==================== 持有期收益 ====================

def _add_months(d: pd.Timestamp, n: int) -> pd.Timestamp:
    """加 n 个自然月（月末 clamp：1-31 + 1 月 → 2-28/29）"""
    y, m = d.year, d.month + n
    while m > 12:
        y += 1
        m -= 12
    last_day = pd.Period(f"{y}-{m:02d}", freq="M").days_in_month
    return pd.Timestamp(y, m, min(d.day, last_day))


def _holding_stats(px: pd.Series, buy_i: int, buy_price: float,
                   dates: pd.DatetimeIndex, months: int):
    """持有 months 自然月：到期最近交易日收盘卖出；返回 (ret, max_dd) 或 None
    max_dd 口径：持有期（买入次日起）收益相对前高点的最大回撤"""
    if buy_price is None or buy_price <= 0 or buy_i >= len(px) - 1:
        return None
    target = _add_months(dates[buy_i], months)
    sell_mask = dates[buy_i + 1:] > target
    if sell_mask.any():
        sell_i = buy_i + 1 + int(np.argmax(np.asarray(sell_mask)))
        if sell_i >= len(px):
            return None
        path = px.iloc[buy_i:sell_i + 1] / buy_price - 1
    else:
        path = px.iloc[buy_i:] / buy_price - 1
    # 脏数据防御：路径含 inf/NaN → 清理后长度不足即剔除
    path = path.replace([np.inf, -np.inf], np.nan).dropna()
    if len(path) < 2:
        return None
    ret = float(path.iloc[-1])
    if not (-2.0 < ret < 2.0):          # 极端收益（涨跌停复权异常/脏行）剔除
        return None
    # ★标准价格回撤口径（2026-08-09 统一）：price=1+rel，回撤=price/前高-1
    # （+50%→-50% 为 -66.7% 而非 -100pp；与 pitch_v2.py 同口径）
    rel = path.iloc[1:]
    if len(rel) == 0:
        dd = 0.0
    else:
        price = 1.0 + rel
        dd = float((price / price.cummax() - 1).min())
        if dd != dd or dd < -1.0:
            dd = -1.0
    return ret, dd


# ==================== 主流程 ====================

def run(start: str = "2019-01", end: str = None, with_pv_consensus: bool = False,
        pv_mode: str = "ge80", out_path: Path = None) -> dict:
    """滚动回测主流程
    start/end: 回测区间（YYYY-MM；end 默认数据末日，如 '2014-12' 跑早期段）
    with_pv_consensus: 是否同时回测第 7 类 pv_consensus（B-6，五强历史重算触发）
    pv_mode: 'ge80' = rank≥0.80 命中（推荐口径）｜ 'le20' = rank≤0.20（scan 当前口径，待主程序裁决）
    out_path: 输出路径（默认 logs/opportunity_winrates.json）
    """
    print("加载数据（日线/财报/质量）...", flush=True)
    bars, fin, q, basic = _load_all()
    dates = pd.DatetimeIndex(sorted(bars["date"].unique()))
    # 按 code 分组日线（触发后 O(1) 取序列）
    by_code = {c: g.sort_values("date") for c, g in bars.groupby("code")}

    # 月度回测日（每月最后交易日）
    month_ends = []
    cur = pd.Timestamp(start)
    end = end or dates.max()
    end = pd.Timestamp(end)
    while cur <= end:
        mask = (dates >= cur) & (dates < _add_months(cur, 1))
        if mask.any():
            month_ends.append(dates[mask][-1])
        cur = _add_months(cur, 1)
    print(f"回测窗口 {month_ends[0].date()} ~ {month_ends[-1].date()}，共 {len(month_ends)} 期", flush=True)

    trig = triggers()
    # {otype: {months: [rets]}}
    samples = {ot: {h: [] for h in HORIZONS} for ot in ORDER}
    if with_pv_consensus:
        samples["pv_consensus"] = {h: [] for h in HORIZONS}

    # ★2026-08-09 性能优化评估结论：预计算全量 rolling（250 交易日精确）与逐期 330 自然日窗口
    # （≈220 交易日，scan.py 同口径，250 日因子靠 min_periods 兜底）触发结果不一致
    # （near_high_250 最大差 0.38，breakout 触发 50 vs 44）→ 一致性优先，保留逐期窗口法。
    # precompute_tech/panel_from_pre 保留作精确口径对照工具（勿用于正式覆盖）。
    for k, t in enumerate(month_ends):
        win = bars[(bars["date"] > t - pd.Timedelta(days=330)) & (bars["date"] <= t)]
        px = win.pivot(index="date", columns="code", values="close").ffill()
        vx = win.pivot(index="date", columns="code", values="volume").fillna(0)
        st = _load_st_on(bars, t)
        fin_pit = _pit_finance(fin, t)
        q_pit = _pit_quality(q, t)
        f = compute_factors_pit(px, vx, fin_pit, q_pit, st, basic)
        if f.empty:
            continue
        for ot in ORDER:
            mask = f.apply(trig[ot], axis=1)
            hits = list(f[mask].index)
            for code in hits:
                g = by_code.get(code)
                if g is None or len(g) < 40:
                    continue
                gdates = pd.DatetimeIndex(g["date"])
                # T+1 开盘买入：t 后第一个交易日
                ixs = np.where(gdates > t)[0]
                if len(ixs) == 0:
                    continue
                buy_i = int(ixs[0])
                buy_price = float(g["open"].iloc[buy_i])
                closes = g["close"].astype(float).reset_index(drop=True)
                for h in HORIZONS:
                    r = _holding_stats(closes, buy_i, buy_price, gdates, h)
                    if r:
                        samples[ot][h].append(r)
        # ★pv_consensus（第 7 类，B-6）：五强历史重算触发（外包 AI-1 2026-08-09）
        if with_pv_consensus and len(win) >= 2000:
            try:
                from factors.opportunities.backtest_pv_consensus import (
                    pv_consensus_codes,
                )
                pv_codes = pv_consensus_codes(win, mode=pv_mode)
                for code in pv_codes:
                    g = by_code.get(code)
                    if g is None or len(g) < 40:
                        continue
                    gdates = pd.DatetimeIndex(g["date"])
                    ixs = np.where(gdates > t)[0]
                    if len(ixs) == 0:
                        continue
                    buy_i = int(ixs[0])
                    buy_price = float(g["open"].iloc[buy_i])
                    closes = g["close"].astype(float).reset_index(drop=True)
                    for h in HORIZONS:
                        r = _holding_stats(closes, buy_i, buy_price, gdates, h)
                        if r:
                            samples["pv_consensus"][h].append(r)
            except Exception as e:
                print(f"  [pv_consensus 触发失败] {str(e)[:60]}")
        if (k + 1) % 12 == 0:
            print(f"  完成 {k + 1}/{len(month_ends)} 期 ({t.date()})", flush=True)

    # 汇总
    result = {}
    for ot in list(ORDER) + (["pv_consensus"] if with_pv_consensus else []):
        result[ot] = {}
        for h, lst in samples[ot].items():
            if not lst:
                result[ot][str(h)] = {"n": 0}
                continue
            rets = np.array([r[0] for r in lst])
            dds = np.array([r[1] for r in lst])
            wins = rets[rets > 0]
            losses = rets[rets <= 0]
            result[ot][str(h)] = {
                "n": int(len(rets)),
                "winrate": round(float((rets > 0).mean()), 4),
                "avg_ret": round(float(rets.mean()), 4),
                "med_ret": round(float(np.median(rets)), 4),
                "max_dd": round(float(dds.min()), 4),
                "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 4)
                           if len(wins) and len(losses) and losses.mean() != 0 else None,
            }

    meta = {
        "period": f"{month_ends[0].date()} ~ {month_ends[-1].date()}",
        "n_months": len(month_ends),
        "horizons_months": [1, 3, 6],
        "pit_rule": "财报按披露窗口对齐（Q1→4-30/H1→8-31/Q3→10-31/年报→次年4-30）；T+1 开盘买入",
        "hardcoded_prev": WIN_HARDCODED,
        "pv_consensus": {
            "included": with_pv_consensus,
            "mode": pv_mode,
            "note": "五强历史重算触发（backtest_pv_consensus.py），方向口径见 pv_consensus_胜率与方向诊断报告.md（待主程序裁决）",
        },
        "data_limits": [
            "行情 qfq 2019+（2010-2018 为 raw 未转 qfq，未混用）",
            "quality 表（负债率/现金流）历史期自 2024-03 起 → 2024 前 value/quality_gap 触发样本偏少",
            "估值列（pb/pe_ttm）回测用近似占位（历史 daily_basic 无本地缓存）：pb=NaN、pe_ttm=1.0、分位=价格分位，value 类触发语义≈价格分位低估+基本面，与 scan.py 无估值数据时一致",
            "财务披露日为窗口近似（无 pub_date 精确对齐）",
            "未处理一字涨停无法买入/停牌；极端收益（|ret|≥2）已剔除",
            "pv_consensus 触发为 bars 历史重算近似（因子池 daily_scores 仅当日）",
        ],
        "note": "winrate 为持有期正收益占比；avg_ret 几何近似（算术均值）；max_dd 为持有路径最大回撤",
    }
    out = {"date": datetime.now().strftime("%Y-%m-%d"), "meta": meta, "results": result}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out_path = out_path or OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default="2019-01", help="回测起点 YYYY-MM")
    ap.add_argument("--end", type=str, default=None, help="回测终点 YYYY-MM（默认数据末日）")
    ap.add_argument("--with-pv-consensus", action="store_true",
                    help="同时回测第 7 类 pv_consensus（五强历史重算触发）")
    ap.add_argument("--pv-mode", type=str, default="ge80", choices=["ge80", "le20"],
                    help="pv_consensus 命中口径：ge80=rank≥0.80（推荐）/ le20=rank≤0.20（scan 当前口径）")
    ap.add_argument("--out", type=str, default=None, help="输出 JSON 路径（默认 logs/opportunity_winrates.json）")
    args = ap.parse_args()
    out_p = Path(args.out) if args.out else None
    r = run(start=args.start, end=args.end, with_pv_consensus=args.with_pv_consensus,
            pv_mode=args.pv_mode, out_path=out_p)
    print("\n===== 每类机会胜率（持有期） =====")
    print(f"{'otype':12s}{'H':>4s}{'n':>7s}{'winrate':>9s}{'avg_ret':>9s}{'med':>9s}{'max_dd':>9s}{'pl':>7s}")
    otypes = list(ORDER) + (["pv_consensus"] if args.with_pv_consensus else [])
    for ot in otypes:
        for h, v in r["results"][ot].items():
            if v.get("n", 0) == 0:
                print(f"{ot:12s}{h:>4s}{'0':>7s}")
                continue
            print(f"{ot:12s}{h:>4s}{v['n']:>7d}{v['winrate']:>9.1%}{v['avg_ret']:>9.1%}"
                  f"{v['med_ret']:>9.1%}{v['max_dd']:>9.1%}"
                  f"{(v.get('pl_ratio') or 0):>7.2f}")
    print(f"\n已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
