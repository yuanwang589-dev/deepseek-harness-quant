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
import re
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


class MissingDataError(RuntimeError):
    """★2026-10-15：回测所需数据文件缺失——带定位信息与补数指引。

    原缺陷链：`_q()` 用 `mode=ro&immutable=1` 打开不存在的库 → sqlite3 抛裸
    `OperationalError("unable to open database file")` → 一路冒到
    `/api/live/backtest_run` → 500 + 满屏 traceback。
    关键问题：**traceback 里不会告诉你是哪个库、该跑什么命令补**，
    每次调用还会重刷一遍 traceback 把日志冲掉真正有用的信息。

    本类型让缺失库变成「可诊断的结构化结果」而不是「一次崩溃 + 一段噪音」。
    """
    def __init__(self, db: str, table: str = "", hint: str = ""):
        self.db, self.table, self.hint = str(db), table, hint
        super().__init__(f"回测所需数据缺失：{self.db} :: {table or '?'}。{hint}")


def _require_db(db_path: str, table: str, hint: str):
    """库文件不存在 → MissingDataError（带库路径/表名/补数指引）"""
    if not Path(db_path).exists():
        raise MissingDataError(db_path, table, hint)


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
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _load_pool(stocks):
    rows = _q("SELECT code, circ_mv FROM hist_mv WHERE month='2020-12'", f"{CACHE}/hist_mv.db")
    top = sorted(rows, key=lambda r: -(r[1] or 0))[:stocks * 3]
    codes = [r[0] for r in top]
    ph = ",".join("?" * len(codes))
    ok = set(r[0] for r in _q(
        f"SELECT code FROM daily_bar WHERE code IN ({ph}) AND date>='2020-06-01' AND date<='2025-12-31' "
        "GROUP BY code HAVING COUNT(*)>=1000", f"{CACHE}/bars.db", codes))
    return [c for c in codes if c in ok][:stocks]


def _load_prices(codes, start, end):
    """加载 close/open/high 面板（T+1 open 执行 + 涨停过滤用）"""
    cache = DailyCache()
    batch = cache.get_daily_batch(codes, start="2020-06-01", end=end, adjust="qfq",
                                  fields=["close", "open", "high"])
    close_s, open_s, high_s = {}, {}, {}
    for c, df in batch.items():
        if len(df) < 250:
            continue
        df = df.set_index("date").sort_index()
        close_s[c] = df["close"]
        open_s[c] = df["open"]
        high_s[c] = df["high"]
    calendar = [r[0] for r in _q(
        "SELECT DISTINCT date FROM daily_bar WHERE date>='2020-06-01' AND date<=? ORDER BY date",
        f"{CACHE}/bars.db", (end,))]
    return {
        "close": pd.DataFrame({c: close_s[c].reindex(calendar) for c in close_s}).ffill(),
        "open": pd.DataFrame({c: open_s[c].reindex(calendar) for c in open_s}).ffill(),
        "high": pd.DataFrame({c: high_s[c].reindex(calendar) for c in high_s}).ffill(),
    }


def _get_panel(stocks, start, end):
    key = (stocks, start, end)
    if _PANEL["key"] != key:
        codes = _load_pool(stocks)
        _PANEL["key"] = key
        _PANEL["data"] = _load_prices(codes, start, end)
    return _PANEL["data"]


def _load_fin():
    global _FIN
    if _FIN is None:
        _fdb = f"{CACHE}/finance_ts.db"
        # ★2026-10-15：先查后连。缺库时改为带指引的 MissingDataError，
        #   替代原先裸 OperationalError（见 MissingDataError 注释）。
        _require_db(_fdb, "financials_ts",
                    "补法：python data/build_unified_db.py（Tushare 三表 PIT 建库）。"
                    "补齐前 script1 策略不可用，请改用 tech3 或 turn_low。")
        rows = _q("SELECT code, end_date, ann_date, total_revenue, n_income FROM financials_ts",
                  _fdb)
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
        rows = _q("SELECT month, code, circ_mv FROM hist_mv WHERE month>='2020-06'", f"{CACHE}/hist_mv.db")
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
    fin = _load_fin()        # ★2026-10-15 前置：缺库立刻失败。
    beta = _compute_beta(closes)   # 原放在 _load_fin() 前，缺库时白算十几秒 beta 才报错
    mdf = _load_mv()
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


_RB_DAYS = {"D": 1, "W": 5, "M": 20, "Q": 60, "Y": 244}


def _rebalance_days(rb) -> int:
    """调仓周期 → 间隔交易日数。

    ★2026-10-15 新增：成本折算的地基。原实现把「60 次全换仓/年」写死在分母里，
    与 meta['rebalance'] 和回测窗口长度双双脱钩（详见 run_backtest 内成本注释）。
    """
    if isinstance(rb, int):
        return max(int(rb), 1)
    return _RB_DAYS.get(str(rb).upper(), 20)


def _monthly_backtest(closes, opens, highs, score, topn):
    """月度调仓 Top N 等权（score 越大越好），★T+1 open 买入 + 一字板涨停过滤，返回日收益序列"""
    return _period_backtest(closes, opens, highs, score, topn, rebalance="M")


def _period_backtest(closes, opens, highs, score, topn, rebalance="M"):
    """泛化调仓回测：rebalance='M' 月频 / 40 等交易日数 / 'Q' 季度。
    ★T+1 open 买入 + 一字板涨停过滤（open≈high 买不进），返回日收益序列。"""
    if isinstance(rebalance, int):
        # 固定交易日调仓：每 rebalance 个交易日选一次（跳过前 60 日热身）
        positions = list(range(60, len(closes.index), rebalance))
        if positions and positions[-1] < len(closes.index) - 1:
            positions.append(len(closes.index) - 1)
        ret = pd.Series(0.0, index=closes.index)
        for i, pos in enumerate(positions[:-1]):
            nxt_pos = positions[i + 1]
            sc = score.iloc[pos].dropna()
            if len(sc) < topn:
                continue
            picks = sc.nlargest(topn).index.tolist()
            buy_pos = pos + 1
            if buy_pos > nxt_pos:
                continue
            bo = opens.iloc[buy_pos].reindex(picks)
            bh = highs.iloc[buy_pos].reindex(picks)
            buyable = bo[(bo < bh - 1e-9)].index
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
        sc = score.iloc[pos].dropna()
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
    ★2026-08-16：按 STRATEGIES 注册表分发（tech3/script1/turn_low 即时；factor_all 批处理）。
    ★2026-10-19 安全修复：strategy 来自 HTTP 查询参数且会拼进存档文件名，
       原 `STRATEGIES.get(strategy) or STRATEGIES["tech3"]` 对未知值回落而非拒绝，
       `../../../../evil` 可把 JSON/HTML 写到 output/backtest_archive/ 之外（路径穿越任意写）。
       改为：入口白名单拒绝未知策略 + 文件名消毒。"""
    from backtest.bt_report import archive, compute_metrics, save_latest
    _load_external_strategies()   # ★动态化：合并外部策略注册（config/strategies.yaml）
    # 白名单：strategy 必须命中注册表，否则拒绝（不再静默回落到 tech3）
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy!r}（可选：{sorted(STRATEGIES)}）")
    meta = STRATEGIES[strategy]

    # ── factor_all：批处理（调用 backtest_all_factors.main，结果已归档到 output/backtest_archive/）──
    if strategy == "factor_all":
        t0 = time.time()
        py = sys.executable
        try:
            subprocess.run([py, str(BASE / "backtest" / "backtest_all_factors.py")],
                           cwd=str(BASE), timeout=600)
            return {
                "ok": True,
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
    # ★动态化：外部策略评分分发（factor_list 声明式组合 / scorer 内置函数 / 内置策略）
    try:
        if meta.get("factor_list"):
            score = _compose_score(closes, meta["factor_list"])
        elif meta.get("scorer") == "script1":
            score = _script1_score(closes)
        elif meta.get("scorer") == "turn_low" or (meta.get("external") and meta.get("factors") == ["turn_20d"]):
            score = _turn_low_score(closes)
        elif strategy == "script1":
            score = _script1_score(closes)
        elif strategy == "turn_low":
            score = _turn_low_score(closes)
        else:
            score = _tech3_score(closes)
    except MissingDataError as _mde:
        # ★2026-10-15：数据缺失 → 结构化返回（ok:False + missing_db + hint），
        #   前端可直接把补数指引显示给用户；不再抛异常、不再满屏 traceback 冲掉日志。
        return {"ok": False, "error": str(_mde), "missing_db": _mde.db,
                "missing_table": _mde.table, "hint": _mde.hint,
                "params": {"strategy": strategy, "topn": topn, "stocks": stocks,
                           "start": start, "end": end}}
    ret = _period_backtest(closes, opens, highs, score, topn, rebalance=meta.get("rebalance", "M"))
    ret = ret.loc[ret.index.astype(str) >= start]
    # ★2026-10-15 成本折算修正
    #   正确关系：一次「全换仓」摩擦 = 2×cost（卖旧+买新各一次）；
    #             回测窗口内全换仓次数 = len(ret)/调仓间隔(交易日)；
    #             → 日均拖累 = 2×cost/调仓间隔。
    #   原式 `cost * 2 * 60 / max(len(ret),1)` 把「60 次/年」写死，两处脱钩：
    #     ① 与回测窗口长度脱钩——只有恰好 5 年窗口才对，短窗口分母偏小→成本被高估；
    #     ② 与策略调仓频率脱钩——本条在 run_backtest 内对所有策略一刀切：
    #        turn_low（40 交易日调仓）真实拖累应为 2×cost/40，
    #        factor_all（季度）应为 2×cost/60，原式都按 60 次/年算 → 系统性高估 2~3 倍成本。
    cost = 0.00026 + 0.0005 + 0.001   # 佣金万2.6 + 印花税万5 + 滑点千1
    _rb_days = _rebalance_days(meta.get("rebalance", "M"))
    ret = ret - 2.0 * cost / _rb_days
    bench = closes.pct_change().fillna(0).mean(axis=1).loc[ret.index]
    nav = (1 + ret).cumprod()
    bench_nav = (1 + bench).cumprod()
    metrics = compute_metrics(ret)
    bench_metrics = compute_metrics(bench)
    excess_nav = (nav / bench_nav)   # 超额净值（策略相对基准）
    excess_annual = metrics["annual_return"] - bench_metrics["annual_return"]

    # 归档（每次运行都存档；latest 覆盖旧值，历史时间戳保留）
    n_stocks = int(closes.shape[1])
    # ★2026-10-19 安全修复：key/slug 会成为 output/backtest_archive/ 下的文件名。
    #   原实现仅 `.replace("-","")`（只删连字符，斜杠原样保留）→ 路径穿越任意文件写。
    #   现在做白名单消毒：只保留字母数字下划线连字符点，任何分隔符都会被剔除。
    _san = lambda s: re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))
    key = f"{_san(strategy)}_t{topn}_s{n_stocks}_{_san(start)}_{_san(end)}"
    title = f"{meta['name']}Top{topn}"
    category = meta["category"]
    verdict = "有效" if metrics["annual_return"] >= 0 else "无效"
    pfull = {"name": title, "strategy": strategy, "topn": topn, "stocks": n_stocks,
             "start": start, "end": end}
    slug = f"{_san(strategy)}_t{topn}_s{n_stocks}"
    archive(ret, params=pfull, benchmark=bench, name=slug, category=category,
            factors=meta.get("factors", []), verdict=verdict, save_html=False)   # 历史（JSON，轻量）
    save_latest(key, ret, params=pfull, benchmark=bench, category=category,
                factors=meta.get("factors", []), verdict=verdict)                 # 当前最新（覆盖）

    return {
        # ★2026-10-15：成功路径必须显式声明 ok:True。
        #   此前成功返回没有 ok 字段，而失败路径有 ok:False —— 调用方
        #   `if r.get("ok")` 对「成功」与「结构异常」无法区分（两者都取不到真值），
        #   导致 script1 跑通后 test_backtest_script1_runs 仍判失败、
        #   test_script1_returns_structured_failure 的 skip 守卫也不生效。
        "ok": True,
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
    r = run_backtest("tech3", 5, 300)
    m = r["metrics"]
    print(f"tech3: 年化 {m['annual_return']:.1%} 回撤 {m['max_drawdown']:.1%} 夏普 {m['sharpe']:.2f} "
          f"耗时 {r['elapsed_s']}s 样本 {m['n_days']} 天")
    r2 = run_backtest("script1", 5, 300)
    m2 = r2["metrics"]
    print(f"script1: 年化 {m2['annual_return']:.1%} 回撤 {m2['max_drawdown']:.1%} 夏普 {m2['sharpe']:.2f} "
          f"耗时 {r2['elapsed_s']}s")
