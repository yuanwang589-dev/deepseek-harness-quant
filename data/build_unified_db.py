"""data/build_unified_db.py — ★运行数据整合库（用户需求 #273 落地，2026-08-13）

data/cache/unified.db —— 统一查询入口 + 数据资产管理：
  1. asset_inventory  数据资产清单（审计快照：文件/库/表/大小/最新日）
  2. data_lineage     数据链路映射（db → 消费代码 → 更新频率）——防删错的关键
  3. factor_pitch_perf 因子归因业绩（从 factor_pitch_perf.db 迁移，统一查询）
  ★2026-08-14 Phase2 扩展（自有数据库蓝图 v1）：
  4. macro_series     宏观序列统一表（社融/国债利率/EPU——替代散落 macro.db/CSV）
  5. event_series     事件数据统一索引（龙虎榜/两融/社保/股东户数——跨库目录）
  6. minute_factors   分钟因子产物索引（intraday/kline5m parquet 元数据）
  7. dual_source      双源对照表（finance.db vs finance_ts.db 等，口径/最新日/健康）

用法：python data/build_unified_db.py   （幂等：重建全部表，保留 factor 数据并 upsert）
"""
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from data.io_utils import read_json, read_text

BASE = Path(__file__).resolve().parent.parent
CACHE = Path(r"data/cache")
UNIFIED = CACHE / "unified.db"
OLD_FP = CACHE / "factor_pitch_perf.db"
REPORT = BASE / "report"


def _con(db):
    return sqlite3.connect(str(db))


def build_inventory(con):
    """从最新审计报告导入资产清单"""
    aud = sorted(glob.glob(str(REPORT / "data_asset_audit_*.json")), key=os.path.getmtime)
    con.execute("""CREATE TABLE IF NOT EXISTS asset_inventory (
        ts TEXT, name TEXT, kind TEXT, size INTEGER, detail TEXT)""")
    con.execute("DELETE FROM asset_inventory")
    if not aud:
        return 0
    d = read_json(aud[-1])
    ts = d["ts"]
    n = 0
    # 文件级
    for f in d.get("cold_files", []):
        con.execute("INSERT INTO asset_inventory VALUES (?,?,?,?,?)",
                    (ts, f["name"], "cold_file", f["size"], "冷数据候选"))
        n += 1
    for grp, names in d.get("dup_groups", {}).items():
        con.execute("INSERT INTO asset_inventory VALUES (?,?,?,?,?)",
                    (ts, grp, "dup_group", 0, "重复/冗余组: " + ",".join(names)))
        n += 1
    # 库级
    for db in d.get("dbs", []):
        for t in db.get("tables", []):
            con.execute("INSERT INTO asset_inventory VALUES (?,?,?,?,?)",
                        (ts, f"{db['name']}::{t['name']}", "table",
                         t.get("rows", 0) or 0,
                         f"最新 {t.get('max_date') or '—'}"))
            n += 1
    # ★2026-08-14 Phase2：实时盘点 cache/ 全部文件（不依赖审计报告，直接扫）
    try:
        for f in sorted(CACHE.glob("*")):
            if f.is_file():
                sz = f.stat().st_size
                kind = "db" if f.suffix == ".db" else ("csv" if f.suffix == ".csv" else "other")
                con.execute("INSERT INTO asset_inventory VALUES (?,?,?,?,?)",
                            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f.name, kind,
                             sz, f"实时盘点 {datetime.now().strftime('%m-%d')}"))
                n += 1
    except Exception:
        pass
    return n


def build_lineage(con):
    """扫描代码库 .db 引用 → 数据链路映射"""
    con.execute("""CREATE TABLE IF NOT EXISTS data_lineage (
        db_name TEXT PRIMARY KEY, consumers TEXT, updated TEXT)""")
    con.execute("DELETE FROM data_lineage")
    refs = {}
    # 管理/审计脚本自身的 .db 字符串引用不算真实消费（避免误报）
    EXCLUDE = ("audit_data_assets.py", "build_unified_db.py", "build_factor_pitch_db.py")
    # ★2026-08-14 Phase2：因子池侧也纳入血缘（事件库在因子池消费）
    roots = [BASE, Path(r"data/factorpool")]
    for root in roots:
        for p in root.rglob("*.py"):
            if "site-packages" in str(p) or "__pycache__" in str(p):
                continue
            if p.name in EXCLUDE:
                continue
            try:
                src = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in re.finditer(r"([a-zA-Z0-9_]+\.db)", src):
                dbn = m.group(1)
                rel = str(p.relative_to(root)).replace("\\", "/")
                refs.setdefault(dbn, set()).add(rel)
    for dbn, consumers in sorted(refs.items()):
        con.execute("INSERT INTO data_lineage VALUES (?,?,?)",
                    (dbn, ";".join(sorted(consumers)[:15]), datetime.now().strftime("%Y-%m-%d")))
    return len(refs)


# ════════════════════════════════════════════════════════════
# ★2026-08-14 Phase2：基础数据整合层
# ════════════════════════════════════════════════════════════

def build_macro_series(con):
    """宏观序列统一表：从 macro.db 迁移（社融/国债利率），EPU 从 epu.db（若存在）
    ——替代散落多源，unified.db 成为宏观唯一查询入口。"""
    con.execute("""CREATE TABLE IF NOT EXISTS macro_series (
        series TEXT, period TEXT, value REAL,
        PRIMARY KEY (series, period))""")
    con.execute("DELETE FROM macro_series")
    n = 0
    for dbn, tbl, series_map in [
        ("macro.db", "social_finance", {"social_finance": "sf_increment"}),
        ("macro.db", "bond_yield", {"bond_yield": "y10"}),
    ]:
        p = CACHE / dbn
        if not p.exists():
            continue
        try:
            src = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
            for series, col in series_map.items():
                for row in src.execute(f"SELECT {col} FROM {tbl}").fetchall():
                    if row[0] is not None:
                        con.execute("INSERT OR REPLACE INTO macro_series VALUES (?,?,?)",
                                    (series, row[0], row[0]))
            # bond_yield 存 y10+y2 两列 → 按列展开
            if tbl == "bond_yield":
                for r in src.execute("SELECT * FROM bond_yield").fetchall():
                    if r[1] is not None:
                        con.execute("INSERT OR REPLACE INTO macro_series VALUES (?,?,?)",
                                    ("y10", r[0], r[1]))
                        n += 1
                    if r[2] is not None:
                        con.execute("INSERT OR REPLACE INTO macro_series VALUES (?,?,?)",
                                    ("y2", r[0], r[2]))
                        n += 1
            src.close()
        except Exception:
            pass
    return n


def build_event_series(con):
    """事件数据统一索引：盘点各事件库的最新日/行数（目录层，不拷贝数据）
    ——让"事件数据在哪、多新"一眼可见（跨 lhb/gdhs/shebao/rzrq/dzjy）。"""
    con.execute("""CREATE TABLE IF NOT EXISTS event_series (
        source TEXT PRIMARY KEY, rows INTEGER, max_date TEXT, updated TEXT)""")
    con.execute("DELETE FROM event_series")
    checks = [
        ("lhb_full.db", "lhb_detail", "date"),
        ("gdhs_full.db", None, None),
        ("shebao.db", None, None),
        ("rzrq_full.db", None, None),
        ("stock_dividend.db", None, None),
    ]
    n = 0
    for dbn, tbl, date_col in checks:
        p = CACHE / dbn
        if not p.exists():
            continue
        try:
            src = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
            tbs = [r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            total = 0
            mx = None
            for t in tbs[:3]:
                try:
                    cnt = src.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    total += cnt
                except Exception:
                    continue
                if date_col:
                    try:
                        mx = src.execute(
                            f"SELECT MAX({date_col}) FROM [{t}]").fetchone()[0]
                    except Exception:
                        pass
            con.execute("INSERT OR REPLACE INTO event_series VALUES (?,?,?,?)",
                        (dbn, total, mx, datetime.now().strftime("%Y-%m-%d")))
            n += 1
            src.close()
        except Exception:
            pass
    return n


def build_minute_factors(con):
    """分钟因子产物索引：intraday/kline5m parquet 元数据（大小/更新日）
    ——让分钟数据形态（zip 原始/parquet 因子/增量）一目了然。"""
    con.execute("""CREATE TABLE IF NOT EXISTS minute_factors (
        name TEXT PRIMARY KEY, size_bytes INTEGER, mtime TEXT, note TEXT)""")
    con.execute("DELETE FROM minute_factors")
    root = Path(r"data/minute")
    n = 0
    for f in root.glob("*.parquet"):
        try:
            con.execute("INSERT OR REPLACE INTO minute_factors VALUES (?,?,?,?)",
                        (f.name, f.stat().st_size,
                         datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"),
                         "分钟因子产物"))
            n += 1
        except Exception:
            pass
    # 增量 parquet 统计
    ip = root / "incr_parquet"
    if ip.exists():
        try:
            fs = list(ip.glob("*.parquet"))
            total = sum(f.stat().st_size for f in fs)
            mx = max((datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                      for f in fs), default="—")
            con.execute("INSERT OR REPLACE INTO minute_factors VALUES (?,?,?,?)",
                        ("incr_parquet/", total, mx, f"7z 增量 parquet {len(fs)} 个"))
            n += 1
        except Exception:
            pass
    return n


def build_dual_source(con):
    """双源对照表：finance.db vs finance_ts.db（口径/最新日/行数）——双源统一决策依据"""
    con.execute("""CREATE TABLE IF NOT EXISTS dual_source (
        group_name TEXT, source TEXT, rows INTEGER, max_date TEXT, note TEXT,
        PRIMARY KEY (group_name, source))""")
    con.execute("DELETE FROM dual_source")
    n = 0
    for grp, dbn, tbl, date_col, note in [
        ("finance", "finance.db", "finance_report", "period", "baostock 单季（旧）"),
        ("finance", "finance_ts.db", "financials_ts", "ann_date", "Tushare 三表 PIT（新）"),
        ("quality", "finance_quality.db", "quality", None, "质量表（baostock/tushare 双源写入）"),
    ]:
        p = CACHE / dbn
        if not p.exists():
            continue
        try:
            src = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
            cnt = src.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
            mx = None
            if date_col:
                try:
                    mx = src.execute(f"SELECT MAX({date_col}) FROM [{tbl}]").fetchone()[0]
                except Exception:
                    pass
            con.execute("INSERT OR REPLACE INTO dual_source VALUES (?,?,?,?,?)",
                        (grp, dbn, cnt, mx, note))
            n += 1
            src.close()
        except Exception:
            pass
    return n


def main():
    con = _con(UNIFIED)
    n_inv = build_inventory(con)
    n_line = build_lineage(con)
    # 迁移 factor 数据（factor_pitch_perf.db → unified.db）
    if OLD_FP.exists():
        old = sqlite3.connect(f"file:{OLD_FP}?mode=ro&immutable=1", uri=True)
        for tbl in ("factor_pitch", "factor_agg"):
            cols = [r[1] for r in old.execute(f"PRAGMA table_info({tbl})").fetchall()]
            con.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({', '.join(cols)})")
            con.execute(f"DELETE FROM {tbl}")
            for row in old.execute(f"SELECT * FROM {tbl}"):
                con.execute(f"INSERT INTO {tbl} VALUES ({','.join('?'*len(cols))})", row)
        old.close()
    n_fp = con.execute("SELECT COUNT(*) FROM factor_pitch").fetchone()[0]
    # ★2026-08-14 Phase2：基础整合层
    n_macro = build_macro_series(con)
    n_event = build_event_series(con)
    n_min = build_minute_factors(con)
    n_dual = build_dual_source(con)
    con.commit()
    con.close()
    print(f"✅ unified.db 建成: {UNIFIED}")
    print(f"  asset_inventory {n_inv} 行 | data_lineage {n_line} 库 | factor_pitch {n_fp} 行")
    print(f"  ★Phase2: macro_series {n_macro} | event_series {n_event} | minute_factors {n_min} | dual_source {n_dual}")


if __name__ == "__main__":
    main()
