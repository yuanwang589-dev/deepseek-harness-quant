"""data/build_factor_pitch_db.py — ★因子归因业绩库构建器（用户需求 #272）

把「每只 pitch 的因子归因」×「远期池实际业绩」join 成因子维度数据库：
回答：因子 X 参与过哪些 pitch？这些 pitch 的 T+1/T+5/T+20/T+60 实际业绩如何？
→ 评估「因子 X 的推荐质量」（pitch 时因子信号 vs 实际结果）

数据源：
  1. pitch_v2_*.json（logs/）——pitch 候选的 factors 归因（{因子:值}）/ otype / signal_family / stop_plan
  2. 远期池 pitch_track（factors/opportunities/pitch_track.py load_latest）——fwd 实际业绩 + pool_type
  3. deck_decisions_*.json（logs/）——人工 buy 的 pitch_meta 归因（补充）

输出：data/cache/factor_pitch_perf.db
  表 factor_pitch：factor × pitch 展开行（1 个 pitch 多个因子 = 多行）
  表 factor_agg：因子维度聚合（pitch 次数 / T+1 T+5 平均 / 命中率 / 超额）——查询免重复计算

用法：python data/build_factor_pitch_db.py
"""
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from data.io_utils import read_json, read_text

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
DB = Path(r"data/cache/factor_pitch_perf.db")

sys.path.insert(0, str(BASE))


def _pitch_files():
    """pitch_v2 历史文件（时间升序）"""
    return sorted(glob.glob(str(LOGS / "pitch_v2*.json")), key=os.path.getmtime)


def load_pitch_factors_map() -> dict:
    """code → (日期, factors, otype, signal_family, score, stop_plan)
    取该 code 最近一次出现在 pitch_v2 的归因（按文件时间）"""
    m = {}
    for f in _pitch_files():
        try:
            d = read_json(f)
            ts = str(d.get("ts") or os.path.basename(f))
            for p in (d.get("pitch") or []):
                code = p.get("code")
                if not code:
                    continue
                m[code] = {
                    "date": ts[:10],
                    "factors": p.get("factors") or {},
                    "otype": p.get("otype", ""),
                    "signal_family": p.get("signal_family", ""),
                    "score": p.get("score"),
                    "stop_plan": p.get("stop_plan") or {},
                }
        except Exception:
            continue
    return m


def load_decisions_factors() -> dict:
    """decisions buy 的 pitch_meta factors（人工审批归因补充）"""
    m = {}
    for f in sorted(glob.glob(str(LOGS / "deck_decisions_*.json")), key=os.path.getmtime):
        try:
            for r in read_json(f):
                if isinstance(r, dict) and r.get("action") == "buy":
                    pm = r.get("pitch_meta") or {}
                    if pm.get("factors"):
                        m[r.get("code")] = pm
        except Exception:
            continue
    return m


def build():
    import factors.opportunities.pitch_track as pt  # 复用主系统远期池读取

    pool = pt.load_latest()
    entries = pool.get("entries", [])
    print(f"远期池: {len(entries)} 条")

    pitch_map = load_pitch_factors_map()
    dec_map = load_decisions_factors()
    print(f"pitch_v2 归因可匹配: {len(pitch_map)} 只 | decisions 归因: {len(dec_map)} 只")

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS factor_pitch (
        factor TEXT, code TEXT, entry_date TEXT, otype TEXT, pool_type TEXT,
        signal_family TEXT, score REAL, stop_loss_pct REAL,
        t1 REAL, t5 REAL, t20 REAL, t60 REAL, t5_done INTEGER,
        mkt_t1 REAL, excess_t1 REAL, src TEXT,
        PRIMARY KEY(factor, code, entry_date))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fp_factor ON factor_pitch(factor)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fp_date ON factor_pitch(entry_date)")
    con.execute("""CREATE TABLE IF NOT EXISTS factor_agg (
        factor TEXT PRIMARY KEY, n_pitch INTEGER, n_done_t5 INTEGER,
        t1_avg REAL, t5_avg REAL, t5_win REAL, excess_avg REAL,
        srcs TEXT)""")
    con.execute("DELETE FROM factor_pitch")
    con.execute("DELETE FROM factor_agg")

    n_row = 0
    n_covered = 0
    per_factor = defaultdict(list)
    for e in entries:
        code = e["code"]
        pm = dec_map.get(code) or pitch_map.get(code)
        factors = (pm or {}).get("factors") or {}
        if not factors:
            continue  # 无因子归因的旧批次（如实不入库，覆盖率见统计）
        n_covered += 1
        otype = (pm or {}).get("otype") or e.get("otype", "")
        fam = (pm or {}).get("signal_family") or ""
        sp = (pm or {}).get("stop_plan") or e.get("stop_plan") or {}
        fwd = e.get("fwd") or {}
        t1 = (fwd.get("t1") or {}).get("ret")
        t5 = (fwd.get("t5") or {}).get("ret")
        t20 = (fwd.get("t20") or {}).get("ret")
        t60 = (fwd.get("t60") or {}).get("ret")
        mkt1 = (fwd.get("t1") or {}).get("mkt_ret")
        excess = (t1 - mkt1) if (t1 is not None and mkt1 is not None) else None
        src = e.get("pool_type") or "legacy"
        for fname, fval in factors.items():
            con.execute("""INSERT INTO factor_pitch VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fname, code, e.get("entry_date", ""), otype, src,
                         fam, (pm or {}).get("score") or e.get("score"), sp.get("stop_loss_pct"),
                         t1, t5, t20, t60, 1 if t5 is not None else 0,
                         mkt1, excess, src))
            per_factor[fname].append({"t1": t1, "t5": t5, "excess": excess})
            n_row += 1

    # 因子聚合
    for fname, lst in per_factor.items():
        t1s = [x["t1"] for x in lst if x["t1"] is not None]
        t5s = [x["t5"] for x in lst if x["t5"] is not None]
        exs = [x["excess"] for x in lst if x["excess"] is not None]
        con.execute("INSERT INTO factor_agg VALUES (?,?,?,?,?,?,?,?)", (
            fname, len(lst), len(t5s),
            sum(t1s) / len(t1s) if t1s else None,
            sum(t5s) / len(t5s) if t5s else None,
            sum(1 for x in t5s if x > 0) / len(t5s) if t5s else None,
            sum(exs) / len(exs) if exs else None,
            ",".join(sorted({x for x in [""]})) if False else "",
        ))

    con.commit()
    n_factors = len(per_factor)
    con.close()
    print(f"✅ 建库完成: {n_row} 行（{n_factors} 个因子）| 归因覆盖 {n_covered}/{len(entries)} = {n_covered*100//max(len(entries),1)}%")
    print(f"数据库: {DB}")
    return n_row, n_factors


if __name__ == "__main__":
    build()
