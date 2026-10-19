# -*- coding: utf-8 -*-
"""factors/pool/eval_ts.py — ★时序因子评估器（择时信号专用）

定位：因子池对【时序因子】（EPU 政策不确定性、信贷脉冲、日历效应等宏观择时信号）的评估通道。
     横截面选股因子仍走 validation/factor_evaluator.py（8 维体检）。
     时序因子是全市场共享一个序列 → 评估对象是"信号与市场未来收益的关系"而非个股分层。

评估维度（6 项）：
  1. forward_ic    : 因子值 vs 未来 1/3/6 个月市场收益的 Spearman IC + ICIR
  2. group_gap     : 因子三分位（高/中/低）→ 各组未来 1 个月市场收益均值，高-低差（择时区分度）
  3. direction     : IC 符号 + t 统计 → 方向裁决（正/负/无效）
  4. stability     : 前/后半段 IC 一致性 + 近 12 期 IC 胜率
  5. redundancy    : 与现有 Regime 现金比例序列的相关系数（过高=冗余，惩罚）
  6. coverage      : 有效月数 / 缺失率

评分卡（0-100）：IC 0.35 + 分组差 0.20 + 稳定性 0.20 + 方向显著性 0.15 + 覆盖 0.10，冗余度惩罚 ≤10
裁决：≥65 active（可接入择时）/ 40-65 candidate（观察）/ <40 retired（淘汰）

用法：
  python factors/pool/eval_ts.py --factor epu_level
  from factors.pool.eval_ts import evaluate_time_series
  res = evaluate_time_series('epu_level', series, mkt_ret)
"""
import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache

THRESHOLDS = {"active": 65, "candidate": 40}


# ---------- 市场月度收益（沪深300，none 复权）----------
def market_monthly_returns(start="2019-01-01") -> pd.Series:
    """沪深300 月末收益序列（index=YYYY-MM）"""
    cache = DailyCache()
    df = cache.get_daily("SH.000300", start=start, adjust="none")
    if df is None or df.empty:
        return pd.Series(dtype=float)
    close = df.set_index("date")["close"].astype(float)
    close.index = pd.to_datetime(close.index)
    m = close.resample("ME").last()
    ret = m.pct_change().dropna()
    ret.index = ret.index.strftime("%Y-%m")
    return ret


def _fwd_ret(ret: pd.Series, h: int) -> pd.Series:
    """t 月末已知因子 → 未来 h 个月累计市场收益（t+1..t+h）
    ★2026-08-14 审计修复：原 `shift(-1).rolling(h).sum()` 实为 [t-h+2..t+1] 窗口
    （h=3 时 = 过去 2 月 + 未来 1 月），动量类时序因子与自身过去收益虚假相关。
    修正：fwd[t] = Σ ret[t+1..t+h]（shift(-1) 后 rolling(h).sum() 再左移 h-1 对齐）"""
    return ret.shift(-1).rolling(h, min_periods=h).sum().shift(-(h - 1))


def _spearman_ic(factor: pd.Series, fwd: pd.Series) -> dict:
    """逐月配对 → 12 个月滚动窗口 Spearman IC 序列 → {ic_mean, icir, ic_pos_ratio, t_stat, n}
    （时间序列 IC：信号值与未来收益的滚动相关，非截面分组）"""
    import scipy.stats as st
    df = pd.concat([factor, fwd], axis=1, keys=["f", "r"]).dropna()
    if len(df) < 12:
        return {"ic_mean": np.nan, "icir": np.nan, "ic_pos_ratio": np.nan,
                "t_stat": np.nan, "n": 0, "ic_series": {}}
    win = 12
    vals, idxs = [], []
    x, y = df["f"].values, df["r"].values
    for i in range(win - 1, len(x)):
        rho, _ = st.spearmanr(x[i - win + 1:i + 1], y[i - win + 1:i + 1])
        vals.append(rho)
        idxs.append(df.index[i])
    ic_series = pd.Series(vals, index=idxs).dropna()
    if len(ic_series) < 6:
        return {"ic_mean": np.nan, "icir": np.nan, "ic_pos_ratio": np.nan,
                "t_stat": np.nan, "n": len(ic_series),
                "ic_series": {str(k): float(v) for k, v in ic_series.items()}}
    m = ic_series.mean()
    sd = ic_series.std(ddof=1)
    t = m / (sd / np.sqrt(len(ic_series))) if sd > 0 else 0.0
    return {"ic_mean": float(m), "icir": float(m / sd) if sd > 0 else 0.0,
            "ic_pos_ratio": float((ic_series > 0).mean()), "t_stat": float(t), "n": int(len(ic_series)),
            # ★2026-10-18 补"有池无序列"：逐窗口序列随结果返回（原来算完就扔，只交汇总）
            "ic_series": {str(k): float(v) for k, v in ic_series.items()}}


def _group_gap(factor: pd.Series, fwd1: pd.Series) -> dict:
    """三分位：高/中/低组未来 1 个月收益均值 → 高-低差"""
    df = pd.concat([factor, fwd1], axis=1, keys=["f", "r"]).dropna()
    if len(df) < 30:
        return {"gap": np.nan, "high": np.nan, "low": np.nan}
    q3 = df["f"].quantile(2 / 3)
    q1 = df["f"].quantile(1 / 3)
    hi = df.loc[df["f"] >= q3, "r"].mean()
    lo = df.loc[df["f"] <= q1, "r"].mean()
    return {"gap": float(hi - lo), "high": float(hi), "low": float(lo)}


def _stability(factor: pd.Series, fwd1: pd.Series) -> dict:
    """前/后半段 IC + 近 12 期 IC 胜率"""
    df = pd.concat([factor, fwd1], axis=1, keys=["f", "r"]).dropna()
    if len(df) < 24:
        return {"split_consistent": np.nan, "recent_win": np.nan, "ic1": np.nan, "ic2": np.nan}
    half = len(df) // 2
    ic1 = df.iloc[:half]["f"].corr(df.iloc[:half]["r"], method="spearman")
    ic2 = df.iloc[half:]["f"].corr(df.iloc[half:]["r"], method="spearman")
    recent = df.iloc[-12:]
    ic_pos = 1.0 if (not recent.empty and recent["f"].corr(recent["r"], method="spearman") > 0) else 0.0
    same_sign = (ic1 > 0) == (ic2 > 0) if not np.isnan(ic1) and not np.isnan(ic2) else np.nan
    return {"split_consistent": bool(same_sign) if not pd.isna(same_sign) else np.nan,
            "recent_win": ic_pos, "ic1": float(ic1) if not np.isnan(ic1) else np.nan,
            "ic2": float(ic2) if not np.isnan(ic2) else np.nan}


def _redundancy(factor: pd.Series, regime_cash: pd.Series = None) -> float:
    """与 Regime 现金比例的相关系数（无 regime 数据返回 0 不惩罚）"""
    if regime_cash is None or regime_cash.empty:
        return 0.0
    df = pd.concat([factor, regime_cash], axis=1, keys=["f", "c"]).dropna()
    if len(df) < 12:
        return 0.0
    return float(df["f"].corr(df["c"], method="spearman"))


def regime_cash_series(start="2020-01") -> pd.Series:
    """现有 Regime 现金比例月度序列（供冗余度检验；实现取自 validation/test_regime_classified）"""
    try:
        from validation.test_regime_classified import load_index, regime_cash_at
        idx = load_index()
        idx.index = pd.to_datetime(idx.index)
        m = idx.resample("ME").last()
        m.index = m.index.strftime("%Y-%m")
        cash = m.index.to_series().apply(lambda x: regime_cash_at(m.loc[x], m.index.get_loc(x) if hasattr(m.index, 'get_loc') else 0))
        cash.index = m.index
        return cash.astype(float)
    except Exception:
        return pd.Series(dtype=float)


def evaluate_time_series(name: str, factor: pd.Series, mkt_ret: pd.Series,
                         regime_cash: pd.Series = None, horizon: tuple = (1, 3, 6)) -> dict:
    """时序因子评估主函数 → 评分卡 + 裁决"""
    factor = factor.astype(float).dropna()
    ic_results = {}
    for h in horizon:
        fwd = _fwd_ret(mkt_ret, h)
        ic_results[f"h{h}"] = _spearman_ic(factor, fwd)

    fwd1 = _fwd_ret(mkt_ret, 1)
    gap = _group_gap(factor, fwd1)
    stab = _stability(factor, fwd1)
    red = _redundancy(factor, regime_cash)

    ic1 = ic_results.get("h1", {})
    ic3 = ic_results.get("h3", {})
    ic6 = ic_results.get("h6", {})
    # ---- 评分（0-100）----
    def sig_score(t, ic):
        if np.isnan(t) or np.isnan(ic):
            return 50.0
        base = min(abs(ic) * 100 * 2, 80)          # |IC|=0.4 → 80
        if abs(t) >= 1.96:
            base += 20
        elif abs(t) >= 1.3:
            base += 10
        return min(base, 100)

    ic_score = 0.5 * sig_score(ic1.get("t_stat", np.nan), ic1.get("ic_mean", np.nan)) \
             + 0.3 * sig_score(ic3.get("t_stat", np.nan), ic3.get("ic_mean", np.nan)) \
             + 0.2 * (100 if ic1.get("n", 0) >= 30 else 60)
    gap_score = min(abs(gap.get("gap", 0)) * 100 * 3, 100) if not np.isnan(gap.get("gap", np.nan)) else 40
    stab_score = (100 if stab.get("split_consistent") else 40) * 0.5 \
               + (100 if stab.get("recent_win") else 40) * 0.5
    dir_score = 100 if abs(ic1.get("t_stat", 0)) >= 1.96 else (70 if abs(ic1.get("t_stat", 0)) >= 1.3 else 40)
    cov_score = min(ic1.get("n", 0) / 36 * 100, 100)
    raw = (ic_score * 0.35 + gap_score * 0.20 + stab_score * 0.20
           + dir_score * 0.15 + cov_score * 0.10)
    raw = raw * (1 - min(abs(red), 0.5) * 0.2)     # 冗余度惩罚 ≤10%

    # ---- 裁决 ----
    status = "active" if raw >= THRESHOLDS["active"] else ("candidate" if raw >= THRESHOLDS["candidate"] else "retired")
    direction = "+" if ic1.get("ic_mean", 0) > 0 else "-" if ic1.get("ic_mean", 0) < 0 else "0"
    verdict = f"方向{direction}（信号高 → 未来市场收益{'正' if direction=='+' else '负' if direction=='-' else '无'}）"

    return {
        "factor": name, "kind": "time_series",
        "score": round(raw, 1), "status": status, "verdict": verdict,
        "ic_h1": round(ic1.get("ic_mean", np.nan), 3) if not np.isnan(ic1.get("ic_mean", np.nan)) else None,
        "icir_h1": round(ic1.get("icir", np.nan), 2) if not np.isnan(ic1.get("icir", np.nan)) else None,
        "t_h1": round(ic1.get("t_stat", np.nan), 2) if not np.isnan(ic1.get("t_stat", np.nan)) else None,
        "ic_h3": round(ic3.get("ic_mean", np.nan), 3) if not np.isnan(ic3.get("ic_mean", np.nan)) else None,
        "t_h3": round(ic3.get("t_stat", np.nan), 2) if not np.isnan(ic3.get("t_stat", np.nan)) else None,
        "icir_h3": round(ic3.get("icir", np.nan), 2) if not np.isnan(ic3.get("icir", np.nan)) else None,
        "ic_h6": round(ic6.get("ic_mean", np.nan), 3) if not np.isnan(ic6.get("ic_mean", np.nan)) else None,
        "icir_h6": round(ic6.get("icir", np.nan), 2) if not np.isnan(ic6.get("icir", np.nan)) else None,
        # ★2026-10-18：h1 逐窗口 IC 序列（YYYY-MM → IC），供 ic_store 落库 / monitor 衰减巡检
        "ic_series_h1": ic1.get("ic_series") or {},
        "gap_hl": round(gap.get("gap", np.nan), 3) if not np.isnan(gap.get("gap", np.nan)) else None,
        "gap_high": round(gap.get("high", np.nan), 3) if not np.isnan(gap.get("high", np.nan)) else None,
        "gap_low": round(gap.get("low", np.nan), 3) if not np.isnan(gap.get("low", np.nan)) else None,
        "split_consistent": stab.get("split_consistent"),
        "redundancy": round(red, 2),
        "n_months": int(ic1.get("n", 0)),
        "thresholds": THRESHOLDS,
    }


def main():
    ap = argparse.ArgumentParser(description="时序因子评估")
    ap.add_argument("--factor", required=True, help="因子名（epu_level 等）")
    args = ap.parse_args()

    from factors.policy.epu_factors import get_factor
    series = get_factor(args.factor)
    mkt = market_monthly_returns()
    res = evaluate_time_series(args.factor, series, mkt)
    # ★2026-10-18 补"有池无序列"：逐窗口 IC 序列落库 + 因子表指标回填（ic_store）
    try:
        from factors.pool.ic_store import connect as _ic_connect, save_series as _ic_save
        _con = _ic_connect()
        _s = pd.Series(res.get("ic_series_h1") or {})
        if not _s.empty:
            _n = _ic_save(_con, args.factor, _s, source="eval_ts")
            print(f"[ic_store] {args.factor}: 落库 {_n} 期 IC 序列", file=sys.stderr)
        else:
            print(f"[ic_store] {args.factor}: 无 IC 序列可落（评估未产出）", file=sys.stderr)
        _con.close()
    except Exception as e:
        print(f"[ic_store] 落库失败（不影响评估结果输出）：{type(e).__name__}: {e}",
              file=sys.stderr)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
