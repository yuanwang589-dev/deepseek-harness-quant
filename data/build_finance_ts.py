# -*- coding: utf-8 -*-
"""data/build_finance_ts.py — 构建 PIT 财务库 `finance_ts.db`（2026-10-15 新增）

背景
----
`docs/资产盘点.md` 与 `tests/test_gaps.py` 长期点名 `data/cache/finance_ts.db` 缺失，
导致 ① `backtest/bt_runner.py::_load_fin()` 抛 `MissingDataError`（注册策略 script1 不可用）、
② `data/sector_research.py::_break_net()` 算不出 PB、③ `backtest/backtest_script1.py` 跑不通。

原设计走 Tushare 三表（`income`/`balancesheet`/`cashflow`）。**实测（2026-10-15）
当前 token 无这三个接口权限**（`validation/test_tushare_access.py` → 「没有接口访问权限」），
且 `top_list`（龙虎榜）同样无权限。故改走 **AkShare 免费源**——
与 `finance.db` 的实际来源一致（该库 `source` 列实测全为 `akshare_ths`）。

数据源（全部免费、无 token）
---------------------------
  列                      AkShare 接口                    字段
  ----------------------  ------------------------------  ------------------------------
  total_revenue           stock_lrb_em(date=报告期)       营业总收入        （批量：一次全市场）
  n_income                stock_lrb_em(date=报告期)       净利润
  ann_date(利润表口径)     stock_lrb_em(date=报告期)       公告日期
  total_hldr_eqy_          stock_financial_report_sina     归属于母公司股东权益合计
    exc_min_int           (stock=sh600519,               （逐股：新浪三表）
                           symbol=资产负债表)
  ann_date(资产负债表口径)  stock_financial_report_sina     公告日期

★ 为什么归母权益必须逐股取
  `stock_zcfz_em`（东财批量资产负债表）实测只有「股东权益合计」（含少数股东），
  **没有「归属于母公司股东权益合计」**。本表列名沿用 Tushare `total_hldr_eqy_exc_min_int`
  = 归母口径，若拿含少数股东的口径填进去就是**静默的语义错误**（茅台少数股东权益 108 亿
  / 归母 2512 亿，误差 4%）。故归母权益走新浪逐股接口（实测 1.7 s/股，含公告日期）。

★ 单位：**元**（两个源实测均为元，已交叉验证）
  茅台 2026H1 营业总收入 = 922.78 亿 → 92278072083.21；
  茅台 2026H1 归母权益   = 2512.5 亿 → 251253594419.5。
  这与 `data/sector_research.py::_break_net()` 的 PB 公式
  `pb = circ_mv(亿元) * 1e8 / nav(元)` 单位假设一致——**勿改成万元/亿元**。

★ PIT 语义
  `ann_date` = 公告日期，即该期财报**实际可用**的日期。回测/选股必须按
  `ann_date <= 决策日` 取数，不得用 `end_date`（会引入前视偏差）。
  两个源都提供公告日期，这正是本库相对 `finance.db`（无 ann_date，非 PIT）的核心增量。

产出
----
  data/cache/finance_ts.db
    表 financials_ts(code, end_date, ann_date, total_revenue, n_income,
                     total_hldr_eqy_exc_min_int, source, updated)  PK(code, end_date)
    表 build_meta(ts, key, value)   建库溯源（哪个列来自哪个源、覆盖期、行数）

用法
----
  python data/build_finance_ts.py                      # 全量（利润表批量 + 归母逐股）
  python data/build_finance_ts.py --skip-equity        # 只建利润表（约 12 分钟，先解锁 script1）
  python data/build_finance_ts.py --equity-only        # 只补归母权益（续跑）
  python data/build_finance_ts.py --limit 50           # 小样本验证
  python data/build_finance_ts.py --workers 6          # 归母逐股的并发数

幂等：`INSERT ... ON CONFLICT DO UPDATE`，只填空值（COALESCE），不覆盖已有非空数据。
断点续传：`--equity-only` 自动跳过已入库的股票（`--all` 可强制重拉）。
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from multiprocessing import Pool
from pathlib import Path

for _k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _cache_dir() -> Path:
    """缓存目录解析：LWQUANT_CACHE_DIR > config/params.yaml data.cache_dir > data/cache

    与 data/cache.py::_resolve_cache_dir() 同一优先级约定（此处内联实现，
    避免在子进程里 import 重量级模块）。
    """
    env = os.environ.get("LWQUANT_CACHE_DIR")
    if env:
        return Path(env)
    cfg = BASE / "config" / "params.yaml"
    if cfg.exists():
        try:
            import yaml
            v = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("data", {})
            p = v.get("cache_dir")
            if p:
                return Path(p) if Path(p).is_absolute() else (BASE / p)
        except Exception:
            pass
    return BASE / "data" / "cache"


CACHE = _cache_dir()
DB = CACHE / "finance_ts.db"
STOCK_BASIC = CACHE / "stock_basic.db"
LOG_FILE = BASE / "logs" / "build_finance_ts.log"

# 报告期起点：2010Q1。实测 stock_lrb_em 可回溯到 2012、stock_zcfz_em 到 2010，
# 但归母逐股（新浪）能到更早；统一起点便于口径一致。
START_YEAR = 2010

SCHEMA_MAIN = """CREATE TABLE IF NOT EXISTS financials_ts (
    code TEXT NOT NULL,
    end_date TEXT NOT NULL,
    ann_date TEXT,
    total_revenue REAL,
    n_income REAL,
    total_hldr_eqy_exc_min_int REAL,
    source TEXT,
    updated TEXT,
    PRIMARY KEY (code, end_date))"""
SCHEMA_META = """CREATE TABLE IF NOT EXISTS build_meta (
    ts TEXT, key TEXT, value TEXT)"""

SRC_LRB = "akshare_lrb_em"      # 利润表（批量，按报告期）
SRC_SINA = "akshare_sina_bs"    # 资产负债表（逐股，新浪）


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _conn() -> sqlite3.Connection:
    CACHE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.execute(SCHEMA_MAIN)
    con.execute(SCHEMA_META)
    con.commit()
    return con


# ---------------------------------------------------------------- 工具

def periods(start_year: int = START_YEAR, today: date = None):
    """报告期列表（季末）：['2010-03-31', '2010-06-30', ...] 截至今日"""
    today = today or date.today()
    out = []
    for y in range(start_year, today.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = date(y, m, d)
            if p <= today:
                out.append(p.isoformat())
    return out


def norm_date(v):
    """公告日期/报告期 → 'YYYY-MM-DD'；无法解析返回 None"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    s = s.replace("/", "-").replace(".", "-")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    try:
        import pandas as pd
        t = pd.to_datetime(s, errors="coerce")
        return None if pd.isna(t) else t.strftime("%Y-%m-%d")
    except Exception:
        return None


def load_code_map() -> dict:
    """6 位代码 → ts_code（如 '600519' → '600519.SH'）；以 stock_basic 为准"""
    m = {}
    if STOCK_BASIC.exists():
        con = sqlite3.connect(f"file:{STOCK_BASIC}?mode=ro", uri=True)
        try:
            for code, in con.execute("SELECT code FROM stock_basic"):
                code = str(code)
                if len(code) >= 8:
                    m[code[:6]] = code
        finally:
            con.close()
    return m


def to_ts_code(c6: str, cmap: dict) -> str:
    """6 位 → ts_code。stock_basic 查不到时按板块规则推断（退市/新股兜底）"""
    c6 = str(c6).strip().zfill(6)
    if c6 in cmap:
        return cmap[c6]
    if c6[0] == "6":
        return c6 + ".SH"
    if c6[0] in ("0", "3"):
        return c6 + ".SZ"
    if c6[0] in ("4", "8"):
        return c6 + ".BJ"
    if c6[0] == "9":
        return c6 + ".SH"
    return c6 + ".SZ"


def _f(v):
    """安全转 float；空值/非数 → None"""
    if v is None:
        return None
    try:
        import math
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


UPSERT = """INSERT INTO financials_ts
    (code, end_date, ann_date, total_revenue, n_income,
     total_hldr_eqy_exc_min_int, source, updated)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(code, end_date) DO UPDATE SET
      ann_date = COALESCE(excluded.ann_date, ann_date),
      total_revenue = COALESCE(excluded.total_revenue, total_revenue),
      n_income = COALESCE(excluded.n_income, n_income),
      total_hldr_eqy_exc_min_int = COALESCE(
          excluded.total_hldr_eqy_exc_min_int, total_hldr_eqy_exc_min_int),
      source = CASE
          WHEN financials_ts.source IS NULL OR financials_ts.source = ''
            THEN excluded.source
          WHEN instr(financials_ts.source, excluded.source) > 0
            THEN financials_ts.source
          ELSE financials_ts.source || '+' || excluded.source END,
      updated = excluded.updated"""


# ---------------------------------------------------------------- 利润表（批量）

def fetch_lrb_period(period: str, cmap: dict, retries: int = 3):
    """一个报告期 → [(code, end_date, ann_date, total_revenue, n_income, ...)]

    用东财批量接口，**一次调用覆盖全市场**（实测 ~5200 只 / 10 秒）。
    """
    import akshare as ak
    stamp = period.replace("-", "")
    last = None
    for attempt in range(retries):
        try:
            df = ak.stock_lrb_em(date=stamp)
            if df is None or df.empty:
                return period, [], None
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = []
            for _, r in df.iterrows():
                c6 = str(r.get("股票代码") or "").strip()
                if not c6:
                    continue
                rows.append((
                    to_ts_code(c6, cmap), period,
                    norm_date(r.get("公告日期")),
                    _f(r.get("营业总收入")), _f(r.get("净利润")),
                    None, SRC_LRB, now,
                ))
            return period, rows, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return period, None, last


def build_revenue(con, args, cmap):
    ps = periods(START_YEAR)
    if args.limit:
        ps = ps[-args.limit:]
    log(f"[利润表] 报告期 {len(ps)} 个（{ps[0]} ~ {ps[-1]}），批量接口逐期拉取")
    ok = empty = 0
    total = 0
    t0 = time.time()
    for i, p in enumerate(ps, 1):
        _, rows, err = fetch_lrb_period(p, cmap)
        if err:
            log(f"  [{i}/{len(ps)}] {p} 失败：{err}")
            continue
        if not rows:
            empty += 1
        else:
            con.executemany(UPSERT, rows)
            con.commit()
            ok += 1
            total += len(rows)
        if i % 10 == 0 or i == len(ps):
            el = time.time() - t0
            left = (len(ps) - i) * (el / i) / 60 if i else 0
            log(f"  [进度] {i}/{len(ps)} 有数{ok} 空{empty} 累计{total}行 "
                f"耗时{el/60:.1f}分 剩余约{left:.0f}分")
    log(f"[利润表] 完成：{ok} 期有数 / {empty} 期空，共 {total} 行，"
        f"耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 归母权益（逐股）

def _sina_symbol(code: str) -> str:
    """'600519.SH' → 'sh600519'"""
    c6, _, mkt = code.partition(".")
    return mkt.lower() + c6 if mkt else "sh" + c6


def _equity_worker(code: str):
    """子进程：拉一只股票的资产负债表 → 归母权益行（不写库）"""
    import akshare as ak
    last = None
    for attempt in range(3):
        try:
            df = ak.stock_financial_report_sina(stock=_sina_symbol(code),
                                                symbol="资产负债表")
            if df is None or df.empty:
                return code, [], None
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = []
            for _, r in df.iterrows():
                end = norm_date(r.get("报告日"))
                if not end or end < f"{START_YEAR}-01-01":
                    continue
                eqy = _f(r.get("归属于母公司股东权益合计"))
                if eqy is None:
                    continue
                rows.append((code, end, norm_date(r.get("公告日期")),
                             None, None, eqy, SRC_SINA, now))
            return code, rows, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    return code, None, last


def load_codes():
    if not STOCK_BASIC.exists():
        return []
    con = sqlite3.connect(f"file:{STOCK_BASIC}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT code FROM stock_basic WHERE code LIKE '%.SH' OR code LIKE '%.SZ' "
            "OR code LIKE '%.BJ' ORDER BY code")]
    finally:
        con.close()


def done_equity_codes():
    """已入库归母权益的股票（用于断点续传）"""
    if not DB.exists():
        return set()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT code FROM financials_ts "
            "WHERE total_hldr_eqy_exc_min_int IS NOT NULL")}
    except Exception:
        return set()
    finally:
        con.close()


def build_equity(con, args):
    codes = load_codes()
    have = set() if args.all else done_equity_codes()
    todo = [c for c in codes if c not in have]
    if args.limit:
        todo = todo[:args.limit]
    log(f"[归母权益] 股票池 {len(codes)}，已入库 {len(have)}，本次 {len(todo)}，"
        f"{args.workers} 进程（逐股新浪三表，实测 ~1.7 s/股）")
    if not todo:
        log("[归母权益] 无缺口，跳过")
        return 0

    t0 = time.time()
    ok = empty = 0
    fails = []
    total = 0
    with Pool(args.workers) as pool:
        for i, (code, rows, err) in enumerate(pool.imap_unordered(_equity_worker, todo), 1):
            if err:
                fails.append(code)
            elif not rows:
                empty += 1
            else:
                con.executemany(UPSERT, rows)
                ok += 1
                total += len(rows)
            if i % 50 == 0 or i == len(todo):
                con.commit()
                el = time.time() - t0
                rate = i / el if el > 0 else 0
                left = (len(todo) - i) / rate / 60 if rate > 0 else 0
                log(f"  [进度] {i}/{len(todo)} 成功{ok} 空{empty} 失败{len(fails)} "
                    f"速率{rate:.2f}股/s 剩余约{left:.0f}分")
    con.commit()
    if fails:
        fp = BASE / "logs" / "finance_ts_equity_failed.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        import json
        fp.write_text(json.dumps(fails, ensure_ascii=False), encoding="utf-8")
        log(f"  失败 {len(fails)} 只已落盘 {fp}")
    log(f"[归母权益] 完成：成功 {ok} / 空 {empty} / 失败 {len(fails)}，"
        f"共 {total} 行，耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 溯源 / 自检

def write_meta(con):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = con.execute("SELECT COUNT(*) FROM financials_ts").fetchone()[0]
    n_rev = con.execute("SELECT COUNT(*) FROM financials_ts "
                        "WHERE total_revenue IS NOT NULL").fetchone()[0]
    n_eqy = con.execute("SELECT COUNT(*) FROM financials_ts "
                        "WHERE total_hldr_eqy_exc_min_int IS NOT NULL").fetchone()[0]
    n_pit = con.execute("SELECT COUNT(*) FROM financials_ts "
                        "WHERE ann_date IS NOT NULL").fetchone()[0]
    rng = con.execute("SELECT MIN(end_date), MAX(end_date) FROM financials_ts").fetchone()
    ncode = con.execute("SELECT COUNT(DISTINCT code) FROM financials_ts").fetchone()[0]
    items = [
        ("built_at", now),
        ("rows_total", str(n)),
        ("rows_with_revenue", str(n_rev)),
        ("rows_with_equity", str(n_eqy)),
        ("rows_with_ann_date", str(n_pit)),
        ("distinct_codes", str(ncode)),
        ("end_date_range", f"{rng[0]} ~ {rng[1]}"),
        ("col_total_revenue", SRC_LRB + "（东财利润表-按报告期，批量）"),
        ("col_n_income", SRC_LRB + "（东财利润表-按报告期，批量）"),
        ("col_total_hldr_eqy_exc_min_int",
         SRC_SINA + "（新浪资产负债表，逐股；★归母口径，非含少数股东）"),
        ("col_ann_date", "两源各自的公告日期（PIT 对齐基准）"),
        ("unit", "元（两源实测均为元，与 sector_research PB 公式一致）"),
        ("builder", "data/build_finance_ts.py"),
    ]
    con.execute("DELETE FROM build_meta")
    con.executemany("INSERT INTO build_meta VALUES (?,?,?)",
                    [(now, k, v) for k, v in items])
    con.commit()
    log(f"[溯源] 总行数 {n}｜有营收 {n_rev}｜有归母权益 {n_eqy}｜有公告日 {n_pit}"
        f"｜股票 {ncode} 只｜报告期 {rng[0]} ~ {rng[1]}")


def self_check(con):
    """建完自检：消费方要的列是否真的可用"""
    print("\n" + "=" * 66)
    print("自检（消费方视角）")
    print("=" * 66)
    q = ("SELECT code, end_date, ann_date, total_revenue, n_income, "
         "total_hldr_eqy_exc_min_int FROM financials_ts "
         "WHERE code=? ORDER BY end_date DESC LIMIT 3")
    for code in ("600519.SH", "000001.SZ"):
        rows = con.execute(q, (code,)).fetchall()
        print(f"\n  {code}:")
        for r in rows:
            print(f"    报告期 {r[1]}  公告日 {r[2]}  营收 {r[3]}  净利 {r[4]}  归母权益 {r[5]}")
        if not rows:
            print("    （无数据）")

    # bt_runner 的 rev_yoy 路径（shift 4 季度同比）
    rows = con.execute(
        "SELECT end_date, total_revenue FROM financials_ts "
        "WHERE code='600519.SH' AND total_revenue IS NOT NULL "
        "ORDER BY end_date").fetchall()
    if len(rows) >= 5:
        cur, prev = rows[-1][1], rows[-5][1]
        if prev:
            print(f"\n  茅台最新期营收同比（shift4）= {cur/prev-1:+.2%}  "
                  f"（{rows[-5][0]} → {rows[-1][0]}）")
    print("\n" + "=" * 66)


def main():
    ap = argparse.ArgumentParser(description="构建 PIT 财务库 finance_ts.db（AkShare 免费源）")
    ap.add_argument("--workers", type=int, default=6, help="归母权益逐股并发数")
    ap.add_argument("--limit", type=int, default=None, help="小样本验证（股票数/报告期数）")
    ap.add_argument("--skip-equity", action="store_true", help="只建利润表（快，约 12 分钟）")
    ap.add_argument("--equity-only", action="store_true", help="只补归母权益（续跑）")
    ap.add_argument("--all", action="store_true", help="归母权益不跳过已入库股票")
    args = ap.parse_args()

    log("=" * 60)
    log(f"构建 finance_ts.db → {DB}")
    log(f"缓存目录 {CACHE}｜参数 workers={args.workers} limit={args.limit} "
        f"skip_equity={args.skip_equity} equity_only={args.equity_only}")
    con = _conn()
    cmap = load_code_map()
    log(f"股票代码映射 {len(cmap)} 条（源 stock_basic.db）")

    if not args.equity_only:
        build_revenue(con, args, cmap)
    if not args.skip_equity:
        build_equity(con, args)

    write_meta(con)
    self_check(con)
    con.close()
    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
