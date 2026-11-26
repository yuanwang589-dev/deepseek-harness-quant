# -*- coding: utf-8 -*-
"""
strategy/pitch.py — ★Pitch 引擎（2026-08-07，用户定调：优中选优 → pitch → 人审批）

执行契约：
  1. 每次给出执行指令，必须同时给出该标的的历史回测（含择时）——本模块逐只输出
     买入持有 1/2/3 年窗口回测（2020-2025 每季初买入），并用动态择时仓位修正
  2. 指令频率极低：月度评估一次；防守档（现金≥50%）暂停 pitch
  3. 每次优中选优 3-5 只 pitch，人审批后才可买入（pitch 状态 pending → approved/rejected）
  4. 候选来源：底座池 base_pool.json（统计学意义合格池，全市场硬筛+质量门槛）

筛选链（底座池 → 候选 → Top N）：
  质量：F-Score ≥ 5（数据未就绪降级：年化 ROE ≥ 10%）
        年化 ROE ≥ 10%（底座池已 ≥8%）
  成长：单季净利同比 ≥ 25%（欧奈尔 C 门槛）
        连续 2 期同比 > 0（持续性）
  时机：距 52 周高点回撤 10%-40%（不追高、不接飞刀）
  排序：质量 40% + 成长 30% + 时机 30%

用法：python strategy/pitch.py [--n 5] [--force]
输出：output/pitch_report.md（人读）+ output/pitch_report.json（含审批状态）
审批：python strategy/pitch.py --approve 600519.SH  /  --reject 600519.SH
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
import json
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from data.pit import latest_disclosed_period
from strategy.ranking_v2 import load_basic

CACHE = Path(r"data/cache")
OUT_JSON = BASE / "output" / "pitch_report.json"
OUT_MD = BASE / "output" / "pitch_report.md"

MIN_YOY = 25.0          # 单季净利同比门槛 %
MIN_ROE_ANN = 10.0      # 年化 ROE 门槛（百分点，与 finance_report.roe 同口径）
MIN_FSCORE = 5          # F-Score 门槛（质量）
DIST_RANGE = (10.0, 40.0)  # 距 52 周高点回撤区间 %（10-40）
MAX_N = 5


def _load_pool() -> list:
    p = BASE / "output" / "base_pool.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("codes", []) or []


def _load_finance():
    """最新期基本面：code6 → {period, roe, yoy, prev_yoy}"""
    con = sqlite3.connect(str(CACHE / "finance.db"))
    # ★2026-11-10 PIT：只用已过法定披露截止日的期
    _cut = latest_disclosed_period()
    periods = con.execute(
        "SELECT period, COUNT(DISTINCT code) FROM finance_report WHERE period<=? "
        "GROUP BY period ORDER BY period DESC", (_cut,)).fetchall()
    if not periods:
        con.close()
        return {}, ""
    latest = periods[0][0]
    for pp, n in periods:
        if n >= 500:
            latest = pp
            break
    rows = con.execute(
        """SELECT code, period, roe, sq_net_yoy FROM finance_report
           WHERE period IN (SELECT DISTINCT period FROM finance_report WHERE period<=?
                            ORDER BY period DESC LIMIT 2)""", (_cut,)).fetchall()
    con.close()
    out = {}
    for code, period, roe, yoy in rows:
        d = out.setdefault(code, {})
        d[period] = {"roe": roe, "yoy": yoy}
    return out, latest


def _load_bars_panel(codes, days=300):
    """近 days 交易日 close/volume 面板 + 最新交易日"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    last = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
    ph = ",".join("?" * len(codes))
    df = pd.read_sql(
        f"SELECT date, code, close, volume FROM daily_bar WHERE adjust='qfq' "
        f"AND date<=? AND code IN ({ph}) AND close>0", con, params=(last, *codes))
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index().tail(days)
    v = df.pivot_table(index="date", columns="code", values="volume").reindex(p.index)
    return p, v, last


def _tech(codes, date=None):
    """技术状态：距52周高点/量比/距MA50（面板一次性算）"""
    px, vx, last = _load_bars_panel(codes)
    if px.empty:
        return {}
    close = px.astype(float)
    hi52 = close.rolling(250, min_periods=150).max()
    ma50 = close.rolling(50, min_periods=40).mean()
    vol20 = vx.rolling(20, min_periods=10).mean()
    vol60 = vx.rolling(60, min_periods=30).mean()
    out = {}
    for c in close.columns:
        last_c = close[c].iloc[-1]
        if pd.isna(last_c):
            continue
        h = hi52[c].iloc[-1]
        m50 = ma50[c].iloc[-1]
        vr = (vol20[c] / vol60[c]).iloc[-1] if vol60[c].iloc[-1] else None
        out[c] = {
            "price": round(float(last_c), 2),
            "dist_high_pct": round((last_c / h - 1) * 100, 1) if pd.notna(h) and h else None,
            "vol_ratio": round(float(vr), 2) if pd.notna(vr) else None,
            "above_ma50": bool(last_c > m50) if pd.notna(m50) else None,
        }
    return out


def _hist_holdout(code, years=1):
    """买入持有历史回测（含择时）：2020-2025 每季初买入持有 N 年
    返回 {avg, med, win, n, bench_avg, with_timing_avg}（收益 %）
    择时修正：持有期月收益 × 动态择时月度仓位（dynamic_regime.json）"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code=? AND adjust='qfq' ORDER BY date", (code,)).fetchall()
    con.close()
    if len(rows) < 500:
        return None
    px = pd.Series({d: float(c) for d, c in rows})
    px.index = pd.to_datetime(px.index)
    m = px.resample("ME").last()
    # 基准沪深300
    con = sqlite3.connect(str(CACHE / "bars.db"))
    rows3 = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
    con.close()
    bx = pd.Series({d: float(c) for d, c in rows3})
    bx.index = pd.to_datetime(bx.index)
    bm = bx.resample("ME").last()
    # 动态择时仓位（月度 pos，calendar+投票；无则全仓）
    dyn = {}
    dp = BASE / "output" / "dynamic_regime.json"
    if dp.exists():
        dyn = {k: float(v.get("pos", 1.0)) for k, v in json.loads(dp.read_text(encoding="utf-8")).items()}
    months = m.index
    starts = [d for d in months if (d.month in (3, 6, 9, 12) or d.month == 1) and 2020 <= d.year <= 2026 - years]
    res = []
    for s in starts:
        e = s + pd.DateOffset(years=years)
        if e not in m.index and e > months[-1]:
            continue
        if e in m.index:
            r = m.loc[e] / m.loc[s] - 1
            rb = bm.loc[e] / bm.loc[s] - 1 if e in bm.index else np.nan
        else:
            continue
        if pd.isna(r):
            continue
        # 择时修正：持有期逐月 pos 乘积（简化：月收益×pos 复利）
        seg = m.loc[s:e]
        cum = 1.0
        for i in range(len(seg) - 1):
            mr = seg.iloc[i + 1] / seg.iloc[i] - 1
            pos = dyn.get(seg.index[i].strftime("%Y-%m"), 1.0)
            cum *= 1 + mr * pos
        res.append({"r": r * 100, "rb": rb * 100 if not pd.isna(rb) else None,
                    "rt": (cum - 1) * 100})
    if len(res) < 3:
        return None
    df = pd.DataFrame(res)
    out = {
        "n": len(df),
        "avg": round(df["r"].mean(), 1),
        "med": round(df["r"].median(), 1),
        "win": round(100 * (df["r"] > 0).mean()),
        "bench_avg": round(df["rb"].mean(), 1) if df["rb"].notna().any() else None,
        "timing_avg": round(df["rt"].mean(), 1),
        "excess_avg": round(df["r"].mean() - df["rb"].mean(), 1) if df["rb"].notna().any() else None,
    }
    return out


def build_pitch(n=MAX_N, force=False) -> dict:
    """主流程：底座池 → 筛选 → Top N pitch"""
    pool = _load_pool()
    if not pool:
        return {"error": "底座池未生成（先跑 strategy/base_pool.py）", "picks": []}
    fin, latest = _load_finance()
    basic = load_basic()
    codes6 = [c.split(".")[0] for c in pool]
    fin6 = {c6: v for c6, v in fin.items() if c6 in set(codes6)}
    # 候选：成长 + 质量（F-Score 就绪则加分；未就绪用 ROE 门槛）
    cand = []
    for c6, recs in fin6.items():
        if latest not in recs:
            continue
        cur = recs[latest]
        yoy = cur.get("yoy")
        roe = cur.get("roe")
        if yoy is None or float(yoy) < MIN_YOY:
            continue
        if roe is None or float(roe) < MIN_ROE_ANN:
            continue
        prev = [r for p, r in recs.items() if p != latest]
        prev_yoy = max((float(r.get("yoy") or 0) for r in prev), default=0)
        if prev_yoy <= 0:
            continue
        cand.append({"code6": c6, "yoy": float(yoy), "roe": float(roe),
                     "prev_yoy": prev_yoy})
    if not cand:
        return {"error": f"底座池 {len(pool)} 只中无候选通过成长+质量门槛（yoy≥{MIN_YOY}% & ROE≥{MIN_ROE_ANN:.0f}%）", "picks": []}
    # 技术状态 + F-Score
    codes = [c6 + (".SH" if c6[:2] in ("60", "68") else ".SZ") for c6 in [x["code6"] for x in cand]]
    tech = _tech(codes)
    try:
        from factors.fscore import fscore_batch
        fscores = fscore_batch(codes)
    except Exception:
        fscores = {}
    # 打分：质量 40%（F-Score 归一）+ 成长 30%（yoy 分位）+ 时机 30%（回撤位置）
    scored = []
    for x in cand:
        c = x["code6"] + (".SH" if x["code6"][:2] in ("60", "68") else ".SZ")
        t = tech.get(c, {})
        dist = t.get("dist_high_pct")
        # ★dist_high_pct 为负值（-28.9 = 回撤 28.9%）：区间按 |回撤| ∈ [10, 40] 判断
        if dist is None or not (DIST_RANGE[0] <= abs(dist) <= DIST_RANGE[1]):
            continue  # 时机不符：追高（<10%）或趋势破坏（>40%）
        fs = fscores.get(c, {})
        fv = fs.get("score")
        # 质量分：F-Score 可得 → /7*10；否则 ROE 分位近似
        if fv is not None and fs.get("n_available", 0) >= 5:
            q_score = min(fv / 7 * 10, 10)
        else:
            q_score = min(x["roe"] / 25, 10) if x["roe"] else 5
        g_score = min(x["yoy"] / 100, 10)
        t_score = 10 * (1 - abs(abs(dist) - 25) / 15)  # 回撤 25% 最佳
        score = q_score * 0.4 + g_score * 0.3 + max(t_score, 0) * 0.3
        scored.append({**x, "code": c, "tech": t, "fscore": fs,
                       "score": round(score, 1), "q": round(q_score, 1),
                       "g": round(g_score, 1), "tm": round(t_score, 1)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    picks = scored[:n]
    # 逐只历史回测（含择时）1/2/3 年
    for p in picks:
        p["hist"] = {}
        for yrs in (1, 2, 3):
            h = _hist_holdout(p["code"], yrs)
            if h:
                p["hist"][f"{yrs}y"] = h
    # 组装
    out = {
        "date": tech.get("__last__", datetime.now().strftime("%Y-%m-%d")) if False else datetime.now().strftime("%Y-%m-%d"),
        "pool_size": len(pool),
        "cand_n": len(cand),
        "latest_period": latest,
        "status": "pending",          # 待用户审批
        "picks": picks,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return out


def approve(code, action="approve"):
    """审批：approve/reject → 更新 pitch_report.json 状态"""
    if not OUT_JSON.exists():
        print("无 pitch 报告")
        return 1
    d = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    changed = False
    for p in d.get("picks", []):
        if p.get("code") == code:
            p["approval"] = action
            p["approval_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True
    if changed:
        OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{code} → {action}")
    else:
        print(f"{code} 不在本批 pitch 中")
    return 0


def write_md(d: dict):
    if d.get("error"):
        OUT_MD.write_text(f"# Pitch 报告（{d.get('generated_at', '')}）\n\n**{d['error']}**\n", encoding="utf-8")
        return
    lines = [
        f"# 📌 Pitch 报告 · {d['date']}", "",
        f"- 底座池 {d['pool_size']} 只（统计学意义合格池）→ 候选 {d['cand_n']} 只 → 精选 {len(d['picks'])} 只",
        f"- 报告期 {d['latest_period']} · 生成 {d['generated_at']} · **状态：待你审批**（--approve/--reject）",
        "- 买入条件（缺一不可）：质量（F-Score≥5 或 ROE≥10%）· 成长（单季净利同比≥25% 且连续 2 期正）· 时机（距52周高点回撤 10-40%）· 择时窗口",
        "",
    ]
    for i, p in enumerate(d["picks"], 1):
        t = p.get("tech", {})
        fs = p.get("fscore", {})
        lines += [
            f"## {i}. {p['code']}（评分 {p['score']}：质量{p['q']} 成长{p['g']} 时机{p['tm']}）", "",
            f"- 成长：单季净利同比 **{p['yoy']:.0f}%**（上期 {p['prev_yoy']:.0f}%）· 年化 ROE {p['roe']:.0f}%"
            f" · F-Score {fs.get('score', '—')}/{fs.get('n_available', 0)}（期 {fs.get('period', '—')}）",
            f"- 时机：现价 {t.get('price')} · 距52周高点 {t.get('dist_high_pct')}% · 量比 {t.get('vol_ratio')}"
            f" · {'站上' if t.get('above_ma50') else '跌破'} MA50",
        ]
        if p.get("hist"):
            lines.append("- **历史回测（含择时）**：")
            for k, h in p["hist"].items():
                lines.append(
                    f"  - 持有 {k}：均值 {h['avg']:+.1f}%（中位 {h['med']:+.1f}% · 胜率 {h['win']:.0f}% · {h['n']} 次窗口）"
                    f" vs 沪深300 {h['bench_avg']:+.1f}% → **超额 {h['excess_avg']:+.1f}pp**"
                    f" · 择时修正后 {h['timing_avg']:+.1f}%")
        lines += ["", "---", ""]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"pitch 报告已生成：{OUT_MD}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=MAX_N)
    ap.add_argument("--force", action="store_true", help="忽略择时窗口强制 pitch（测试用）")
    ap.add_argument("--approve", default=None, help="审批买入某代码")
    ap.add_argument("--reject", default=None)
    args = ap.parse_args()
    if args.approve:
        return approve(args.approve, "approve")
    if args.reject:
        return approve(args.reject, "reject")
    # 择时窗口：防守档（现金≥50%）暂停 pitch
    if not args.force:
        try:
            from strategy.equal_weight_timing import regime_cash
            cash = regime_cash(datetime.now().strftime("%Y-%m-%d"))
            if cash >= 0.5:
                print(f"防守档（现金 {cash:.0%}）→ 暂停 pitch（--force 可跳过）")
                OUT_JSON.write_text(json.dumps(
                    {"date": datetime.now().strftime("%Y-%m-%d"), "status": "halted",
                     "reason": f"防守档现金 {cash:.0%}，只减不加", "picks": []},
                    ensure_ascii=False, indent=1), encoding="utf-8")
                OUT_MD.write_text("# Pitch 报告\n\n**防守档：只减不加，暂停 pitch。**\n", encoding="utf-8")
                return 0
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/pitch.py:353 main")
    d = build_pitch(n=args.n, force=args.force)
    OUT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(d)
    if not d.get("error"):
        for p in d["picks"]:
            print(f"  #{p['score']:.1f} {p['code']} yoy {p['yoy']:.0f}% ROE {p['roe']:.0f}% "
                  f"距高点 {p.get('tech', {}).get('dist_high_pct')}%")
    else:
        print("  ", d["error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
