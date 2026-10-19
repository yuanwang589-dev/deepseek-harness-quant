# -*- coding: utf-8 -*-
"""factors/pool/ic_store.py — 因子 IC 序列仓库（2026-10-18 · 立项：补"有池无序列"）

借鉴 VernonOY/alpha-skills（Apache-2.0）alpha-library 的两条设计：
  ① 因子表除业务字段外必须存 expression / ic_mean / icir / best_holding_period
     —— 原 factors 表四列全缺，"因子怎么算的、实测多少"都没留档；
  ② 因子的实测指标必须形成可查询的**时序**，而不是只留最后一次评估的汇总值
     —— 原 eval_ts._spearman_ic 内部算了逐月 IC 序列，返回时却只交汇总，序列被丢弃。

本项目落地（零新依赖，只用标准库 + pandas）：
  · factor_pool.db.factors 补 4 列（幂等 ALTER，旧数据不动）；
  · 新表 factor_ic_series：每因子逐窗口 IC 序列。
      - source='eval_ts'        ：时序通道，逐 12 个月滚动窗口一条（YYYY-MM = 窗口结束月）；
      - source='cross_sectional'：横截面通道，月末评估月度快照一条。

用法：
    from factors.pool.ic_store import connect, ensure_schema, save_series, save_snapshot
    con = connect()
    save_series(con, "epu_level", ic_series)                 # 时序：整条序列
    save_snapshot(con, "mom_20", "2026-10", 0.031, 0.42)     # 横截面：单点快照+回填指标
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
POOL_DB = BASE / "data" / "cache" / "factor_pool.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_ic_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name TEXT NOT NULL,
    window_end  TEXT NOT NULL,           -- IC 窗口结束标记：YYYY-MM（月度口径）
    ic          REAL,
    source      TEXT DEFAULT 'eval_ts',  -- eval_ts=时序逐窗口 / cross_sectional=横截面月度快照
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(factor_name, window_end, source)
);
CREATE INDEX IF NOT EXISTS idx_ic_series_name
    ON factor_ic_series(factor_name, window_end);
"""

# 借鉴 alpha-skills 的因子表字段（原表缺 4 列 → 幂等补齐，列存在则跳过）
_ADD_COLS = [
    ("expression", "TEXT DEFAULT ''"),   # 因子怎么算的（表达式/口径）
    ("ic_mean", "REAL"),                 # 最近一次实测 IC 均值
    ("icir", "REAL"),                    # 最近一次实测 ICIR
    ("best_holding_period", "INTEGER"),  # 最优持有期（按月，取 ICIR 最优 horizon）
]


def connect(pool_db: Path = POOL_DB, ensure: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(pool_db, timeout=10)
    con.row_factory = sqlite3.Row
    if ensure:
        ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """幂等：建序列表 + 给 factors 补 4 列（factors 表不存在则只建序列表）。可反复调用。"""
    con.executescript(_SCHEMA)
    has_factors = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='factors'").fetchone()
    if not has_factors:
        con.commit()
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(factors)")}
    for col, ddl in _ADD_COLS:
        if col not in have:
            con.execute(f"ALTER TABLE factors ADD COLUMN {col} {ddl}")
    con.commit()


def save_series(con: sqlite3.Connection, name: str, series, source: str = "eval_ts") -> int:
    """upsert 一个因子的 IC 序列（index=YYYY-MM，值=IC）。返回写入/更新行数。"""
    ensure_schema(con)
    if series is None:
        return 0
    s = pd.Series(series).dropna()
    if s.empty:
        return 0
    n = 0
    for k, v in s.items():
        con.execute(
            "INSERT INTO factor_ic_series(factor_name, window_end, ic, source) "
            "VALUES(?,?,?,?) ON CONFLICT(factor_name, window_end, source) "
            "DO UPDATE SET ic=excluded.ic",
            (name, str(k), float(v), source),
        )
        n += 1
    con.commit()
    return n


def save_snapshot(con: sqlite3.Connection, name: str, ym: str, ic: float,
                  icir: float | None = None, source: str = "cross_sectional",
                  best_holding_period: int | None = None) -> None:
    """横截面通道：单点快照（月末一次评估=一条记录），并回填 factors 指标列。"""
    ensure_schema(con)
    if ic is None or pd.isna(ic):
        return
    con.execute(
        "INSERT INTO factor_ic_series(factor_name, window_end, ic, source) "
        "VALUES(?,?,?,?) ON CONFLICT(factor_name, window_end, source) "
        "DO UPDATE SET ic=excluded.ic",
        (name, ym, float(ic), source),
    )
    sets = ["ic_mean = ?"]
    args = [float(ic)]
    if icir is not None and not pd.isna(icir):
        sets.append("icir = ?")
        args.append(float(icir))
    if best_holding_period is not None:
        sets.append("best_holding_period = ?")
        args.append(int(best_holding_period))
    args.append(name)
    con.execute(f"UPDATE factors SET {', '.join(sets)} WHERE name = ?", args)
    con.commit()


def load_series(con: sqlite3.Connection, name: str,
                source: str | None = None) -> pd.Series:
    """读某因子 IC 序列（按 window_end 升序）。无数据 → 空 Series（不假装有）。"""
    if source:
        rows = con.execute(
            "SELECT window_end, ic FROM factor_ic_series "
            "WHERE factor_name=? AND source=? ORDER BY window_end",
            (name, source)).fetchall()
    else:
        rows = con.execute(
            "SELECT window_end, ic FROM factor_ic_series "
            "WHERE factor_name=? ORDER BY window_end", (name,)).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({r["window_end"]: r["ic"] for r in rows}).astype(float)


def list_factors(con: sqlite3.Connection) -> list:
    """所有已有 IC 序列的因子名。"""
    return [r["factor_name"] for r in con.execute(
        "SELECT DISTINCT factor_name FROM factor_ic_series ORDER BY factor_name")]
