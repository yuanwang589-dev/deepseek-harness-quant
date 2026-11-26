# -*- coding: utf-8 -*-
"""validation/retail_hot_verdict.py — 主系统审计员裁决：热门×低换手 是否真有增量 alpha
复用因子池 T+1 修复后的 core.combo_backtest（d收盘信号→d+1开盘执行）+ 同一 2019+ 面板。
回答：热门板块维度（crowd 正向）在 T+1 口径下对纯低换手是否贡献增量。
对照：w=0~1 全曲线 + 纯热门 + 纯低换手 + 基准。NaN 安全：两因子共同有效区评分（避免 0×NaN）。
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
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception as _dsh_exc:
    _dsh_swallow(_dsh_exc, "validation/retail_hot_verdict.py:16 <module>")

FP = Path(r"data/factorpool")
sys.path.insert(0, str(FP))

from core import run_pool as rp
from core.combo_backtest import combo_backtest
import core.factors as _f
import core.factors_industry as _fi

t0 = time.time()
print("[verdict] 加载面板...", flush=True)
panel = rp.load_panel()
panel = panel[panel.index.get_level_values("date") >= "2019-01-01"]

turn_low = -_f.factor_turnover(panel).reindex(panel.index)   # 低换手=高值
crowd = _fi.factor_ind_crowd_60(panel).reindex(panel.index)  # 拥挤度原始：高=热门

def z(s):
    return s.groupby(level="date").transform(lambda x: (x - x.mean()) / (x.std() + 1e-9))

turn_z = z(turn_low)
crowd_z = z(crowd)

# ★NaN 安全合成：仅在两因子都有效处评分（杜绝 w=0 时 0×NaN 污染）
valid = turn_z.notna() & crowd_z.notna()
turn_zc = turn_z.where(valid)
crowd_zc = crowd_z.where(valid)

def bt(score, name, top_n=50):
    r = combo_backtest(panel, score, name, rebalance_days=10, top_n=top_n,
                       min_stocks=30, min_price=1.5)
    return r["stats"]

def row(name, s):
    print(f"  {name:<28s} 年化 {s['年化']*100:+7.2f}%  回撤 {s['最大回撤']*100:6.1f}%  "
          f"夏普 {s['夏普']:+5.2f}  净值 {s['期末净值']:6.2f}", flush=True)

print(f"\n[verdict] T+1 口径 2019+ 10日调仓 top50 —— 热门板块权重 w 全曲线:", flush=True)
print(f"  {'策略':<28s} {'年化':>8s} {'回撤':>7s} {'夏普':>6s} {'净值':>7s}", flush=True)
for w in (0.0, 0.3, 0.5, 0.7, 1.0):
    score = (1 - w) * turn_zc + w * crowd_zc
    row(f"热门w={w:.1f}×低换手", bt(score, f"w{w}"))

row("纯低换手 turn_z", bt(turn_z, "turn"))
row("纯热门板块 crowd_z", bt(crowd_z, "crowd"))

# 基准（等权）
bench = pd.Series(1.0, index=panel.index)
row("等权基准", bt(bench, "bench", 999))

print(f"\n[verdict] 完成 {time.time()-t0:.0f}s", flush=True)
