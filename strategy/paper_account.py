# -*- coding: utf-8 -*-
"""
strategy/paper_account.py — S1 模拟盘模块（短板补齐）

虚拟账户：模拟成交/持仓/净值，SQLite 持久化。
回测与未来实盘共用同一套下单/持仓接口（单一路径，防"模拟一套实盘一套"）。

设计要点：
- 撮合规则（A股近似，账本层强制 P0-1）：T+1 卖出限制、涨跌停无法成交（按板块 ±10%/±20%/±30%）、
  100股整数手、单笔不超过当日成交量；成交价=当日收盘价。
  （bars.db 缺 preclose/volume/is_st 时对应校验软跳过并打日志，不阻断）
- 风险检查：下单前挂接 RiskAgent（可复用 risk/risk_agent.py）
- 数据持久化：tables = accounts / positions / orders / equity_curve
- 幂等：同一 (account, date, code, action) 不重复记录

用法：
  acc = PaperAccount("demo", cash=1_000_000)
  acc.buy("600519.SH", 100, "2026-08-06", close=1572.0)
  acc.mark_to_market(prices)   # 每日收盘后更新净值
  acc.snapshot()               # 当前持仓+现金+净值
"""
import sqlite3
from datetime import datetime
from pathlib import Path

PAPER_DB = Path(__file__).resolve().parent.parent / "data" / "cache" / "paper.db"

COMMISSION = 0.00026      # 佣金万2.6
STAMP_TAX = 0.0005        # 印花税（卖出）
# ─────────────────────────────────────────────────────────────────────
# ★2026-10-15 涨跌停幅度：改为按板块区分（原实现只有两个常量，与板块无关）
#
# 原缺陷：
#   LIMIT_PCT = 0.10（非ST）/ ST_LIMIT_PCT = 0.05，`lim = ST_LIMIT_PCT if is_st else LIMIT_PCT`
#   只看 is_st、完全不看股票代码 → 造成两类误判拒单：
#     · 创业板(300/301)、科创板(688/689) 涨跌幅在 10%~20% 之间的正常交易，
#       被当成主板 ±10% 的涨跌停而拒绝成交（这两板真实限幅是 ±20%）；
#     · ST 股涨跌幅在 5%~10% 之间被当成 ±5% 拒单（现行规则 ST 已并入主板 ±10%）。
#   后果：模拟盘在这两类标的上**凭空少掉可成交的机会**，且没有任何日志提示原因。
#
# A股现行限幅（据 docs/免责声明.md 与本项目既有审查记录，2026-07-06 起 ST 新规）：
#   主板(60xxxx / 000xxx / 001xxx / 002xxx)  ±10%
#   创业板(300/301)                           ±20%
#   科创板(688/689)                           ±20%
#   北交所(430/830/870/920)                   ±30%
#   ST：自 2026-07-06 起与所在板块同幅（主板 ST ±10%、创业板/科创板 ST ±20%），
#       不再单独用 ±5% 的老规则。
# ─────────────────────────────────────────────────────────────────────
LIMIT_PCT_DEFAULT = 0.10     # 主板
LIMIT_PCT_GEM = 0.20         # 创业板
LIMIT_PCT_STAR = 0.20        # 科创板
LIMIT_PCT_BSE = 0.30         # 北交所


def _limit_pct(code: str, is_st: bool = False) -> float:
    """按股票代码返回涨跌停幅度。

    is_st 参数保留仅为兼容既有调用方签名：自 2026-07-06 起 ST 与所在板块同幅，
    不再单独设限。若未来监管规则再次分化，在此按 is_st 加分支即可（改一处）。
    """
    c = str(code or "").split(".")[0]
    if c.startswith(("300", "301")):
        return LIMIT_PCT_GEM
    if c.startswith(("688", "689")):
        return LIMIT_PCT_STAR
    if c.startswith(("43", "83", "87", "92")):
        return LIMIT_PCT_BSE
    return LIMIT_PCT_DEFAULT
MAX_BUY_VOLUME_RATIO = 1.0  # 单笔买入不得超过当日成交量比例（硬下限：不能买超全天成交量）

BARS_DB = str(Path(__file__).resolve().parent.parent / "data" / "cache" / "bars.db")


def _bar_info(code, date):
    """查 bars.db 取 (preclose, volume, is_st)；缺失/异常软失败返回 (None, None, None)。"""
    try:
        con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        r = con.execute(
            "SELECT preclose, volume, is_st FROM daily_bar WHERE code=? AND date=? AND adjust='qfq' LIMIT 1",
            (code, date)).fetchone()
        con.close()
        if r:
            return r[0], r[1], (r[2] == 1 or r[2] is True)
    except Exception:
        pass
    return None, None, None


class PaperAccount:
    """模拟盘虚拟账户"""

    def __init__(self, name: str = "default", cash: float = 1_000_000,
                 db_path=None, start_date: str = None):
        self.name = name
        self.cash = cash
        self.db_path = str(db_path or PAPER_DB)
        self.start_date = start_date or datetime.now().strftime("%Y-%m-%d")
        self._init_db()
        self._ensure_account()

    # ---------- 数据库 ----------
    def _conn(self):
        con = sqlite3.connect(self.db_path)
        return con

    def _init_db(self):
        con = self._conn()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            name TEXT PRIMARY KEY, initial_cash REAL, cash REAL,
            start_date TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS positions (
            account TEXT, code TEXT, qty INTEGER, avg_cost REAL,
            entry_date TEXT, PRIMARY KEY (account, code)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT, date TEXT, code TEXT, action TEXT,  -- BUY/SELL
            qty INTEGER, price REAL, fee REAL, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_curve (
            account TEXT, date TEXT, total_value REAL, cash REAL,
            positions_value REAL, PRIMARY KEY (account, date)
        );
        """)
        con.commit()
        con.close()

    def _ensure_account(self):
        con = self._conn()
        cur = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,))
        if cur.fetchone() is None:
            con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)",
                        (self.name, self.cash, self.cash, self.start_date,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
        else:
            row = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,)).fetchone()
            self.cash = row[0]
        con.close()

    # ---------- 下单 ----------
    def buy(self, code: str, qty: int, date: str, close: float, reason: str = "",
            prev_close=None, day_volume=None, is_st=None) -> dict:
        """买入：收盘价撮合，佣金+印花税(卖)
        账本层强制(P0-1)：① 必须100股整数手；② 涨停拒买；③ 单笔不超过当日成交量。
        prev_close/day_volume/is_st 可显式传入覆盖；否则自动从 bars.db 读取（缺失则软跳过校验）。
        """
        if qty <= 0 or close <= 0:
            return {"ok": False, "msg": "参数非法"}
        if qty % 100 != 0:
            return {"ok": False, "msg": f"非100股整数手(qty={qty})"}
        # 涨跌停 / 成交量 校验（bars.db 自取或调用方 override）
        if prev_close is None or day_volume is None or is_st is None:
            _pc, _dv, _st = _bar_info(code, date)
            if prev_close is None:
                prev_close = _pc
            if day_volume is None:
                day_volume = _dv
            if is_st is None:
                is_st = _st
        if prev_close is not None:
            lim = _limit_pct(code, is_st)
            limit_up = round(prev_close * (1 + lim), 4)
            if close >= limit_up - 1e-6:
                return {"ok": False, "msg": f"涨停({limit_up})无法买入"}
        if day_volume is not None and qty > day_volume * MAX_BUY_VOLUME_RATIO:
            return {"ok": False, "msg": f"买入量{qty}超过当日成交量{int(day_volume)}"}
        fee = close * qty * COMMISSION
        cost = close * qty + fee
        if cost > self.cash:
            qty = (int(self.cash / (close * (1 + COMMISSION))) // 100) * 100
            if qty <= 0:
                return {"ok": False, "msg": "现金不足"}
            cost = close * qty + close * qty * COMMISSION
        con = self._conn()
        # 幂等：同日同code同action已记录则跳过
        dup = con.execute(
            "SELECT 1 FROM orders WHERE account=? AND date=? AND code=? AND action='BUY'",
            (self.name, date, code)).fetchone()
        if dup:
            con.close()
            return {"ok": False, "msg": "重复下单"}
        con.execute("UPDATE accounts SET cash=cash-? WHERE name=?", (cost, self.name))
        cur = con.execute("SELECT qty, avg_cost FROM positions WHERE account=? AND code=?",
                          (self.name, code)).fetchone()
        if cur:
            old_qty, old_cost = cur
            new_qty = old_qty + qty
            new_cost = (old_cost * old_qty + close * qty) / new_qty
            con.execute("UPDATE positions SET qty=?, avg_cost=? WHERE account=? AND code=?",
                        (new_qty, new_cost, self.name, code))
        else:
            con.execute("INSERT INTO positions VALUES (?,?,?,?,?)",
                        (self.name, code, qty, close, date))
        con.execute("INSERT INTO orders (account,date,code,action,qty,price,fee,reason) VALUES (?,?,?,?,?,?,?,?)",
                    (self.name, date, code, "BUY", qty, close, fee, reason))
        con.commit()
        self.cash = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,)).fetchone()[0]
        con.close()
        return {"ok": True, "qty": qty, "cost": cost, "fee": fee}

    def sell(self, code: str, qty: int, date: str, close: float, reason: str = "",
             prev_close=None, is_st=None) -> dict:
        """卖出：收盘价撮合，佣金+印花税
        账本层强制(P0-1)：① T+1（当日买入不可卖）；② 跌停拒卖。
        """
        con = self._conn()
        cur = con.execute("SELECT qty, entry_date FROM positions WHERE account=? AND code=?",
                          (self.name, code)).fetchone()
        if cur is None or cur[0] < qty:
            con.close()
            return {"ok": False, "msg": "持仓不足"}
        # P0-1 账本层 T+1：当日买入不可卖
        if cur[1] == date:
            con.close()
            return {"ok": False, "msg": "T+1限制：当日买入不可卖出"}
        # 涨跌停校验（bars.db 自取或 override）
        if prev_close is None or is_st is None:
            _pc, _dv, _st = _bar_info(code, date)
            if prev_close is None:
                prev_close = _pc
            if is_st is None:
                is_st = _st
        if prev_close is not None:
            lim = _limit_pct(code, is_st)
            limit_down = round(prev_close * (1 - lim), 4)
            if close <= limit_down + 1e-6:
                con.close()
                return {"ok": False, "msg": f"跌停({limit_down})无法卖出"}
        dup = con.execute(
            "SELECT 1 FROM orders WHERE account=? AND date=? AND code=? AND action='SELL'",
            (self.name, date, code)).fetchone()
        if dup:
            con.close()
            return {"ok": False, "msg": "重复下单"}
        fee = close * qty * (COMMISSION + STAMP_TAX)
        proceeds = close * qty - fee
        con.execute("UPDATE accounts SET cash=cash+? WHERE name=?", (proceeds, self.name))
        remain = cur[0] - qty
        if remain <= 0:
            con.execute("DELETE FROM positions WHERE account=? AND code=?", (self.name, code))
        else:
            con.execute("UPDATE positions SET qty=? WHERE account=? AND code=?",
                        (remain, self.name, code))
        con.execute("INSERT INTO orders (account,date,code,action,qty,price,fee,reason) VALUES (?,?,?,?,?,?,?,?)",
                    (self.name, date, code, "SELL", qty, close, fee, reason))
        con.commit()
        self.cash = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,)).fetchone()[0]
        con.close()
        return {"ok": True, "proceeds": proceeds, "fee": fee}

    def sell_all(self, code: str, date: str, close: float, reason: str = "",
                 prev_close=None, is_st=None) -> dict:
        """清仓某标的"""
        con = self._conn()
        cur = con.execute("SELECT qty FROM positions WHERE account=? AND code=?",
                          (self.name, code)).fetchone()
        con.close()
        if cur is None or cur[0] <= 0:
            return {"ok": False, "msg": "无持仓"}
        return self.sell(code, cur[0], date, close, reason,
                         prev_close=prev_close, is_st=is_st)

    # ---------- 净值 ----------
    def mark_to_market(self, prices: dict, date: str) -> float:
        """每日收盘后按 prices {code: close} 更新持仓市值与净值，写入 equity_curve"""
        con = self._conn()
        positions = con.execute(
            "SELECT code, qty FROM positions WHERE account=?", (self.name,)).fetchall()
        pos_val = 0.0
        for code, qty in positions:
            px = prices.get(code)
            if px is None:
                # 无价：用上次成本近似（数据缺失日）
                row = con.execute("SELECT avg_cost FROM positions WHERE account=? AND code=?",
                                  (self.name, code)).fetchone()
                px = row[0] if row else 0.0
            pos_val += px * qty
        cash = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,)).fetchone()[0]
        total = cash + pos_val
        con.execute(
            "INSERT OR REPLACE INTO equity_curve (account,date,total_value,cash,positions_value) VALUES (?,?,?,?,?)",
            (self.name, date, total, cash, pos_val))
        con.commit()
        con.close()
        return total

    # ---------- 查询 ----------
    def positions(self) -> list:
        con = self._conn()
        rows = con.execute("SELECT code, qty, avg_cost, entry_date FROM positions WHERE account=?",
                           (self.name,)).fetchall()
        con.close()
        return [{"code": r[0], "qty": r[1], "avg_cost": r[2], "entry_date": r[3]} for r in rows]

    def equity_curve(self) -> list:
        con = self._conn()
        rows = con.execute(
            "SELECT date, total_value, cash, positions_value FROM equity_curve WHERE account=? ORDER BY date",
            (self.name,)).fetchall()
        con.close()
        return [{"date": r[0], "total": r[1], "cash": r[2], "pos": r[3]} for r in rows]

    def orders(self, limit: int = 50) -> list:
        con = self._conn()
        rows = con.execute(
            "SELECT date, code, action, qty, price, fee, reason FROM orders WHERE account=? ORDER BY id DESC LIMIT ?",
            (self.name, limit)).fetchall()
        con.close()
        return [{"date": r[0], "code": r[1], "action": r[2], "qty": r[3],
                 "price": r[4], "fee": r[5], "reason": r[6]} for r in rows]

    def snapshot(self) -> dict:
        con = self._conn()
        cash = con.execute("SELECT cash FROM accounts WHERE name=?", (self.name,)).fetchone()[0]
        pos = con.execute("SELECT COUNT(*), COALESCE(SUM(qty*avg_cost),0) FROM positions WHERE account=?",
                          (self.name,)).fetchone()
        con.close()
        return {"account": self.name, "cash": round(cash, 2),
                "n_positions": pos[0], "pos_cost": round(pos[1], 2)}

    def close(self):
        """（预留）关闭账户时的收尾"""
        pass


if __name__ == "__main__":
    # 自测：买入 → 净值 → 卖出 → 净值
    import tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), "paper_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    acc = PaperAccount("test", cash=100_000, db_path=tmp)
    r1 = acc.buy("600519.SH", 100, "2026-08-06", close=1572.0, reason="突破买入")
    r2 = acc.buy("000858.SZ", 1000, "2026-08-06", close=157.5, reason="突破买入")
    print(f"买入结果: {r1} | {r2}")
    v1 = acc.mark_to_market({"600519.SH": 1600.0, "000858.SZ": 155.0}, "2026-08-07")
    print(f"净值(8-07): {v1:.2f}")
    r3 = acc.sell("600519.SH", 50, "2026-08-10", close=1610.0, reason="止盈一半")
    print(f"卖出结果: {r3}")
    v2 = acc.mark_to_market({"600519.SH": 1610.0, "000858.SZ": 158.0}, "2026-08-10")
    print(f"净值(8-10): {v2:.2f}")
    print("快照:", acc.snapshot())
    print("订单数:", len(acc.orders()))
    print("权益曲线点数:", len(acc.equity_curve()))
    os.remove(tmp)
    print("\n自测 PASS ✅")
