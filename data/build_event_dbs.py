# -*- coding: utf-8 -*-
"""data/build_event_dbs.py — 构建「事件类」数据库（2026-10-15 新增）

背景
----
`docs/资产盘点.md` §3/§4.1 与 `tests/test_gaps.py` 点名四个库缺失：
`lhb_full.db`（龙虎榜）/ `rzrq_full.db`（两融）/ `gdhs_full.db`（股东户数）/ `shebao.db`（社保）。
仓库里**只有消费方、没有生产方**——这四个库此前没有任何构建脚本。

原设计依赖 Tushare（`top_list` 等）。**实测（2026-10-15）当前 token 无 `top_list` 权限**
（`validation/test_tushare_access.py` → 「没有接口访问权限」，token 仅日线档），
故全部改走 **AkShare 免费源**。

产出与 schema（★ 标注的是**消费方硬性要求**，改动即破坏下游）
-------------------------------------------------------------
1. `data/cache/lhb_full.db` → 表 `lhb_detail`
   ★ 表名 `lhb_detail` + 列 `date`：`data/build_unified_db.py:159` 的
     `event_series` 索引按 `("lhb_full.db", "lhb_detail", "date")` 取 `MAX(date)`。
   源：`stock_lhb_detail_em(start_date, end_date)`（东财龙虎榜，区间批量）

2. `data/cache/rzrq_full.db` → 表 `rzrq`
   （无消费方查询，schema 由本脚本定义）
   源：`stock_margin_detail_sse(date)` + `stock_margin_detail_szse(date)`（两市逐日明细）

3. `data/cache/gdhs_full.db` → 表 `gdhs`
   ★ 列 `code` / `chg_pct` / `ann_date`：`data/sector_research.py:374,452` 的
     `SELECT code, chg_pct, ann_date FROM gdhs` 直接引用，缺一列即崩。
     语义：`chg_pct` = 股东户数环比增减比例(%)，用于「散户涌入排雷」（户数大增 → 剔除）。
   源：`stock_zh_a_gdhs(symbol=报告期)`（按报告期批量，实测回溯到 2014）

4. `data/cache/shebao.db` → 表 `shebao`
   （无消费方查询，schema 由本脚本定义）
   源：`stock_gdfx_free_holding_analyse_em(date)`（十大流通股东分析，从中筛「社保」字样）

★ 覆盖期边界（诚实声明）
  `bars.db` 的 `daily_bar` 实测覆盖 **2019-01-02 ~ 2026-10-14**（1885 个交易日）。
  本脚本默认起点与之一致（`--start 2019-01-01`），使事件库与行情库口径对齐，
  避免"事件库有数据但行情库没有"的错配。要更长历史用 `--start` 显式指定
  （lhb 接口实测可回溯到 2015、gdhs 到 2014）。

★ 单位
  龙虎榜金额、两融余额、流通市值等均为**元**（东财/交易所口径，实测未做换算）。

用法
----
  python data/build_event_dbs.py                      # 全建（默认四库）
  python data/build_event_dbs.py --only lhb,gdhs      # 只建指定库
  python data/build_event_dbs.py --only rzrq --workers 12
  python data/build_event_dbs.py --start 2015-01-01 --only lhb   # 拉更长历史
  python data/build_event_dbs.py --only gdhs --limit 4           # 小样本验证

幂等：`INSERT OR REPLACE`，重跑不产生重复行。断点续传：rzrq/lhb 按日/按月跳过已完成。
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from multiprocessing import Pool
from pathlib import Path

for _k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _cache_dir() -> Path:
    """LWQUANT_CACHE_DIR > config/params.yaml data.cache_dir > data/cache"""
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
BARS = CACHE / "bars.db"
STOCK_BASIC = CACHE / "stock_basic.db"
LOG_FILE = BASE / "logs" / "build_event_dbs.log"

DEFAULT_START = "2019-01-01"   # 与 bars.db 的 daily_bar 覆盖起点对齐

SCHEMAS = {
    "lhb": (CACHE / "lhb_full.db", """CREATE TABLE IF NOT EXISTS lhb_detail (
        date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, reason TEXT,
        close REAL, pct_chg REAL,
        net_buy REAL, buy_amt REAL, sell_amt REAL, lhb_amt REAL, mkt_amt REAL,
        net_ratio REAL, amt_ratio REAL, turnover REAL, circ_mv REAL,
        ret_1d REAL, ret_2d REAL, ret_5d REAL, ret_10d REAL,
        interpret TEXT, source TEXT,
        PRIMARY KEY (date, code, reason))"""),
    "rzrq": (CACHE / "rzrq_full.db", """CREATE TABLE IF NOT EXISTS rzrq (
        date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, exchange TEXT,
        rzye REAL, rzmre REAL, rzche REAL,
        rqyl REAL, rqmcl REAL, rqchl REAL, rqye REAL, rong_bal REAL,
        source TEXT,
        PRIMARY KEY (date, code))"""),
    "gdhs": (CACHE / "gdhs_full.db", """CREATE TABLE IF NOT EXISTS gdhs (
        code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT, name TEXT,
        holder_num INTEGER, holder_num_prev INTEGER, chg INTEGER, chg_pct REAL,
        avg_mv REAL, avg_shares REAL, total_mv REAL, total_shares REAL,
        source TEXT,
        PRIMARY KEY (code, end_date))"""),
    # ★列名严格对应 stock_gdfx_free_holding_analyse_em 的实际返回：
    #   该批量接口**没有**「占总流通股本持股比例」，只有「期末持股-流通市值」，
    #   故不设 ratio 列（宁缺勿造）。chg_ratio = 期末持股-数量变化比例(%)。
    "shebao": (CACHE / "shebao.db", """CREATE TABLE IF NOT EXISTS shebao (
        code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT,
        holder_name TEXT NOT NULL, holder_type TEXT,
        shares REAL, shares_chg REAL, chg_ratio REAL, chg TEXT,
        mv REAL, rank INTEGER, source TEXT,
        PRIMARY KEY (code, end_date, holder_name))"""),
}

SRC_LHB = "akshare_lhb_detail_em"
SRC_RZRQ = "akshare_margin_detail"
SRC_GDHS = "akshare_zh_a_gdhs"
SRC_SHEBAO = "akshare_gdfx_free_holding_analyse_em"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _conn(key: str) -> sqlite3.Connection:
    path, schema = SCHEMAS[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(schema)
    con.commit()
    return con


# ---------------------------------------------------------------- 通用工具

def norm_date(v):
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    s = s.replace("/", "-")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _f(v):
    if v is None:
        return None
    try:
        import math
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return None if x is None else int(x)


def pick(row, *names):
    """按候选名取第一个非空值（兼容 akshare 列名变动）"""
    for n in names:
        if n in row:
            v = row[n]
            if v is None:
                continue
            try:
                import pandas as pd
                if pd.isna(v):
                    continue
            except Exception:
                pass
            return v
    return None


def load_code_map() -> dict:
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


def to_ts_code(c6, cmap) -> str:
    c6 = str(c6).strip().zfill(6)
    if c6 in cmap:
        return cmap[c6]
    if c6[0] == "6" or c6[0] == "9":
        return c6 + ".SH"
    if c6[0] in ("0", "3"):
        return c6 + ".SZ"
    if c6[0] in ("4", "8"):
        return c6 + ".BJ"
    return c6 + ".SZ"


def trading_days(start: str, end: str = None):
    """从 bars.db 取交易日（项目自己的日历，与行情库天然一致）"""
    end = end or date.today().isoformat()
    if not BARS.exists():
        raise SystemExit(f"缺少 {BARS} —— 交易日历依赖它，无法继续")
    con = sqlite3.connect(f"file:{BARS}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE date>=? AND date<=? ORDER BY date",
            (start, end))]
    finally:
        con.close()


def quarter_ends(start: str, end: str = None):
    """报告期（季末）列表，如 2019-03-31 ..."""
    end = end or date.today().isoformat()
    sy = int(start[:4])
    ey = int(end[:4])
    out = []
    for y in range(sy, ey + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = f"{y}-{m:02d}-{d:02d}"
            if start <= p <= end:
                out.append(p)
    return out


def month_chunks(start: str, end: str):
    """按月切区间 → [('2019-01-01','2019-01-31'), ...]（lhb 区间接口用）"""
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cur <= last:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        chunk_end = min(nxt - timedelta(days=1), last)
        yield cur.isoformat(), chunk_end.isoformat()
        cur = nxt


# ---------------------------------------------------------------- 1. 龙虎榜

def fetch_lhb_chunk(args):
    """子进程：拉一个日期区间的龙虎榜"""
    start, end, cmap = args
    import akshare as ak
    last = None
    for attempt in range(3):
        try:
            df = ak.stock_lhb_detail_em(start_date=start.replace("-", ""),
                                        end_date=end.replace("-", ""))
            if df is None or df.empty:
                return (start, end), [], None
            rows = []
            for _, r in df.iterrows():
                d = norm_date(pick(r, "上榜日", "日期"))
                c6 = pick(r, "代码", "股票代码")
                if not d or not c6:
                    continue
                rows.append((
                    d, to_ts_code(c6, cmap), pick(r, "名称", "股票简称"),
                    pick(r, "上榜原因", "原因"),
                    _f(pick(r, "收盘价")), _f(pick(r, "涨跌幅")),
                    _f(pick(r, "龙虎榜净买额", "净买额")),
                    _f(pick(r, "龙虎榜买入额", "买入额")),
                    _f(pick(r, "龙虎榜卖出额", "卖出额")),
                    _f(pick(r, "龙虎榜成交额", "成交额")),
                    _f(pick(r, "市场总成交额")),
                    _f(pick(r, "净买额占总成交比")), _f(pick(r, "成交额占总成交比")),
                    _f(pick(r, "换手率")), _f(pick(r, "流通市值")),
                    _f(pick(r, "上榜后1日")), _f(pick(r, "上榜后2日")),
                    _f(pick(r, "上榜后5日")), _f(pick(r, "上榜后10日")),
                    pick(r, "解读"), SRC_LHB,
                ))
            return (start, end), rows, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    return (start, end), None, last


def build_lhb(con, args, cmap):
    end = args.end or date.today().isoformat()
    chunks = list(month_chunks(args.start, end))
    if args.limit:
        chunks = chunks[:args.limit]
    log(f"[龙虎榜] {len(chunks)} 个月区间（{args.start} ~ {end}），{args.workers} 进程")
    t0, ok, fails, total = time.time(), 0, [], 0
    with Pool(args.workers) as pool:
        jobs = [(s, e, cmap) for s, e in chunks]
        for i, ((s, e), rows, err) in enumerate(pool.imap_unordered(fetch_lhb_chunk, jobs), 1):
            if err:
                fails.append(f"{s}~{e}")
            elif rows:
                con.executemany(
                    "INSERT OR REPLACE INTO lhb_detail VALUES (" +
                    ",".join("?" * 21) + ")", rows)
                ok += 1
                total += len(rows)
            if i % 10 == 0 or i == len(chunks):
                con.commit()
                el = time.time() - t0
                log(f"  [进度] {i}/{len(chunks)} 有数{ok} 失败{len(fails)} "
                    f"累计{total}行 耗时{el/60:.1f}分")
    con.commit()
    log(f"[龙虎榜] 完成：{ok} 区间有数 / 失败 {len(fails)}，共 {total} 行，"
        f"耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 2. 两融

def fetch_rzrq_day(args):
    """子进程：拉一个交易日的两市两融明细"""
    day, cmap, stocks_only = args
    import akshare as ak
    stamp = day.replace("-", "")
    rows, errs = [], []
    for ex, fn in (("SSE", ak.stock_margin_detail_sse),
                   ("SZSE", ak.stock_margin_detail_szse)):
        last = None
        for attempt in range(3):
            try:
                df = fn(date=stamp)
                if df is None or df.empty:
                    last = None
                    break
                for _, r in df.iterrows():
                    c6 = pick(r, "标的证券代码", "证券代码", "代码")
                    if not c6:
                        continue
                    c6s = str(c6).strip().zfill(6)
                    if stocks_only and c6s not in cmap:
                        continue
                    rows.append((
                        day, to_ts_code(c6s, cmap),
                        pick(r, "标的证券简称", "证券简称", "名称"), ex,
                        _f(pick(r, "融资余额")), _f(pick(r, "融资买入额")),
                        _f(pick(r, "融资偿还额")),
                        _f(pick(r, "融券余量")), _f(pick(r, "融券卖出量")),
                        _f(pick(r, "融券偿还量")), _f(pick(r, "融券余额")),
                        _f(pick(r, "融资融券余额")), SRC_RZRQ,
                    ))
                last = None
                break
            except Exception as e:
                last = f"{ex}:{type(e).__name__}:{str(e)[:70]}"
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
        if last:
            errs.append(last)
    return day, rows, (";".join(errs) if errs else None)


def build_rzrq(con, args, cmap):
    days = trading_days(args.start, args.end)
    if args.limit:
        days = days[:args.limit]
    have = set()
    if not args.all:
        try:
            have = {r[0] for r in con.execute("SELECT DISTINCT date FROM rzrq")}
        except Exception:
            have = set()
    todo = [d for d in days if d not in have]
    log(f"[两融] 交易日 {len(days)}，已入库 {len(have)}，本次 {len(todo)}，"
        f"{args.workers} 进程（两市逐日，实测 ~1.2 s/日）")
    if not todo:
        log("[两融] 无缺口，跳过")
        return 0
    t0, ok, empty, fails, total = time.time(), 0, 0, [], 0
    with Pool(args.workers) as pool:
        jobs = [(d, cmap, not args.include_etf) for d in todo]
        for i, (day, rows, err) in enumerate(pool.imap_unordered(fetch_rzrq_day, jobs), 1):
            if err:
                fails.append(day)
            elif not rows:
                empty += 1
            else:
                con.executemany(
                    "INSERT OR REPLACE INTO rzrq VALUES (" +
                    ",".join("?" * 13) + ")", rows)
                ok += 1
                total += len(rows)
            if i % 50 == 0 or i == len(todo):
                con.commit()
                el = time.time() - t0
                rate = i / el if el else 0
                log(f"  [进度] {i}/{len(todo)} 成功{ok} 空{empty} 失败{len(fails)} "
                    f"速率{rate:.2f}日/s 剩余约{(len(todo)-i)/rate/60 if rate else 0:.0f}分")
    con.commit()
    if fails:
        fp = BASE / "logs" / "rzrq_failed_days.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(fails, ensure_ascii=False), encoding="utf-8")
        log(f"  失败 {len(fails)} 日已落盘 {fp}")
    log(f"[两融] 完成：成功 {ok} / 空 {empty} / 失败 {len(fails)}，共 {total} 行，"
        f"耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 3. 股东户数

def fetch_gdhs_period(period, cmap):
    import akshare as ak
    stamp = period.replace("-", "")
    last = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_gdhs(symbol=stamp)
            if df is None or df.empty:
                return period, [], None
            rows = []
            for _, r in df.iterrows():
                c6 = pick(r, "代码", "股票代码")
                if not c6:
                    continue
                rows.append((
                    to_ts_code(c6, cmap), period,
                    norm_date(pick(r, "公告日期")), pick(r, "名称"),
                    _i(pick(r, "股东户数-本次")), _i(pick(r, "股东户数-上次")),
                    _i(pick(r, "股东户数-增减")), _f(pick(r, "股东户数-增减比例")),
                    _f(pick(r, "户均持股市值")), _f(pick(r, "户均持股数量")),
                    _f(pick(r, "总市值")), _f(pick(r, "总股本")), SRC_GDHS,
                ))
            return period, rows, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    return period, None, last


def build_gdhs(con, args, cmap):
    ps = quarter_ends(args.start, args.end)
    if args.limit:
        ps = ps[-args.limit:]
    log(f"[股东户数] {len(ps)} 个报告期（{ps[0] if ps else '-'} ~ {ps[-1] if ps else '-'}）")
    t0, ok, empty, fails, total = time.time(), 0, 0, [], 0
    for i, p in enumerate(ps, 1):
        _, rows, err = fetch_gdhs_period(p, cmap)
        if err:
            fails.append(p)
            log(f"  [{i}/{len(ps)}] {p} 失败：{err}")
            continue
        if not rows:
            empty += 1
        else:
            con.executemany(
                "INSERT OR REPLACE INTO gdhs VALUES (" + ",".join("?" * 13) + ")", rows)
            con.commit()
            ok += 1
            total += len(rows)
        if i % 5 == 0 or i == len(ps):
            log(f"  [进度] {i}/{len(ps)} 有数{ok} 空{empty} 失败{len(fails)} 累计{total}行")
    log(f"[股东户数] 完成：{ok} 期有数 / {empty} 空 / {len(fails)} 失败，"
        f"共 {total} 行，耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 4. 社保

def fetch_shebao_period(args):
    """子进程：拉一个报告期的十大流通股东，筛出「社保」持股

    ★该接口（stock_gdfx_free_holding_analyse_em）实测 ~150 s/次、全市场 5.6 万行，
      是四库里最慢的；故用进程池并发，并对单期失败只重试 1 次（重试成本高）。
    实测列名（2026-10-15）：
      序号/股东名称/股东类型/股票代码/股票简称/报告期/期末持股-数量/
      期末持股-数量变化/期末持股-数量变化比例/期末持股-持股变动/
      期末持股-流通市值/公告日/公告日后涨跌幅-{10,30,60}个交易日
    """
    period, cmap = args
    import akshare as ak
    stamp = period.replace("-", "")
    last = None
    for attempt in range(2):
        try:
            df = ak.stock_gdfx_free_holding_analyse_em(date=stamp)
            if df is None or df.empty:
                return period, [], None
            name_col = None
            for c in df.columns:
                if "股东名称" in str(c):
                    name_col = c
                    break
            if name_col is None:
                return period, [], f"未找到股东名称列，实有列：{list(df.columns)}"
            sub = df[df[name_col].astype(str).str.contains("社保", na=False)]
            if sub.empty:
                return period, [], None
            rows = []
            for _, r in sub.iterrows():
                c6 = pick(r, "股票代码", "代码")
                if not c6:
                    continue
                rows.append((
                    to_ts_code(c6, cmap),
                    norm_date(pick(r, "报告期")) or period,
                    norm_date(pick(r, "公告日", "公告日期")),
                    str(pick(r, name_col)),
                    pick(r, "股东类型", "股东性质"),
                    _f(pick(r, "期末持股-数量", "持股数")),
                    _f(pick(r, "期末持股-数量变化")),
                    _f(pick(r, "期末持股-数量变化比例")),
                    str(pick(r, "期末持股-持股变动", "增减") or ""),
                    _f(pick(r, "期末持股-流通市值")),
                    _i(pick(r, "序号", "名次")),
                    SRC_SHEBAO,
                ))
            return period, rows, None
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            if attempt < 1:
                time.sleep(2.0)
    return period, None, last


def build_shebao(con, args, cmap):
    ps = quarter_ends(args.start, args.end)
    if args.limit:
        ps = ps[-args.limit:]
    log(f"[社保] {len(ps)} 个报告期，{args.workers} 进程（★该接口实测 ~150 s/期，是四库中最慢的）")
    t0, ok, empty, fails, total = time.time(), 0, 0, [], 0
    with Pool(args.workers) as pool:
        jobs = [(p, cmap) for p in ps]
        for i, (period, rows, err) in enumerate(pool.imap_unordered(fetch_shebao_period, jobs), 1):
            if err:
                fails.append(period)
                log(f"  [{i}/{len(ps)}] {period} 失败：{err}")
            elif not rows:
                empty += 1
            else:
                con.executemany(
                    "INSERT OR REPLACE INTO shebao VALUES (" + ",".join("?" * 12) + ")", rows)
                ok += 1
                total += len(rows)
            con.commit()
            el = time.time() - t0
            log(f"  [进度] {i}/{len(ps)} 有社保持仓{ok} 空{empty} 失败{len(fails)} "
                f"累计{total}行 耗时{el/60:.1f}分")
    log(f"[社保] 完成：{ok} 期有社保持仓 / {empty} 空 / {len(fails)} 失败，"
        f"共 {total} 行，耗时 {(time.time()-t0)/60:.1f} 分钟")
    return total


# ---------------------------------------------------------------- 自检

def report(args):
    print("\n" + "=" * 68)
    print("事件库自检（消费方视角）")
    print("=" * 68)
    checks = [
        ("lhb", "lhb_full.db", "lhb_detail", "date"),
        ("rzrq", "rzrq_full.db", "rzrq", "date"),
        ("gdhs", "gdhs_full.db", "gdhs", "ann_date"),
        ("shebao", "shebao.db", "shebao", "end_date"),
    ]
    for key, dbn, tbl, datecol in checks:
        p = CACHE / dbn
        if not p.exists():
            print(f"  ❌ {dbn:16s} 不存在")
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            mn, mx = con.execute(f"SELECT MIN({datecol}), MAX({datecol}) FROM {tbl}").fetchone()
            ncode = con.execute(f"SELECT COUNT(DISTINCT code) FROM {tbl}").fetchone()[0]
            size = p.stat().st_size / 1e6
            print(f"  ✅ {dbn:16s} 表{tbl:12s} {n:>9,} 行  {ncode:>5} 只  "
                  f"{mn} ~ {mx}  {size:.1f} MB")
        except Exception as e:
            print(f"  ⚠️  {dbn:16s} {type(e).__name__}: {e}")
        finally:
            con.close()

    # 硬性 schema 契约
    print("\n  硬性契约检查：")
    g = CACHE / "gdhs_full.db"
    if g.exists():
        con = sqlite3.connect(f"file:{g}?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(gdhs)")}
        con.close()
        need = {"code", "chg_pct", "ann_date"}
        print(f"    gdhs 必需列 {sorted(need)} → {'✅ 齐备' if need <= cols else '❌ 缺 ' + str(need - cols)}"
              f"（sector_research.py:374,452 依赖）")
    l = CACHE / "lhb_full.db"
    if l.exists():
        con = sqlite3.connect(f"file:{l}?mode=ro", uri=True)
        tbs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(lhb_detail)")} if "lhb_detail" in tbs else set()
        con.close()
        ok = "lhb_detail" in tbs and "date" in cols
        print(f"    lhb_detail 表 + date 列 → {'✅ 齐备' if ok else '❌ 缺失'}"
              f"（build_unified_db.py:159 依赖）")
    print("=" * 68)


def main():
    ap = argparse.ArgumentParser(description="构建事件类数据库（AkShare 免费源）")
    ap.add_argument("--only", default="lhb,rzrq,gdhs,shebao",
                    help="逗号分隔：lhb,rzrq,gdhs,shebao")
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"起始日期（默认 {DEFAULT_START}，与 bars.db 覆盖期对齐）")
    ap.add_argument("--end", default=None, help="结束日期（默认今天）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="小样本验证")
    ap.add_argument("--all", action="store_true", help="两融不跳过已入库日期")
    ap.add_argument("--include-etf", action="store_true",
                    help="两融保留 ETF/债券等非股票标的（默认只留 stock_basic 内的股票）")
    args = ap.parse_args()

    keys = [k.strip() for k in args.only.split(",") if k.strip()]
    bad = [k for k in keys if k not in SCHEMAS]
    if bad:
        raise SystemExit(f"未知库名 {bad}，可选 {list(SCHEMAS)}")

    log("=" * 62)
    log(f"构建事件库 {keys}｜起点 {args.start}｜并发 {args.workers}｜limit={args.limit}")
    log(f"缓存目录 {CACHE}")
    cmap = load_code_map()
    log(f"股票代码映射 {len(cmap)} 条")

    t0 = time.time()
    for k in keys:
        con = _conn(k)
        try:
            if k == "lhb":
                build_lhb(con, args, cmap)
            elif k == "rzrq":
                build_rzrq(con, args, cmap)
            elif k == "gdhs":
                build_gdhs(con, args, cmap)
            elif k == "shebao":
                build_shebao(con, args, cmap)
        finally:
            con.close()
    log(f"全部完成，总耗时 {(time.time()-t0)/60:.1f} 分钟")
    report(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
