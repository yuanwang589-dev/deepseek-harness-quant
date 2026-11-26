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
            "SELECT close FROM daily_bar WHERE code=? AND date=? AND adjust='qfq' LIMIT 1",
            (code, date)).fetchone()
        if not row:
            row = con.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date=? AND adjust IN ('none','raw') LIMIT 1",
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
        import glob as _glob
        import sqlite3
        from pathlib import Path as _P
        paths = ["data/cache/bars.db"]
        try:
            from data.cache import CACHE_DIR
            paths += [str(p) for p in sorted(CACHE_DIR.glob("bars_incr_*.db"))[-3:]]
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/portfolio.py:88 _latest_close_of")
        best = None
        for _p in paths:
            try:
                # ★2026-11-20 修复：相对路径 as_uri() 恒抛 ValueError（被 except 吞掉）
                #   → 现价恒 None、持仓收益全空。resolve() 转绝对路径（同 portfolio_perf #2026-08-25 修法）
                _uri = _P(_p).resolve().as_uri() + "?mode=ro&immutable=1"
                con = sqlite3.connect(_uri, uri=True, timeout=3)
                row = con.execute(
                    "SELECT close, date FROM daily_bar WHERE code=? AND adjust='qfq' "
                    "ORDER BY date DESC LIMIT 1",
                    (code,)).fetchone()
                if not row:
                    row = con.execute(
                        "SELECT close, date FROM daily_bar WHERE code=? AND adjust IN ('none','raw') "
                        "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
                con.close()
                if row and row[0] and (best is None or row[1] > best[1]):
                    best = (float(row[0]), row[1])
            except Exception:
                continue
        return best[0] if best else None
    except Exception:
        return None


def _drawdown_proxy(positions: list) -> tuple:
    """★2026-11-26 风控接线：当前组合回撤代理（项目无 L2 台账净值曲线）。

    用持仓 entry_price + 最新收盘价算等权浮亏：max(0, -mean(close/entry-1))。
    返回 (dd: float, available: bool)；无有效配对时 (0.0, False)，
    由调用方把 dd_available=False 传给 risk_gate（size 折半 + 审计）。
    """
    vals = []
    for p in positions or []:
        if p.get("status") != "holding":
            continue
        entry = p.get("entry_price")
        if not entry:
            continue
        cur = _latest_close_of(p.get("code"))
        if not cur:
            continue
        try:
            vals.append(float(cur) / float(entry) - 1)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    if not vals:
        return 0.0, False
    return max(0.0, -sum(vals) / len(vals)), True


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
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/portfolio.py:169 _load")
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
    """按 otype 从止损矩阵 v2 生成 stop_plan（无 otype/矩阵缺失 → _default 兜底）
    ★2026-11-20 矩阵字段为空时用 config/params.yaml(risk) 默认值补齐（不再返回全 None 方案）"""
    matrix = matrix if matrix is not None else _load_stop_matrix()
    row = matrix.get(otype) or matrix.get("_default") or {}
    plan = {
        "otype": otype or "unknown",
        "stop_loss_pct": row.get("stop_loss_pct"),
        "time_stop_weeks": row.get("time_stop_weeks"),
        "max_drawdown_pct": row.get("max_drawdown_pct"),
        "trailing_ma": row.get("trailing_ma"),
        "note": (row.get("stop_loss_pct_note") or "")[:100],
        "source": "stop_matrix_v2.json（分包2 交付）",
    }
    try:
        from risk.risk_config import fill_stop_plan
        fill_stop_plan(plan)
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "strategy/portfolio.py:257 stop_plan_for")
    return plan


def _ensure_legacy_fields(d: dict):
    """兼容旧持仓：补 otype/stop_plan 缺失字段（旧持仓无类型 → _default 止损计划）"""
    matrix = _load_stop_matrix()
    for p in d.get("positions", []):
        if "otype" not in p:
            p["otype"] = _otype_of(p.get("code"))
        if "stop_plan" not in p:
            p["stop_plan"] = stop_plan_for(p.get("otype"), matrix)
    return d


def sync_from_decisions(path=None) -> dict:
    """读 deck_decisions（最新时间戳文件）：action=buy 且不在持仓 → 入持仓（holding）
    path: 指定决策文件（auto_execute 写 auto_decisions_*.json 时用）
    ★2026-11-26 风控接线：每笔 buy 先过 l0_gate + pretrade_check，通过才建仓。
    返回摘要 {added, over_limit, skipped, rejected}"""
    # ★延迟 import：risk_gate → risk_agent/stop_rules，不反向 import portfolio，杜绝循环
    from risk import risk_gate as RG

    # ★2026-08-11 写保护免疫：deck_decisions 已是时间戳文件名，固定名可能不存在 → glob 取最新
    _dec = Path(path) if path else _latest_glob("deck_decisions_*.json", DECISIONS_JSON)
    if not _dec.exists():
        return {"added": [], "over_limit": [], "skipped": [], "rejected": []}
    decisions = json.loads(_dec.read_text(encoding="utf-8"))
    if not isinstance(decisions, list):
        decisions = []
    d = _load()
    d = _ensure_legacy_fields(d)          # ★B-11：旧持仓补 otype/stop_plan
    # ★2026-11-20 over_limit 只是「最近一次决策的名额快照」，不再跨日累积
    #   （用户反馈：历史超限越堆越多，持仓页顶部的超限清单一长串）。
    #   每次 sync 先清空，随后按本次 decisions 重建；entry_price=None 的记录本就从未买入。
    if any(p.get("status") == "over_limit" for p in d["positions"]):
        d["positions"] = [p for p in d["positions"] if p.get("status") != "over_limit"]
    # ★2026-08-11 修复：existing 含 over_limit——否则同一条 buy 记录每次 sync 都重复 append（实测 002414 累积 5 条）
    # ★2026-11-20：over_limit 记录允许「升格」——名额空出后自动买入，否则被拒过的候选永久占位、自动买入永远买不进
    existing = {p["code"] for p in d["positions"] if p["status"] in ("holding", "over_limit")}
    _ol_map = {p["code"]: p for p in d["positions"] if p["status"] == "over_limit"}
    risk = _risk_map()
    matrix = _load_stop_matrix()
    added, over_limit, skipped, rejected = [], [], [], []

    # ★2026-11-26 风控上下文（一次算好，避免逐笔重复）：
    #   weights 用等权资金份额（1/MAX_POSITIONS），与 size 口径一致，避免归一化到 1 后总仓位恒超限
    _holding = [p for p in d["positions"] if p["status"] == "holding"]
    _unit = 1.0 / max(MAX_POSITIONS, 1)
    weights = {p["code"]: _unit for p in _holding}
    dd, dd_available = _drawdown_proxy(_holding)
    # 行业映射（复用 S6 的 _industry_map，从机会池/科技池反查）
    try:
        from risk.position_monitor import _industry_map
        sector_map = _industry_map(list(weights) +
                                   [r.get("code") for r in decisions if r.get("code")])
    except Exception:
        sector_map = {}

    for rec in decisions:
        if rec.get("action") != "buy":
            continue
        code = rec.get("code")
        _op = _ol_map.get(code) if code in existing else None
        if not code or (code in existing and _op is None):
            continue
        # ★2026-08-12 十轮#172 防御：entry_date 早于最新交易日 7 天视为历史已完成（清零/复盘后不复位持仓）
        try:
            _ed = rec.get("date") or ""
            if _ed and (_mx := _load_latest_trade_date()):
                _ed_d = datetime.strptime(str(_ed)[:10], "%Y-%m-%d")
                _mx_d = datetime.strptime(str(_mx)[:10], "%Y-%m-%d")
                if _ed_d < (_mx_d - timedelta(days=7)):
                    skipped.append(code)
                    continue
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "strategy/portfolio.py:333 sync_from_decisions")
        # 去重：同一 code 已 exit 过 → 允许再买（入 history 检查）
        otype = (rec.get("pitch_meta") or {}).get("otype") or _otype_of(code)   # ★优先审批记录 pitch_meta

        # ★★2026-11-26 门禁 ①：L0 择时硬门控（全 fail-closed）
        _ok, _why = RG.l0_gate(otype, code)
        if not _ok:
            rejected.append({"code": code, "reason": _why, "layer": "L0"})
            RG.audit("SYNC_L0_REJECT", _why, code=code, otype=otype)
            continue
        # ★★2026-11-26 门禁 ②：RiskAgent 一票否决（REJECT 拦截 / REDUCE 留痕）
        _chk = RG.pretrade_check(
            code, _unit, weights, dd,
            sector=sector_map.get(code, ""), sector_map=sector_map,
            dd_available=dd_available,
        )
        from risk.risk_agent import Decision as _D
        if _chk.decision == _D.REJECT:
            rejected.append({"code": code, "reason": _chk.reason, "layer": "PRETRADE"})
            continue
        _risk_cap = _chk.adjusted_size if _chk.decision == _D.REDUCE else _unit

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
            "source": rec.get("source") or "deck",
            "decide_ts": rec.get("ts"),
            "risk_level": risk.get(code, "NO_DATA"),
            "risk_size_cap": round(float(_risk_cap), 4),  # ★风控缩放留痕（台账无仓位字段，不静默缩数量）
            "risk_check": _chk.decision.value,
            "note": rec.get("note") or "",
            "exit": None,
        }
        if _op is not None:
            # 已有 over_limit 记录：有名额才升格，否则保持原状（不重复 append）
            if len([p for p in d["positions"] if p["status"] == "holding"]) >= MAX_POSITIONS:
                over_limit.append(code)
                continue
            _op.update(pos)
            _op["status"] = "holding"
            added.append(code)
            continue
        if len([p for p in d["positions"] if p["status"] == "holding"]) >= MAX_POSITIONS:
            pos["status"] = "over_limit"
            over_limit.append(code)
        else:
            added.append(code)
        d["positions"].append(pos)
        existing.add(code)
        weights[code] = _unit  # 本轮新增也计入后续门禁检查
    _save(d)
    return {"added": added, "over_limit": over_limit, "skipped": skipped,
            "rejected": rejected}


def sell(code: str, price: float = None, reason: str = "manual") -> dict:
    """卖出：holding → exit，写入 history"""
    d = _load()
    target = None
    for p in d["positions"]:
        if p["code"] == code and p["status"] in ("holding", "over_limit"):
            # ★2026-11-26 卖出侧 StopRules 留痕：手动卖出不阻断，但记录是否触发铁律
            try:
                from risk import risk_gate as RG
                _cur = float(price) if price else _latest_close_of(code)
                _sd = RG.sell_check({
                    "code": code, "entry_price": p.get("entry_price"),
                    "current_price": _cur, "entry_date": p.get("entry_date"),
                    "stop": p.get("stop"),
                })
                _tag = ",".join(t.value for t, _ in _sd.reasons) if _sd.reasons else "none"
                RG.audit("MANUAL_SELL_RULE" if _sd.should_sell else "MANUAL_SELL_NO_RULE",
                         f"{_sd.action} | {_tag} | {reason}", code=code)
                p["risk_sell_check"] = {"should_sell": _sd.should_sell,
                                        "action": _sd.action, "reasons": _tag}
            except Exception as _dsh_exc:
                _dsh_swallow(_dsh_exc, "strategy/portfolio.py:416 sell")
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
