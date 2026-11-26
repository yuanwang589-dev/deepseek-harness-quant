# -*- coding: utf-8 -*-
"""data/convert_7z_to_parquet.py — 7z 增量 → parquet 增量目录（绕开 minute.db 锁）

背景：minute.db 被外部句柄锁（SQLite readonly），7z 增量入库被阻塞；
     但 1m_price_zip parquet 体系不受影响 → 把 7z 内每股 CSV 合并为单日 parquet，
     存到 data/minute/incr_parquet/YYYYMMDD.parquet
     （minute_reader 已支持 fallback 读取该目录，见 _zip_path/read_day 改动）

用法：
  python data/convert_7z_to_parquet.py --dir "D:/.../每日数据" --min-date 20260411
  python data/convert_7z_to_parquet.py --file xxx.7z          # 单文件
"""
import argparse
import io
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    import py7zr
except ImportError:  # 延迟到 main 检查，避免 import 期直接崩
    py7zr = None

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OUT_DIR = Path(r"data/minute/incr_parquet")
CN_COLS = {"日期": "trade_time", "时间": "trade_time", "开盘": "open", "最高": "high",
           "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(BASE / "logs" / f"convert_7z_{datetime.now():%Y%m%d_%H%M%S}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_csv(content: str, date_s: str) -> pd.DataFrame:
    """单股 CSV 内容 → 标准分钟行（中文列名自适应）
    ★2026-08-09 修复：'日期'+'时间' 两列不能都映射 trade_time（rename 重名崩溃）→
      先只映射 OHLCV，再单独合成 trade_time
    """
    try:
        df = pd.read_csv(io.StringIO(content), encoding="utf-8", on_bad_lines="skip")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    cols = {str(c).strip(): c for c in df.columns}
    # 时间合成：日期/时间 两列（20260411 增量 7z 格式）或单一时间列
    has_date_col = "日期" in cols
    has_time_col = "时间" in cols
    if has_date_col and has_time_col:
        df["trade_time"] = df[cols["日期"]].astype(str) + " " + df[cols["时间"]].astype(str)
    elif has_time_col:
        df["trade_time"] = df[cols["时间"]]
    elif "trade_time" in df.columns:
        df["trade_time"] = df["trade_time"]
    else:
        return None
    # OHLCV 映射（除日期/时间外的中文列）
    rename = {}
    for cn, en in (("开盘", "open"), ("最高", "high"), ("最低", "low"),
                   ("收盘", "close"), ("成交量", "volume"), ("成交额", "amount")):
        if cn in cols and en not in df.columns:
            rename[cols[cn]] = en
    df = df.rename(columns=rename)
    need = {"trade_time", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        return None
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df = df.dropna(subset=["trade_time"])
    df = df[df["trade_time"].dt.strftime("%Y%m%d") == date_s]
    for c in ("volume", "amount"):
        if c not in df.columns:
            df[c] = None
    return df[["trade_time", "open", "high", "low", "close", "volume", "amount"]]


def ingest_7z_file(path: Path) -> tuple:
    """单个 7z → 单日 parquet。返回 (date_s, n_rows, err)"""
    date_s = path.stem
    tmp_dir = Path(tempfile.mkdtemp(prefix="c7z_"))
    try:
        with py7zr.SevenZipFile(path) as z:
            names = z.getnames()
            # 兼容两种结构：1min/xxx.csv 或 20260411/1min/xxx.csv
            prefix = None
            for cand in ("1min/", f"{date_s}/1min/"):
                if any(n.startswith(cand) and n.endswith(".csv") for n in names):
                    prefix = cand
                    break
            if prefix is None:
                return date_s, 0, f"无 1min CSV（结构: {names[:3]}）"
            csv_names = [n for n in names if n.startswith(prefix) and n.endswith(".csv")]
            z.extract(path=tmp_dir, targets=csv_names)
    except Exception as e:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return date_s, 0, str(e)[:100]

    # 合并所有股票 CSV
    frames = []
    src_dir = tmp_dir / prefix.rstrip("/")
    for csv_p in sorted(src_dir.glob("*.csv")):
        # 文件名 code：000001.SZ.csv / SH600519.csv / 600519.SH.csv
        code = csv_p.stem
        # ★code 规范化（2026-08-09）：统一为标准格式 600519.SH（与 bars.db/minute.db 一致）
        #   处理：SH600519 → 600519.SH；600519.SH → 600519.SH；BJ920000 → 920000.BJ
        if code[:2].lower() in ("sh", "sz", "bj") and "." not in code:
            code = code[2:] + "." + code[:2].upper()
        elif "." in code:
            parts = code.split(".")
            if len(parts) == 2 and parts[1].upper() in ("SH", "SZ", "BJ"):
                code = f"{parts[0]}.{parts[1].upper()}"
            else:
                code = code.upper()
        else:
            code = code.upper()
        df = parse_csv(csv_p.read_text(encoding="utf-8", errors="replace"), date_s)
        if df is None or df.empty:
            continue
        df["code"] = code
        frames.append(df)
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not frames:
        return date_s, 0, "解析后无数据"
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[["code", "trade_time", "open", "high", "low", "close", "volume", "amount"]]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{date_s}.parquet"
    # 已有文件（当天重跑）→ 覆盖（INSERT OR REPLACE 语义）
    all_df.to_parquet(out, index=False)
    return date_s, len(all_df), None


def main():
    if py7zr is None:
        print("缺少依赖 py7zr（分钟 7z 解压必需）：pip install py7zr")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=str, default=None)
    ap.add_argument("--file", type=str, default=None)
    ap.add_argument("--min-date", type=str, default="20260411")
    args = ap.parse_args()

    # ★2026-08-10 自动发现最新更新目录（用户每日数据更新生成新目录）
    _ROOT = Path(r"data/minute/download/【2】2026单年A股分钟日频-持续更新到年底")
    _DEFAULT_DIR = Path(r"data/minute/download/【2】2026单年A股分钟日频-持续更新到年底/8.9日更新/2026(1)/每日数据")
    if _ROOT.exists():
        cands = []
        for p in _ROOT.glob("*日更新*"):
            if p.is_dir():
                for s in list(p.glob("*/每日数据")) + list(p.glob("每日数据")):
                    if s.is_dir() and list(s.glob("*.7z")):
                        cands.append((p.stat().st_mtime, s))
        if cands:
            cands.sort(reverse=True)
            _DEFAULT_DIR = cands[0][1]
    DEFAULT_DIR = _DEFAULT_DIR
    if args.file:
        paths = [Path(args.file)]
    else:
        d = Path(args.dir) if args.dir else DEFAULT_DIR
        if not d.exists():
            log(f"目录不存在: {d}")
            return
        paths = sorted(d.glob("*.7z"))

    # 已完成检查
    done = {p.stem for p in OUT_DIR.glob("*.parquet")}
    todo = []
    seen = set()
    for p in paths:
        stem = p.stem
        is_copy = stem.endswith("(1)")
        date_s = stem[:-3] if is_copy else stem
        if is_copy or date_s in seen or date_s in done:
            continue
        if date_s < args.min_date:
            continue
        seen.add(date_s)
        todo.append((date_s, p))
    log(f"待转换 7z: {len(todo)} 个（≥{args.min_date}，已完成 {len(done)} 天跳过）")

    t0 = time.time()
    ok = 0
    for date_s, p in todo:
        ds, n, err = ingest_7z_file(p)
        if n:
            ok += 1
            log(f"  {ds}: {n:,} 行 → {ds}.parquet")
        elif err:
            log(f"  [失败] {ds}: {err[:100]}")
        if ok and ok % 5 == 0:
            log(f"  进度 {ok}/{len(todo)}（{(time.time()-t0)/60:.1f} 分钟）")
    log(f"完成: {ok}/{len(todo)} 天，耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
