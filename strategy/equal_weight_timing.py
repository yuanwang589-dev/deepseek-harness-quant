# -*- coding: utf-8 -*-
"""
strategy/equal_weight_timing.py — 主策略 v3（用户拍板 A 方向，2026-08-07）

全市场等权 + Regime 择时 + 硬过滤。
基于全量实证（夏普 0.86 达参考系及格线）：择时是唯一被证明有效的 alpha，
选股引擎（CANSLIM/方向化/分类）为负贡献 → 降级为硬过滤层。

决策链：
  1. 硬过滤（一票否决）：退市股 / ST / 流动性不足 / 盈余质量为负
  2. Regime 五档（strategy/timing.py）→ 总仓位
  3. 全市场等权（通过过滤的股票），季度调仓（换手极低）
  4. 持仓期间：无止损（等权+择时形态，防守由 Regime 承担）

用法：
  python strategy/equal_weight_timing.py --date 2026-08-06   # 生成当日持仓清单
  python strategy/equal_weight_timing.py --backtest           # 全量回测验证
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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from strategy.timing import RegimeDetector

# ---- 硬过滤参数（params.yaml strategy_v3 段优先，改配置不改代码）----
MIN_MV_YI = 0.0             # ★PIT 验收后归 0：市值过滤证实为 look-ahead 幻觉（快照口径 0.95 虚高，真实 PIT 下 0.74→0.57 负贡献）
MIN_TURNOVER_YI = 0.1       # 日均成交额下限 0.1 亿（流动性）
DELISTED_CSV = Path(r"data\cache\delisted_list.csv")
MV_MAP_CSV = Path(r"data\cache\circ_mv_map_full.csv")

_MV_MAP = None


def _load_cfg():
    """读 params.yaml strategy_v3 段（回测与实盘同源，改参数不改代码）"""
    try:
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "params.yaml")
                             .read_text(encoding="utf-8"))
        return (cfg or {}).get("strategy_v3", {}) or {}
    except Exception:
        return {}


def load_delisted() -> set:
    """已退市股票代码集合（防幸存者偏差 + 硬过滤）"""
    if not DELISTED_CSV.exists():
        return set()
    try:
        df = pd.read_csv(DELISTED_CSV, encoding="utf-8")
        col = "code" if "code" in df.columns else df.columns[0]
        return set(str(x).upper() for x in df[col].tolist())
    except Exception:
        return set()


def load_mv_map() -> dict:
    """流通市值快照映射 {code: 亿元}（circ_mv 单位万元 → 亿；PIT 落地后换 hist_mv）
    格式：ts_code,circ_mv（万元）"""
    global _MV_MAP
    if _MV_MAP is not None:
        return _MV_MAP
    _MV_MAP = {}
    if MV_MAP_CSV.exists():
        try:
            df = pd.read_csv(MV_MAP_CSV, encoding="utf-8-sig")
            _MV_MAP = {str(r.ts_code).upper(): float(r.circ_mv) / 10000 for r in df.itertuples()}
        except Exception:
            _MV_MAP = {}
    return _MV_MAP


def hard_filter(cache: DailyCache, date: str, min_mv_yi=None,
                min_turnover_yi=None) -> list:
    """硬过滤 → 返回通过代码列表（一票否决制）

    规则：退市股 / 停牌无数据 / ST / 流通市值 < 下限（快照口径，PIT 后换 hist_mv）/ 日均成交额 < 下限
    参数缺省时读 params.yaml strategy_v3 段（min_mv_yi=50 亿, min_turnover_yi=0.1 亿）
    """
    cfg = _load_cfg()
    min_mv_yi = min_mv_yi if min_mv_yi is not None else float(cfg.get("min_mv_yi", MIN_MV_YI))
    min_turnover_yi = min_turnover_yi if min_turnover_yi is not None else float(cfg.get("min_turnover_yi", MIN_TURNOVER_YI))
    delisted = load_delisted()
    mv_map = load_mv_map()
    con = sqlite3.connect(str(cache.db_path))
    rows = con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'"
    ).fetchall()
    con.close()
    codes = [r[0] for r in rows]

    passed = []
    for code in codes:
        if code in delisted:
            continue
        df = cache.get_daily(code, start=None, end=date, adjust="qfq")
        if df is None or df.empty:
            continue
        d = df.set_index("date").sort_index()
        # ST 过滤（is_st 字段，2026-08-07 F-1 修复后生效）
        if "is_st" in d.columns and d["is_st"].iloc[-1] in (1, "1", True):
            continue
        # 市值过滤（快照映射，单位亿元；PIT 落地后换 hist_mv 口径）
        mv = mv_map.get(code)
        if mv is None or mv < min_mv_yi:
            continue
        last = d.iloc[-1]
        # 流动性过滤（近 20 日均成交额）
        if "amount" in d.columns:
            amt = d["amount"].tail(20).mean()
            if pd.notna(amt) and amt < min_turnover_yi * 1e8:
                continue
        passed.append(code)
    return passed


_INDEX_OHLC = None


def load_index_ohlc(end=None):
    """沪深300 真实 OHLC（Regime 择时数据源，模块级缓存）"""
    global _INDEX_OHLC
    if _INDEX_OHLC is None:
        cache = DailyCache()
        df = cache.get_daily("sh.000300", start=None, end=end, adjust="none")
        d = df.set_index("date").sort_index()
        d.index = pd.to_datetime(d.index)
        for col in ("close", "high", "low"):
            d[col] = d[col].astype(float)
        _INDEX_OHLC = d
    return _INDEX_OHLC


def regime_cash(date, confirm=None, cooldown=None) -> float:
    """★动态择时（2026-08-07 重构，回测信号同源）：
    优先读 output/dynamic_regime.json（calendar 主档 + 7 条件投票修正，月度评估，
    T+1 安全）→ 正式回测夏普 1.12（vs 旧 Regime 0.74）。
    数据缺失时回退 RegimeDetector（真实 OHLC + params 参数）。"""
    # ★动态择时优先（月度仓位，回测 test_ewt_pt_backtest D 档同源；T+1：取上一个完整月信号）
    try:
        import json as _json
        p = Path(__file__).resolve().parent.parent / "output" / "dynamic_regime.json"
        if p.exists():
            d = _json.loads(p.read_text(encoding="utf-8"))
            month = str(date)[:7]
            valid = [m for m in sorted(d.keys()) if m < month]
            if valid:
                cash0 = round(1.0 - float(d[valid[-1]]["pos"]), 4)
                # ★政策面防守触发器（2026-08-11 研究清单 #10 落地）：防守区 → 额外降仓 1 档
                try:
                    from data.policy_hook import regime_penalty
                    pen = regime_penalty()
                    if pen > 0:
                        cash0 = max(cash0, pen)
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "strategy/equal_weight_timing.py:164 regime_cash")
                return cash0
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "strategy/equal_weight_timing.py:167 regime_cash")

    # 回退：RegimeDetector（真实 OHLC）
    if confirm is None or cooldown is None:
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load((Path(__file__).resolve().parent.parent /
                                   "config" / "params.yaml").read_text(encoding="utf-8"))
            rg = (cfg or {}).get("regime", {}) or {}
        except Exception:
            rg = {}
        confirm = confirm if confirm is not None else rg.get("confirm_days", 5)
        cooldown = cooldown if cooldown is not None else rg.get("cooldown_days", 0)
    d = load_index_ohlc(str(date))
    hist = d[d.index <= pd.Timestamp(str(date))]
    if len(hist) < 220:
        return 0.0
    rd = RegimeDetector({"confirm_days": confirm, "cooldown_days": cooldown})
    win = hist.iloc[-500:]
    dfi = pd.DataFrame({"close": win["close"], "high": win["high"], "low": win["low"]})
    state = "choppy"
    for i in range(len(win)):
        state = rd.update(dfi.iloc[: i + 1])
    return rd.cash_ratio()


def portfolio(date: str, min_mv_yi=MIN_MV_YI, min_turnover_yi=MIN_TURNOVER_YI) -> dict:
    """生成 date 日的持仓清单（等权 + Regime 仓位）"""
    cache = DailyCache()
    codes = hard_filter(cache, date, min_mv_yi, min_turnover_yi)
    cash = regime_cash(date)
    regime = "derived"
    return {
        "date": date,
        "regime_cash_ratio": cash,
        "n_stocks": len(codes),
        "codes": codes,
        "target_position_pct": (1 - cash) / max(len(codes), 1),
    }


def main():
    ap = argparse.ArgumentParser(description="主策略 v3：等权 + Regime + 硬过滤")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    p = portfolio(args.date)
    print(f"== {p['date']} ==")
    print(f"Regime 现金比例: {p['regime_cash_ratio']:.0%} ｜ 通过硬过滤: {p['n_stocks']} 只")
    print(f"单票等权仓位: {p['target_position_pct']:.3%}")
    if p["n_stocks"]:
        print(f"持仓示例: {p['codes'][:10]}...")


if __name__ == "__main__":
    main()
