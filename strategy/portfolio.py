# -*- coding: utf-8 -*-
"""strategy/portfolio.py — 持仓管理（T-2 Deck 审批闭环 · 外包 AI-1 · 2026-08-09）

★目的：落地用户纪律"持股≤5 只"。Deck 审批 buy → 自动入持仓（状态机：holding→exit），
Deck 显示当前持仓 + 卖出操作；模拟盘实时段联动（sim_tracks 读持仓快照）。

数据流：
  deck_decisions.json（审批记录，外包 #1 产出）
      → portfolio.sync_from_decisions()  →  logs/portfolio.json（持仓表 + 历史）
      → deck.html "💼 当前持仓"卡片（GET /api/portfolio）
      → sim_tracks.py meta 快照（模拟盘实时段联动）

状态机：
  holding 持有（entry_price/entry_date/target/stop）
  exit    已卖出（exit_date/exit_price/reason 入 history）
  over_limit 超 5 只纪律拒绝（保留记录但不入持仓）

用法：
  python strategy/portfolio.py --sync           # 从 deck_decisions 同步新买入
  python strategy/portfolio.py --sell CODE PRICE REASON   # 卖出
  python strategy/portfolio.py --status         # 查看持仓
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PORTFOLIO_JSON = BASE / "logs" / "portfolio.json"
DECISIONS_JSON = BASE / "logs" / "deck_decisions.json"
# ★2026-08-10 审计修复：主程序 08-09 起风控输出为 stock_risk_map_v2.json（5202 只全市场），v1(1767 只)已过时
RISK_JSON = BASE / "logs" / "stock_risk_map_v2.json"
if not RISK_JSON.exists():
    RISK_JSON = BASE / "logs" / "stock_risk_map.json"
# ★2026-08-11 写保护免疫：portfolio/deck_decisions 固定文件名多次写被锁（Permission denied 实测）
#   → _load 读最新时间戳文件（glob），_save 写时间戳文件；读取方（deck_server /api/portfolio）同样走 _load
def _latest_glob(pat: str, fallback: Path) -> Path:
    import glob as _g
    # ★排除固定名（portfolio.json 等被锁残留可能是空/旧内容）：glob * 可匹配空字符会误收固定名
    fs = sorted([Path(p) for p in _g.glob(str(BASE / "logs" / pat))
                 if Path(p).name != fallback.name],
                key=lambda x: x.stat().st_mtime)
    return fs[-1] if fs else fallback

# ★2026-08-10 B-11 配合（AI-2 认领自动止损引擎，持仓需带 otype/stop_plan）：止损矩阵 v2（分包2 交付）
STOP_MATRIX_JSON = Path(
    r"data/factorpool/output/combo_reports/stop_matrix_v2.json")

MAX_POSITIONS = 5          # 用户纪律：持股 ≤ 5 只


def _entry_price_of(code: str, date: str):
    """★2026-08-11 百轮#38：从 bars.db 取该股 date 当日收盘价（immutable 快速连接）
    审批买入价自动补——持仓有 entry_price 才能算收益/盈亏/收益率"""
    try:
        import sqlite3
        con = sqlite3.connect("file:data/cache/bars.db?mode=ro&immutable=1",
                              uri=True, timeout=3)
        row = con.execute(
            "SELECT close FROM daily_bar WHERE code=? AND date=? ORDER BY adjust DESC LIMIT 1",
            (code, date)).fetchone()
        con.close()
        return float(row[0]) if row and row[0] else None
    except Exception:
        return None


def _latest_close_of(code: str):
    """★2026-08-11 百轮#38：该股最新收盘价（组合盈亏现价基准）
    ★2026-08-12 百轮#102：双库合并——主库写保护后最新日数据在 bars_incr 增量库，
    单读主库会让盈亏停在旧日（#65 教训）"""
    try:
        import sqlite3
        import glob as _glob
        from pathlib import Path as _P
        # ★2026-10-15 修复：绝对路径（as_uri() 不接受相对路径，原写法静默失败）
        paths = [str(BASE / "data" / "cache" / "bars.db")]
        try:
            from data.cache import CACHE_DIR
            paths += [str(p) for p in sorted(CACHE_DIR.glob("bars_incr_*.db"))[-3:]]
        except Exception:
            pass
        best = None
        for _p in paths:
            try:
                from data.cache import db_uri as _db_uri
                _uri = _db_uri(_p)
                con = sqlite3.connect(_uri, uri=True, timeout=3)
                row = con.execute(
                    "SELECT close, date FROM daily_bar WHERE code=? ORDER BY date DESC LIMIT 1",
                    (code,)).fetchone()
                con.close()
                if row and row[0] and (best is None or row[1] > best[1]):
                    best = (float(row[0]), row[1])
            except Exception:
                continue
        return best[0] if best else None
    except Exception:
        return None


def _load_latest_trade_date() -> str | None:
    """★2026-08-12 十轮#172：最新交易日（主库+增量库合并探测，immutable 防锁）"""
    try:
        import sqlite3 as _sq
        from data.cache import CACHE_DIR
        dates = []
        for _db in [CACHE_DIR / "bars.db"] + sorted(CACHE_DIR.glob("bars_incr_*.db"))[-3:]:
            if not _db.exists():
                continue
            try:
                with _sq.connect(f"file:{_db.as_posix()}?mode=ro&immutable=1", uri=True, timeout=3) as _c:
                    _d = _c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
                    if _d:
                        dates.append(_d)
            except Exception:
                continue
        return max(dates) if dates else None
    except Exception:
        return None


def _load() -> dict:
    # ★2026-08-11 写保护免疫：读最新 portfolio_*.json（时间戳文件）；无则回退固定名
    p = _latest_glob("portfolio_*.json", PORTFOLIO_JSON)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "updated_at": None, "max_positions": MAX_POSITIONS,
            "positions": [], "history": []}


def _save(d: dict):
    d["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d["max_positions"] = MAX_POSITIONS
    # ★2026-08-11 写保护免疫：时间戳文件名（固定名 portfolio.json 多次写被锁 → Permission denied 实测）
    p = BASE / "logs" / f"portfolio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _risk_map() -> dict:
    # ★2026-08-11 写保护免疫：stock_risk_map 已是时间戳文件（scan_all 主写时间戳）→ glob 取最新，排除固定名
    p = _latest_glob("stock_risk_map_*.json", RISK_JSON)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {r["code"]: r.get("level") for r in d.get("results", [])}
    except Exception:
        return {}


# ============ B-11 配合：otype / stop_plan（AI-1 提供，AI-2 的 position_stop_check 只读消费） ============

def _load_stop_matrix() -> dict:
    """止损矩阵 v2（分包2 交付）：{otype: {stop_loss_pct/time_stop_weeks/max_drawdown_pct/trailing_ma}}"""
    if not STOP_MATRIX_JSON.exists():
        return {}
    try:
        return json.loads(STOP_MATRIX_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _otype_of(code: str):
    """从最新机会池（按 mtime，排除 calib 测试池）查 otype；找不到返回 None"""
    import glob
    pools = sorted(glob.glob(str(BASE / "logs" / "opp_pool_*.json")),
                   key=lambda p: Path(p).stat().st_mtime, reverse=True)
    for p in pools[:5]:
        name = Path(p).name
        if "calib" in name:
            continue
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            for o in d.get("opportunities", []):
                if o.get("code") == code:
                    return o.get("otype")
        except Exception:
            continue
    return None


def _name_of(code: str):
    """★#321 股票名兜底：从 stock_basic.db（全市场名表 5542 只，code→name）查；找不到返回 None"""
    try:
        import sqlite3
        con = sqlite3.connect("file:data/cache/stock_basic.db?mode=ro&immutable=1", uri=True, timeout=3)
        try:
            row = con.execute("SELECT name FROM stock_basic WHERE code=?", (code,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception:
        return None


def stop_plan_for(otype, matrix: dict = None) -> dict:
    """按 otype 从止损矩阵 v2 生成 stop_plan（无 otype/矩阵缺失 → _default 兜底）"""
    matrix = matrix if matrix is not None else _load_stop_matrix()
    row = matrix.get(otype) or matrix.get("_default") or {}
    return {
        "otype": otype or "unknown",
        "stop_loss_pct": row.get("stop_loss_pct"),
        "time_stop_weeks": row.get("time_stop_weeks"),
        "max_drawdown_pct": row.get("max_drawdown_pct"),
        "trailing_ma": row.get("trailing_ma"),
        "note": (row.get("stop_loss_pct_note") or "")[:100],
        "source": "stop_matrix_v2.json（分包2 交付）",
    }


def _ensure_legacy_fields(d: dict):
    """兼容旧持仓：补 otype/stop_plan 缺失字段（旧持仓无类型 → _default 止损计划）"""
    matrix = _load_stop_matrix()
    for p in d.get("positions", []):
        if "otype" not in p:
            p["otype"] = _otype_of(p.get("code"))
        if "stop_plan" not in p:
            p["stop_plan"] = stop_plan_for(p.get("otype"), matrix)
    return d


def sync_from_decisions() -> dict:
    """读 deck_decisions（最新时间戳文件）：action=buy 且不在持仓 → 入持仓（holding）
    返回本次同步摘要 {added, over_limit, skipped}"""
    # ★2026-08-11 写保护免疫：deck_decisions 已是时间戳文件名，固定名可能不存在 → glob 取最新
    _dec = _latest_glob("deck_decisions_*.json", DECISIONS_JSON)
    if not _dec.exists():
        return {"added": [], "over_limit": [], "skipped": []}
    decisions = json.loads(_dec.read_text(encoding="utf-8"))
    if not isinstance(decisions, list):
        decisions = []
    d = _load()
    d = _ensure_legacy_fields(d)          # ★B-11：旧持仓补 otype/stop_plan
    # ★2026-08-11 修复：existing 含 over_limit——否则同一条 buy 记录每次 sync 都重复 append（实测 002414 累积 5 条）
    existing = {p["code"] for p in d["positions"] if p["status"] in ("holding", "over_limit")}
    risk = _risk_map()
    matrix = _load_stop_matrix()
    added, over_limit, skipped = [], [], []
    for rec in decisions:
        if rec.get("action") != "buy":
            continue
        code = rec.get("code")
        if not code or code in existing:
            continue
        # ★2026-08-12 十轮#172 防御：entry_date 早于最新交易日 7 天视为历史已完成（清零/复盘后不复位持仓）
        try:
            _ed = rec.get("date") or ""
            if _ed and (_mx := _load_latest_trade_date()) and _ed < (_mx - timedelta(days=7)):
                skipped.append(code)
                continue
        except Exception:
            pass
        # 去重：同一 code 已 exit 过 → 允许再买（入 history 检查）
        otype = _otype_of(code)           # ★B-11：机会类型（从最新池查）
        pos = {
            "code": code,
            "name": rec.get("name") or _name_of(code) or code,
            "entry_date": rec.get("date") or datetime.now().strftime("%Y-%m-%d"),
            "entry_price": _entry_price_of(code, rec.get("date") or ""),   # ★2026-08-11 百轮#38：审批日收盘价自动补（原 None 导致收益算不出）
            "target": rec.get("target"),
            "stop": rec.get("stop"),
            "otype": otype,               # ★B-11：AI-2 自动止损引擎按此查止损计划
            "stop_plan": stop_plan_for(otype, matrix),   # ★B-11：止损矩阵 v2 映射
            "status": "holding",
            "source": "deck",
            "decide_ts": rec.get("ts"),
            "risk_level": risk.get(code, "NO_DATA"),
            "note": rec.get("note") or "",
            "exit": None,
        }
        if len([p for p in d["positions"] if p["status"] == "holding"]) >= MAX_POSITIONS:
            pos["status"] = "over_limit"
            over_limit.append(code)
        else:
            added.append(code)
        d["positions"].append(pos)
        existing.add(code)
    _save(d)
    return {"added": added, "over_limit": over_limit, "skipped": skipped}


def sell(code: str, price: float = None, reason: str = "manual") -> dict:
    """卖出：holding → exit，写入 history"""
    d = _load()
    target = None
    for p in d["positions"]:
        if p["code"] == code and p["status"] in ("holding", "over_limit"):
            p["status"] = "exit"
            p["exit"] = {"date": datetime.now().strftime("%Y-%m-%d"),
                         "price": price, "reason": reason}
            target = p
            break
    if target is None:
        return {"error": f"{code} 不在持仓中"}
    d["history"].append({
        "code": code, "name": target.get("name", code),
        "entry_date": target.get("entry_date"), "exit_date": target["exit"]["date"],
        "entry_price": target.get("entry_price"), "exit_price": price,
        "reason": reason, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save(d)
    return {"ok": True, "code": code, "status": "exit"}


def positions() -> list:
    d = _load()
    return [p for p in d["positions"] if p["status"] in ("holding", "over_limit")]


def status() -> str:
    d = _load()
    lines = [f"持仓 {len([p for p in d['positions'] if p['status']=='holding'])}/{MAX_POSITIONS} 只"]
    for p in d["positions"]:
        if p["status"] in ("holding", "over_limit"):
            lines.append(f"  [{p['status']}] {p['code']} {p['name']} 入{p['entry_date']} "
                         f"目标{p.get('target')} 止损{p.get('stop')} 风控{p.get('risk_level')}")
    if d["history"]:
        lines.append(f"历史交易 {len(d['history'])} 笔（最近: {d['history'][-1]['code']} {d['history'][-1]['reason']}）")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="持仓管理（T-2）")
    ap.add_argument("--sync", action="store_true", help="从 deck_decisions 同步买入")
    ap.add_argument("--sell", nargs="+", metavar=("CODE", "PRICE", "REASON"), help="卖出")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.sync:
        r = sync_from_decisions()
        print(f"同步完成: 入持仓 {len(r['added'])} 只 {r['added']} | 超限拒绝 {r['over_limit']}")
    elif args.sell:
        code = args.sell[0]
        price = float(args.sell[1]) if len(args.sell) > 1 and args.sell[1] not in ("-", "None") else None
        reason = args.sell[2] if len(args.sell) > 2 else "manual"
        print(json.dumps(sell(code, price, reason), ensure_ascii=False))
    else:
        print(status())
