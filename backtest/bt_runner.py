# -*- coding: utf-8 -*-
"""动态回测运行器（2026-08-14 用户：回测做成动态，按参数即时跑 + 前端 SVG 渲染）

提供 run_backtest(strategy, topn, stocks, start, end) → 指标 + 净值序列，
供 /api/live/backtest_run 调用（因子页「回测」Tab 动态跑）。

策略注册表（STRATEGIES，前端 /api/live/backtest_strategies 动态读取）：
  tech3      技术三因子（rps_120 反转 + lowvol_60 反转 + near_high_250 正向）· 月频
  script1    大市值三因子（营收增长率 + 市值 + Beta，高增长大市值高Beta）· 月频
  turn_low   低换手防御（20 日均换手截面最低 TopN，40 交易日调仓）· 防守主方案
  factor_all 因子全量回测（外包因子池技术面因子逐个 top10% → 归档）· 批处理

性能：价格面板 + 财务/市值数据按 key 缓存，重复跑不重载（首次 ~5-15s → 后续 ~1-2s）。
"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import DailyCache

CACHE = r"data\cache"
# 缓存（动态回测重复跑用）
_PANEL = {"key": None, "closes": None}
_FIN = None
_MV = None

# ★2026-08-16 策略注册表（前端菜单 = 本表动态生成；新增策略在此登记 + 在 run_backtest 分发）
STRATEGIES = {
    "tech3": {
        "name": "技术三因子", "category": "策略", "instant": True,
        "desc": "rps_120 反转 + lowvol_60 反转 + near_high_250 正向，月频 TopN 等权，T+1 开盘执行",
        "factors": ["rps_120", "lowvol_60", "near_high_250"],
        "defaults": {"topn": 5, "stocks": 300},
        "rebalance": "M",
    },
    "script1": {
        "name": "大市值三因子", "category": "复刻", "instant": True,
        "desc": "营收增长率 + 市值 + Beta（高增长大市值高Beta），月频 TopN 等权，T+1 开盘执行",
        "factors": ["营收增长率", "市值", "Beta"],
        "defaults": {"topn": 5, "stocks": 300},
        "rebalance": "M",
    },
    "turn_low": {
        "name": "低换手防御", "category": "策略", "instant": True,
        "desc": "20 日均换手截面最低 TopN 等权，40 交易日调仓，T+1 执行，无止损无择时（防守主方案）",
        "factors": ["turn_20d"],
        "defaults": {"topn": 20, "stocks": 300},
        "rebalance": 40,
    },
    "factor_all": {
        "name": "因子全量回测", "category": "因子", "instant": False,
        "desc": "外包因子池技术面因子逐个 top10% 季度调仓全量回测（批处理 ~1-3 分钟，结果入归档）",
        "factors": ["tech 因子族"],
        "defaults": {"topn": 10, "stocks": 300},
        "rebalance": "Q",
    },
}


def list_strategies() -> dict:
    """策略目录（前端菜单/筛选/选择用）——先合并外部注册（config/strategies.yaml）"""
    _load_external_strategies()
    return {k: {**v, "id": k} for k, v in STRATEGIES.items()}


# ★2026-08-16 动态化铁律：策略外部注册（config/strategies.yaml extra 合并进注册表，同名覆盖）
_EXT_LOADED = False


def _load_external_strategies():
    """读 config/strategies.yaml 的 extra 段 → 合并进 STRATEGIES（新增策略 = 改配置，不改代码）。
    策略类型：
      factor_list: [{name, sign}] 声明式因子组合（FACTOR_FUNCS 动态合成评分）
      scorer: 内置评分函数名（tech3/script1/turn_low）
    """
    global _EXT_LOADED
    if _EXT_LOADED:
        return
    _EXT_LOADED = True
    try:
        import yaml as _yaml
        cfg_path = BASE / "config" / "strategies.yaml"
        if not cfg_path.exists():
            return
        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for k, v in (cfg.get("extra") or {}).items():
            if isinstance(v, dict):
                STRATEGIES[str(k)] = {**v, "external": True}
    except Exception as _e:
        print(f"[bt_runner] strategies.yaml 加载失败（用内置注册表）: {_e}")


def _compose_score(closes, factor_list):
    """声明式因子组合评分：factor_list=[{name, sign}] → 各因子方向化 rank 均值（score 越大越好）"""
    from factors.factor_engine import FACTOR_FUNCS
    s = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for f in factor_list or []:
        name = f.get("name")
        sign = float(f.get("sign", 1) or 1)
        fn = FACTOR_FUNCS.get(name)
        if not fn:
            continue
        s = s + (fn(closes.astype(float)) * sign).rank(axis=1, pct=True)
    return s / max(len(factor_list or []), 1)


def _q(sql, db, params=()):
    # ★2026-11-11：显式检查数据文件，避免 script1 在缺库时抛裸 OperationalError 中断进程
    p = Path(str(db)).resolve()
    if not p.exists():
        raise FileNotFoundError(f"数据文件不存在: {p}")
    con = sqlite3.connect(f"{p.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _load_pool(stocks, start, end=None, min_rows=150):
    """★2026-11-26 PIT 候选全集：用 start 之前最近一个月数据选 Top stocks*3。

    原实现固定 month='2020-12' 却回测 2020-2025 —— look-ahead + 幸存者偏差。
    本函数只用 start 之前可知的数据，返回候选全集（每期再由 _active_codes 动态重选）。
    min_rows 参数化便于测试（临时库造不出 1000 行）。
    """
    d = pd.Timestamp(str(start))
    pm = (d.replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m")
    lo = (d - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    hi = (d - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # 优先 hist_mv.db（真实市值，PIT 月份从 start 推导）
    rows = []
    try:
        rows = _q("SELECT code, circ_mv FROM hist_mv WHERE month=?",
                  f"{CACHE}/hist_mv.db", (pm,))
    except Exception:
        rows = []
    if not rows:
        # 成交额代理：start 前 30 天；归一 tushare 千元→元（与 cache.normalize_units 同口径）
        try:
            rows = _q(
                "SELECT code, AVG(CASE WHEN source IN ('tushare','tushare_backup') "
                "THEN amount*1000 ELSE amount END) AS a FROM daily_bar "
                "WHERE adjust='qfq' AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%' "
                "AND date>=? AND date<=? AND amount IS NOT NULL AND amount>0 "
                "GROUP BY code ORDER BY a DESC", f"{CACHE}/bars.db", (lo, hi))
        except Exception:
            rows = []
    codes = [r[0] for r in sorted(rows, key=lambda r: -(r[1] or 0))[:stocks * 3]]
    if not codes:
        return []
    ph = ",".join("?" * len(codes))
    # ★数据充足性用 start 之前的 400 天历史窗口判断（PIT，且与回测区间长度无关）；
    #   原用回测区间导致短区间（如 6 个月 ~120 行）永远达不到 1000 行 → 选池为空。
    lb = (d - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    sql = f"SELECT code FROM daily_bar WHERE code IN ({ph}) AND date>=? AND date<? "
    args = list(codes) + [lb, str(start)[:10]]
    sql += "GROUP BY code HAVING COUNT(*)>=?"
    args.append(int(min_rows))
    try:
        ok = set(r[0] for r in _q(sql, f"{CACHE}/bars.db", args))
    except Exception:
        ok = set()
    return [c for c in codes if c in ok][:stocks * 3]


def _load_prices(codes, start, end, warmup_days=400):
    """加载 close/open/high/amount 四面板（T+1 open 执行 + 成交额动态选池）。

    ★2026-11-26：加载起点 = start - warmup_days（near_high_250 需约 250 交易日），
    因子有足够历史；最终收益仍由 run_backtest 按 start 截断。
    """
    from data.cache import normalize_units
    load_start = (pd.Timestamp(str(start)) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    cache = DailyCache()
    batch = cache.get_daily_batch(codes, start=load_start, end=end, adjust="qfq",
                                  fields=["close", "open", "high", "amount", "source"])
    series = {}
    for c, df in batch.items():
        if len(df) < 250:
            continue
        df = normalize_units(df).set_index("date").sort_index()
        for col in ("close", "open", "high", "amount"):
            series.setdefault(col, {})[c] = df[col]
    calendar = [r[0] for r in _q(
        "SELECT DISTINCT date FROM daily_bar WHERE date>=? AND date<=? ORDER BY date",
        f"{CACHE}/bars.db", (load_start, str(end)[:10]))]
    out = {}
    for col in ("close", "open", "high", "amount"):
        # ★2026-11-26：批量读取无数据时返回空面板，不再 KeyError（健壮性）
        if not series.get(col):
            out[col] = pd.DataFrame(index=calendar)
            continue
        frame = pd.DataFrame({c: series[col][c].reindex(calendar) for c in series[col]})
        # 停牌日成交额应为 0（不能 ffill 旧成交额，否则退市/停牌股仍被动态池选中）
        out[col] = frame.fillna(0.0) if col == "amount" else frame.ffill()
    return out


def _active_codes(amount, pos, universe, pool_size):
    """★2026-11-26 动态选池：过去 20 日平均成交额 Top pool_size（仅用 pos 及之前数据，PIT）。"""
    if amount is None or not pool_size:
        return list(universe)
    a = amount.iloc[max(0, pos - 20):pos + 1].mean().reindex(universe).dropna()
    return a.nlargest(min(int(pool_size), len(a))).index.tolist()


def _get_panel(stocks, start, end):
    key = (stocks, start, end)
    if _PANEL["key"] != key:
        codes = _load_pool(stocks, start, end)
        _PANEL["key"] = key
        _PANEL["data"] = _load_prices(codes, start, end)
    return _PANEL["data"]


def _load_fin():
    global _FIN
    if _FIN is None:
        try:
            rows = _q("SELECT code, end_date, ann_date, total_revenue, n_income FROM financials_ts",
                      f"{CACHE}/finance_ts.db")
        except Exception as e:
            print(f"[bt_runner] finance_ts.db 不可用（{e}）→ script1 回退 tech3")
            _FIN = pd.DataFrame()
            return _FIN
        fin = pd.DataFrame(rows, columns=["code", "end_date", "ann_date", "total_revenue", "n_income"])
        fin["code6"] = fin["code"].str[:6]
        fin["end"] = pd.to_datetime(fin["end_date"])
        fin["ann"] = pd.to_datetime(fin["ann_date"])
        fin = fin.sort_values(["code6", "end"])
        fin["rev_yoy"] = fin.groupby("code6")["total_revenue"].transform(lambda s: s / s.shift(4) - 1)
        _FIN = fin
    return _FIN


def _load_mv():
    global _MV
    if _MV is None:
        try:
            rows = _q("SELECT month, code, circ_mv FROM hist_mv WHERE month>='2020-06'", f"{CACHE}/hist_mv.db")
        except Exception as e:
            print(f"[bt_runner] hist_mv.db 不可用（{e}）→ script1 回退 tech3")
            _MV = pd.DataFrame()
            return _MV
        _MV = pd.DataFrame(rows, columns=["month", "code", "circ_mv"]).pivot_table(
            index="month", columns="code", values="circ_mv")
    return _MV


def _compute_beta(closes, window=60):
    ret = closes.pct_change()
    mkt = ret.mean(axis=1)
    rm = ret.rolling(window).mean()
    mm = mkt.rolling(window).mean()
    cov = (ret.sub(rm, axis=0).mul((mkt - mm), axis=0)).rolling(window).mean()
    var = (mkt - mm).pow(2).rolling(window).mean()
    return cov.div(var.replace(0, np.nan), axis=0)


def _tech3_score(closes):
    """技术三因子：rank 越大越好"""
    from factors.factor_engine import FACTOR_FUNCS
    s = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, sign in [("rps_120", -1), ("lowvol_60", -1), ("near_high_250", 1)]:
        s = s + (FACTOR_FUNCS[name](closes.astype(float)) * sign).rank(axis=1, pct=True)
    return s / 3


def _script1_score(closes):
    """脚本1 三因子（营收增长率 + 市值 + Beta）：只算月末，返回 rank 越大越好"""
    beta = _compute_beta(closes)
    fin = _load_fin()
    mdf = _load_mv()
    # ★2026-11-11：缺库时返回 None（由 run_backtest 回退 tech3 并给出明确提示），不崩溃
    if fin is None or fin.empty or mdf is None or mdf.empty:
        return None
    codes6 = [c.split(".")[0] for c in closes.columns]
    score = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    ym = closes.index.astype(str).str[:7]
    month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    for me in month_ends:
        pos = closes.index.get_loc(me)
        if pos < 60:
            continue
        me_dt = pd.to_datetime(me)
        b = beta.iloc[pos].dropna()
        latest = fin[fin["ann"] <= me_dt].sort_values("end").groupby("code6").tail(1)
        rmap = dict(zip(latest["code6"], latest["rev_yoy"]))
        mrow = mdf.reindex([me_dt.strftime("%Y-%m")])
        mcap = mrow.iloc[0].dropna() if len(mrow) else pd.Series(dtype=float)
        common = [c for c in closes.columns if c in b.index]
        rev_s = pd.Series({c: rmap.get(c6, np.nan) for c, c6 in zip(closes.columns, codes6)})
        rr = rev_s[common].rank(ascending=False, method="min")
        cr = pd.Series({c: mcap.get(c, np.nan) for c in common}).rank(ascending=False, method="min")
        br = b[common].rank(ascending=False, method="min")
        score.loc[me, common] = -((rr + cr + br) / 3)   # 负号 → rank 越大越好
    return score


def _turn_low_score(closes):
    """低换手防御：20 日均换手截面 rank 取反（低换手=高分），score 越大越好。
    数据：bars.db turn 列（2019 起覆盖 90%+；2019 前缺失，回测窗口从 2019-04 起）。"""
    codes = closes.columns.tolist()
    ph = ",".join("?" * len(codes))
    rows = _q(
        "SELECT date, code, turn FROM daily_bar WHERE code IN ({}) AND turn IS NOT NULL AND date>=?".format(ph),
        f"{CACHE}/bars.db", codes + [str(closes.index[0])])
    if not rows:
        return pd.DataFrame(0.5, index=closes.index, columns=codes)
    t = pd.DataFrame(rows, columns=["date", "code", "turn"]).pivot_table(
        index="date", columns="code", values="turn", aggfunc="last")
    t20 = t.rolling(20, min_periods=20).mean()
    r = t20.rank(axis=1, pct=True)                 # 小 = 低换手
    r = r.reindex(index=closes.index, columns=codes).ffill().fillna(0.5)
    return -r                                      # 取反 → 低换手高分（score 越大越好）


def _monthly_backtest(closes, opens, highs, score, topn):
    """月度调仓 Top N 等权（score 越大越好），★T+1 open 买入 + 一字板涨停过滤，返回日收益序列"""
    return _period_backtest(closes, opens, highs, score, topn, rebalance="M")


def _period_backtest(closes, opens, highs, score, topn, rebalance="M",
                     amount=None, pool_size=None):
    """泛化调仓回测：rebalance='M' 月频 / 40 等交易日数 / 'Q' 季度。
    ★T+1 open 买入 + 一字板涨停过滤（open≈high 买不进），返回日收益序列。
    ★2026-11-26：每期用 amount 动态重选流动性池（PIT），再按 score 选 TopN。"""
    if isinstance(rebalance, int):
        # 固定交易日调仓：每 rebalance 个交易日选一次（跳过前 60 日热身）
        positions = list(range(60, len(closes.index), rebalance))
        if positions and positions[-1] < len(closes.index) - 1:
            positions.append(len(closes.index) - 1)
        ret = pd.Series(0.0, index=closes.index)
        for i, pos in enumerate(positions[:-1]):
            nxt_pos = positions[i + 1]
            sc = score.iloc[pos].reindex(
                _active_codes(amount, pos, closes.columns, pool_size)).dropna()
            if len(sc) < topn:
                continue
            picks = sc.nlargest(topn).index.tolist()
            buy_pos = pos + 1
            if buy_pos > nxt_pos:
                continue
            bo = opens.iloc[buy_pos].reindex(picks)
            bh = highs.iloc[buy_pos].reindex(picks)
            bc = closes.iloc[buy_pos].reindex(picks)
            # ★2026-11-10 修复：原 `open==high` 判一字板，实测 327 只中 322 只并非涨停（误杀 5.8%）。
            #   真正一字板 = 开=高=收（全天单一定价）；开盘即最高但盘中回落可成交。
            _bb = pd.DataFrame({"o": bo, "h": bh, "c": bc}).dropna()
            buyable = _bb.index[~((_bb["o"] >= _bb["h"] - 1e-9) & (_bb["o"] == _bb["c"]))]
            if len(buyable) < 1:
                continue
            seg = closes[buyable].iloc[buy_pos: nxt_pos + 1].pct_change().fillna(0)
            seg.iloc[0] = (closes.iloc[buy_pos][buyable] / opens.iloc[buy_pos][buyable] - 1).values
            if len(seg):
                ret.loc[seg.index] = seg.mean(axis=1)
        return ret
    ym = closes.index.astype(str).str[:7]
    if rebalance == "Q":
        qm = pd.Series(closes.index).dt.to_period("Q").astype(str)
        period_ends = pd.Series(closes.index).groupby(qm.values).max().tolist()
    else:
        period_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    ret = pd.Series(0.0, index=closes.index)
    for i, me in enumerate(period_ends):
        pos = closes.index.get_loc(me)
        if pos < 60:
            continue
        sc = score.iloc[pos].reindex(
            _active_codes(amount, pos, closes.columns, pool_size)).dropna()
        if len(sc) < topn:
            continue
        picks = sc.nlargest(topn).index.tolist()
        nxt = period_ends[i + 1] if i + 1 < len(period_ends) else closes.index[-1]
        nxt_pos = closes.index.get_loc(nxt) if nxt in closes.index else len(closes) - 1
        buy_pos = pos + 1
        if buy_pos > nxt_pos:
            continue
        # T+1 open 买入 + 一字板涨停过滤（open≈high 买不进）
        bo = opens.iloc[buy_pos].reindex(picks)
        bh = highs.iloc[buy_pos].reindex(picks)
        buyable = bo[(bo < bh - 1e-9)].index
        if len(buyable) < 1:
            continue
        seg = closes[buyable].iloc[buy_pos: nxt_pos + 1].pct_change().fillna(0)
        # 首日收益 = close[buy_pos]/open[buy_pos] - 1（open 买入）
        seg.iloc[0] = (closes.iloc[buy_pos][buyable] / opens.iloc[buy_pos][buyable] - 1).values
        if len(seg):
            ret.loc[seg.index] = seg.mean(axis=1)
    return ret


def run_backtest(strategy="tech3", topn=5, stocks=300, start="2021-01-01", end="2025-12-31"):
    """运行回测 → {metrics, dates, nav, bench_nav, params, elapsed_s}。
    ★每次运行自动存档：历史 JSON（时间戳）+ 当前最新（latest_{key}，同参数覆盖）。
    ★2026-08-16：按 STRATEGIES 注册表分发（tech3/script1/turn_low 即时；factor_all 批处理）。"""
    from backtest.bt_report import archive, compute_metrics, save_latest
    _load_external_strategies()   # ★动态化：合并外部策略注册（config/strategies.yaml）
    meta = STRATEGIES.get(strategy) or STRATEGIES["tech3"]

    # ── factor_all：批处理（调用 backtest_all_factors.main，结果已归档到 output/backtest_archive/）──
    if strategy == "factor_all":
        t0 = time.time()
        py = sys.executable
        try:
            subprocess.run([py, str(BASE / "backtest" / "backtest_all_factors.py")],
                           cwd=str(BASE), timeout=600)
            return {
                "metrics": {"annual_return": None, "sharpe": None, "max_drawdown": None, "n_days": 0},
                "params": {"strategy": strategy, "topn": topn, "stocks": stocks, "start": start, "end": end},
                "elapsed_s": round(time.time() - t0, 2),
                "archived": True, "batch": True,
                "note": "因子全量回测已完成，结果见下方「历史归档」（category=因子）",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "因子全量回测超时（>10 分钟）", "params": {"strategy": strategy}}

    t0 = time.time()
    panel = _get_panel(stocks, start, end)
    closes, opens, highs = panel["close"], panel["open"], panel["high"]
    amount = panel.get("amount")   # ★2026-11-26：成交额面板（动态选池用）
    # ★动态化：外部策略评分分发（factor_list 声明式组合 / scorer 内置函数 / 内置策略）
    if meta.get("factor_list"):
        score = _compose_score(closes, meta["factor_list"])
    elif meta.get("scorer") == "script1":
        score = _script1_score(closes)
        if score is None:
            return {"ok": False, "error": "script1 依赖 finance_ts.db / hist_mv.db（当前缺失）。"
                                           "请先运行 data/build_factor_backtest_db.py 与 data/build_hist_mv.py，或改用 --strategy tech3"}
    elif meta.get("scorer") == "turn_low" or (meta.get("external") and meta.get("factors") == ["turn_20d"]):
        score = _turn_low_score(closes)
    elif strategy == "script1":
        score = _script1_score(closes)
        if score is None:
            return {"ok": False, "error": "script1 依赖 finance_ts.db / hist_mv.db（当前缺失）。"
                                           "请先运行 data/build_factor_backtest_db.py 与 data/build_hist_mv.py，或改用 --strategy tech3"}
    elif strategy == "turn_low":
        score = _turn_low_score(closes)
    else:
        score = _tech3_score(closes)
    ret = _period_backtest(closes, opens, highs, score, topn,
                           rebalance=meta.get("rebalance", "M"),
                           amount=amount, pool_size=stocks)
    ret = ret.loc[ret.index.astype(str) >= start]
    # ★2026-11-10 修复：原按写死"全期 60 次调仓"摊销成本，与实际调仓频率无关
    #   （季度策略被年化多扣 6+ 个百分点）。按 rebalance 周期估算实际调仓次数。
    _reb = meta.get("rebalance", "M")
    if isinstance(_reb, str) and _reb.upper() == "Q":
        _per = 63
    elif isinstance(_reb, str) and _reb.upper() == "M":
        _per = 21
    elif isinstance(_reb, int) and _reb > 0:
        _per = _reb
    else:
        _per = 21
    # ★2026-11-26：统一成本口径，印花税仅卖出侧（原双边多扣一次印花税）
    from backtest.bt_cost import round_trip_cost
    cost = round_trip_cost()
    _n_reb = max(1, int(round(len(ret) / _per)))
    ret = ret - cost * _n_reb / max(len(ret), 1)
    bench = closes.pct_change().fillna(0).mean(axis=1).loc[ret.index]
    nav = (1 + ret).cumprod()
    bench_nav = (1 + bench).cumprod()
    metrics = compute_metrics(ret)
    bench_metrics = compute_metrics(bench)
    excess_nav = (nav / bench_nav)   # 超额净值（策略相对基准）
    # ★2026-11-26：空序列时 compute_metrics 返回 {}，用 .get 兜底避免 KeyError
    excess_annual = metrics.get("annual_return", 0) - bench_metrics.get("annual_return", 0)

    # 归档（每次运行都存档；latest 覆盖旧值，历史时间戳保留）
    n_stocks = int(closes.shape[1])
    key = f"{strategy}_t{topn}_s{n_stocks}_{start}_{end}".replace("-", "")
    title = f"{meta['name']}Top{topn}"
    category = meta["category"]
    verdict = "有效" if metrics["annual_return"] >= 0 else "无效"
    pfull = {"name": title, "strategy": strategy, "topn": topn, "stocks": n_stocks,
             "start": start, "end": end}
    slug = f"{strategy}_t{topn}_s{n_stocks}"
    archive(ret, params=pfull, benchmark=bench, name=slug, category=category,
            factors=meta.get("factors", []), verdict=verdict, save_html=False)   # 历史（JSON，轻量）
    save_latest(key, ret, params=pfull, benchmark=bench, category=category,
                factors=meta.get("factors", []), verdict=verdict)                 # 当前最新（覆盖）

    return {
        "metrics": metrics,
        "bench_metrics": {"annual_return": bench_metrics["annual_return"],
                          "sharpe": bench_metrics["sharpe"],
                          "max_drawdown": bench_metrics["max_drawdown"]},
        "excess_annual": excess_annual,
        "dates": [str(d)[:10] for d in nav.index],
        "nav": [round(float(v), 4) for v in nav.values],
        "bench_nav": [round(float(v), 4) for v in bench_nav.values],
        "excess_nav": [round(float(v), 4) for v in excess_nav.values],
        "params": {"strategy": strategy, "topn": topn, "stocks": n_stocks,
                   "start": start, "end": end},
        "elapsed_s": round(time.time() - t0, 2),
        "archived": True, "key": key,
    }


if __name__ == "__main__":
    # ★2026-11-11：加 argparse（原无参数解析，`--help` 被忽略并直接跑两策略写归档）
    import argparse
    ap = argparse.ArgumentParser(description="bt_runner 回测（默认只打印帮助；需显式 --strategy）")
    ap.add_argument("--strategy", default=None, help="tech3 / script1 / turn_low / factor_all / 外部注册策略")
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--stocks", type=int, default=300)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2025-12-31")
    args = ap.parse_args()
    if not args.strategy:
        ap.print_help()
        sys.exit(0)
    r = run_backtest(args.strategy, args.topn, args.stocks, args.start, args.end)
    if "metrics" not in r:
        print(f"[bt_runner] 回测失败：{r.get('error', r)}")
        sys.exit(1)
    m = r["metrics"]
    print(f"{args.strategy}: 年化 {m['annual_return']:.1%} 回撤 {m['max_drawdown']:.1%} "
          f"夏普 {m['sharpe']:.2f} 耗时 {r.get('elapsed_s')}s 样本 {m.get('n_days')} 天")
