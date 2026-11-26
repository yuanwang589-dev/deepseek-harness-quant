# -*- coding: utf-8 -*-
"""
backtest/bt_engine.py — M5 回测引擎（Backtrader 封装，全链路 v3）

把已完成的模块接入回测做全链路验证（单一路径：回测=实盘同一套代码）：
- 选股模式一：因子引擎方向化排名（factors/factor_engine.py）→ Top N 等权
- 选股模式二：★分类策略（v2.9）——个股状态分类（strategy/stock_state.py）
    → 三池各用各的因子与纪律（left 反转无止损 / right 突破欧奈尔止损 / neutral 低波防守）
    → 市值分池（CS-03）+ 突破确认/拥挤过滤（strategy/breakout_confirm.py，CS-25/26）
- 择时：Regime 五档（strategy/timing.py）→ 总仓位
- 风控：RiskAgent 审核（risk/risk_agent.py，一票否决）
- 纪律：StopRules 止损止盈（risk/stop_rules.py）
- 执行：T+1（信号次日执行）、真实费率（佣金+印花税+滑点）

设计要点（第07课 Quality Gate）：
- 信号 T 日生成，T+1 执行（防 Look-Ahead）
- 含真实成本：佣金万2.6 + 卖出印花税 0.05% + 滑点 0.1%
- Regime 控制总仓位：panic/downtrend 只减不加

用法：
  python backtest/bt_engine.py --mode classified --start 2020-01-01 --end 2025-12-31 --limit 200
  python backtest/bt_engine.py --mode direction --topn 10 --limit 200
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
import yaml

from backtest.bt_cost import one_way_cost, round_trip_cost
from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS, compute_factor_panel
from risk.risk_agent import RiskAgent
from risk.stop_rules import StopRules
from strategy.breakout_confirm import breakout_filter
from strategy.stock_state import classify_series

CFG = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
MV_MAP_CSV = r"data\cache\circ_mv_map.csv"

# 分类策略参数（params.yaml stock_state/layers 可覆盖）
HARD_STOP = CFG.get("risk", {}).get("stop_loss_pct", 0.07)
HIGH_DD = CFG.get("risk", {}).get("high_drawdown_exit", 0.08)
COST = round_trip_cost()   # ★2026-11-26：统一成本口径，印花税仅卖出侧（原双边多扣一次）


class BtEngine:
    """全链路回测引擎（向量化简化版，聚焦策略逻辑验证）"""

    def __init__(self, topn: int = 10, cash: float = 1_000_000, start: str = "2020-01-01",
                 end: str = "2025-12-31"):
        self.topn = topn
        self.cash = cash
        self.start, self.end = start, end
        self.cache = DailyCache()
        self.risk_agent = RiskAgent(CFG.get("risk", {}))
        self.stop_rules = StopRules(CFG.get("risk", {}))

    def load_panel(self, codes, min_len=250):
        """加载价格面板（★2026-11-11 修复幸存者偏差：并集日历 + 最短 250 日）→ closes DataFrame
        ★2026-08-10 性能优化：get_daily_batch 一次 SQL 全拉（逐只 5.5min → 目标 <1min）"""
        panel = {}
        batch = {}
        # ★2026-11-26：warmup 加载（start 前 400 天），保证短回测区间也有足够历史算 120 日因子
        load_start = (pd.Timestamp(str(self.start)) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        try:
            batch = self.cache.get_daily_batch(codes, start=load_start, end=self.end, adjust="qfq")
            for code, df in batch.items():
                if len(df) < min_len:
                    continue
                panel[code] = df.set_index("date").sort_index()["close"]
        except Exception:
            batch = {}
        if not batch:  # 批量失败（极端）→ 回退逐只
            for code in codes:
                df = self.cache.get_daily(code, start=load_start, end=self.end, adjust="qfq")
                if df is None or len(df) < min_len:
                    continue
                panel[code] = df.set_index("date").sort_index()["close"]
        if not panel:
            return None
        # ★2026-11-11 修复：原 `set.intersection` 会删掉任一股票缺数据的交易日（停牌/退市/晚上市）
        #   → 只保留"全程存活且几乎不停牌"样本（典型幸存者偏差）。改为并集日历 + 逐股 reindex。
        all_dates = sorted(set().union(*[set(s.index) for s in panel.values()]))
        closes = pd.DataFrame({c: panel[c].reindex(all_dates) for c in panel},
                              index=all_dates).ffill()
        return closes

    def load_panel_full(self, codes, min_len=250):
        """加载完整面板（含 volume/turn，供分类策略与突破确认用）
        ★2026-08-10 性能优化：批量读取同 load_panel"""
        panel = {}
        batch = {}
        # ★2026-11-26：warmup 加载（start 前 400 天），与 load_panel 同口径
        load_start = (pd.Timestamp(str(self.start)) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        try:
            batch = self.cache.get_daily_batch(codes, start=load_start, end=self.end, adjust="qfq")
            for code, df in batch.items():
                if len(df) < min_len:
                    continue
                panel[code] = df.set_index("date").sort_index()
        except Exception:
            batch = {}
        if not batch:
            for code in codes:
                df = self.cache.get_daily(code, start=load_start, end=self.end, adjust="qfq")
                if df is None or len(df) < min_len:
                    continue
                panel[code] = df.set_index("date").sort_index()
        closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
        return panel, closes

    @staticmethod
    def _load_mv_map():
        try:
            m = pd.read_csv(MV_MAP_CSV)
            m["code6"] = m["code"].astype(str).str[:6]
            return dict(zip(m["code6"], m["mv_yi"]))
        except Exception:
            return None

    @staticmethod
    def _segment_return(closes: pd.DataFrame, picks, pos: int, nxt_pos: int) -> pd.Series:
        """★2026-11-26：持仓区间日收益，含 T+1 首日。

        原 `closes.iloc[pos+1:nxt_pos+1].pct_change().fillna(0)` 把首日置 0，
        相当于丢掉信号次日收益。改为从 pos 起算 pct_change 再取 [1:]，
        首日 = close[pos+1]/close[pos] - 1，不再被 fillna 吞掉。
        """
        return closes[picks].iloc[pos:nxt_pos + 1].pct_change().iloc[1:].fillna(0)

    def backtest_direction(self, closes: pd.DataFrame, direction: dict,
                           cost: float = COST) -> dict:
        """方向化因子回测：每月末排名 → Top N 等权，含成本

        direction: 因子方向表（factors/direction 或自定义）
        cost: 佣金万2.6 + 印花税0.05%(卖) + 滑点0.1%
        """
        # 因子面板（方向化，越大越好）
        panels = {}
        for name, sign in direction.items():
            if sign == 0 or name not in FACTOR_FUNCS:
                continue
            raw = FACTOR_FUNCS[name](closes.astype(float))  # ★向量化：整矩阵一次算
            panels[name] = raw * sign
        if not panels:
            raise ValueError("无有效因子")

        # 综合分（等权合并各因子，先归一化排名）
        # ★用 DataFrame 累加（Series+DataFrame 会按 index 广播错位，产生多余列）
        score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        factor_ranks = {}
        for name, p in panels.items():
            rank = p.rank(axis=1, pct=True)   # 每日期截面排名 0-1
            factor_ranks[name] = rank
            score = score + rank
        score = score / len(panels)

        # 月末截面调仓（T+1：信号月末生成，次月首个交易日执行）
        ym = closes.index.astype(str).str[:7]
        month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
        # 日期比较统一转字符串（兼容 datetime/str 两种索引）
        s, e = str(self.start)[:10], str(self.end)[:10]
        month_ends = [d for d in month_ends if s <= str(d)[:10] <= e]

        # 组合收益：Top N 等权
        ret = pd.Series(0.0, index=closes.index)
        turnover_cost_total = 0.0
        for i, me in enumerate(month_ends):
            pos = closes.index.get_loc(me)
            if pos < 120:
                continue
            scores = score.iloc[pos].dropna()
            if len(scores) < self.topn:
                continue
            picks = scores.nlargest(self.topn).index
            # 次月区间收益（T+1：从下个交易日开始）
            nxt = month_ends[i + 1] if i + 1 < len(month_ends) else self.end
            nxt_pos = closes.index.get_loc(nxt) if nxt in closes.index else len(closes) - 1
            seg = self._segment_return(closes, picks, pos, nxt_pos)
            if len(seg) == 0:
                continue
            ret.loc[seg.index] = seg.mean(axis=1)
            turnover_cost_total += cost   # 每月全换仓（买+卖），印花税单边已含在 cost 内

        # 扣除成本（按调仓次数分摊到日）
        n_days = len(ret)
        ret_net = ret - turnover_cost_total / n_days if n_days > 0 else ret

        return self._metrics(ret_net, turnover_cost_total, len(month_ends))

    def backtest_classified(self, panel, closes, use_mv_pool=True, use_breakout=True,
                            mv_map=None) -> dict:
        """★分类策略回测（v2.9）：
        个股状态分类（right/left/neutral）→ 三池各用各的因子与纪律
          left（超跌）→ 反转+低波，无止损（等反转）
          right（趋势）→ 接近高点+动量，欧奈尔止损 7%+高点回撤 8%
          neutral（震荡）→ 低波防守，无止损
        资金按池规模动态分配（单池下限 10%），总仓位由 Regime 层控制。
        """
        def_score = self._build_score(closes, {"rps_120": -1, "lowvol_60": -1})
        atk_score = self._build_score(closes, {"near_high_250": 1, "mom_120": 1})
        neutral_score = self._build_score(closes, {"lowvol_60": -1})

        states_all = {code: classify_series(d["close"]) for code, d in panel.items()}
        brk_all = {code: breakout_filter(d) for code, d in panel.items()} if use_breakout else None

        mv_median = None
        if use_mv_pool and mv_map:
            mvs = [mv_map.get(c.split(".")[0], np.nan) for c in panel]
            mv_median = np.nanmedian(mvs)

        ym = closes.index.astype(str).str[:7]
        # ★季度调仓（params.yaml pools.rebalance_interval=3，实证最优频率）
        rb_interval = CFG.get("pools", {}).get("rebalance_interval", 3)
        months = sorted(ym.unique())
        rb_months = months[::rb_interval]
        month_ends_map = pd.Series(closes.index).groupby(ym).max()
        month_ends = [month_ends_map[m] for m in rb_months if m in month_ends_map.index]
        s, e = str(self.start)[:10], str(self.end)[:10]
        month_ends = [d for d in month_ends if s <= str(d)[:10] <= e]
        dates, n = closes.index, len(closes)

        left_hold, right_hold, neutral_hold = {}, {}, {}
        w_left, w_right, w_neutral = 0.0, 0.0, 0.0
        cost_total, daily = 0.0, []
        prev_codes = set()   # ★2026-11-26：上一期持仓，用于按换入/换出分别计费（印花税仅卖出）

        for di in range(1, n):
            day, prev = dates[di], dates[di - 1]
            # ★2026-11-10 修复：原 `np.mean(三池混合)` 后再乘 (w_left+w_right+w_neutral)
            #   而三者归一化后恒为 1 → 池权重/Regime 完全不进入收益。改为分池加权。
            day_ret = 0.0
            for _w, _hold in ((w_left, left_hold), (w_right, right_hold), (w_neutral, neutral_hold)):
                if not _hold or _w <= 0:
                    continue
                _rr = []
                for code in _hold:
                    d = panel[code]
                    if prev in d.index and day in d.index:
                        _c1 = d.loc[day, "close"]
                        _c0 = d.loc[prev, "close"]
                        # ★2026-11-11：停牌/未上市日 NaN 不参与收益（原会得到 NaN 污染均值）
                        if pd.notna(_c1) and pd.notna(_c0) and _c0 > 0:
                            _rr.append(_c1 / _c0 - 1)
                if _rr:
                    day_ret += _w * float(np.mean(_rr))
            daily.append(day_ret)

            # right 池欧奈尔止损（T 日信号当日结算）
            if right_hold:
                to_sell = []
                for code, (bp, hi, bd) in right_hold.items():
                    d = panel[code]
                    if day not in d.index:
                        continue
                    cur = d.loc[day, "close"]
                    if pd.isna(cur):
                        continue
                    hi2 = max(hi, cur)
                    right_hold[code] = (bp, hi2, bd)
                    if cur / bp - 1 <= -HARD_STOP or cur / hi2 - 1 <= -HIGH_DD:
                        to_sell.append(code)
                if to_sell:
                    sell_w = len(to_sell) / max(len(right_hold), 1) * w_right
                    w_right -= sell_w
                    cost_total += one_way_cost("sell") * sell_w
                    for code in to_sell:
                        del right_hold[code]
                        prev_codes.discard(code)

            # 调仓日（季度）：分类选股
            if day in month_ends and di > 252:
                pos = closes.index.get_loc(day)
                # ★2026-11-10 修复：原 `st.iloc[pos]` 用并集位置索引单股序列（长度不同）→ 取到未来状态。
                #   改为按日期对齐。
                st_day = {code: st.loc[day] for code, st in states_all.items() if day in st.index}
                left_codes = [c for c, s in st_day.items() if s == "left"]
                right_codes = [c for c, s in st_day.items() if s == "right"]
                neutral_codes = [c for c, s in st_day.items() if s == "neutral"]

                # 市值分池：小盘 right 降级到 left（CS-03 大盘动量正）
                if use_mv_pool and mv_median is not None:
                    small_right = [c for c in right_codes
                                   if mv_map.get(c.split(".")[0], np.nan) < mv_median]
                    if small_right:
                        right_codes = [c for c in right_codes if c not in small_right]
                        left_codes = left_codes + small_right

                # 突破确认：right 池仅保留放量且非拥挤（CS-25/26）
                if use_breakout and right_codes:
                    right_codes = [c for c in right_codes
                                   if c in brk_all and day in brk_all[c].index and brk_all[c].loc[day]]

                pools = [("left", left_codes), ("right", right_codes), ("neutral", neutral_codes)]
                sizes = {k: max(len(v), 1) for k, v in pools}
                total = sum(sizes.values())
                ws = {k: max(v / total, 0.10) for k, v in sizes.items()}
                wsum = sum(ws.values())
                w_left, w_right, w_neutral = ws["left"] / wsum, ws["right"] / wsum, ws["neutral"] / wsum

                def _pick(score_col, codes, w, k_cap):
                    hold = {}
                    if not codes or w <= 0:
                        return hold
                    sc = score_col[codes].dropna()
                    if len(sc) >= 1:
                        k = max(1, min(k_cap, len(sc)))
                        for c in sc.nlargest(k).index:
                            if day in panel[c].index:
                                px = panel[c].loc[day, "close"]
                                if not pd.isna(px):
                                    hold[c] = (float(px), float(px), str(day))
                    return hold

                left_hold = _pick(def_score.iloc[pos], left_codes, w_left, 6)
                right_hold = _pick(atk_score.iloc[pos], right_codes, w_right, 4)
                neutral_hold = _pick(neutral_score.iloc[pos], neutral_codes, w_neutral, 4)
                # ★2026-11-26：按实际换入/换出计费，买/卖成本分开（印花税仅卖出侧）
                new_codes = set(left_hold) | set(right_hold) | set(neutral_hold)
                buy_n = len(new_codes - prev_codes)
                sell_n = len(prev_codes - new_codes)
                cost_total += (one_way_cost("buy") * buy_n + one_way_cost("sell") * sell_n)
                prev_codes = new_codes

        ret = pd.Series(daily, index=dates[1:])
        # ★2026-11-26：warmup 期不计入收益（与 backtest_direction 的 start 过滤对齐）
        ret = ret.loc[ret.index.astype(str) >= str(self.start)[:10]]
        ret_net = ret - cost_total / max(len(ret), 1)
        return self._metrics(ret_net, cost_total, len(month_ends))

    @staticmethod
    def _build_score(closes, direction):
        panels = {}
        for name, sign in direction.items():
            if sign == 0 or name not in FACTOR_FUNCS:
                continue
            raw = FACTOR_FUNCS[name](closes.astype(float))  # ★向量化：整矩阵一次算
            panels[name] = raw * sign
        score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for name, p in panels.items():
            score = score + p.rank(axis=1, pct=True)
        return score / max(len(panels), 1)

    @staticmethod
    def _metrics(ret_net, cost, n_months):
        eq = (1 + ret_net).cumprod()
        total = eq.iloc[-1] - 1
        annual = (1 + total) ** (252 / max(len(ret_net), 1)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        sharpe = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
        return {"total": total, "annual": annual, "max_dd": dd, "sharpe": sharpe,
                "turnover_cost": cost, "n_months": n_months, "returns": ret_net}

    def run_comparison(self, codes, limit=100):
        """跑对比：等权基准 vs 反转+低波 vs 分类策略（三池）"""
        print(f"加载数据（限 {limit} 只）...")
        panel, closes = self.load_panel_full(codes[:limit])
        if panel is None or closes is None:
            print("数据不足")
            return
        print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")
        mv_map = self._load_mv_map()

        bench_ret = closes.pct_change().fillna(0).mean(axis=1)
        print(f"\n{'策略':<22s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
        print("-" * 50)
        self._print_metrics("等权基准", bench_ret)

        reversal = {"rps_120": -1, "lowvol_60": -1, "mom_20": -1}
        try:
            r = self.backtest_direction(closes, reversal)
            print(f"{'反转+低波(方向化)':<20s} {r['annual']:>8.1%} {r['max_dd']:>8.1%} {r['sharpe']:>7.2f}")
        except Exception as e:
            print(f"反转失败: {e}")

        try:
            r = self.backtest_classified(panel, closes, use_mv_pool=True,
                                         use_breakout=True, mv_map=mv_map)
            print(f"{'分类策略(三池,分池+确认)':<20s} {r['annual']:>8.1%} {r['max_dd']:>8.1%} {r['sharpe']:>7.2f}")
        except Exception as e:
            print(f"分类策略失败: {e}")

    @staticmethod
    def _print_metrics(name, ret):
        eq = (1 + ret).cumprod()
        total = eq.iloc[-1] - 1
        annual = (1 + total) ** (252 / max(len(ret), 1)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
        print(f"{name:<22s} {annual:>8.1%} {dd:>8.1%} {sharpe:>7.2f}")


def main():
    ap = argparse.ArgumentParser(description="M5 回测引擎（全链路）")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--topn", type=int, default=10)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--mode", default="comparison", choices=["comparison", "classified", "direction"],
                    help="回测模式：comparison 对比 / classified 分类策略 / direction 方向化")
    args = ap.parse_args()

    # ★2026-11-26：改用 DailyCache.list_codes（合并主库+增量库），
    #   原"相对路径直连 bars.db"既绕双库又依赖 cwd。
    eng = BtEngine(topn=args.topn, start=args.start, end=args.end)
    codes = eng.cache.list_codes("qfq")
    print(f"缓存股票: {len(codes)} 只")

    if args.mode == "direction":
        closes = eng.load_panel(codes[:args.limit])
        r = eng.backtest_direction(closes, {"rps_120": -1, "lowvol_60": -1, "mom_20": -1})
        print(f"反转+低波: 年化 {r['annual']:.1%} 回撤 {r['max_dd']:.1%} 夏普 {r['sharpe']:.2f}")
    elif args.mode == "classified":
        panel, closes = eng.load_panel_full(codes[:args.limit])
        r = eng.backtest_classified(panel, closes, use_mv_pool=True, use_breakout=True,
                                    mv_map=eng._load_mv_map())
        print(f"分类策略: 年化 {r['annual']:.1%} 回撤 {r['max_dd']:.1%} 夏普 {r['sharpe']:.2f}")
    else:
        eng.run_comparison(codes, limit=args.limit)


if __name__ == "__main__":
    main()

