# -*- coding: utf-8 -*-
"""风格权重动态化（中观层择时增强，CS-29 国泰海通落地）
参考："仅在大盘信号强、确信度高时配大盘，其他时间超配小盘"（CS-29）
     + 华鑫大小盘相对强度（1月均线 vs 9月均线动量）

机制：
  1. 全市场按流通市值分 大盘组(前20%) / 小盘组(后20%)
  2. 计算 小盘组近 N 日累计收益 - 大盘组近 N 日累计收益 = 相对强度
     （>0 小盘强势 → 超配 left 池；<0 大盘强势 → 超配 right 池）
  3. 相对强度过 20 日均线平滑（防单日噪声），映射为池权重偏移（±上限）
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

# 参数（params.yaml stock_state 可覆盖）
STRENGTH_WINDOW = 60         # 相对强度回看窗口（交易日）
SMOOTH_WINDOW = 20           # 平滑窗口
MAX_SHIFT = 0.25             # 池权重最大偏移（±25%）
LARGE_PCT = 0.20             # 大盘组：市值前 20%
SMALL_PCT = 0.20             # 小盘组：市值后 20%


def small_large_strength(closes: pd.DataFrame, mv_map: dict,
                         window: int = STRENGTH_WINDOW,
                         smooth: int = SMOOTH_WINDOW) -> pd.Series:
    """大小盘相对强度序列（按日期）。
    >0 = 小盘近 window 日跑赢大盘；<0 = 大盘跑赢小盘。
    closes: 全市场收盘价面板（列=股票代码 600519.SH）
    mv_map: {code6 -> 流通市值(亿)}
    """
    # 市值分组
    mvs = pd.Series({c: mv_map.get(c.split(".")[0], np.nan) for c in closes.columns})
    valid = mvs.dropna()
    if len(valid) < 40:
        return pd.Series(0.0, index=closes.index)

    large_q = valid.quantile(1 - LARGE_PCT)
    small_q = valid.quantile(SMALL_PCT)
    large_codes = valid[valid >= large_q].index
    small_codes = valid[valid <= small_q].index
    if len(large_codes) < 5 or len(small_codes) < 5:
        return pd.Series(0.0, index=closes.index)

    # 两组等权净值
    def group_nav(codes):
        sub = closes[codes].ffill()
        return (1 + sub.pct_change().fillna(0).mean(axis=1)).cumprod()

    nav_large = group_nav(large_codes)
    nav_small = group_nav(small_codes)

    # 相对强度 = 小盘 window 日累计收益 - 大盘 window 日累计收益
    ret_large = nav_large / nav_large.shift(window) - 1
    ret_small = nav_small / nav_small.shift(window) - 1
    strength = (ret_small - ret_large).fillna(0)

    # 平滑（防单日噪声）
    if smooth > 1:
        strength = strength.rolling(smooth).mean().fillna(0)
    return strength


def pool_weight_shift(strength: pd.Series, date, max_shift: float = MAX_SHIFT) -> float:
    """调仓日 left 池权重偏移量（right 池反向）。
    strength(date) > 0（小盘强）→ left +偏移；< 0（大盘强）→ left -偏移。"""
    if date not in strength.index:
        return 0.0
    s = float(strength.loc[date])
    # 归一化：以 ±0.5 为满档（20日收益差 50% 属极端），线性映射
    shift = np.clip(s / 0.5, -1.0, 1.0) * max_shift
    return shift


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache
    import sqlite3

    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:400]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start="2020-01-01", end="2025-12-31", adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()

    mv_map = {}
    try:
        m = pd.read_csv(r"data\cache\circ_mv_map.csv")
        m["code6"] = m["code"].astype(str).str[:6]
        mv_map = dict(zip(m["code6"], m["mv_yi"]))
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "strategy/style_timing.py:104 <module>")

    print("=== 大小盘相对强度自测（400只样本）===")
    st = small_large_strength(closes, mv_map)
    # 查看关键时点
    for d in ["2021-12-31", "2022-10-31", "2024-01-31", "2024-10-31", "2025-06-30"]:
        if d in st.index:
            s = st.loc[d]
            shift = pool_weight_shift(st, d)
            print(f"  {d}: 相对强度 {s:+.3f} → left池偏移 {shift:+.2f}")
    # 整体分布
    print(f"  强度范围: {st.min():.3f} ~ {st.max():.3f} | 均值 {st.mean():.3f}")
    pos = (st > 0).mean()
    print(f"  小盘强势天数占比: {pos:.0%}")
