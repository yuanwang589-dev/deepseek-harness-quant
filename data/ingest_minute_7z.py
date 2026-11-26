# -*- coding: utf-8 -*-
"""data/ingest_minute_7z.py — 2026 每日分钟增量（7z）→ minute.db（2026-08-09）

数据源：数据"2026单年A股分钟日频-持续更新到年底"每日更新（16-18 点）
  【2】.../8.9日更新/2026(1)/每日数据/20260807.7z
  7z 内结构：1min/bj920000.csv（每股一个 CSV，5536 只）+ 5min/ 15min/ 30min/ 60min/

★与 ingest_minute.py 的关系：
  - ingest_minute.py：历史分钟包（parquet 按日分片）入库（minute.db）
  - 本脚本：每日增量 7z → CSV → 追加入库（minute.db，同表同口径）

用法：
  python data/ingest_minute_7z.py --dir "D:/.../每日数据"      # 处理目录内全部 7z
  python data/ingest_minute_7z.py --file xxx.7z               # 单个 7z
  python data/ingest_minute_7z.py --status                    # 查看已入库日期
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

import pandas as pd
import py7zr

MINUTE_DB = r"data\cache\minute.db"
LOG = BASE / "logs" / "ingest_minute_7z.log"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/ingest_minute_7z.py:39 log")


def _conn():
    return sqlite3.connect(MINUTE_DB)


def _parse_csv(content: str, date_s: str, freq: str = "1min"):
    """CSV 内容 → DataFrame（支持中英文列名；trade_time 由 日期+时间 合成）"""
    import io
    try:
        df = pd.read_csv(io.StringIO(content), encoding="utf-8-sig", on_bad_lines="skip")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df.columns = [str(c).strip().lower() for c in df.columns]
    # 中英文列名统一映射
    rename = {}
    for src, dst in [("time", "time"), ("datetime", "trade_time"),
                     ("时间", "time"), ("日期", "date"),
                     ("开盘", "open"), ("最高", "high"), ("最低", "low"), ("收盘", "close"),
                     ("成交量", "volume"), ("成交额", "amount"),
                     ("vol", "volume"), ("volume", "volume"), ("amount", "amount")]:
        if src in df.columns and dst not in df.columns:
            rename[src] = dst
    df = df.rename(columns=rename)
    # trade_time 合成：有 date+time 列 → 拼成 datetime；有 trade_time 直接用
    if "trade_time" not in df.columns and "date" in df.columns and "time" in df.columns:
        df["trade_time"] = pd.to_datetime(
            df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
    elif "trade_time" not in df.columns and "time" in df.columns:
        df["trade_time"] = pd.to_datetime(date_s + " " + df["time"].astype(str), errors="coerce")
    need = {"trade_time", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        return None
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df = df.dropna(subset=["trade_time"])
    if "code" not in df.columns:
        df["code"] = None
    for c in ("volume", "amount"):
        if c not in df.columns:
            df[c] = None
    return df[["code", "trade_time", "open", "high", "low", "close", "volume", "amount"]]


def ingest_7z(path: Path, freq: str = "1min"):
    """单个 7z → 入库。返回 (日期, 行数, 错误)
    ★结构自适应：① 1min/xxx.csv ② 20260408/1min/xxx.csv（多一层日期目录）
    """
    date_s = path.stem  # 20260807
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="min7z_"))
    try:
        with py7zr.SevenZipFile(path) as z:
            # 只解压目标频率目录（1min/ 等，避免解全部 5 个频率）
            names = z.getnames()
            # 匹配两种结构：1min/xxx.csv 或 20260408/1min/xxx.csv
            csv_names = [n for n in names
                         if n.endswith(".csv") and f"/{freq}/" in n]
            if not csv_names:
                # 兜底：直接顶层 csv
                csv_names = [n for n in names if n.endswith(".csv")]
            if not csv_names:
                return date_s, 0, f"无 {freq} CSV"
            z.extract(path=tmp_dir, targets=csv_names)
    except Exception as e:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return date_s, 0, str(e)[:100]

    # 找到解压后的 freq 目录（可能在 tmp_dir/1min 或 tmp_dir/20260408/1min）
    freq_dirs = [d for d in tmp_dir.rglob(freq) if d.is_dir()]
    if not freq_dirs:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return date_s, 0, "解压后无 freq 目录"
    freq_dir = freq_dirs[0]

    con = _conn()
    total = 0
    n_code = 0
    for csv_p in freq_dir.glob("*.csv"):
        try:
            content = csv_p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 从文件名提取 code（1min/000001.SZ.csv → 000001.SZ；bj920000.csv → bj920000）
        # ★统一为 bars.db 标准格式：600519.SH（交易所后缀在后）；SH.600519 → 600519.SH
        code = csv_p.stem
        if code[:2].upper() in ("SH", "SZ", "BJ") and "." in code:
            exch, num = code[:2].upper(), code[3:]
            code = f"{num}.{exch}"
        elif code[:2].upper() in ("SH", "SZ", "BJ") and "." not in code:
            code = f"{code[2:]}.{code[:2].upper()}"
        elif "." in code:
            code = code.upper()
        df = _parse_csv(content, date_s)
        if df is None or df.empty:
            continue
        df["code"] = code
        df = df[df["trade_time"].dt.strftime("%Y%m%d") == date_s]
        if df.empty:
            continue
        rows = [(r.code, r.trade_time.strftime("%Y-%m-%d %H:%M:%S"),
                 r.open, r.high, r.low, r.close, r.volume, r.amount, "a_share")
                for r in df.itertuples()]
        con.executemany(
            "INSERT OR REPLACE INTO minute_1m (code, trade_time, open, high, low, close, volume, amount, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        total += len(rows)
        n_code += 1
    con.close()
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return date_s, total, None


def run(dir_path: str = None, file_path: str = None, min_date: str = None, skip_dup: bool = True):
    if file_path:
        paths = [Path(file_path)]
    else:
        d = Path(dir_path)
        if not d.exists():
            log(f"目录不存在: {d}")
            return
        paths = sorted(d.glob("*.7z"))
        # ★2026-08-09 优化：跳过 (1) 重复副本（如 20260413(1).7z 与 20260413.7z）
        if skip_dup:
            seen = set()
            filtered = []
            for p in paths:
                stem = p.stem
                base = stem.split("(")[0]
                if base in seen:
                    continue
                seen.add(base)
                filtered.append(p)
            paths = filtered
        # ★日期过滤：只处理 min_date 之后的（历史已有 parquet，无需重复入库）
        if min_date:
            paths = [p for p in paths if p.stem.split("(")[0] >= min_date]
    log(f"待处理 7z: {len(paths)} 个" + (f"（≥{min_date}，已去重）" if min_date else ""))
    for p in paths:
        try:
            ds, n, err = ingest_7z(p)
            if n:
                log(f"  {ds}: 入库 {n} 行")
            elif err:
                log(f"  [失败] {ds}: {err}")
        except Exception as e:
            log(f"  [异常] {p.name}: {str(e)[:80]}")


def status():
    con = _conn()
    rows = con.execute(
        "SELECT substr(trade_time,1,10) d, COUNT(*), COUNT(DISTINCT code) FROM minute_1m "
        "WHERE trade_time >= '2026-01-01' GROUP BY d ORDER BY d DESC LIMIT 5").fetchall()
    n = con.execute("SELECT COUNT(*) FROM minute_1m").fetchone()[0]
    print(f"minute.db 总行数: {n:,}")
    print("2026 最近 5 天:")
    for r in rows:
        print(f"  {r[0]}: {r[1]:,} 行 / {r[2]} 只")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=str, default=None, help="每日数据目录（含 7z）")
    ap.add_argument("--file", type=str, default=None, help="单个 7z 文件")
    ap.add_argument("--min-date", type=str, default=None, help="只处理 >= 此日期（YYYYMMDD，历史已有 parquet 跳过）")
    ap.add_argument("--keep-dup", action="store_true", help="保留 (1) 重复副本（默认跳过）")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(dir_path=args.dir, file_path=args.file,
            min_date=args.min_date, skip_dup=not args.keep_dup)
