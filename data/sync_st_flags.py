"""data/sync_st_flags.py — ST 风险警示标记同步（bars.db 的 is_st 列）

★2026-10-16 背景（根因）：
  D 盘源库（掘金量化）无 ST 历史 → import_from_ddrive 导入时 is_st 硬编码为 0
  → bars.db 的 is_st 恒 0 → scan.load_st_codes() 的异常回溯（找 is_st=1 ≥50 只的日）
    也永远找不到 → 返回空集合 → **ST 过滤静默失效**（策略可能买入 ST 股）。
  check_consistency 报「is_st 覆盖率 最新日 is_st=1 0 只（正常 ~200）」即此问题。

数据源：data/cache/stock_basic.db 的 stock_basic.name（名称含 ST/*ST 即风险警示；
  本地文件、秒级、零网络）。该库由 fetcher_tushare.get_stock_list() 低频维护。

局限：名称是「当前」状态 → 最新交易日准确；历史日用当前名单近似
  （ST 摘帽/戴帽变化缓慢，`--days N` 可回溯近似标记最近 N 日）。

用法（项目根）：
  python -X utf8 data/sync_st_flags.py            # 标记最新交易日
  python -X utf8 data/sync_st_flags.py --days 10  # 回溯近似标记最近 10 个交易日

调度：晨更链「D盘行情同步」之后（增量导入会把新行 is_st 重置为 0，须在其后修正）。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR  # noqa: E402  （需先注入 BASE 到 sys.path）


def _st_codes_from_basic() -> set:
    """从 stock_basic.db 取当前 ST 名单（上市中；名称含 ST 即风险警示）"""
    p = CACHE_DIR / "stock_basic.db"
    if not p.exists():
        return set()
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return {r[0] for r in con.execute(
            "SELECT code FROM stock_basic WHERE name LIKE '%ST%' "
            "AND (status IS NULL OR status='1')")}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                    help="回溯标记最近 N 个交易日（默认 1 = 仅最新日）")
    args = ap.parse_args()

    st_codes = _st_codes_from_basic()
    if len(st_codes) < 50:
        # 名称源异常（正常 ~200 只）→ 中止血，避免把 is_st 全量清零
        print(f"[ST] 名称源仅 {len(st_codes)} 只 ST（<50 视为异常）→ 中止，不覆盖 is_st")
        return 2

    bars = CACHE_DIR / "bars.db"
    con = sqlite3.connect(bars)
    try:
        con.execute("PRAGMA synchronous=OFF")
        mx = con.execute(
            "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
        if not mx:
            print("[ST] bars.db 无 qfq 数据")
            return 1
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' AND date<=? "
            "ORDER BY date DESC LIMIT ?", (mx, args.days))]
        lo = min(days)
        n_clear = con.execute(
            "UPDATE daily_bar SET is_st=0 WHERE adjust='qfq' AND date>=?", (lo,)).rowcount
        # 临时表 + 静态 SQL：避免拼接 IN 占位符串（同时比 6000 个 ? 更快）
        con.execute("DROP TABLE IF EXISTS temp.st_tmp")
        con.execute("CREATE TEMP TABLE st_tmp (code TEXT PRIMARY KEY)")
        con.executemany("INSERT OR IGNORE INTO st_tmp (code) VALUES (?)",
                        [(c,) for c in sorted(st_codes)])
        n_set = con.execute(
            "UPDATE daily_bar SET is_st=1 WHERE adjust='qfq' AND date>=? "
            "AND code IN (SELECT code FROM st_tmp)", (lo,)).rowcount
        con.commit()
        chk = con.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE date=? AND is_st=1", (mx,)).fetchone()[0]
    finally:
        con.close()
    print(f"[ST] 标记 {lo} ~ {mx}：ST 名单 {len(st_codes)} 只 | "
          f"置 0 {n_clear} 行 / 置 1 {n_set} 行 | 最新日 is_st=1 {chk} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
