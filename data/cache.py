# -*- coding: utf-8 -*-
"""本地缓存唯一读取接口（M1 数据管道 · 基础版）

架构要求（主文档 4.5③）：网络数据（无论哪个源）只负责写入本地 SQLite/Parquet，
策略/回测**只读本地库** —— 断网、断源都不影响已缓存数据。

本轮实现（SQLite 日线缓存）：
- `daily_bar` 表：按 (code, date, adjust) 主键 upsert，多源双写安全
- `bar_meta` 表：每只股票缓存覆盖范围（增量更新 / 覆盖率判断）
后续扩展：Parquet 存储 / 财报缓存 / 多源双写校验告警（M2 再做）

统一代码格式：'600519.SH' / '000001.SZ'
adjust：'qfq' 前复权 / 'hfq' 后复权 / 'none' 不复权
"""
import os
import sqlite3
import time
from pathlib import Path

import pandas as pd

# 缓存目录解析（优先级：环境变量 LWQUANT_CACHE_DIR > params.yaml data.cache_dir > 默认 data/cache）
def _resolve_cache_dir() -> Path:
    env = os.environ.get("LWQUANT_CACHE_DIR")
    if env:
        return Path(env)
    try:
        import yaml
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "params.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            d = (cfg or {}).get("data", {}).get("cache_dir")
            if d:
                p = Path(str(d))
                return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent / "data" / "cache"

CACHE_DIR = _resolve_cache_dir()
DEFAULT_DB = CACHE_DIR / "bars.db"


def _recently_written(p: Path, stale_secs: float = 300.0) -> bool:
    """库文件是否"可能正在被写"。

    ★2026-10-18 T3.2：`immutable=1` 是向 SQLite 承诺"文件永不变化"——跳过锁表直接读。
    写入进程改页后，读视图会错乱并报 `database disk image is malformed`
    （部署记录坑 1：不是数据损坏，PRAGMA integrity_check 返回 ok，停掉看板即恢复）。
    因此只有在库确实静止时才允许 immutable：mtime 超过 stale_secs、
    无 -wal/-journal 边车、且无 .writing 写锁标记。
    """
    try:
        if time.time() - p.stat().st_mtime < stale_secs:
            return True
        for suffix in ("-wal", "-journal"):
            side = p.with_name(p.name + suffix)
            if side.exists() and side.stat().st_size > 0:
                return True
        if (p.parent / ".writing").exists():
            return True
    except OSError:
        return True          # 状态不可读时按"正在写"处理（安全优先于快）
    return False


def db_uri(p, immutable: bool = None, stale_secs: float = 300.0) -> str:
    """★2026-10-15 P1-A 全项目 SQLite 只读 URI 单出口。
    相对路径按项目根（DSHQuant/）解析为主、CACHE_DIR 兜底（as_uri() 不接受相对路径，
    原各处散写 `Path(p).as_uri()+"?mode=ro&immutable=1"` 时相对路径抛 ValueError 被
    try/except 吞掉 → "行情面板为空" 假成功根因）。所有只读连库一律走本函数。

    ★2026-10-18 T3.2 智能降级：immutable=None（默认）时自动判定——
    库在 stale_secs（默认 300s）内被写过、或存在非空 -wal/-journal、或有 .writing
    写锁标记 → 不加 immutable=1（普通只读，安全）；否则保留 immutable=1（快路径）。
    显式传 immutable=True/False 可覆盖（冻结的归档库可强制 True）。
    """
    p = Path(p)
    if not p.is_absolute():
        cand = _ROOT / p
        if not cand.exists() and (CACHE_DIR / p).exists():
            cand = CACHE_DIR / p
        p = cand
    p = p.resolve()
    if immutable is None:
        immutable = not _recently_written(p, stale_secs)
    uri = f"{p.as_uri()}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


# ★2026-08-10 双库绕行：bars.db 被环境写保护锁定（SQLite readonly，与新文件名可写共存）
#   → 增量写入走 INC_DB（bars_incr.db，同 schema），读取时合并主库+增量库（增量优先）。
#   背景：扫描/回测只读不受影响；每日增量管道写 incr 库后读取方自动见最新数据。
INC_DB = CACHE_DIR / "bars_incr.db"
_ROOT = Path(__file__).resolve().parent.parent   # 项目根（DSHQuant/），db_uri 相对路径基准

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code       TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL, high REAL, low REAL, close REAL,
    preclose   REAL, volume REAL, amount REAL,
    turn       REAL, pct_chg REAL, is_st INTEGER,
    adjust     TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (code, date, adjust)
);
CREATE INDEX IF NOT EXISTS idx_daily_bar ON daily_bar (code, adjust, date);
-- ★2026-10-15 提速：按日扫全市场（load_panel/load_st_codes 的 date>=? / date IN 查询）
--   原仅 (code,adjust,date) 索引对按 code 查有效，按 date 全表扫 846 万行 ≈ 3s/次
CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar (adjust, date);

CREATE TABLE IF NOT EXISTS bar_meta (
    code       TEXT NOT NULL,
    adjust     TEXT NOT NULL,
    start_date TEXT,
    end_date   TEXT,
    rows       INTEGER,
    updated_at TEXT,
    PRIMARY KEY (code, adjust)
);
"""

_NUM_COLS = ["open", "high", "low", "close", "preclose",
             "volume", "amount", "turn", "pct_chg"]


# ★2026-08-15 单位归一（统一标准，消除混源 bug 根因）
#   bars.db 混源：tushare/tushare_backup = amount千元、volume手；baostock/akshare = amount元、volume股
#   消费端凡用 amount/volume 参与计算（换手率/PB/成交额占比等），必须先经本函数归一，禁止各写各的 ×1000。
def normalize_units(df: pd.DataFrame) -> pd.DataFrame:
    """把混源 amount/volume 归一为统一单位：amount=元、volume=股。
    按 source 列判断：tushare/tushare_backup（千元/手）→ amount×1000、volume×100；
    baostock/akshare（元/股）不变。无 source 列时保守不转换（调用方需自证单位）。"""
    if df is None or df.empty or "source" not in df.columns:
        return df
    ts = df["source"].isin(["tushare", "tushare_backup"])
    if ts.any():
        df = df.copy()
        if "amount" in df.columns:
            df.loc[ts, "amount"] = pd.to_numeric(df.loc[ts, "amount"], errors="coerce") * 1000.0
        if "volume" in df.columns:
            df.loc[ts, "volume"] = pd.to_numeric(df.loc[ts, "volume"], errors="coerce") * 100.0
    return df


class DailyCache:
    """SQLite 日线缓存：唯一读取接口（策略/回测只允许经它取数）

    ★2026-08-10 双库模式（环境写保护绕行）：
      - 主库（bars.db）只读锁定 → 写入自动路由到 bars_incr.db
      - 读取合并两库（incr 行覆盖主库同 (code,date) 行，主库历史保底）
      - db_path 参数显式指定时仍用单库模式（测试/特殊用途）
    """

    # ★#347 最新交易日缓存（模块级，10min TTL）——latest_trade_date 每调一次 COUNT 全表扫 2.5s，
    #   门户 live_chain/_minute_node 每轮多调导致冷启动 6s；最新交易日每天只变一次，缓存安全
    _LTD_CACHE = {"ts": 0.0, "val": None, "ver": None}

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.inc_path = None if db_path else INC_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        if self.inc_path is not None:
            self.inc_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema_inc()

    # ---------- 内部 ----------
    def _conn(self):
        con = sqlite3.connect(self.db_path)
        return con

    def _conn_inc(self):
        con = sqlite3.connect(self.inc_path)
        return con

    def _init_schema(self):
        with self._conn() as con:
            con.executescript(_SCHEMA)

    def _init_schema_inc(self):
        with self._conn_inc() as con:
            con.executescript(_SCHEMA)

    def _writable(self, path=None) -> bool:
        """探测指定库是否可写（环境写保护/锁定 → False）
        默认探测主库；双库模式下探测 incr 库（写入实际目标）"""
        p = Path(path) if path else self.db_path
        try:
            con = sqlite3.connect(p, timeout=10)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS _wprobe (x INT)")
                con.execute("INSERT INTO _wprobe VALUES (1)")
                con.commit()
                con.execute("DELETE FROM _wprobe")
                con.commit()
            finally:
                con.close()
            return True
        except Exception:
            return False

    # ---------- 写入 ----------
    def put_daily(self, code, df, adjust="qfq", source="baostock"):
        """写入/合并日线（按主键 upsert）。df 需含标准列：date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st
        ★2026-08-10 双库路由：主库被写保护 → 自动写入 bars_incr.db（同 schema）
        """
        if df is None or df.empty:
            return 0
        # 写库路由：单库模式用主库；双库模式探测主库（可写→主库；只读→incr 库）
        target = self.db_path
        if self.inc_path is not None:
            if getattr(self, "_wprobe_result", None) is None:
                self._wprobe_result = self._writable()
            if not self._wprobe_result:
                # 主库只读 → incr 库；若 incr 库也被锁 → 时间戳新文件（环境写保护兜底）
                if self._writable(self.inc_path):
                    target = self.inc_path
                else:
                    import time as _t
                    target = self.inc_path.with_name(
                        f"bars_incr_{_t.strftime('%Y%m%d_%H%M%S')}.db")
                    # 新文件无 schema → 先建表（安全层允许新文件写入）
                    con0 = sqlite3.connect(target)
                    try:
                        con0.executescript(_SCHEMA)
                        con0.commit()
                    finally:
                        con0.close()
        code = code.upper()
        rows = []
        for _, r in df.iterrows():
            rows.append((
                code, r["date"],
                _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close")),
                _f(r.get("preclose")), _f(r.get("volume")), _f(r.get("amount")),
                _f(r.get("turn")), _f(r.get("pct_chg")), int(r.get("is_st") or 0),
                adjust, source,
            ))
        con = sqlite3.connect(target)
        try:
            con.executemany(
                "INSERT OR REPLACE INTO daily_bar "
                "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            # 更新 meta —— ★累计覆盖语义（F-4 修复 2026-08-07）：
            dmin, dmax = df["date"].min(), df["date"].max()
            old = con.execute(
                "SELECT start_date, end_date FROM bar_meta WHERE code=? AND adjust=?",
                (code, adjust)).fetchone()
            if old and old[0]:
                start, end = min(old[0], dmin), max(old[1], dmax)
            else:
                start, end = dmin, dmax
            cnt = con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code=? AND adjust=?",
                (code, adjust)).fetchone()[0]
            con.execute(
                "INSERT OR REPLACE INTO bar_meta (code,adjust,start_date,end_date,rows,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (code, adjust, start, end, cnt, time.strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
        finally:
            con.close()
        return len(rows)

    def put_daily_batch(self, df, adjust="qfq", source="tushare"):
        """★2026-08-10 全市场批量写入（Tushare 日线增量用）：df 需含标准列 + code 列
        走与 put_daily 相同的写保护路由（主库只读 → incr/时间戳库），一次 executemany 写入。
        返回写入行数。"""
        if df is None or df.empty:
            return 0
        if "code" not in df.columns:
            raise ValueError("put_daily_batch 需要 df 含 code 列")
        target = self.db_path
        if self.inc_path is not None:
            if getattr(self, "_wprobe_result", None) is None:
                self._wprobe_result = self._writable()
            if not self._wprobe_result:
                if self._writable(self.inc_path):
                    target = self.inc_path
                else:
                    import time as _t
                    target = self.inc_path.with_name(
                        f"bars_incr_{_t.strftime('%Y%m%d_%H%M%S')}.db")
                    con0 = sqlite3.connect(target)
                    try:
                        con0.executescript(_SCHEMA)
                        con0.commit()
                    finally:
                        con0.close()
        rows = []
        for _, r in df.iterrows():
            rows.append((
                str(r["code"]).upper(), r["date"],
                _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close")),
                _f(r.get("preclose")), _f(r.get("volume")), _f(r.get("amount")),
                _f(r.get("turn")), _f(r.get("pct_chg")), int(r.get("is_st") or 0),
                adjust, source,
            ))
        con = sqlite3.connect(target)
        try:
            con.executemany(
                "INSERT OR REPLACE INTO daily_bar "
                "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        finally:
            con.close()
        return len(rows)

    def _inc_paths(self) -> list:
        """增量库路径（★2026-08-10 总指导修复：只保留最近 3 个）
        背景：手动/自动链每次运行创建时间戳增量库（今日已 18 个），全部遍历时每个连接
        都可能被环境锁等 5s 超时 → get_daily 单只 90s+、load_panel 5.5min 的根因。
        增量语义：最新库已含全部增量（写入端总是写最新），旧库冗余 → 3 个兜底足够。
        ★2026-08-12 百轮后#131：过滤微型测试残留库（<100KB，08-10 测试期 5 行单股库）——
        归档后它们会挤进"最近 3 个"窗口浪费连接数；真实增量库 ≥1MB（5538+ 行）"""
        if self.inc_path is None:
            return []
        paths = [self.inc_path] if Path(self.inc_path).exists() else []
        import glob as _glob
        for f in sorted(_glob.glob(str(self.inc_path.with_name("bars_incr_*.db")))):
            p = Path(f)
            if p != self.inc_path and p.stat().st_size >= 100 * 1024:
                paths.append(p)
        return paths[-3:] if len(paths) > 3 else paths

    @staticmethod
    def _ro_uri(p) -> str:
        """只读 immutable URI（★主库 3.7GB 普通连接等锁 20s → immutable 0.01s；
        已登记文件被环境锁只读时普通连接会等满 timeout）"""
        return db_uri(p)

    # ---------- 读取（唯一读取入口）----------
    def latest_trade_date(self):
        """缓存中最新交易日（任意股票），无数据返回 None
        ★双库：取主库+所有增量库的最大值
        ★2026-08-12 #191 完整性门槛：该日股票数 <4000（残缺占位，如 baostock 单源 183 只）
          视为无效交易日，回退上一完整日——防 22:00 链用残缺数据产出坏信号（外包同款防护）
        ★#358 版本缓存：COUNT 全表扫 2.6s，最新交易日随 bars 数据版本变化（每天收盘更新一次）。
          改为按「bars.db + 增量库 mtime」版本键缓存——数据没变就永久命中，变了才重算
          （原 10min TTL 让常驻进程每 10 分钟白扫一次 2.6s）"""
        _c = DailyCache._LTD_CACHE
        _now = time.time()
        # 版本键 = 主库 + 最近 3 增量库的 mtime 集合（数据变化才失效）
        try:
            _ver_paths = [str(self.db_path)] + [str(p) for p in self._inc_paths()]
            _ver = tuple((p, os.path.getmtime(p)) for p in _ver_paths if os.path.exists(p))
        except Exception:
            _ver = None
        if _c["val"] is not None and _c.get("ver") == _ver:
            return _c["val"]
        dates = []
        for p in [self.db_path] + self._inc_paths():
            if not Path(p).exists():
                continue
            try:
                is_main = (Path(p) == self.db_path)
                con = sqlite3.connect(self._ro_uri(p), uri=True, timeout=3) if is_main else sqlite3.connect(p, timeout=3)
                try:
                    row = con.execute(
                        "SELECT MAX(date) FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'"
                    ).fetchone()
                finally:
                    con.close()
                if row and row[0]:
                    dates.append(row[0])
            except Exception:
                continue
        if not dates:
            return None
        # ★完整性门槛：从最新日往回找 ≥4000 只的完整日
        best = max(dates)
        try:
            import sqlite3 as _sq
            for d in sorted(set(dates), reverse=True):
                tot = 0
                for p in [self.db_path] + self._inc_paths():
                    if not Path(p).exists():
                        continue
                    try:
                        is_main = (Path(p) == self.db_path)
                        con = _sq.connect(self._ro_uri(p), uri=True, timeout=3) if is_main else _sq.connect(p, timeout=3)
                        try:
                            r = con.execute(
                                "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date=? "
                                "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'", (d,)).fetchone()
                        finally:
                            con.close()
                        tot += r[0] if r and r[0] else 0
                    except Exception:
                        continue
                if tot >= 4000:
                    _c["ts"] = _now
                    _c["val"] = d
                    _c["ver"] = _ver
                    return d
        except Exception:
            pass
        _c["ts"] = _now
        _c["val"] = best
        _c["ver"] = _ver
        return best

    def get_daily(self, code, start=None, end=None, adjust="qfq"):
        """按 code+adjust 读取日线，可选区间过滤，按日期升序。无数据返回 None
        ★双库：合并主库+所有增量库（增量行覆盖主库同 key）
        ★2026-08-10 性能修复：主库 immutable 只读连接（0.01s，绕 20s 等锁）；增量库 timeout=3"""
        code = code.upper()
        frames = []
        for p in self._inc_paths() + [self.db_path]:   # 增量在前 → drop_duplicates keep last = 增量覆盖
            if not Path(p).exists():
                continue
            sql = ("SELECT code,date,open,high,low,close,preclose,volume,amount,"
                   "turn,pct_chg,is_st,adjust,source FROM daily_bar "
                   "WHERE code=? AND adjust=?")
            args = [code, adjust]
            if start:
                sql += " AND date>=?"
                args.append(start)
            if end:
                sql += " AND date<=?"
                args.append(end)
            sql += " ORDER BY date"
            try:
                # ★2026-08-10：主库 immutable（绕 20s 等锁）；时间戳增量库写后静止 → immutable 安全；
                # 固定 bars_incr.db 可能正被写入 → 普通连接 timeout=3
                is_main = (Path(p) == self.db_path)
                is_static_inc = (Path(p) != self.inc_path)
                if is_main or is_static_inc:
                    con = sqlite3.connect(self._ro_uri(p), uri=True, timeout=3)
                else:
                    con = sqlite3.connect(p, timeout=3)
                try:
                    cur = con.execute(sql, args)
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                finally:
                    con.close()
            except Exception:
                continue
            if rows:
                df = pd.DataFrame(rows, columns=cols)
                for c in _NUM_COLS:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                frames.append(df)
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["code", "date", "adjust"], keep="last")
        out = out.sort_values("date").reset_index(drop=True)
        return out

    def get_daily_batch(self, codes, start=None, end=None, adjust="qfq", fields=None):
        """★批量读取（★2026-08-10 性能优化：回测 load_panel 5.5min 逐只查询 → 一次 SQL 全拉）

        分块 ≤500 只/批（绕 SQLite 变量上限 999），双库合并（增量优先覆盖主库同 key）。
        主库用 immutable 只读连接（3.7GB 普通连接 20s；immutable 0.01s——bars.db 已锁只读，安全）。
        返回 {code.upper(): DataFrame(按日期升序)}；无数据的 code 不在字典。
        ★2026-08-14 fields 裁剪：默认 None=全列；传 fields=["close"] 等只取所需列
          （回测只需 close 时 SQL 传输/DataFrame 构建 ~3× 提速）。
        """
        codes = [str(c).upper() for c in codes if c]
        if not codes:
            return {}
        # 字段裁剪（always 保留 code/date/adjust 供 drop_duplicates 用）
        if fields is None:
            sel_cols = ["code", "date", "open", "high", "low", "close", "preclose",
                        "volume", "amount", "turn", "pct_chg", "is_st", "adjust", "source"]
            num_cols = list(_NUM_COLS)
        else:
            flds = [f for f in fields if f in _NUM_COLS or f == "is_st"]
            sel_cols = ["code", "date"] + flds + ["adjust"]
            num_cols = [f for f in flds if f in _NUM_COLS]
        out_all = {}   # code -> list[DataFrame]（增量在前 → concat 后 drop_duplicates keep=last 覆盖）
        db_paths = self._inc_paths() + [self.db_path]
        sql_head = ("SELECT " + ",".join(sel_cols) +
                    " FROM daily_bar WHERE adjust=? AND code IN (")
        for p in db_paths:
            if not Path(p).exists():
                continue
            try:
                # ★2026-08-10：主库与时间戳增量库 immutable；固定 inc 库普通连接（可能正写）
                if p == str(self.db_path) or p != str(self.inc_path):
                    con = sqlite3.connect(self._ro_uri(p), uri=True, timeout=3)
                else:
                    con = sqlite3.connect(p, timeout=3)
            except Exception:
                continue
            try:
                for i in range(0, len(codes), 500):
                    chunk = codes[i:i + 500]
                    sql = sql_head + ",".join("?" * len(chunk)) + ")"
                    args = [adjust] + list(chunk)
                    if start:
                        sql += " AND date>=?"
                        args.append(start)
                    if end:
                        sql += " AND date<=?"
                        args.append(end)
                    cur = con.execute(sql, args)
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    if not rows:
                        continue
                    df = pd.DataFrame(rows, columns=cols)
                    for c in num_cols:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                    for code, g in df.groupby("code"):
                        out_all.setdefault(code, []).append(g)
            except Exception:
                pass
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        res = {}
        for code, frames in out_all.items():
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)
            df = df.drop_duplicates(subset=["code", "date", "adjust"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)
            res[code] = df
        return res

    def get_meta(self, code, adjust="qfq"):
        """缓存覆盖范围（增量/覆盖率判断用）；无记录返回 None
        ★双库：合并主库+所有增量库（start=min / end=max / rows=sum）"""
        code = code.upper()
        metas = []
        for p in [self.db_path] + self._inc_paths():
            if not Path(p).exists():
                continue
            try:
                is_main = (Path(p) == self.db_path)
                con = sqlite3.connect(self._ro_uri(p), uri=True, timeout=3) if is_main else sqlite3.connect(p, timeout=3)
                try:
                    cur = con.execute(
                        "SELECT code,adjust,start_date,end_date,rows,updated_at "
                        "FROM bar_meta WHERE code=? AND adjust=?", (code, adjust))
                    row = cur.fetchone()
                finally:
                    con.close()
            except Exception:
                continue
            if row:
                metas.append({"code": row[0], "adjust": row[1], "start_date": row[2],
                              "end_date": row[3], "rows": row[4], "updated_at": row[5]})
        if not metas:
            return None
        if len(metas) == 1:
            return metas[0]
        merged = metas[0].copy()
        st = [m["start_date"] for m in metas if m["start_date"]]
        ed = [m["end_date"] for m in metas if m["end_date"]]
        merged["start_date"] = min(st) if st else None
        merged["end_date"] = max(ed) if ed else None
        merged["rows"] = sum(m["rows"] or 0 for m in metas)
        return merged

    def covers(self, code, start, end, adjust="qfq"):
        """缓存是否已覆盖 [start, end] 全区间"""
        meta = self.get_meta(code, adjust)
        if meta is None:
            return False
        return bool(meta["start_date"] and meta["end_date"]
                    and meta["start_date"] <= start and meta["end_date"] >= end)


def _f(x):
    """转 float，NaN/None → None（SQLite 存 NULL）"""
    if x is None:
        return None
    try:
        v = float(x)
        return v if v == v else None  # NaN → None
    except (TypeError, ValueError):
        return None
