# -*- coding: utf-8 -*-
"""从 D 盘股票数据库导入全市场日线到本项目 bars.db

源：D:/quant_data/quant_data/enhanced_quant_database.db（掘金量化项目库，220GB）
  - market_data_daily：tushare_qfq 前复权日线（13.86M 行 / 5569 只 / 2010-2026）
  - daily_basic：换手率（2025-11 起，覆盖近期）
目标：data/cache/bars.db daily_bar + bar_meta（本项目唯一读取接口）

★单位约定：源库 volume=股、amount=元（实测 amount/volume≈均价验证）。
  source 标记 'local_import'（非 tushare/tushare_backup），
  normalize_units / scan._amount_20d_yi 均按 元/股 直通，禁止 ×1000/×100。
★幂等：INSERT OR REPLACE，D 盘库更新后重跑即可增量覆盖同步。
★局限：is_st 恒 0（源库无 ST 历史）；delisted_raw 源 21 行未导入（单位约定不明）。

★2026-10-15 增量改造（根治晨更链 1200s 超时）：
  背景：原实现全量重导 13.86M 行（含 79 万行换手率 temp 表 + 全表 LAG 窗口函数补 preclose +
        bar_meta 全量 GROUP BY），耗时从 8 月的 ~6 分钟涨到 9 月底 ~20 分钟，
        9/22 起连续 16 个交易日撞上晨更链 1200s 超时被 kill → bars.db 停在 09-18。
  方案：默认【增量】——只导源库比目标库新的交易日（1 天 ~5560 行，秒级完成）；
        preclose/pct_chg 用「目标库上一日 close + 窗口内滚动」补齐（源库 9 月中下旬起
        pre_close/pct_chg 全 NULL，2026-10-15 实测 09-21~10-14 66698 行全 NULL，必须补）。
  权衡：增量不捕获源库对旧日的复权修正 → 保留 --full 全量模式（每周校准 / 复权调整后跑一次）。

用法：python data/import_from_ddrive.py [--full] [--since YYYYMMDD] [--retry-sleep 600]
  --full         全量重导（原逻辑，保留 LAG 补齐），用于复权校准；日常勿用
  --since YYYYMMDD  手动指定增量起点（默认自动取目标库最新交易日，如 20260918）
  --retry-sleep 秒数：D 盘源库被掘金项目活跃写入导致读页撕裂（malformed）时，
  等待后重试（默认 10 分钟，最多 5 次）。幂等，重试安全。
"""
import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR, _SCHEMA

SRC_DB = r"D:\quant_data\quant_data\enhanced_quant_database.db"


def _mount_src(con, src: Path):
    """D 盘源库只读挂载（mode=ro）+ 写入自检（物理只读硬约束）"""
    con.execute("ATTACH DATABASE ? AS src", (src.as_uri() + "?mode=ro",))
    try:
        con.execute("CREATE TABLE src._ro_guard(x)")
        raise RuntimeError("D 盘源库意外可写！立即中止")
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e).lower():
            raise
    print("[导入] D 盘源库已只读挂载（写入自检通过）")


# ★2026-10-15 排除非股票行（SH./SZ. 前缀指数，如 fetch_index_daily 写的 SH.000300
#   adjust='none' 10-15 行），否则 MAX(date) 会取到指数行导致增量起点前移、跳过同步。
#   与 cache.latest_trade_date() 的过滤口径一致（SQLite LIKE 不区分大小写，兼容 SH./sh.）。
#   注：用完整 SQL 常量而非 f-string 拼过滤条件——常量无用户输入、无注入面。
_SQL_MAX_STOCK_DATE = ("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' "
                       "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")
_SQL_CNT_STOCK_DATE = ("SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date=? "
                       "AND adjust='qfq' AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")
_SQL_CNT_NULL_PRE = ("SELECT COUNT(*) FROM daily_bar WHERE date=? AND preclose IS NULL "
                     "AND adjust='qfq' AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")
_SQL_CNT_NULL_PCT = ("SELECT COUNT(*) FROM daily_bar WHERE date=? AND pct_chg IS NULL "
                     "AND adjust='qfq' AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")


def _last_local_date(con) -> int:
    """目标库最新股票 qfq 交易日 → int（20260918）；空库返回 0（触发全量）"""
    r = con.execute(_SQL_MAX_STOCK_DATE).fetchone()
    if not r or not r[0]:
        return 0
    return int(str(r[0]).replace("-", ""))


def _run_incremental(dst: Path, since: int) -> int:
    """增量导入：只导 trade_date_int > since 的源库行，Python 滚动补 preclose/pct_chg"""
    con = sqlite3.connect(dst.as_uri(), uri=True)
    t0 = time.time()
    try:
        con.executescript(_SCHEMA)
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-2000000")  # 2GB 页缓存
        _mount_src(con, Path(SRC_DB))

        # 换手率 temp 表：仅增量窗口（1 天 ~5500 行，补历史 16 天 ~8.8 万行）
        con.execute("DROP TABLE IF EXISTS temp.turn_src")
        con.execute(
            "CREATE TEMP TABLE turn_src AS "
            "SELECT ts_code, CAST(trade_date AS INTEGER) AS d, turnover_rate "
            "FROM src.daily_basic WHERE turnover_rate IS NOT NULL "
            "AND CAST(trade_date AS INTEGER) > ?", (since,))
        con.execute("CREATE INDEX temp.idx_turn ON turn_src (ts_code, d)")
        n_turn = con.execute("SELECT COUNT(*) FROM turn_src").fetchone()[0]
        print(f"[增量] 换手率窗口就绪: {n_turn} 行（>{since}）")

        # 增量行情行（官方 pre_close/pct_chg 常为 NULL，后续滚动补）
        cur = con.execute(
            """
            SELECT m.symbol, m.datetime, m.open, m.high, m.low, m.close,
                   m.pre_close, m.volume, m.amount, t.turnover_rate, m.pct_chg
            FROM src.market_data_daily m
            LEFT JOIN turn_src t ON t.ts_code = m.symbol AND t.d = m.trade_date_int
            WHERE m.data_source = 'tushare_qfq' AND m.trade_date_int > ?
            """, (since,))
        rows = cur.fetchall()
        if not rows:
            print(f"[增量] 无新行（目标库已是最新 {since}），直接收尾")
            con.commit()
            return 0
        print(f"[增量] 拉取 {len(rows)} 行（>{since}），{(time.time()-t0):.0f}s")

        # 滚动补 preclose/pct_chg：起点 = 各股在「增量窗口最早日之前」的最后收盘价
        # ★语义：必须严格取 date < min_dt 的最近 close——否则幂等重跑时会把窗口内的
        #   close 当起点（如 09-21 的 preclose 错用 10-14 close）。
        # ★性能：逐只走 idx_daily_bar(code,adjust,date) 索引点查（5.6K 只 ~2s）；
        #   原「daily_bar 全表 GROUP BY code 取 MAX(date)」需 163s（5560 只≈全市场=全表扫描）。
        min_dt = min(r[1] for r in rows)  # 增量窗口最早交易日（如 2026-09-21）
        codes = sorted({r[0] for r in rows})
        last_close = {}
        for c in codes:
            row = con.execute(
                "SELECT close FROM daily_bar WHERE code=? AND adjust='qfq' AND date<? "
                "ORDER BY date DESC LIMIT 1", (c, min_dt)).fetchone()
            if row and row[0] is not None:
                last_close[c] = row[0]
        print(f"[增量] 起点 close: {len(last_close)}/{len(codes)} 只（窗口 <{min_dt}，"
              f"{(time.time()-t0):.0f}s）")

        by_code = defaultdict(list)
        for r in rows:
            by_code[r[0]].append(r)
        final = []
        n_pre_fill = 0
        n_pct_fill = 0
        for code, rs in by_code.items():
            rs.sort(key=lambda r: r[1])  # datetime 升序
            prev = last_close.get(code)
            for r in rs:
                (_sym, dt, o, h, lo, c, pre, vol, amt, turn, pct) = r
                if pre is None:
                    pre = prev
                    n_pre_fill += 1
                if pct is None and pre and pre > 0:
                    pct = round((c - pre) / pre * 100, 4)
                    n_pct_fill += 1
                final.append((_sym, dt, o, h, lo, c, pre, vol, amt, turn, pct,
                              0, 'qfq', 'local_import'))
                prev = c
        print(f"[增量] preclose 滚动补 {n_pre_fill} 行 / pct_chg 补 {n_pct_fill} 行")

        con.executemany(
            "INSERT OR REPLACE INTO daily_bar "
            "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", final)
        print(f"[增量] 写入 {len(final)} 行（{(time.time()-t0):.0f}s）")

        _rebuild_meta(con)
        con.commit()
        print(f"[增量] 提交完成，总耗时 {time.time()-t0:.0f}s")
    finally:
        con.close()
    return len(final)


def _rebuild_meta(con):
    """bar_meta 全量重建（GROUP BY 全表，重跑安全；~13.9M 行）"""
    con.execute("DELETE FROM bar_meta")
    con.execute(
        """
        INSERT INTO bar_meta (code, adjust, start_date, end_date, rows, updated_at)
        SELECT code, adjust, MIN(date), MAX(date), COUNT(*), datetime('now')
        FROM daily_bar GROUP BY code, adjust
        """)
    n = con.execute("SELECT COUNT(*) FROM bar_meta").fetchone()[0]
    print(f"[增量] bar_meta 重建 {n} 行")


def _run_full(dst: Path) -> int:
    """全量重导（原逻辑）：13.86M 行 INSERT OR REPLACE + 源库 LAG 补 preclose。
    仅用于复权校准 / 增量起点异常时；日常晨更走 _run_incremental。"""
    con = sqlite3.connect(dst.as_uri(), uri=True)
    t0 = time.time()
    try:
        con.executescript(_SCHEMA)
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-2000000")  # 2GB 页缓存
        _mount_src(con, Path(SRC_DB))

        con.execute("DROP TABLE IF EXISTS temp.turn_src")
        con.execute(
            "CREATE TEMP TABLE turn_src AS "
            "SELECT ts_code, CAST(trade_date AS INTEGER) AS d, turnover_rate "
            "FROM src.daily_basic WHERE turnover_rate IS NOT NULL")
        con.execute("CREATE INDEX temp.idx_turn ON turn_src (ts_code, d)")
        n_turn = con.execute("SELECT COUNT(*) FROM turn_src").fetchone()[0]
        print(f"[全量] 换手率表就绪: {n_turn} 行")

        cur = con.execute(
            """
            INSERT OR REPLACE INTO daily_bar
              (code, date, open, high, low, close, preclose,
               volume, amount, turn, pct_chg, is_st, adjust, source)
            SELECT m.symbol, m.datetime, m.open, m.high, m.low, m.close, m.pre_close,
                   m.volume, m.amount, t.turnover_rate, m.pct_chg, 0, 'qfq', 'local_import'
            FROM src.market_data_daily m
            LEFT JOIN turn_src t
                   ON t.ts_code = m.symbol AND t.d = m.trade_date_int
            WHERE m.data_source = 'tushare_qfq'
            """)
        n_bar = cur.rowcount
        print(f"[全量] daily_bar 写入 {n_bar} 行（{time.time()-t0:.0f}s）")

        # 源库近期 pre_close/pct_chg 大量 NULL（2026-09 中下旬起实测全 NULL）
        # -> LAG(close) 按股票推算前收盘（仅填 NULL，不覆盖官方值，官方值含除权调整）
        con.execute("DROP TABLE IF EXISTS temp.fixp")
        con.execute(
            "CREATE TEMP TABLE fixp AS "
            "SELECT code, date, LAG(close) OVER (PARTITION BY code ORDER BY date) AS pc "
            "FROM daily_bar WHERE adjust='qfq'")
        con.execute("CREATE INDEX temp.idx_fixp ON fixp (code, date)")
        cur = con.execute(
            "UPDATE daily_bar SET preclose = "
            "(SELECT pc FROM fixp WHERE fixp.code=daily_bar.code AND fixp.date=daily_bar.date) "
            "WHERE preclose IS NULL")
        print(f"[全量] preclose 补齐 {cur.rowcount} 行")
        cur = con.execute(
            "UPDATE daily_bar SET pct_chg = ROUND((close-preclose)/preclose*100, 4) "
            "WHERE pct_chg IS NULL AND preclose > 0")
        print(f"[全量] pct_chg 重算 {cur.rowcount} 行")

        _rebuild_meta(con)
        con.commit()
        print(f"[全量] 提交完成，总耗时 {time.time()-t0:.0f}s")
    finally:
        con.close()
    return n_bar


def _verify(dst: Path):
    """验证：总行数 / 最新交易日股票数（≥4000 门槛）/ 换手率覆盖 / 补值质量
    ★2026-10-15 最新日只认股票行（排除 SH./SZ. 指数），对齐 cache.latest_trade_date()"""
    con = sqlite3.connect(dst)
    try:
        total = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
        mx = con.execute(_SQL_MAX_STOCK_DATE).fetchone()[0]
        n_latest = con.execute(_SQL_CNT_STOCK_DATE, (mx,)).fetchone()[0]
        turn_cov = con.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE turn IS NOT NULL").fetchone()[0]
        null_pre = con.execute(_SQL_CNT_NULL_PRE, (mx,)).fetchone()[0]
        null_pct = con.execute(_SQL_CNT_NULL_PCT, (mx,)).fetchone()[0]
        sample = con.execute(
            "SELECT code, date, close, preclose, pct_chg, volume, amount, turn, source "
            "FROM daily_bar WHERE code='000001.SZ' ORDER BY date DESC LIMIT 1").fetchone()
    finally:
        con.close()
    print(f"[验证] 总行数 {total}，最新股票日 {mx}（{n_latest} 只，门槛 4000）"
          f"，换手率覆盖 {turn_cov} 行，最新日 preclose NULL {null_pre} / pct_chg NULL {null_pct}")
    print(f"[验证] 平安银行样例: {sample}")
    return n_latest >= 4000 and null_pre == 0 and null_pct == 0


def main() -> int:
    """带写入冲突重试的入口：D 盘源库被掘金项目活跃写入时读页可能撕裂
    （sqlite3.DatabaseError: malformed，实测 2026-08-21 13:46 持续 30+ 分钟），
    等待后重试；重试 5 次仍失败才放弃。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重导（复权校准用，日常勿用）")
    ap.add_argument("--since", type=str, default=None, help="增量起点 YYYYMMDD（默认自动取目标库最新）")
    ap.add_argument("--retry-sleep", type=int, default=600)
    args = ap.parse_args()

    src = Path(SRC_DB)
    if not src.exists():
        print(f"[导入] 源库不存在: {src}")
        return 1
    dst = CACHE_DIR / "bars.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[导入] 源: {src}")
    print(f"[导入] 目标: {dst}")

    for attempt in range(1, 6):
        try:
            if args.full:
                _run_full(dst)
            else:
                # 增量起点：--since 显式 > 自动取目标库最新日
                con = sqlite3.connect(dst.as_uri(), uri=True)
                try:
                    auto = _last_local_date(con)
                finally:
                    con.close()
                since = int(args.since) if args.since else auto
                if since and since >= 0:
                    src_max = _src_max_date()
                    if src_max <= since:
                        print(f"[增量] 源库最新 {src_max} ≤ 目标库 {since}，无新数据")
                        return 0
                print(f"[增量] 起点 since={since}（{'手动' if args.since else '自动=目标库最新'}）")
                _run_incremental(dst, since)
            ok = _verify(dst)
            print("[完成] 耗时" if ok else "[警告] 验证未通过（见上方）")
            return 0 if ok else 2
        except sqlite3.DatabaseError as e:
            if "malformed" not in str(e).lower():
                raise
            print(f"[重试] 第 {attempt} 次读取 D 盘源库失败（源库正被活跃写入，读页撕裂）：{str(e)[:60]}")
            print(f"[重试] 等待 {args.retry_sleep}s 后重试（幂等，安全）...")
            time.sleep(args.retry_sleep)
    print("[导入] 重试 5 次均失败：D 盘源库持续处于写入中，请稍后手动重跑")
    return 1


def _src_max_date() -> int:
    """源库最新交易日（int）"""
    con = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True, timeout=8)
    try:
        r = con.execute("SELECT MAX(trade_date_int) FROM market_data_daily").fetchone()
        return r[0] if r and r[0] else 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
