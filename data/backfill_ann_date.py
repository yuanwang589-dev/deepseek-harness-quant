# -*- coding: utf-8 -*-
"""data/backfill_ann_date.py — finance.db 补 PIT 披露日（任务书 T3.3 · 2026-10-18）

背景
----
`finance.db.finance_report`（31.9 万行）没有披露日期列，只能用"一季报 4-30 /
中报 8-31 / 三季报 10-31 / 年报 4-30"的固定延迟近似（见部署记录「已知边界」）。
而 `finance_ts.db.financials_ts`（27.5 万行）有真实 `ann_date`，回测
（bt_runner / backtest_script1）已按「ann_date ≤ 月末」防前视。

本脚本把 financials_ts 的真实披露日回填到 finance_report，使两库口径一致。

口径差异（实测，勿踩）
----------------------
- `finance_report.code`   = '600519'      （6 位，无后缀）
- `financials_ts.code`    = '600519.SH'   （带交易所后缀）
  → 关联必须用 `substr(t.code, 1, 6) = f.code`

覆盖率（2026-10-18 实测）
------------------------
可关联 259,454 / 319,038 = 81.3%；剩余 18.7% 为 financials_ts 未覆盖的历史期
（financials_ts 起点 2010 年）或两库股票池差异。**不伪造**：关联不到就留空，
查询侧仍可用固定延迟兜底（`WHERE ann_date <= :asof OR ann_date IS NULL`）。

用法
----
    python data/backfill_ann_date.py            # 执行回填（幂等，只填空值）
    python data/backfill_ann_date.py --stats    # 只统计，不写库
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "data" / "cache"
FIN = CACHE / "finance.db"
TS = CACHE / "finance_ts.db"


def run(stats_only: bool = False) -> int:
    if not FIN.exists() or not TS.exists():
        print(f"缺库: finance.db={FIN.exists()} finance_ts.db={TS.exists()}")
        return 1

    con = sqlite3.connect(str(FIN), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("ATTACH ? AS ts", (str(TS),))
        cols = {r[1] for r in con.execute("PRAGMA table_info(finance_report)")}
        total = con.execute("SELECT COUNT(*) FROM finance_report").fetchone()[0]

        if "ann_date" not in cols:
            if stats_only:
                print("ann_date 列不存在（stats 模式不建列）")
            else:
                con.execute("ALTER TABLE finance_report ADD COLUMN ann_date TEXT")
                con.commit()
                print("已新增列 finance_report.ann_date")

        # 可关联行数（不依赖是否已回填）
        match = con.execute("""
            SELECT COUNT(*) FROM finance_report f
            JOIN ts.financials_ts t
              ON substr(t.code, 1, 6) = f.code AND t.end_date = f.period
            WHERE t.ann_date IS NOT NULL AND t.ann_date != ''""").fetchone()[0]

        if not stats_only:
            # 幂等：只填 NULL 行；同一 (code6, period) 多条时取最早披露日（先见先得）
            # ★性能：先在 temp 建 (code6, period) 映射并加索引，再 UPDATE 走索引查找。
            #   原 rowid 关联写法在 31.9 万行上无法用索引，实测超时（2026-10-18）。
            con.execute("DROP TABLE IF EXISTS temp._ann")
            con.execute("""
                CREATE TEMP TABLE _ann AS
                SELECT substr(code, 1, 6) AS code6, end_date AS period, MIN(ann_date) AS ann
                FROM ts.financials_ts
                WHERE ann_date IS NOT NULL AND ann_date != ''
                GROUP BY substr(code, 1, 6), end_date""")
            con.execute("CREATE INDEX temp._ann_idx ON _ann(code6, period)")
            con.execute("""
                UPDATE finance_report
                SET ann_date = (
                    SELECT ann FROM _ann
                    WHERE _ann.code6 = finance_report.code
                      AND _ann.period = finance_report.period)
                WHERE ann_date IS NULL
                  AND EXISTS (
                    SELECT 1 FROM _ann
                    WHERE _ann.code6 = finance_report.code
                      AND _ann.period = finance_report.period)""")
            con.commit()

        filled = con.execute(
            "SELECT COUNT(*) FROM finance_report WHERE ann_date IS NOT NULL").fetchone()[0] \
            if "ann_date" in {r[1] for r in con.execute("PRAGMA table_info(finance_report)")} else 0

        print(f"总行数        : {total:,}")
        print(f"可关联行      : {match:,} ({match / total:.1%})")
        print(f"ann_date 非空 : {filled:,} ({filled / total:.1%})")
        print(f"未覆盖        : {total - filled:,}（financials_ts 起点 2010 前 / 股票池差异）")
        if stats_only:
            print("（stats 模式，未写库）")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(run(stats_only="--stats" in sys.argv))
