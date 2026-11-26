# -*- coding: utf-8 -*-
"""data/rebuild_wufu_cache.py — 重建五福轮动缓存（wufu_etf/wufu_idx parquet）

★2026-08-25 背景：data/factorpool/（外包因子池运行时目录，gitignored）整目录丢失，
  global_rotation.widget()/compute() 读 data/factorpool/output/cache/wufu_etf.parquet
  抛 FileNotFoundError → 门户"五福轮动数据暂不可用"。本脚本从公开行情源重建这两个
  parquet（只重建 wufu 摆件所需，不重建因子池其它产物 daily_scores/health 等）。

数据源（★2026-11-02 改造）：
  ETF / 指数：tushare pro 主源（fund_daily / index_daily，国内直连、无 TLS 指纹拦截、与 akshare
    收盘价实测零偏差），失败或空数据时无缝回退 akshare：
      ETF  → 新浪 fund_etf_hist_sina（复用 etf/etf_map.fetch_etf：缓存+节流+重试）
      指数 → 东财 index_zh_a_hist，再回退新浪 stock_zh_index_daily
  改造背景：akshare 东财指数接口在本机被 TLS 指纹拒绝（RemoteDisconnected），只能每次绕新浪兜底。
口径：不复权收盘价（ETF 分红少，动量/MA10/20日收益判断可接受；两源同口径已验证）。
  ★ETF 列顺序必须与 global_rotation.widget() 按列位置映射的 9 列一致，勿改顺序：
  [沪深300, 黄金, 恒指, 中证500, 日经, 标普500, 纳指, 避险, 黄金2]
  指数列与 global_rotation.IDX_COLS 一致：[沪深300, 中小综指, 创业板指, 中证A500]。

用法（项目根运行）：python -X utf8 data/rebuild_wufu_cache.py
"""
import os
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.chdir(BASE)   # etf_map 内部缓存路径为相对路径（data/cache/etf），统一 CWD 到项目根

from etf.etf_map import fetch_etf   # 新浪源 ETF 日线（带本地缓存增量）
from data.fetcher_tushare import get_fund_daily, get_index_daily   # tushare 主源

CACHE = BASE / "data" / "factorpool" / "output" / "cache"
START = "2019-01-01"   # 对齐原因子池口径（1847 交易日 ≈ 2019-01 起）
TS_START = "20130101"  # tushare 拉全历史，后续按 START 截断（比 akshare 缓存更齐，且无增量维护）

# ★位置顺序勿改（widget() 按列位置映射展示名）
#   (展示名, akshare/新浪代码, tushare ts_code)
ETF_COLS = [
    ("沪深300", "510300", "510300.SH"),   # 沪深300ETF
    ("黄金", "518880", "518880.SH"),       # 黄金ETF
    ("恒指", "513600", "513600.SH"),       # 恒生ETF
    ("中证500", "510500", "510500.SH"),    # 中证500ETF
    ("日经", "513520", "513520.SH"),       # 日经ETF（2019-06 上市，此前 NaN，widget 按 dropna 处理）
    ("标普500", "513500", "513500.SH"),    # 标普500ETF
    ("纳指", "513100", "513100.SH"),       # 纳指ETF
    ("避险", "511010", "511010.SH"),       # 国债ETF（原代理池"与全资产低相关"的避险位）
    ("黄金2", "518800", "518800.SH"),      # 黄金ETF华夏
]
IDX_COLS = [("沪深300", "000300", "000300.SH"), ("中小综指", "399101", "399101.SZ"),
            ("创业板指", "399006", "399006.SZ"), ("中证A500", "000510", "000510.SH")]


def _series_from_ts(api_fn, ts_code: str) -> pd.Series | None:
    """tushare 日线 → close Series（date 索引）；不可用/为空返回 None，由调用方回退 akshare。"""
    try:
        df = api_fn(ts_code, start_date=TS_START)
    except Exception as e:
        print(f"    tushare {ts_code} 不可用（{str(e)[:70]}）→ 回退 akshare")
        return None
    if df is None or df.empty:
        print(f"    tushare {ts_code} 返回空 → 回退 akshare")
        return None
    s = pd.to_numeric(df["close"], errors="coerce")
    s.index = pd.to_datetime(df["trade_date"])
    return s.sort_index().dropna()


def fetch_index(code: str) -> pd.Series:
    """指数日线收盘价（date 索引）。东财主源，新浪兜底。"""
    import akshare as ak
    try:
        df = ak.index_zh_a_hist(symbol=code, period="daily")
        df = df.rename(columns={"日期": "date", "收盘": "close"})
        cols = df.columns.tolist()
        if "date" not in cols or "close" not in cols:
            raise ValueError(f"意外列名: {cols[:6]}")
    except Exception as e:
        sym = ("sh" if code.startswith("000") else "sz") + code
        df = ak.stock_zh_index_daily(symbol=sym)
        df = df[["date", "close"]]
        print(f"[指数] {code} 东财失败（{str(e)[:50]}）→ 新浪 {sym}")
    s = pd.to_numeric(df["close"], errors="coerce")
    s.index = pd.to_datetime(df["date"])
    return s.sort_index().dropna()


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)

    print("[1/3] 拉 9 只 ETF 日线（tushare 主源 fund_daily，akshare/新浪兜底）...")
    etf_frames = {}
    for name, code, ts_code in ETF_COLS:
        s = _series_from_ts(get_fund_daily, ts_code)
        src = "tushare"
        if s is None:
            df = fetch_etf(code)
            s = pd.to_numeric(df["close"], errors="coerce")
            s.index = pd.to_datetime(df["date"])
            s = s.sort_index()
            src = "akshare"
        etf_frames[name] = s
        print(f"  {code} {name}: {len(s)} 行（{s.index.min().date()} ~ {s.index.max().date()}）[{src}]")
    etf = pd.DataFrame(etf_frames)
    etf = etf[etf.index >= START]
    etf.to_parquet(CACHE / "wufu_etf.parquet")
    print(f"  → wufu_etf.parquet {etf.shape[0]} 天 × {etf.shape[1]} 列")

    print("[2/3] 拉 4 指数日线（tushare 主源 index_daily，akshare 兜底）...")
    idx_frames = {}
    for name, code, ts_code in IDX_COLS:
        try:
            s = _series_from_ts(get_index_daily, ts_code)
            src = "tushare"
            if s is None:
                s = fetch_index(code)
                src = "akshare"
            idx_frames[name] = s
            print(f"  {code} {name}: {len(s)} 行（{s.index.min().date()} ~ {s.index.max().date()}）[{src}]")
        except Exception as e:
            print(f"  ⚠ {code} {name} 拉取失败: {str(e)[:80]}（弱市投票按可用指数计）")
    idx = pd.DataFrame(idx_frames)
    idx = idx[idx.index >= START]
    idx.to_parquet(CACHE / "wufu_idx.parquet")
    print(f"  → wufu_idx.parquet {idx.shape[0]} 天 × {idx.shape[1]} 列")

    print("[3/3] 自验证（widget + compute 跑通）...")
    from factors.policy import global_rotation as gr
    w = gr.widget()
    print(f"  widget: date={w['date']} assets={len(w['assets'])}")
    for a in w["assets"][:3]:
        print(f"    {a['name']} mom={a['mom_ann']} r2={a['r2']} above_ma10={a['above_ma10']}")
    r = gr.compute()
    print(f"  compute: date={r['date']} a_share_weak={r['a_share_weak']}（投票 {r['weak_vote']}/4）")
    print(f"[完成] 缓存已重建: {CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
