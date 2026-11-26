# -*- coding: utf-8 -*-
"""factors/opportunities/auction_strength.py — 竞价强度信号（T-3 · 外包 AI-2 · 2026-08-09）

★需求（00_总指导任务发布.md 三章 T-3）：分钟数据入库后构建集合竞价强度信号
  （开盘量能 vs 前日 → 盘前择时输入）+ 分钟级入场回测精化。
  数据源：data/minute/download/1m_price_zip/{year}.zip（parquet 按交易日，2010-2026 完整）

信号定义（PIT：只用当日 9:35 前可得数据；1m 首根 09:30 = 集合竞价撮合 bar）：
  gap        = 9:30 open / pre_close - 1                    高开/低开幅度（方向）
  v30        = 9:30 bar vol（竞价+开盘撮合量）
  v30_ratio  = v30 / 前 20 日 v30 均值（rolling，PIT）      竞价量能倍数（核心）
  first5v    = 9:31-9:35 累计 vol                           开盘承接
  first5_ratio = first5v / 前 20 日同时段均值
  综合强度分 strength（0-10）= 量能(0-4) + 方向(0-3) + 承接(0-3)

输出：logs/auction_signal.json {date: {code: {gap, v30_ratio, first5_ratio, strength}}}

用法：
  python factors/opportunities/auction_strength.py --start 2019-01 --end 2019-01   # 信号计算
  python factors/opportunities/auction_strength.py --status                        # 输出概况
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
import glob
import io
import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

MINUTE_DIR = Path(r"data/minute/download/1m_price_zip")
OUT = BASE / "logs" / "auction_signal.json"
ROLL_DAYS = 20          # 量能滚动窗口
SIGNAL_WINDOW = 5       # 开盘承接窗口（分钟数）


def list_days(year: int) -> list:
    """当年可用交易日：incr_parquet（7z 增量，2026 最新）∪ zip 内全部
    ★2026-08-10：7z 增量转的 incr_parquet 优先（zip 可能不含最新交易日）
    """
    days = set()
    incr_dir = Path(r"data/minute/incr_parquet")
    if incr_dir.exists():
        for p in incr_dir.glob("*.parquet"):
            if p.stem.startswith(str(year)):
                days.add(p.stem)
    z = zipfile.ZipFile(MINUTE_DIR / f"{year}.zip")
    days.update(n.replace(".parquet", "") for n in z.namelist() if n.endswith(".parquet"))
    return sorted(days)


def read_day(year: int, date8: str) -> pd.DataFrame:
    """读单日 1m parquet → DataFrame（code, trade_time, open, vol, pre_close）
    ★2026-08-10：incr_parquet（7z 增量最新）优先，zip fallback；
      ① trade_time datetime64 → 字符串（与 zip 口径一致）
      ② volume → vol（增量列名差异）
      ③ pre_close 缺失（增量无此列）→ 用当日 09:31 前收盘价近似前收（PIT：当日可得）
    """
    incr = Path(r"data/minute/incr_parquet") / f"{date8}.parquet"
    if incr.exists():
        df = pd.read_parquet(incr)
        if "trade_time" in df.columns and df["trade_time"].dtype.kind == "M":
            df["trade_time"] = df["trade_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        if "vol" not in df.columns and "volume" in df.columns:
            df["vol"] = df["volume"]
        if "pre_close" not in df.columns:
            # PIT 近似（增量数据无前收列）：首根 09:30 的 close 视作"昨收参考"
            #   （09:30 撮合价 ≈ 前收，gap=open/pre_close 即当日首笔相对昨收的方向）
            df["pre_close"] = df.groupby("code")["close"].shift(1)
            # 每只股票首行（09:30）用自身 open 兜底（gap=0，不产生伪信号）
            first_mask = df.groupby("code").cumcount() == 0
            df.loc[first_mask, "pre_close"] = df.loc[first_mask, "open"]
        return df
    z = zipfile.ZipFile(MINUTE_DIR / f"{year}.zip")
    df = pd.read_parquet(io.BytesIO(z.read(f"{date8}.parquet")))
    return df


def compute_signals(start: str, end: str) -> dict:
    """逐日计算竞价强度信号（PIT rolling）→ {date8: {code: {...}}}"""
    ys, ms = int(start[:4]), int(start[5:7])
    ye, me = int(end[:4]), int(end[5:7])
    # 年份范围
    years = list(range(ys, ye + 1))
    # 收集所有日期（含 start 前 ROLL_DAYS 个交易日用于 rolling 预热）
    all_days = []
    for y in years:
        all_days.extend((y, d) for d in list_days(y))
    all_days.sort()
    # 计算日期范围过滤
    def within(y, d):
        return (y > ys or (y == ys and d[4:6] >= f"{ms:02d}")) and \
               (y < ye or (y == ye and d[4:6] <= f"{me:02d}"))
    target = [(y, d) for y, d in all_days if within(y, d)]
    if not target:
        return {"error": "日期范围内无交易日"}
    # rolling 缓存：code -> v30 序列 + first5 序列（仅保留 ROLL_DAYS 窗口）
    v30_hist, f5_hist = {}, {}
    out = {}
    print(f"计算 {len(target)} 个交易日（{start} ~ {end}）...", flush=True)
    for i, (y, d) in enumerate(target):
        df = read_day(y, d)
        df["hhmm"] = df["trade_time"].str[11:16]
        f30 = df[df["hhmm"] == "09:30"].set_index("code")
        f5 = df[(df["hhmm"] >= "09:31") & (df["hhmm"] <= "09:35")].groupby("code")["vol"].sum()
        f31 = df[df["hhmm"] == "09:31"].set_index("code")
        sig = {}
        for code in f30.index:
            v30 = f30.loc[code, "vol"]
            if v30 is None or v30 != v30:
                continue
            v30 = float(v30)
            hist_v = v30_hist.setdefault(code, [])
            hist_f = f5_hist.setdefault(code, [])
            # 量能比（PIT：仅历史窗口）
            base_v = float(np.mean(hist_v)) if len(hist_v) >= ROLL_DAYS else np.nan
            base_f = float(np.mean(hist_f)) if len(hist_f) >= ROLL_DAYS else np.nan
            open_px = float(f30.loc[code, "open"])
            pre = float(f30.loc[code, "pre_close"]) if f30.loc[code, "pre_close"] is not None else np.nan
            gap = open_px / pre - 1 if pre and pre > 0 else np.nan
            f5v = float(f5.get(code, np.nan)) if code in f5.index else np.nan
            if np.isfinite(gap) and np.isfinite(base_v) and base_v > 0:
                v30_ratio = v30 / base_v
                f5_ratio = f5v / base_f if np.isfinite(f5v) and base_f and base_f > 0 else np.nan
                # 综合强度分 0-10：量能(0-4) + 方向(0-3) + 承接(0-3)
                sc_vol = 0.0 if v30_ratio <= 0 else min(max(np.log2(v30_ratio) / 2 + 2, 0), 4)   # ratio=1→2分, 4→3分, 16→4分
                sc_dir = 0 if not np.isfinite(gap) else min(max(gap * 20 + 1.5, 0), 3)  # gap≥7.5%→3分
                sc_f5 = 0.0 if not np.isfinite(f5_ratio) or f5_ratio <= 0 else min(max(np.log2(f5_ratio) / 2 + 1.5, 0), 3)
                sig[code] = {
                    "gap": round(gap, 4),
                    "v30_ratio": round(v30_ratio, 3),
                    "first5_ratio": round(f5_ratio, 3) if np.isfinite(f5_ratio) else None,
                    "strength": round(min(sc_vol + sc_dir + sc_f5, 10), 2),
                }
            # 更新 rolling 窗口（该 code 当日有数据即记录，无信号也记录量能）
            hist_v.append(v30)
            if np.isfinite(f5v):
                hist_f.append(f5v)
            if len(hist_v) > ROLL_DAYS * 2:
                v30_hist[code] = hist_v[-ROLL_DAYS * 2:]
            if len(hist_f) > ROLL_DAYS * 2:
                f5_hist[code] = hist_f[-ROLL_DAYS * 2:]
        out[d] = sig
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(target)} ({d}) 信号 {len(sig)} 只", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01", help="起始 YYYY-MM")
    ap.add_argument("--end", default="2019-01", help="结束 YYYY-MM")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--out", default=None, help="输出文件名（默认 logs/auction_signal.json，防覆盖可指定区间后缀）")
    args = ap.parse_args()
    if args.status:
        if OUT.exists():
            d = json.loads(OUT.read_text(encoding="utf-8"))
            days = list(d.keys())
            print(f"auction_signal.json: {len(days)} 个交易日 {days[0] if days else '-'} ~ {days[-1] if days else '-'}")
        else:
            print("auction_signal.json 不存在")
        return
    r = compute_signals(args.start, args.end)
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    # ★#413 幂等：数据源未更新（本次日期集合与已有同源文件相同）→ 跳过写文件，
    #   防每日管道每 4h 跑出相同 66 天、写 21MB 重复文件（供应商 1 分钟卡 08-07 时的膨胀根因）
    _existing = sorted(
        [p for p in glob.glob(str(BASE / "logs" / "auction_signal_*.json")) if "_sina_" not in p],
        key=lambda p: Path(p).stat().st_mtime)
    if _existing:
        try:
            _prev = json.loads(Path(_existing[-1]).read_text(encoding="utf-8"))
            if isinstance(_prev, dict) and isinstance(r, dict) and set(_prev.keys()) == set(r.keys()):
                _mx = max(r.keys()) if r else ""
                print(f"数据源未更新（日期集合与已有文件相同，最新 {_mx}），跳过写文件")
                return 0
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/auction_strength.py:187 main")
    # ★2026-08-10 输出时间戳文件名（防环境写保护+保留历史；读取方 glob 取最新）
    out_path = Path(args.out) if args.out else OUT
    if args.out is None:
        out_path = BASE / "logs" / f"auction_signal_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    print(f"\n已存 {out_path}（{len(r)} 个交易日）")
    # 概况：strength 分布
    ss = [v["strength"] for day in r.values() for v in day.values()]
    if ss:
        a = np.array(ss)
        print(f"strength 分布: n={len(a)} 均值={a.mean():.2f} 中位={np.median(a):.2f} "
              f"≥6 占比={(a>=6).mean():.1%} ≥8 占比={(a>=8).mean():.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
