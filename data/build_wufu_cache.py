# -*- coding: utf-8 -*-
"""data/build_wufu_cache.py — 五福轮动缓存构建器（2026-10-15 新增，本机无因子池缓存时的替代源）

★背景：factors/policy/global_rotation.py 只读因子池缓存（data/factorpool/output/cache/wufu_*.parquet），
  该缓存由外部因子池研究流程生成，本发行包不含 → 门户五福轮动卡/跨资产防守信号永久报错。
  本脚本用 akshare 新浪源（ETF fund_etf_hist_sina + 指数 stock_zh_index_daily）重建同构缓存。

★列顺序契约（勿改）：global_rotation.widget() 按位置映射名称：
  [沪深300ETF, 黄金ETF, 恒指ETF, 中证500ETF, 日经ETF, 标普500ETF, 纳指ETF, 避险资产, 黄金ETF2]
  wufu_idx 列 = [沪深300, 中小综指, 创业板指, 中证A500]（compute() MA10 3/4 投票）。

用法：python data/build_wufu_cache.py   （幂等，可反复跑；建议每周/每日管道前刷新）
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import os
os.environ.setdefault("NO_PROXY", "*")   # 直连（系统代理拒连外部行情源）

import akshare as ak
import pandas as pd

OUT_DIR = BASE / "data" / "factorpool" / "output" / "cache"

# 位置顺序与 widget() 的名称映射严格对应
ETF_MAP = [
    ("沪深300ETF", "sh510300"), ("黄金ETF", "sh518880"), ("恒指ETF", "sh513180"),
    ("中证500ETF", "sh510500"), ("日经ETF", "sh513520"), ("标普500ETF", "sh513500"),
    ("纳指ETF", "sh513100"), ("避险资产", "sh511010"), ("黄金ETF2", "sh518800"),
]
IDX_MAP = [("沪深300", "sh000300"), ("中小综指", "sz399101"),
           ("创业板指", "sz399006"), ("中证A500", "sh000510")]


def _fetch(close_series_fn, code: str, retries: int = 3) -> pd.Series:
    last = None
    for i in range(retries):
        try:
            s = close_series_fn(code)
            if s is not None and len(s):
                return s
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{code} 拉取失败: {str(last)[:80]}")


def _etf_close(sym: str) -> pd.Series:
    h = ak.fund_etf_hist_sina(symbol=sym)
    s = pd.Series(h["close"].astype(float).values,
                  index=pd.to_datetime(h["date"]), name=sym)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _idx_close(sym: str) -> pd.Series:
    h = ak.stock_zh_index_daily(symbol=sym)
    s = pd.Series(h["close"].astype(float).values,
                  index=pd.to_datetime(h["date"]), name=sym)
    return s[~s.index.duplicated(keep="last")].sort_index()


def build():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # --- ETF 9 只 ---
    cols = {}
    for name, sym in ETF_MAP:
        cols[name] = _fetch(_etf_close, sym)
        print(f"  ETF {name}({sym}) {len(cols[name])} 行 → {cols[name].index.max():%Y-%m-%d}")
    etf = pd.DataFrame(cols).sort_index()
    etf.index.name = "date"
    etf.to_parquet(OUT_DIR / "wufu_etf.parquet")
    # --- 指数 4 只 ---
    icols = {}
    for name, sym in IDX_MAP:
        icols[name] = _fetch(_idx_close, sym)
        print(f"  IDX {name}({sym}) {len(icols[name])} 行 → {icols[name].index.max():%Y-%m-%d}")
    idx = pd.DataFrame(icols).sort_index()
    idx.index.name = "date"
    idx.to_parquet(OUT_DIR / "wufu_idx.parquet")
    print(f"完成：wufu_etf{etf.shape} / wufu_idx{idx.shape}（{time.time()-t0:.0f}s）")
    print(f"区间 {etf.index.min():%Y-%m-%d} → {etf.index.max():%Y-%m-%d}")


if __name__ == "__main__":
    build()
