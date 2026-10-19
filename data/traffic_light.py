# -*- coding: utf-8 -*-
"""data/traffic_light.py — 择时红绿灯（贪婪/观望/恐慌）· 均线金叉6/12 趋势信号（2026-08-14 v2）

信号源（★v2 从"回撤触发"换成"均线金叉6/12"领先型趋势）：
    沪深300 月末收盘 → 6月均线 MA6 vs 12月均线 MA12
    ratio = MA6/MA12 - 1
      greedy(绿) : ratio >  +1%   明确上升趋势 → 满仓
      wait(黄)   : -1% ≤ ratio ≤ +1%   均线纠缠/方向不明 → 半仓观望
      panic(红)  : ratio <  -1%   明确下降趋势 → 空仓/降杠杆

为何换：满仓主义大波段/回撤触发经 T+1 复核跑输满仓（回撤=滞后指标，卖在底部）；
        均线金叉6/12 是领先趋势信号，2019+ T+1 年化 +14.5% vs 满仓 +11.5%、回撤 -15.7% vs -26.7%、
        夏普 0.95 vs 0.62、7年仅 5 次调仓。定位=决策参考（降回撤/控风险），非收益增强α。

★T+1 执行（无前视）：月末信号 → 次月执行。
数据源：2005+ baostock 缓存 parquet；2019+ bars.db（主链每日刷新，保证实时）。
产出：output/traffic_light.json（当前信号 + T+1 回测 + 下降趋势周期 + 近24月历史）
用法：python data/traffic_light.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
BARS_DB = r"data/cache/bars.db"
PARQUET = BASE / "output" / "hs300_monthly.parquet"
EQ_PARQUET = BASE / "output" / "eq_monthly.parquet"
OUTPUT = BASE / "output" / "traffic_light.json"
REGIME_JSON = BASE / "output" / "dynamic_regime.json"   # ★仓位档（供 pool_layers/equal_weight_timing 消费）

# ---------------- 参数 ----------------
MA_FAST = 6           # 快线：6月均线
MA_SLOW = 12          # 慢线：12月均线
BAND = 0.01           # 观望死区：|MA6/MA12-1| <= 1% 视为方向不明

LABELS = {"greedy": "贪婪", "wait": "观望", "panic": "恐慌"}
COLORS = {"greedy": "green", "wait": "yellow", "panic": "red"}


def _ensure_parquet():
    # ★2026-10-15 加固：① 缓存超 7 天重拉（bars.db 无指数时这是唯一 2019+ 数据源）
    #   ② 空文件视为无效（baostock 登录/查询静默失败会写出 0 行 → 下游 IndexError）
    #   ③ baostock 失败 → akshare 兜底
    import time as _t
    if PARQUET.exists():
        try:
            if pd.read_parquet(PARQUET).shape[0] > 0 and _t.time() - PARQUET.stat().st_mtime < 7 * 86400:
                return
        except Exception:
            pass
    df = None
    try:
        import baostock as bs
        lg = bs.login()
        if getattr(lg, "error_code", "0") != "0" or getattr(lg, "error_msg", ""):
            raise RuntimeError(f"baostock 登录失败: {getattr(lg, 'error_msg', '')[:60]}")
        rs = bs.query_history_k_data_plus(
            "sh.000300", "date,close", start_date="2005-01-01", end_date="2026-12-31",
            frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if rows:
            df = pd.DataFrame(rows, columns=["date", "close"])
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = df["close"].astype(float)
            df = df.set_index("date")
    except Exception:
        df = None
    if df is None or df.empty:
        import os as _os
        _os.environ["NO_PROXY"] = "*"   # ★2026-10-15：系统代理拒连外部行情源 → 强制直连
        _os.environ["no_proxy"] = "*"
        # ★2026-10-15：eastmoney clist 接口本机被拒 → 改用新浪指数源（实测 2002→最新 6008 行）
        import akshare as ak
        h = ak.stock_zh_index_daily(symbol="sh000300")
        if h is None or h.empty:
            raise RuntimeError("沪深300 数据源（baostock/akshare-sina）均无数据")
        df = pd.DataFrame({"date": pd.to_datetime(h["date"]),
                           "close": h["close"].astype(float)}).set_index("date")
    df = df.dropna().sort_index()
    PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PARQUET)


def _bars_latest() -> tuple:
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True)
    row = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date DESC LIMIT 1").fetchone()
    con.close()
    return (row[0], float(row[1])) if row else (None, None)


def load_monthly() -> pd.Series:
    """沪深300 月末收盘：2005-2018 baostock 缓存 + 2019+ bars.db（每日刷新，保证实时）"""
    _ensure_parquet()
    parts = []
    if PARQUET.exists():
        pre = pd.read_parquet(PARQUET)["close"].resample("ME").last()
        pre = pre[pre.index < pd.Timestamp("2019-01-01")]
        parts.append(pre)
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True)
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' AND date>='2019-01-01' ORDER BY date").fetchall()
    con.close()
    if rows:
        d = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows}).sort_index()
        parts.append(d.resample("ME").last())
    elif PARQUET.exists():
        # ★2026-10-15 修复：bars.db 无指数行（bulk_loader 只下股票）→ 回退用 baostock 缓存
        #   的 2019+ 段（原逻辑直接漏掉 2019+ → 近月窗口空序列 → IndexError 崩溃）
        post = pd.read_parquet(PARQUET)["close"]
        post = post[post.index >= pd.Timestamp("2019-01-01")].resample("ME").last()
        parts.append(post)
    monthly = pd.concat(parts)
    monthly = monthly[~monthly.index.duplicated(keep="last")].sort_index()
    return monthly.astype(float)


def compute_signal(monthly: pd.Series):
    """月度信号：6月均线 MA6、12月均线 MA12、ratio=MA6/MA12-1"""
    ma_fast = monthly.rolling(MA_FAST, min_periods=MA_FAST // 2).mean()
    ma_slow = monthly.rolling(MA_SLOW, min_periods=MA_SLOW // 2).mean()
    ratio = ma_fast / ma_slow - 1
    return ma_fast, ma_slow, ratio


def run_state_machine(monthly, ratio):
    """三态（死区阈值）：ratio>+1% 贪婪 / <-1% 恐慌 / 之间观望；返回 states + 恐慌周期"""
    states = {}
    episodes = []
    cur_start = None
    for mo in monthly.index:
        r = ratio.get(mo, float("nan"))
        if pd.isna(r):
            states[mo] = "greedy"
            continue
        if r > BAND:
            st = "greedy"
        elif r < -BAND:
            st = "panic"
        else:
            st = "wait"
        states[mo] = st
        if st == "panic" and cur_start is None:
            cur_start = mo
        elif st != "panic" and cur_start is not None:
            episodes.append((cur_start, mo))
            cur_start = None
    if cur_start is not None:
        episodes.append((cur_start, monthly.index[-1]))
    return states, episodes


def load_eq() -> pd.Series:
    """全池等权月收益（bars.db qfq，2010+），缓存 parquet 加速"""
    if EQ_PARQUET.exists():
        return pd.read_parquet(EQ_PARQUET)["eq"]
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True)
    df = pd.read_sql("SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    m = p.resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    eq = m.pct_change().mean(axis=1).dropna()
    EQ_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"eq": eq}).to_parquet(EQ_PARQUET)
    return eq


def _perf(r: pd.Series):
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else 0
    return ann, mdd, sharpe, nav.iloc[-1]


_REGIME_LEVEL = {"greedy": (1.0, "full"), "wait": (0.5, "half"), "panic": (0.0, "exit")}


def build_regime() -> dict:
    """均线金叉6/12 → dynamic_regime.json 格式 {month: {pos, top, level}}（回测/信号同源）
    greedy→full(1.0) / wait→half(0.5) / panic→exit(0.0)，替代原"满仓主义大波段"回撤档"""
    monthly = load_monthly()
    _, _, ratio = compute_signal(monthly)
    states, _ = run_state_machine(monthly, ratio)
    out = {}
    for mo, st in states.items():
        pos, level = _REGIME_LEVEL[st]
        out[str(mo)[:7]] = {"pos": pos, "top": "均线金叉6/12", "level": level}
    return out


def build() -> dict:
    monthly = load_monthly()
    ma_fast, ma_slow, ratio = compute_signal(monthly)
    states, episodes = run_state_machine(monthly, ratio)
    eq = load_eq()          # 全池等权月收益（用户实际持仓宇宙）

    # T+1 回测：仓位 贪婪1.0/观望0.5/恐慌0.0，基准=全池等权买入持有
    pos_map = {"greedy": 1.0, "wait": 0.5, "panic": 0.0}
    states_s = {str(mo)[:7]: v for mo, v in states.items()}
    pos = pd.Series({mo: pos_map[states_s.get(mo, "greedy")] for mo in eq.index})

    def _win(since):
        eqw = eq[eq.index >= since]
        pw = pos.shift(1).fillna(1.0)[eq.index >= since]
        strat = (eqw * pw).dropna()
        a_s, d_s, sh_s, nav_s = _perf(strat)
        a_b, d_b, sh_b, nav_b = _perf(eqw.loc[strat.index])
        return {"annual": round(a_s * 100, 1), "bench_annual": round(a_b * 100, 1),
                "mdd": round(d_s * 100, 1), "bench_mdd": round(d_b * 100, 1),
                "sharpe": round(sh_s, 2), "bench_sharpe": round(sh_b, 2),
                "nav": round(nav_s, 2), "bench_nav": round(nav_b, 2)}

    bt_recent = _win("2019-08")
    bt_full = _win("2010-02")

    latest = monthly.index[-1]
    cur = states[latest]
    s = pd.Series(states)
    yrs = len(s) / 12
    _asof, _hs300 = _bars_latest()

    hist = {}
    for mo in list(states.keys())[-24:]:
        hist[str(mo)[:7]] = {
            "s": states[mo],
            "ratio": round(float(ratio.get(mo, 0)) * 100, 2),
            "ma6": round(float(ma_fast.get(mo, 0)), 0),
            "ma12": round(float(ma_slow.get(mo, 0)), 0),
        }

    # 最近一次金叉/死叉（乖离率穿越 0 线）
    last_cross = None
    prev_r = None
    for mo, r in ratio.items():
        if pd.isna(r) or prev_r is None:
            prev_r = r
            continue
        if prev_r <= 0 < r:
            last_cross = {"month": str(mo)[:7], "dir": "金叉", "to": "greedy"}
        elif prev_r >= 0 > r:
            last_cross = {"month": str(mo)[:7], "dir": "死叉", "to": "panic"}
        prev_r = r

    return {
        "ok": True,
        "schema_version": "2.0",
        "signal": "均线金叉6/12",
        "state": cur,
        "label": LABELS[cur],
        "color": COLORS[cur],
        "ratio": round(float(ratio.iloc[-1]) * 100, 2),
        "ma6": round(float(ma_fast.iloc[-1]), 1),
        "ma12": round(float(ma_slow.iloc[-1]), 1),
        "hs300": round(_hs300 if _hs300 else float(monthly.iloc[-1]), 2),
        "asof": str(_asof) if _asof else str(latest)[:10],
        "last_cross": last_cross,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"ma_fast": MA_FAST, "ma_slow": MA_SLOW, "band": BAND},
        "backtest": {
            "years": round(yrs, 1),
            "recent": bt_recent,       # 2019-08+（数据可靠、贴近当前）
            "full": bt_full,           # 2010-02+（含回填历史）
            "episodes": len(episodes),
            "greedy_pct": round(int((s == "greedy").sum()) / len(s) * 100),
            "wait_pct": round(int((s == "wait").sum()) / len(s) * 100),
            "panic_pct": round(int((s == "panic").sum()) / len(s) * 100),
        },
        "episodes": [
            {"start": str(st)[:7], "end": str(en)[:7],
             "months": (pd.Timestamp(en) - pd.Timestamp(st)).days // 30 + 1,
             "ratio": round(float(ratio.get(st, 0)) * 100, 1)}
            for st, en in episodes
        ],
        "history": hist,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # ★写 dynamic_regime.json（仓位档，替换回撤 regime，供 pool_layers/equal_weight_timing/pitch 消费）
    reg = build_regime()
    REGIME_JSON.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    cur = reg.get(str(out["asof"])[:7], {})
    print(f"[择时红绿灯·均线金叉6/12] 当前 {out['label']}({out['state']})  "
          f"MA6 {out['ma6']} / MA12 {out['ma12']}（ratio {out['ratio']}%）  →  {OUTPUT}")
    print(f"  当前仓位档: {cur.get('level', '?')} (pos {cur.get('pos', '?')})  →  {REGIME_JSON}")
    r = out["backtest"]["recent"]
    print(f"  T+1回测(2019+): 年化 {r['annual']}% vs 满仓 {r['bench_annual']}%  |  "
          f"回撤 {r['mdd']}% vs {r['bench_mdd']}%  |  夏普 {r['sharpe']} vs {r['bench_sharpe']}")
    print(f"  T+1回测(2010+): 年化 {out['backtest']['full']['annual']}% vs 满仓 {out['backtest']['full']['bench_annual']}%  |  "
          f"下降周期 {out['backtest']['episodes']} 次")


if __name__ == "__main__":
    main()
