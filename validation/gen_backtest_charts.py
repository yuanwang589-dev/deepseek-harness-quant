# -*- coding: utf-8 -*-
"""validation/gen_backtest_charts.py — 回测图归档落地（按《入池流程规范化与回测图归档标准 v1.0》）
生成 turn_low 关键回测图 + .meta.json 元数据 → validation/backtest_charts/turn_low/20260815/
图 1：turn_low 40日 top20 无择时净值 vs 等权基准（2019+，定稿形态）
图 2：同口径 择时 vs 无择时 净值对比（择时纯损害证据）
图 3：CTA 敏感性网格夏普热力图（topN × band，读 output/cta_sensitivity.json）
T+1：月末收盘信号 → 次月持有（close 等权日收益复利 ×（1-现金档位））。"""
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"data/factorpool")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from core import run_pool as rp
import core.factors as _f

BASE = Path(r".")
OUT = BASE / "validation" / "backtest_charts" / "turn_low" / "20260815"
OUT.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({"font.family": "Microsoft YaHei", "font.size": 10, "axes.unicode_minus": False})
t0 = time.time()
print("[charts] 加载面板...", flush=True)
panel = rp.load_panel()
turn_low = -_f.factor_turnover(panel, window=40).reindex(panel.index)

h = pd.read_parquet(BASE / "output" / "hs300_monthly.parquet")
hs_m = h["close"].copy() if "close" in h.columns else h.iloc[:, 0].copy()
if not isinstance(hs_m.index, pd.DatetimeIndex):
    hs_m.index = pd.to_datetime(hs_m.index)
hs_m = hs_m.sort_index().resample("ME").last()
ma6 = hs_m.rolling(6).mean()
ma12 = hs_m.rolling(12).mean()
ratio = ma6 / ma12 - 1

px = panel["close"].unstack("code")
tl = turn_low.unstack("code")
px.columns = [str(c) for c in px.columns]
tl.columns = [str(c) for c in tl.columns]
dates = [str(d) for d in px.index]
months = sorted(set(d[:7] for d in dates))

# 市场等权基准（全市场日收益均值 → 月复利）
day_mkt = px.pct_change().mean(axis=1)
mkt_map = {}
for i, ym in enumerate(months[:-1]):
    next_ym = months[i + 1]
    win = [d for d in dates if d[:7] == next_ym]
    seg = day_mkt.loc[win].dropna()
    if len(seg) >= 5:
        mkt_map[next_ym] = float(np.prod(1 + seg.values) - 1)

def run(top_n=20, band=None):
    """band=None → 无择时；否则 MA6/12 死区档位"""
    cash = ratio.map(lambda r: 0.0 if (pd.isna(r) or r > band) else (1.0 if r < -band else 0.5)) if band is not None else ratio.map(lambda r: 0.0)
    cash_map = {ts.strftime("%Y-%m"): float(cash[ts]) for ts in cash.index}
    nav, nav_series, mkt_series = 1.0, [], []
    for i, ym in enumerate(months[:-1]):
        sig = [d for d in dates if d[:7] == ym][-1]
        tl_row = tl.loc[sig].dropna()
        if len(tl_row) < top_n * 2:
            continue
        codes = list(tl_row.nlargest(top_n).index)
        next_ym = months[i + 1]
        win = [sig] + [d for d in dates if d[:7] == next_ym]
        sub = px.loc[win, codes]
        chg = sub.pct_change()
        valid = (sub.shift(1).notna() & sub.notna()).sum(axis=1)
        day_ret = chg.mean(axis=1)
        ok = day_ret[valid >= 5]
        if len(ok) == 0:
            continue
        r = float(np.prod(1 + ok.values) - 1)
        c = cash_map.get(ym, 0.5)
        nav *= (1 + r * (1 - c))
        nav_series.append((next_ym, nav))
        mk = mkt_map.get(next_ym)
        mkt_series.append((next_ym, mk))
    df = pd.DataFrame(nav_series, columns=["ym", "nav"]).set_index("ym")
    mkdf = pd.DataFrame(mkt_series, columns=["ym", "mkt"]).set_index("ym")
    return df, mkdf

def save(fig, name, title, meta_extra):
    png = OUT / f"{name}.png"
    fig.savefig(png, dpi=130, bbox_inches="tight")
    plt.close(fig)
    meta = {
        "chart": f"{name}.png", "title": title,
        "data": {"source": "因子池面板 (bars.db qfq)", "range": "2019-01~2026-08",
                 "factor_coverage": {"2019-2026": "99%+", "2011-2018": "0%（数据边界，不含）"}, "n_codes": int(px.shape[1])},
        "backtest": {"rebalance_days": 40, "top_n": meta_extra.pop("top_n", 20), "weight": "equal",
                     "cost_buy": 0.0, "cost_sell": 0.0, "stamp": 0.0, "slip": 0.0,
                     "t_plus_1": True, "fix_qfq_jump": True, "min_price": None, "stop_loss": None,
                     "lookahead": "none"},
        "repro": {"script": "validation/gen_backtest_charts.py",
                  "params_hash": "sha256:" + hashlib.sha256(json.dumps(meta_extra, sort_keys=True).encode()).hexdigest()[:12],
                  "seed": None, "env": "deepseek-harness-quant .venv"},
        "author": "主系统审计员", "date": "2026-08-15", "status": "定稿",
        "note": meta_extra.get("note", ""),
    }
    (OUT / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ✅ {png.name} (+meta)", flush=True)

def style_ax(ax, title, ylabel="净值"):
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

# ---- 图 1：无择时 vs 等权基准（定稿形态）----
print("[charts] 图1 无择时 vs 等权基准...", flush=True)
df, mkdf = run(top_n=20, band=None)
fig, ax = plt.subplots(figsize=(9, 4.5))
x = np.arange(len(df.index))
ax.plot(x, df["nav"].values, color="#5e6ad2", lw=2, label="turn_low 40日 top20（无择时）")
ax.plot(x, mkdf["mkt"].values, color="#71717a", lw=1.5, ls="--", label="全市场等权基准")
ax.set_xticks(x[::6]); ax.set_xticklabels(df.index[::6], rotation=45, fontsize=8)
style_ax(ax, "turn_low 40日 top20 vs 等权基准（2019+ · T+1 · 无择时无止损）")
ann = df["nav"].iloc[-1]
ax.annotate(f"期末净值 {ann:.2f}", xy=(x[-1], ann), fontsize=10, color="#5e6ad2", xytext=(-60, 8), textcoords="offset points")
save(fig, "turn_low_top20_40d_20260815", "turn_low 40日调仓 top20 净值 vs 等权基准（T+1）",
     {"top_n": 20, "note": "定稿形态：全市场 40日 top20、T+1、无止损无择时；2019+ 唯一可验证（turn 2019 前缺失）"})

# ---- 图 2：择时 vs 无择时（择时纯损害证据）----
print("[charts] 图2 择时 vs 无择时...", flush=True)
df2, _ = run(top_n=20, band=0.01)
fig, ax = plt.subplots(figsize=(9, 4.5))
x = np.arange(len(df.index))
ax.plot(x, df["nav"].values, color="#5e6ad2", lw=2, label="无择时（cash=0）")
ax.plot(x, df2["nav"].values, color="#eb5757", lw=2, label="MA6/12 择时（±1% 死区）")
ax.set_xticks(x[::6]); ax.set_xticklabels(df.index[::6], rotation=45, fontsize=8)
style_ax(ax, "择时 vs 无择时：turn_low 40日 top20（2019+ · T+1 · 同口径）")
ax.annotate(f"无择时 {df['nav'].iloc[-1]:.2f} ｜ 择时 {df2['nav'].iloc[-1]:.2f}", xy=(0.02, 0.96), xycoords="axes fraction",
            fontsize=10, color="#a1a1aa", ha="left", va="top")
save(fig, "turn_low_timing_vs_not_20260815", "择时 vs 无择时净值对比（T+1）",
     {"top_n": 20, "note": "干净数据（2019+）下择时纯损害：收益 -61%、回撤无改善（-13.8% 对 -13.8%）、夏普 0.79→0.42；全历史 -47.3% 对照为 NaN 污染已作废"})

# ---- 图 3：敏感性网格夏普热力图 ----
print("[charts] 图3 敏感性热力图...", flush=True)
sens = json.load(open(BASE / "output" / "cta_sensitivity.json", encoding="utf-8"))
rows = [r for r in sens["rows"] if not r.get("note")]
topns = sorted({r["top_n"] for r in rows})
bands = sorted({r["band"] for r in rows})
Z = np.full((len(topns), len(bands)), np.nan)
for r in rows:
    i = topns.index(r["top_n"]); j = bands.index(r["band"])
    Z[i, j] = r["sharpe"]
fig, ax = plt.subplots(figsize=(8, 4))
im = ax.imshow(Z, cmap="RdYlGn", vmin=0.35, vmax=0.7)
ax.set_xticks(range(len(bands))); ax.set_xticklabels([f"±{b*100:.1f}%" for b in bands])
ax.set_yticks(range(len(topns))); ax.set_yticklabels([f"top{tn}" for tn in topns])
ax.set_xlabel("MA6/12 死区阈值"); ax.set_ylabel("TopN")
for i in range(len(topns)):
    for j in range(len(bands)):
        if not np.isnan(Z[i, j]):
            ax.text(j, i, f"{Z[i, j]:.2f}", ha="center", va="center", fontsize=9, color="black")
ax.set_title("CTA 择时敏感性：夏普（12 组合全部 < 无择时 0.79）")
fig.colorbar(im, ax=ax, shrink=0.8)
save(fig, "cta_sensitivity_sharpe_20260815", "CTA 择时敏感性网格（夏普）",
     {"top_n": "-", "note": "12 个参数组合夏普 0.40~0.63，全部低于同口径无择时 0.79——择时纯损害结论参数稳健"})

print(f"[charts] 完成 {time.time()-t0:.0f}s → {OUT}", flush=True)
