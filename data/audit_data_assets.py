"""data/audit_data_assets.py — ★D 盘数据资产审计（用户需求 #273，2026-08-13）

盘点 data/cache/ 全部数据资产：
  1. 文件清单（大小 / 最近修改——冷热判断）
  2. 各 SQLite 库的表结构 + 行数 + 最新数据日（数据新鲜度）
  3. 重复/冗余检测（同前缀多库：lhb* / finance* / minute* / bars_incr*）
  4. 冷数据候选（>30 天未更新 / 测试残留 / 历史 CSV）

输出：report/data_asset_audit_YYYYMMDD_HHMMSS.json + .md
     ——作为未来优化数据结构的依据（总指挥指示）

用法：python data/audit_data_assets.py
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
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CACHE = Path(r"data/cache")
REPORT = Path(__file__).resolve().parent.parent / "report"

# 冷数据阈值：最新修改 > N 天未动视为冷
COLD_DAYS = 30
# 已知主库（重点盘表）
PRIMARY_DBS = ["bars.db", "finance.db", "finance_ts.db", "finance_quality.db",
               "minute.db", "minute_test.db", "hist_mv.db", "lhb_full.db",
               "lhb.db", "lhb2.db", "rzrq_full.db", "gdhs_full.db", "stock_basic.db"]


def _immutable(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=3)


def scan_files():
    files = []
    for p in CACHE.iterdir():
        if p.is_file():
            st = p.stat()
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                          "ext": p.suffix.lower()})
    # 分组统计
    by_ext = defaultdict(lambda: {"n": 0, "size": 0})
    for f in files:
        by_ext[f["ext"]]["n"] += 1
        by_ext[f["ext"]]["size"] += f["size"]
    # 时间戳前缀分组（重复检测：同前缀多个文件）
    by_prefix = defaultdict(list)
    for f in files:
        base = f["name"]
        for sep in ("_2026", "_20260", "_2025"):
            if sep in base:
                base = base.split(sep)[0]
                break
        by_prefix[base].append(f)
    dup_prefixes = {k: v for k, v in by_prefix.items() if len(v) > 1}
    return files, dict(by_ext), dup_prefixes


def scan_db(name: str):
    p = CACHE / name
    if not p.exists():
        return None
    try:
        con = _immutable(str(p))
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        out = {"name": name, "size": p.stat().st_size, "tables": []}
        for t in tables:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
            except Exception:
                n = -1
            # 最新日期（有 date 列的表）
            maxd = None
            cols = [r[1] for r in con.execute(f"PRAGMA table_info(\"{t}\")").fetchall()]
            for c in ("date", "trade_date", "day", "dt"):
                if c in cols:
                    try:
                        maxd = con.execute(f"SELECT MAX(\"{c}\") FROM \"{t}\"").fetchone()[0]
                    except Exception as _dsh_exc:
                        _dsh_swallow(_dsh_exc, "data/audit_data_assets.py:84 scan_db")
                    break
            out["tables"].append({"name": t, "rows": n, "max_date": maxd})
        con.close()
        return out
    except Exception as e:
        return {"name": name, "size": p.stat().st_size if p.exists() else 0,
                "error": str(e)[:80], "tables": []}


def main():
    files, by_ext, dup_prefixes = scan_files()
    now = datetime.now().timestamp()
    # 冷文件（30 天未动 + 非空）
    cold = [f for f in files if now - f["mtime"] > COLD_DAYS * 86400 and f["size"] > 0]

    dbs = []
    for name in PRIMARY_DBS:
        r = scan_db(name)
        if r:
            dbs.append(r)

    # 增量库统计
    incr = [f for f in files if "bars_incr" in f["name"]]
    incr_size = sum(f["size"] for f in incr)

    # 重复库组（前缀聚类）
    dup_groups = {}
    for pre in ("lhb", "finance", "minute", "bars_incr", "rzrq", "gdhs", "hist_mv"):
        grp = [f["name"] for f in files if f["name"].startswith(pre)]
        if len(grp) > 1:
            dup_groups[pre] = grp

    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_files": len(files),
        "total_size": sum(f["size"] for f in files),
        "by_ext": by_ext,
        "cold_files": sorted(cold, key=lambda x: -x["size"])[:30],
        "incr_dbs": {"n": len(incr), "size": incr_size},
        "dup_groups": dup_groups,
        "dbs": dbs,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORT.mkdir(exist_ok=True)
    jp = REPORT / f"data_asset_audit_{ts}.json"
    jp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # md 报告
    lines = [f"# D 盘数据资产审计 {out['ts']}", "",
             f"- 文件总数: {out['total_files']} | 总大小: {out['total_size']/2**30:.2f} GB",
             f"- 冷文件候选（>30 天未动）: {len(out['cold_files'])} 个",
             f"- 增量库: {out['incr_dbs']['n']} 个 {out['incr_dbs']['size']/2**20:.0f} MB",
             "", "## 重复/冗余库组", ""]
    for pre, grp in dup_groups.items():
        lines.append(f"- **{pre}**: {', '.join(grp)}")
    lines += ["", "## 主库明细", ""]
    for d in dbs:
        lines.append(f"### {d['name']}（{d.get('size',0)/2**20:.0f} MB）")
        if d.get("error"):
            lines.append(f"- ❌ {d['error']}")
        for t in d["tables"]:
            lines.append(f"- {t['name']}: {t['rows']:,} 行 | 最新 {t['max_date']}")
    lines += ["", "## 冷文件 Top 15", ""]
    for f in out["cold_files"][:15]:
        age = (now - f["mtime"]) / 86400
        lines.append(f"- {f['name']}: {f['size']/2**20:.1f} MB | {age:.0f} 天未动")
    mp = REPORT / f"data_asset_audit_{ts}.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 审计完成: {out['total_files']} 文件 {out['total_size']/2**30:.2f}GB | "
          f"冷 {len(out['cold_files'])} | 增量库 {out['incr_dbs']['n']}")
    print(f"报告: {mp.name}")
    return out


if __name__ == "__main__":
    main()
