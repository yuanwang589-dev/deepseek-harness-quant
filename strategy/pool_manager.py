# -*- coding: utf-8 -*-
"""三池管理（程序三大能力之一，主文档 4.4）
全市场 → 排名 Top N → 候选池 → 买点出现 → 持有池 → 卖出 → 历史
                           └→ 未到买点 → 观察池（记录原因）→ 超时降级

池状态机：
  candidate（候选池）: 排名 Top N 且通过硬排除
  watch（观察池）:     有潜力但未到买点（等突破/等Regime/等放量/等财报）
  holding（持有池）:   已买入，每日生成操作指令
  history（历史）:     已卖出
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
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import json
import sqlite3
from datetime import datetime

WATCH_MAX_WEEKS = 8          # 观察池超 8 周无买点降级（params.yaml pools.watch_max_weeks）
CANDIDATE_TOP_N = 15         # 候选池容量


class PoolManager:
    """三池管理器（持久化到 SQLite pools.db）"""

    def __init__(self, db_path: str = None):
        self.db = db_path or (BASE / "data" / "cache" / "pools.db")
        self._init()

    def _init(self):
        con = sqlite3.connect(str(self.db))
        con.execute("""CREATE TABLE IF NOT EXISTS pools (
            code TEXT PRIMARY KEY,
            pool TEXT,                -- candidate/watch/holding/history
            entry_date TEXT,
            entry_reason TEXT,        -- 入池原因（排名分/买点类型）
            watch_reason TEXT,        -- 未到买点原因
            watch_start TEXT,         -- 进观察池日期
            exit_date TEXT,
            exit_reason TEXT,
            note TEXT
        )""")
        con.commit()
        con.close()

    def _conn(self):
        return sqlite3.connect(str(self.db))

    # ---------- 读 ----------
    def get_pool(self, pool: str) -> list:
        con = self._conn()
        rows = con.execute("SELECT code, entry_date, entry_reason, watch_reason FROM pools WHERE pool=?",
                           (pool,)).fetchall()
        con.close()
        return rows

    def all(self) -> dict:
        con = self._conn()
        rows = con.execute("SELECT code, pool, entry_date, entry_reason, watch_reason, "
                           "watch_start, exit_date, exit_reason FROM pools").fetchall()
        con.close()
        return {r[0]: {"pool": r[1], "entry_date": r[2], "entry_reason": r[3],
                       "watch_reason": r[4], "watch_start": r[5],
                       "exit_date": r[6], "exit_reason": r[7]} for r in rows}

    # ---------- 写 ----------
    def _upsert(self, code, **fields):
        con = self._conn()
        existing = con.execute("SELECT pool FROM pools WHERE code=?", (code,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE pools SET {sets} WHERE code=?", (*fields.values(), code))
        else:
            # ★INSERT 必须带 code（此前漏掉导致 code=NULL、流转失败）
            fields["code"] = code
            keys = list(fields.keys())
            vals = list(fields.values())
            con.execute(f"INSERT INTO pools ({','.join(keys)}) VALUES ({','.join('?'*len(keys))})",
                        vals)
        con.commit()
        con.close()

    def to_candidate(self, code, score, date):
        """排名入候选池"""
        self._upsert(code, pool="candidate", entry_date=date,
                     entry_reason=f"排名{score:.3f}", watch_reason=None)

    def to_watch(self, code, reason, date):
        """候选池 → 观察池（未到买点）"""
        self._upsert(code, pool="watch", watch_reason=reason, watch_start=date)

    def to_holding(self, code, reason, date):
        """买入 → 持有池"""
        self._upsert(code, pool="holding", entry_reason=reason,
                     watch_reason=None, watch_start=None)

    def to_history(self, code, reason, date):
        """卖出 → 历史"""
        self._upsert(code, pool="history", exit_date=date, exit_reason=reason)

    # ---------- 状态机 ----------
    def update_pools(self, rank_df, buy_signals=None, holdings_out=None, date=None):
        """每日/每周更新三池流转。
        rank_df: 排名引擎输出（候选来源）
        buy_signals: {code: 买点类型}，候选池中出现买点的 → 持有池
        holdings_out: {code: 卖出原因}，持有池触发卖出的 → 历史
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        buy_signals = buy_signals or {}
        holdings_out = holdings_out or {}

        # 1. 持有池卖出 → 历史（★卖出当日不重新入池，防止拉回）
        sold_today = set()
        for code, reason in holdings_out.items():
            self.to_history(code, reason, date)
            sold_today.add(code)

        # 2. 候选池：排名 Top N 刷新（跳过当日已卖出）
        top_codes = set(rank_df["code"].head(CANDIDATE_TOP_N)) - sold_today
        current = self.all()
        for code in top_codes:
            if code not in current or current[code]["pool"] in ("history",):
                score = rank_df.loc[rank_df["code"] == code, "综合分"].iloc[0]
                self.to_candidate(code, score, date)

        # 3. 候选池 → 买点 → 持有 / 否则观察
        for code, info in self.all().items():
            if info["pool"] != "candidate":
                continue
            if code in buy_signals:
                self.to_holding(code, f"买点:{buy_signals[code]}", date)
            else:
                self.to_watch(code, self._watch_reason(code, rank_df), date)

        # 4. 观察池超时降级
        for code, info in self.all().items():
            if info["pool"] == "watch" and info["watch_start"]:
                try:
                    weeks = (datetime.strptime(date, "%Y-%m-%d")
                             - datetime.strptime(info["watch_start"], "%Y-%m-%d")).days / 7
                    if weeks > WATCH_MAX_WEEKS:
                        self.to_history(code, "观察超时降级", date)
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "strategy/pool_manager.py:146 update_pools")

    @staticmethod
    def _watch_reason(code, rank_df):
        """观察原因判定（简化：默认等买点）"""
        row = rank_df[rank_df["code"] == code]
        if row.empty:
            return "等买点"
        try:
            if "bonus分" in rank_df.columns and row["bonus分"].iloc[0] < 0.5:
                return "等VCP突破"
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/pool_manager.py:158 _watch_reason")
        return "等放量确认"

    def report(self) -> str:
        """三池报告"""
        con = self._conn()
        cnt = dict(con.execute("SELECT pool, COUNT(*) FROM pools GROUP BY pool").fetchall())
        con.close()
        return (f"候选池 {cnt.get('candidate', 0)} | 观察池 {cnt.get('watch', 0)} | "
                f"持有池 {cnt.get('holding', 0)} | 历史 {cnt.get('history', 0)}")


if __name__ == "__main__":
    print("=== 三池管理自测 ===")
    pm = PoolManager(db_path=str(BASE / "data" / "cache" / "pools_test.db"))
    # 模拟：3 只入候选池
    import pandas as pd
    rank_df = pd.DataFrame({
        "code": ["600519.SH", "000858.SZ", "002252.SZ"],
        "综合分": [0.9, 0.85, 0.80],
    })
    pm.update_pools(rank_df, buy_signals={"000858.SZ": "VCP突破"}, date="2026-08-06")
    print("更新后:", pm.report())
    print("观察池:", pm.get_pool("watch"))
    print("持有池:", pm.get_pool("holding"))
    # 卖出一只
    pm.update_pools(rank_df, holdings_out={"600519.SH": "硬止损-7%"}, date="2026-08-10")
    print("卖出后:", pm.report())
