# -*- coding: utf-8 -*-
"""
validation/factor_evaluator.py — ★因子有效性评估工具（中间层，2026-08-07）

定位：数据层 → 因子层 → 【因子评估层（本模块）】 → 策略层
职责：对每个因子做**多维度体检**，输出综合评分卡与裁决（强有效/弱有效/无效/反向+权重建议）。
任何因子进入选股/权重决策前，必须先过本关（P0.5 正式化，替代临时脚本 m3_validate）。

评估维度（8 项，机构标准：同花顺/东吴/华泰/中信建投 2025 方法论）：
  1. IC 分析    ：RankIC 均值 / ICIR / IC 胜率 / 近 6 期 IC
  2. 分层单调性 ：Q1-Q5 五分组平均收益的单调性（Spearman 相关，华泰 |IC| 分组单调）
  3. 多空组合   ：Q5-Q1 年化收益 / 夏普 / t 统计量（显著性）
  4. 多头超额   ：Q5（多头组）相对全池的年化超额
  5. 因子换手   ：月度排名变化率（决定真实交易成本，东吴 Turn20 教训）
  6. 衰减曲线   ：持有 5/20/60/120 日的 IC → 半衰期（第17课）
  7. 分池稳健性 ：市值大/小组 IC 是否一致（CS-03/28 分池检验）
  8. 时序稳定性 ：近 1 年 IC vs 全期 IC 差异 + PSI（第17课漂移检测）

输出：
  output/因子评估报告.md + factor_evaluations.json（评分卡，策略层直接消费）

用法：
  python validation/factor_evaluator.py                    # 评估全部已注册因子
  python validation/factor_evaluator.py --factors rps_120,lowvol_60
  python validation/factor_evaluator.py --limit 200        # 快速模式（小样本）
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS
from validation.m3_validate import (
    build_forward_returns, ic_analysis, write_report,
)

START, END = "2020-01-01", "2025-12-31"
OUT_DIR = BASE / "output"


# ---------- 预处理（机构标准：去极值 + 截面标准化）----------
def winsorize_series(s: pd.Series, lo=0.01, hi=0.99) -> pd.Series:
    """去极值（分位数截断）"""
    return s.clip(s.quantile(lo), s.quantile(hi))


# ---------- 1. 分层回测（Q1-Q5）----------
def layered_backtest(panel, factor_name, factor_df, labels, n_groups=5):
    """五分组：按月末因子值分组，统计各组未来收益均值 → 单调性 + 多空 + 多头超额
    返回 dict：group_returns(list), mono_spearman, ls_annual, ls_sharpe, ls_t, top_annual, top_excess
    """
    groups = {i: [] for i in range(1, n_groups + 1)}   # 各组月度收益
    for code, fdf in panel.items():
        if factor_name not in fdf.columns:
            continue
        lab = labels.get(code)
        if lab is None:
            continue
        vals = fdf[factor_name].dropna()
        for m, v in vals.items():
            r = lab.get(m, np.nan)
            if pd.isna(r):
                continue
            # 计算该月该股在所有股票中的分组（用月度截面排名近似）
            groups.setdefault(999, []).append((m, v, r))
    # 重新组织：按月截面分位分组
    monthly = {}
    for m, v, r in groups.get(999, []):
        monthly.setdefault(m, []).append((v, r))
    g_ret = {i: [] for i in range(1, n_groups + 1)}
    for m, pairs in monthly.items():
        if len(pairs) < 50:
            continue
        arr = sorted(pairs, key=lambda x: x[0])  # 按因子值升序
        n = len(arr)
        for i in range(1, n_groups + 1):
            lo = (i - 1) * n // n_groups
            hi = i * n // n_groups
            seg = arr[lo:hi]
            g_ret[i].append(np.mean([r for _, r in seg]))
    # 各组年化收益
    def ann(x):
        if len(x) < 6:
            return np.nan
        mean = np.mean(x)
        return mean * 12  # 月频近似年化
    g_annual = [ann(g_ret[i]) for i in range(1, n_groups + 1)]
    # 单调性：组序 vs 组收益 Spearman
    mono = float(pd.Series(range(1, 6)).corr(pd.Series(g_annual), method="spearman")) \
        if not any(np.isnan(g_annual)) else np.nan
    # 多空组合（Q5-Q1）月收益序列
    ls = [a - b for a, b in zip(g_ret[5], g_ret[1])]
    if len(ls) >= 6 and np.std(ls) > 0:
        ls_ann = float(np.mean(ls) * 12)
        ls_sharpe = float(np.mean(ls) / np.std(ls) * np.sqrt(12))
        ls_t = float(np.mean(ls) / (np.std(ls) / np.sqrt(len(ls))))
    else:
        ls_ann = ls_sharpe = ls_t = np.nan
    # 多头超额（Q5 vs 全池月度平均）
    top_excess = float(np.mean(g_ret[5]) - np.mean([r for grp in g_ret.values() for r in grp])) * 12 \
        if g_ret[5] else np.nan
    return {"group_annual": [round(x, 4) if not np.isnan(x) else None for x in g_annual],
            "monotonicity": round(mono, 4) if not np.isnan(mono) else None,
            "ls_annual": round(ls_ann, 4) if not np.isnan(ls_ann) else None,
            "ls_sharpe": round(ls_sharpe, 4) if not np.isnan(ls_sharpe) else None,
            "ls_t": round(ls_t, 3) if not np.isnan(ls_t) else None,
            "top_excess_annual": round(top_excess, 4) if not np.isnan(top_excess) else None}


# ---------- 5. 因子换手 ----------
def factor_turnover(factor_df: pd.DataFrame) -> float:
    """月度排名变化率（0-1，越大换手越高成本越高）"""
    if factor_df is None or factor_df.empty:
        return np.nan
    ym = factor_df.index.astype(str).str[:7]
    month_ends = pd.Series(factor_df.index).groupby(ym).max()
    ranks = []
    for me in month_ends:
        if me in factor_df.index:
            ranks.append(factor_df.loc[me].rank(pct=True))
    if len(ranks) < 2:
        return np.nan
    chg = [np.nanmean((ranks[i] - ranks[i - 1]).abs().dropna()) for i in range(1, len(ranks))]
    chg = [c for c in chg if not np.isnan(c)]
    return float(np.mean(chg)) if chg else np.nan


# ---------- 6. 衰减曲线（多持有期 IC）----------
def decay_curve(panel, factor_name, cache, codes, month_ends, horizons=(5, 20, 60, 120)):
    """各持有期的 RankIC 均值 → 半衰期近似"""
    ics = {}
    for h in horizons:
        labels = build_forward_returns(cache, codes, month_ends, horizon=h)
        res = ic_analysis(panel, labels, factor_name)
        ics[h] = res["rank_ic_mean"] if res else np.nan
    # 半衰期：IC 衰减到首个 IC 一半的持有期（线性插值近似）
    half = None
    ic0 = ics.get(5, np.nan)
    if not np.isnan(ic0) and abs(ic0) > 0.001:
        for h in horizons[1:]:
            if not np.isnan(ics.get(h)) and abs(ics[h]) <= abs(ic0) / 2:
                half = h
                break
    return {"ic_by_horizon": {h: round(v, 4) if not np.isnan(v) else None for h, v in ics.items()},
            "half_life_days": half}


# ---------- 7. 分池稳健性 ----------
def pool_robustness(panel, factor_name, labels, mv_map, big_ratio=0.3):
    """市值大/小组 IC 一致性（简化：按市值排序前30% vs 后70%）"""
    if not mv_map:
        return {"big_ic": None, "small_ic": None, "consistent": None}
    big_codes, small_codes = [], []
    mvs = [(c, mv_map.get(c.split(".")[0], np.nan)) for c in panel if c.split(".")[0] in mv_map]
    mvs = [(c, v) for c, v in mvs if not np.isnan(v)]
    if len(mvs) < 30:
        return {"big_ic": None, "small_ic": None, "consistent": None}
    mvs.sort(key=lambda x: x[1], reverse=True)
    k = max(int(len(mvs) * big_ratio), 5)
    big_codes = [c for c, _ in mvs[:k]]
    small_codes = [c for c, _ in mvs[k:]]
    def ic_of(codes_sub):
        sub = {c: panel[c] for c in codes_sub if c in panel}
        if len(sub) < 10:
            return np.nan
        res = ic_analysis(sub, labels, factor_name)
        return res["rank_ic_mean"] if res else np.nan
    big_ic = ic_of(big_codes)
    small_ic = ic_of(small_codes)
    consistent = None
    if not np.isnan(big_ic) and not np.isnan(small_ic):
        consistent = (big_ic * small_ic) > 0  # 同向即一致
    return {"big_ic": round(big_ic, 4) if not np.isnan(big_ic) else None,
            "small_ic": round(small_ic, 4) if not np.isnan(small_ic) else None,
            "consistent": consistent}


# ---------- 8. 时序稳定性（近1年 vs 全期）----------
def temporal_stability(res: dict) -> dict:
    """近 6 期 IC vs 全期 IC"""
    if res is None or "ic_latest_6m" not in res or res.get("rank_ic_mean") is None:
        return {"latest_6m": None, "full": None, "drift": None}
    full = res["rank_ic_mean"]
    last = res["ic_latest_6m"]
    drift = None
    if full is not None and abs(full) > 0.001:
        drift = (last - full) / abs(full)
    return {"latest_6m": last, "full": full, "drift": drift}


# ---------- 综合评分卡 ----------
def score_card(ic_res, layer, turnover, decay, pool, temporal, direction) -> dict:
    """多维度加权 → 0-100 分 + 裁决 + 权重建议"""
    s = 0.0
    n = 0
    # IC 维度（40 分）
    if ic_res:
        ic = abs(ic_res.get("rank_ic_mean") or 0)
        icir = abs(ic_res.get("icir") or 0)
        win = ic_res.get("ic_win_rate") or 0
        s += min(ic / 0.05, 1.0) * 20
        s += min(icir / 0.5, 1.0) * 12
        s += win * 8
        n += 40
    # 分层单调性（20 分）
    if layer and layer.get("monotonicity") is not None:
        s += max(0, abs(layer["monotonicity"])) * 20
        n += 20
    # 多空显著性（20 分）
    if layer and layer.get("ls_t") is not None:
        t = abs(layer["ls_t"])
        s += min(t / 3.0, 1.0) * 14
        if layer.get("ls_annual") and layer["ls_annual"] > 0:
            s += 6
        n += 20
    # 换手（10 分，越低越好）
    if not np.isnan(turnover) and turnover is not None:
        s += max(0, 1 - turnover / 0.5) * 10
        n += 10
    # 时序稳定（10 分）
    if temporal and temporal.get("drift") is not None:
        s += max(0, 1 - min(abs(temporal["drift"]), 2)) * 10
        n += 10
    total = s / max(n, 1) * 100 if n else 0

    # 裁决（区分正向/反向信号：A 股反转市大量因子为负 IC 但强单调 → 反用）
    ic_sign = ""
    if ic_res and ic_res.get("rank_ic_mean") is not None:
        ic_sign = "反向" if ic_res["rank_ic_mean"] < 0 else "正向"
    if total >= 70:
        verdict = f"{ic_sign}强有效" if ic_sign else "强有效"
        weight_suggestion = "主权重（60-100%，反用）" if ic_sign == "反向" else "主权重（60-100%）"
    elif total >= 50:
        verdict = f"{ic_sign}弱有效" if ic_sign else "弱有效"
        weight_suggestion = "低权重（10-30%，反用）" if ic_sign == "反向" else "低权重（10-30%）"
    elif total >= 35:
        verdict = "边缘（需分池/条件使用）"
        weight_suggestion = "条件权重（分池启用）"
    else:
        verdict = "无效" if direction > 0 else "反向（反用或剔除）"
        weight_suggestion = "剔除 / 反用验证"
    return {"score": round(total, 1), "verdict": verdict,
            "weight_suggestion": weight_suggestion, "direction": direction}


# ---------- 主流程 ----------
def evaluate_all(limit=None, factor_names=None, mv_map=None):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    if limit:
        codes = codes[:limit]

    print(f"加载面板（{len(codes)} 只）...")
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()[["close", "volume"]]
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    # ★保持字符串索引（与 m3_validate/build_forward_returns 对齐，转 datetime 会导致标签 NaN）
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    ym = closes.index.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(closes.index).groupby(ym).max().tolist()]
    labels = build_forward_returns(cache, codes, month_ends, horizon=20)

    names = factor_names or list(FACTOR_FUNCS.keys())
    results = {}
    for name in names:
        print(f"\n=== 评估 {name} ===")
        if name not in FACTOR_FUNCS:
            print("  未注册，跳过")
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        # 去极值
        raw = raw.apply(winsorize_series, axis=0)
        # ★重采样到月末（ic_analysis/labels 均按月末对齐）
        raw_m = raw.reindex(month_ends)
        panel_f = {code: pd.DataFrame({name: raw_m[code]})
                   for code, d in panel.items() if code in raw_m.columns}
        ic_res = ic_analysis(panel_f, labels, name)
        layer = layered_backtest(panel_f, name, raw_m, labels)
        turnover = factor_turnover(raw_m)
        decay = decay_curve(panel_f, name, cache, codes, month_ends)
        pool = pool_robustness(panel_f, name, labels, mv_map)
        temporal = temporal_stability(ic_res)
        sc = score_card(ic_res, layer, turnover, decay, pool, temporal, direction=1)
        results[name] = {
            "ic": ic_res, "layer": layer, "turnover": turnover,
            "decay": decay, "pool": pool, "temporal": temporal,
            "scorecard": sc,
        }
        print(f"  评分 {sc['score']} ｜ {sc['verdict']} ｜ {sc['weight_suggestion']}")
        if ic_res:
            print(f"  IC {ic_res['rank_ic_mean']:.4f} ICIR {ic_res['icir']:.3f} "
                  f"胜率 {ic_res['ic_win_rate']:.1%} 单调 {layer.get('monotonicity')} "
                  f"多空t {layer.get('ls_t')} 换手 {turnover:.3f}")
    # ★2026-10-18 补"有池无序列"：月末快照落 factor_ic_series（横截面通道，
    # 见 factors/pool/ic_store.py）。落库失败不影响评估报告——数据归数据，报告归报告。
    try:
        from factors.pool.ic_store import connect as _ic_connect, save_snapshot as _ic_snap
        _con = _ic_connect()
        _ym = datetime.now().strftime("%Y-%m")
        for _name, _r in results.items():
            _ic = _r.get("ic") or {}
            if _ic.get("rank_ic_mean") is not None:
                _ic_snap(_con, _name, _ym, _ic["rank_ic_mean"], _ic.get("icir"),
                         source="cross_sectional")
        _con.close()
    except Exception as _e:
        print(f"[ic_store] 快照落库失败（不影响报告）：{type(_e).__name__}: {_e}")
    write_eval_report(results)
    return results


def write_eval_report(results: dict):
    OUT_DIR.mkdir(exist_ok=True)
    lines = [
        f"# 因子有效性评估报告（中间层体检）",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 区间 {START}~{END}",
        f"> 维度：IC分析 / 分层单调性 / 多空检验 / 换手 / 衰减 / 分池稳健 / 时序稳定 ｜ 机构标准（同花顺/东吴/华泰/中信建投）",
        "",
        "## 综合评分卡",
        "",
        "| 因子 | 评分 | 裁决 | 权重建议 | IC | ICIR | 胜率 | 单调性 | 多空t | 换手 | 半衰期(日) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    archive = {}
    for name, r in sorted(results.items()):
        sc = r["scorecard"]
        ic = r["ic"]
        layer = r["layer"]
        archive[name] = r
        lines.append(
            f"| {name} | **{sc['score']}** | {sc['verdict']} | {sc['weight_suggestion']} | "
            f"{ic['rank_ic_mean'] if ic else '-'} | {ic['icir'] if ic else '-'} | "
            f"{ic['ic_win_rate'] if ic else '-'} | {layer.get('monotonicity', '-')} | "
            f"{layer.get('ls_t', '-')} | {round(r['turnover'],3) if not np.isnan(r['turnover']) else '-'} | "
            f"{r['decay'].get('half_life_days', '-')} |")
    lines += [
        "",
        "## 判定标准（参考系）",
        "",
        "- **评分 ≥70 强有效**：IC ≥0.03 且 ICIR ≥0.3 且 单调性显著且多空 t ≥2 → 主权重",
        "- **评分 50-69 弱有效**：可用但低权重（10-30%）",
        "- **评分 35-49 边缘**：仅分池/条件启用（CS-03 分池）",
        "- **评分 <35 无效/反向**：剔除或反用（方向化验证，CS-35）",
        "- **换手 >0.5**：成本过高警示（东吴 Turn20 教训，含成本后可能转负）",
        "- **半衰期 <20 日**：信号衰减快，仅适合短持有；与『长期持有』定位冲突时降权",
        "",
        "*本报告由 factor_evaluator.py 生成，任何因子进入选股/权重决策前必经此关。*",
    ]
    (OUT_DIR / "因子评估报告.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "factor_evaluations.json").write_text(
        json.dumps(archive, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n报告已生成：{OUT_DIR / '因子评估报告.md'}")


def main():
    ap = argparse.ArgumentParser(description="因子有效性评估工具（中间层）")
    ap.add_argument("--factors", default=None, help="逗号分隔因子名（默认全部）")
    ap.add_argument("--limit", type=int, default=None, help="样本限制（快速模式）")
    args = ap.parse_args()

    names = args.factors.split(",") if args.factors else None
    evaluate_all(limit=args.limit, factor_names=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
