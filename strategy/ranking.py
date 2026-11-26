# -*- coding: utf-8 -*-
"""排名引擎（程序三大能力之一，主文档 4.4）
全市场打分排名：防守四因子 + CANSLIM 修正版 + PEAD + 加分项
综合分 = 防守40% + CANSLIM 40% + PEAD 10% + 加分 10%（权重 P0.5 校准，params.yaml weights 可覆盖）

输出：排名榜 DataFrame（含各因子分项 + 综合分 + 状态标签）
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
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.factor_engine import FACTOR_FUNCS
from factors.fundamental import fundamental_snapshot

# 默认权重（params.yaml weights 段可覆盖）
W_DEFENSE, W_CANSLIM, W_PEAD, W_BONUS = 0.40, 0.40, 0.10, 0.10

# 各板块因子（方向见 factors.direction 实证表）
DEFENSE_FACTORS = {"lowvol_60": -1}        # 防守：低波（质量/红利待财报数据）
CANSLIM_FACTORS = {"rps_120": -1, "mom_20": -1}   # 修正版 CANSLIM（A 股方向化）
PEAD_FACTORS = {}                          # 基本面：走 build_fund_score（截面式）
BONUS_FACTORS = {"near_high_250": 1}       # 加分：接近高点（唯一 120 日转正）

# 基本面评分因子（CS-33 验证：加速度/SUE 最强）
FUND_SCORE_FACTORS = ["sue_factor", "accel_factor"]


def build_fund_score(closes: pd.DataFrame, asof: str) -> pd.DataFrame:
    """基本面截面评分（PEAD 部分）：SUE + 加速度 排名平均 → 0-1
    返回 DataFrame(code, fund_score)"""
    snap = fundamental_snapshot(closes, asof)
    if snap.empty:
        return pd.DataFrame(0.5, index=closes.columns, columns=["fund_score"])
    # ★必须 set_index("code")，否则 rank 后索引错位导致全 NaN
    snap = snap.set_index("code")
    scores = []
    for f in FUND_SCORE_FACTORS:
        if f in snap.columns:
            scores.append(snap[f].rank(pct=True))
    if not scores:
        return pd.DataFrame(0.5, index=closes.columns, columns=["fund_score"])
    fund = pd.concat(scores, axis=1).mean(axis=1)
    out = pd.DataFrame({"fund_score": fund}, index=snap.index)
    return out


def build_score(closes: pd.DataFrame, direction: dict) -> pd.DataFrame:
    """方向化因子 → 截面排名分（0-1）"""
    panels = {}
    for name, sign in direction.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    if not panels:
        return pd.DataFrame(0.5, index=closes.index, columns=closes.columns)
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    return score / len(panels)


def rank_market(closes: pd.DataFrame, asof=None, weights=None,
                defense_f=None, canslim_f=None, pead_f=None, bonus_f=None) -> pd.DataFrame:
    """全市场排名（截至 asof 日期或最新）
    返回 DataFrame: code, 综合分, 防守分, canslim分, pead分, bonus分, 因子明细
    """
    w = weights or {}
    wd = w.get("defense", W_DEFENSE)
    wc = w.get("canslim", W_CANSLIM)
    wp = w.get("pead", W_PEAD)
    wb = w.get("bonus", W_BONUS)

    defense = defense_f or DEFENSE_FACTORS
    canslim = canslim_f or CANSLIM_FACTORS
    pead = pead_f or PEAD_FACTORS
    bonus = bonus_f or BONUS_FACTORS

    date = asof if asof is not None else closes.index[-1]
    if date not in closes.index:
        date = closes.index[closes.index <= date][-1]

    s_def = build_score(closes, defense)
    s_can = build_score(closes, canslim)
    # PEAD：优先用基本面截面评分（SUE+加速度），无则中性 0.5
    try:
        fund_score = build_fund_score(closes, date)
        s_pead = pd.DataFrame(0.5, index=closes.index, columns=closes.columns)
        for code, v in fund_score["fund_score"].items():
            if code in s_pead.columns:
                s_pead.loc[date, code] = v
    except Exception:
        s_pead = pd.DataFrame(0.5, index=closes.index, columns=closes.columns)
    s_bonus = build_score(closes, bonus) if bonus else pd.DataFrame(0.5, index=closes.index, columns=closes.columns)

    # 因子明细（方向化原始值，供展示）
    details = {}
    all_factors = {**defense, **canslim, **pead, **bonus}
    for name, sign in all_factors.items():
        if name in FACTOR_FUNCS:
            raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
            details[name] = raw.loc[date]

    total = (wd * s_def.loc[date] + wc * s_can.loc[date]
             + wp * s_pead.loc[date] + wb * s_bonus.loc[date])
    df = pd.DataFrame({
        "code": closes.columns,
        "综合分": total.values,
        "防守分": s_def.loc[date].values,
        "canslim分": s_can.loc[date].values,
        "pead分": s_pead.loc[date].values,
        "bonus分": s_bonus.loc[date].values,
    })
    # 基本面因子明细（SUE/加速度，供追溯）
    try:
        snap = fundamental_snapshot(closes, date)
        snap = snap.set_index("code")
        for f in FUND_SCORE_FACTORS:
            if f in snap.columns:
                df[f] = df["code"].map(snap[f])
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "strategy/ranking.py:126 rank_market")
    for name, series in details.items():
        df[name] = series.values
    df = df.sort_values("综合分", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache
    import sqlite3

    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:800]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start="2020-01-01", end="2025-12-31", adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()

    print(f"排名引擎自测（800 只，asof=2025-12-31）:")
    rk = rank_market(closes, asof="2025-12-31")
    print(rk[["rank", "code", "综合分", "防守分", "canslim分", "bonus分"]].head(10).to_string(index=False))
    print(f"\n排名池: {len(rk)} 只 | Top10 综合分区间: {rk['综合分'].iloc[0]:.3f} ~ {rk['综合分'].iloc[9]:.3f}")
