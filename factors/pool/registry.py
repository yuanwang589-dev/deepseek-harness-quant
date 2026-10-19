# -*- coding: utf-8 -*-
"""factors/pool/registry.py — 因子池注册表（长期架构 · 动态因子池核心）

定位：全项目因子统一注册/状态管理。因子池 = 横截面因子（选股，FACTOR_FUNCS）+ 时序因子
     （择时，如 EPU 政策不确定性）两类因子的注册表 + 生命周期状态机。

状态机（lifecycle.py 驱动）：
  candidate(候选/新挖) → evaluating(测评中) → active(活跃/可接入) ─┐
        ↑                      │                                  │ 漂移/失效
        │                      ▼                                  ▼
        └────────── 未达标留候选 / 淘汰 ←── retired(淘汰/归档) ←─ monitoring(监控中)

存储：data/cache/factor_pool.db 表 factors
  id, name, family(技术/基本面/政策/另类), kind(cross_sectional/time_series),
  source(数据源), freq, direction(+1/-1/0), status, score,
  added_at, last_eval_at, last_eval_detail(JSON), note

用法：
  from factors.pool.registry import FactorRegistry
  reg = FactorRegistry()
  reg.register(name='epu_level', family='政策', kind='time_series', source='FRED CHNMAINLANDEPU')
  reg.update_score('epu_level', 71.2, 'active', detail={...})
  reg.list_factors(status='active')
"""
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

DB_PATH = BASE / "data" / "cache" / "factor_pool.db"

STATUSES = ("candidate", "evaluating", "active", "monitoring", "retired")
KINDS = ("cross_sectional", "time_series")


class FactorRegistry:
    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self):
        """连接上下文：**退出即关闭**。

        ★2026-10-15 修复：原来各方法都写 `with sqlite3.connect(...) as con:`，
        但那**不是**关闭连接的上下文管理器——它是**事务**上下文，退出只
        commit/rollback，连接与文件句柄一直留着。后果：
          - 本类 11 个方法每次都漏一个连接（句柄泄漏）
          - Windows 上库文件被占用，无法删除/改名（测试里 TemporaryDirectory
            清理直接 WinError 32）
        """
        con = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init(self):
        with self._conn() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS factors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                family TEXT DEFAULT '其他',
                kind TEXT DEFAULT 'cross_sectional',
                source TEXT DEFAULT '',
                freq TEXT DEFAULT 'daily',
                direction INTEGER DEFAULT 1,
                status TEXT DEFAULT 'candidate',
                score REAL,
                added_at TEXT,
                last_eval_at TEXT,
                last_eval_detail TEXT,
                note TEXT DEFAULT '',
                locked INTEGER DEFAULT 0
            )""")
            # 兼容旧库：已有表无 locked 列时补列
            cols = [r[1] for r in con.execute("PRAGMA table_info(factors)").fetchall()]
            if "locked" not in cols:
                con.execute("ALTER TABLE factors ADD COLUMN locked INTEGER DEFAULT 0")
            con.commit()

    # ---------- 写 ----------
    def register(self, name, family="其他", kind="cross_sectional", source="", freq="daily",
                 direction=1, note="") -> bool:
        """注册新因子（已存在则更新元数据，不动 status）→ True 新建 / False 已存在"""
        assert kind in KINDS, f"kind 必须 ∈ {KINDS}"
        with self._conn() as con:
            cur = con.execute("SELECT COUNT(*) FROM factors WHERE name=?", (name,))
            exists = cur.fetchone()[0] > 0
            if not exists:
                con.execute(
                    "INSERT INTO factors (name,family,kind,source,freq,direction,status,added_at,note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (name, family, kind, source, freq, direction, "candidate",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note))
                con.commit()
            else:
                # ★2026-10-15：补上 note。原来只更新 family/kind/source/freq/direction，
                #   note 永远停留在首次插入的版本 —— 于是「已存在则更新元数据」名不副实，
                #   重新登记时描述改动（如合并来源、补数据缺口说明）写不进去。
                con.execute(
                    "UPDATE factors SET family=?,kind=?,source=?,freq=?,direction=?,note=? WHERE name=?",
                    (family, kind, source, freq, direction, note, name))
                con.commit()
            return not exists

    def set_status(self, name, status, score=None, detail: dict = None, note=None,
                   locked: bool = None):
        """更新状态/评分/评估详情（lifecycle 主入口）
        locked=True 时锁定人工裁决：自动评估只更新 score/detail，不再改 status"""
        assert status in STATUSES, f"status 必须 ∈ {STATUSES}"
        with self._conn() as con:
            if score is not None:
                con.execute("UPDATE factors SET status=?, score=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (status, score, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            elif note is not None:
                con.execute("UPDATE factors SET status=?, note=? WHERE name=?", (status, note, name))
            else:
                con.execute("UPDATE factors SET status=? WHERE name=?", (status, name))
            if locked is not None:
                con.execute("UPDATE factors SET locked=? WHERE name=?", (1 if locked else 0, name))
            con.commit()

    def update_score(self, name, score, status=None, detail: dict = None):
        """评估后回写（status 缺省保持现状；locked 因子的人工状态优先，不自动改 status）"""
        with self._conn() as con:
            locked = con.execute("SELECT locked FROM factors WHERE name=?", (name,)).fetchone()
            locked = bool(locked and locked[0])
            eff_status = status if not locked else None   # 锁定因子：人工状态不被自动覆盖
            if eff_status:
                con.execute("UPDATE factors SET score=?, status=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (score, eff_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            else:
                con.execute("UPDATE factors SET score=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (score, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            con.commit()

    def remove(self, name):
        with self._conn() as con:
            con.execute("DELETE FROM factors WHERE name=?", (name,))
            con.commit()

    # ---------- 读 ----------
    def get(self, name) -> dict | None:
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM factors WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["last_eval_detail"] = json.loads(d["last_eval_detail"]) if d.get("last_eval_detail") else {}
        return d

    def list_factors(self, status=None, kind=None) -> list[dict]:
        sql = "SELECT * FROM factors"
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if kind:
            cond.append("kind=?")
            args.append(kind)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY status, score DESC"
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_eval_detail"] = json.loads(d["last_eval_detail"]) if d.get("last_eval_detail") else {}
            out.append(d)
        return out

    def stats(self) -> dict:
        with self._conn() as con:
            rows = con.execute("SELECT status, COUNT(*) FROM factors GROUP BY status").fetchall()
            kinds = con.execute("SELECT kind, COUNT(*) FROM factors GROUP BY kind").fetchall()
        return {"by_status": dict(rows), "by_kind": dict(kinds),
                "total": sum(c for _, c in rows)}


if __name__ == "__main__":
    reg = FactorRegistry()
    print("因子池统计:", reg.stats())
    for f in reg.list_factors():
        print(f"  [{f['status']:9}] {f['name']:<18} {f['kind']:<16} score={f['score']} 源={f['source']}")
