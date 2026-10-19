# -*- coding: utf-8 -*-
"""data/enable_wal.py — 全库 WAL 模式迁移（任务书 T3.1 · 2026-10-18）

为什么要 WAL
------------
SQLite 默认 rollback journal 下，写事务持排他锁，读会被阻塞；本项目 deck 看板
7×24 常驻读、bulk_loader 多进程写，两者必然撞车（2026-08-07 事故注释见
data/fix_st_flags.py:119「避免长连接复用导致 database is locked」）。
WAL（Write-Ahead Logging）下读不阻塞写、写不阻塞读，是并发场景的官方解法。

注意
----
- journal_mode 是**持久化**在库文件头的：执行一次即生效，之后每次连接自动沿用。
- 迁移本身只改文件头，不重写数据；失败不影响原库可用性。
- 回滚：`PRAGMA journal_mode=DELETE`。

用法：python data/enable_wal.py [--revert]
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "data" / "cache"


def migrate(revert: bool = False) -> int:
    target = "DELETE" if revert else "WAL"
    rows, failed = [], 0
    for db in sorted(CACHE.glob("*.db")):
        try:
            con = sqlite3.connect(str(db), timeout=30)
            try:
                before = con.execute("PRAGMA journal_mode").fetchone()[0]
                con.execute("PRAGMA busy_timeout=15000")
                after = con.execute(f"PRAGMA journal_mode={target}").fetchone()[0]
            finally:
                con.close()
            rows.append((db.name, before, after, "OK"))
        except Exception as e:  # 库被占用/损坏：如实记录，不吞
            rows.append((db.name, "-", "-", f"FAIL: {e}"))
            failed += 1

    print(f"{'库':32s} {'迁移前':10s} {'迁移后':10s} 结果")
    print("-" * 72)
    for name, b, a, st in rows:
        print(f"{name:32s} {b:10s} {a:10s} {st}")
    ok = sum(1 for r in rows if r[3] == "OK")
    print(f"\n合计 {len(rows)} 库：成功 {ok}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(migrate(revert="--revert" in sys.argv))
