"""纸上短线轮换引擎（设计见 docs/plans/2026-10-16-paper-rotation-design.md）

职责：读短线分数 → 算买卖清单 → 写 pending → 次日开盘价撮合（无前视偏差）。
记账一律走 PaperAccount（account 由配置决定，默认 'rotation'，绝不碰 default）。
"""
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))   # 支持 `python strategy/paper_rotation.py` 独立运行

DEFAULTS = {
    "enabled": False, "account_name": "rotation", "initial_cash": 1000000,
    "max_positions": 6, "max_weight_per_stock": 0.25, "top_n_buy": 3,
    "exit_rank_threshold": 10, "min_hold_days": 3, "max_hold_days": 20,
    "execution": "next_open", "tech_source": "logs/tech_pitch_*.json",
}


def load_rotation_cfg(path=None) -> dict:
    """读 config/params.yaml 的 paper_rotation 段；文件缺失/字段缺失 → 用 DEFAULTS 兜底"""
    cfg = dict(DEFAULTS)
    p = Path(path) if path else BASE / "config" / "params.yaml"
    try:
        import yaml
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        seg = raw.get("paper_rotation") or {}
        cfg.update({k: v for k, v in seg.items() if v is not None})
    except Exception as e:
        print(f"[paper_rotation] 读配置失败，用默认：{str(e)[:60]}")
    return cfg


def _latest_glob(pattern: str):
    """按 mtime 取最新的匹配文件（项目铁律：Deck glob 必须按 mtime）"""
    fs = sorted(glob.glob(str(BASE / pattern)), key=os.path.getmtime)
    return Path(fs[-1]) if fs else None


def load_shortline_scores(pattern: str = "logs/tech_pitch_*.json") -> dict:
    """短线分数 {code: {score, name, horizon_days}}；无产物 → {}（不抛异常）"""
    f = _latest_glob(pattern)
    if f is None:
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for e in (d.get("entries") or []):
        code = e.get("code")
        if not code:
            continue
        out[code] = {"score": float(e.get("score") or 0.0),
                     "name": e.get("name") or code,
                     "horizon_days": 20}
    return out


PAPER_DB = BASE / "data" / "cache" / "paper.db"

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS rotation_pending (
  signal_date TEXT NOT NULL, exec_date TEXT NOT NULL,
  code TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  reason TEXT, created_at TEXT,
  PRIMARY KEY (signal_date, code, side)
);
"""


def ensure_pending_table(db_path=None) -> None:
    """建 pending 表（幂等；与 PaperAccount 同库不同表，不改其 schema）"""
    p = Path(db_path or PAPER_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        con.execute(_PENDING_DDL)
        con.commit()
    finally:
        con.close()


def write_pending(db_path, signal_date: str, exec_date: str, orders: list) -> int:
    """写入待执行订单（T-1 出单、T 日开盘撑合）；同主键覆盖，幂等重跑安全"""
    import datetime as _dt
    con = sqlite3.connect(str(db_path))
    try:
        con.executemany(
            "INSERT OR REPLACE INTO rotation_pending "
            "(signal_date, exec_date, code, side, qty, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(signal_date, exec_date, o["code"], o["side"], int(o["qty"]),
              o.get("reason", ""),
              _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")) for o in orders])
        con.commit()
    finally:
        con.close()
    return len(orders)


def load_pending(db_path, exec_date: str) -> list:
    """读 exec_date **<= 指定日** 的待执行订单（含停牌/无行情顺延的旧单；先卖后买）

    语义：exec_date = 订单的「意图成交日」。晨更链 09:10 时当日行情尚不可得
    （源库次日落库）→ 执行时用**订单自身 exec_date 的开盘价**撮合。
    表/库不存在 → 返回 []（首次运行/dry-run 安全，不抛异常）"""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            "SELECT exec_date, code, side, qty, reason FROM rotation_pending "
            "WHERE exec_date<=? ORDER BY exec_date, side DESC, code", (exec_date,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []          # rotation_pending 尚未创建（建表在非 dry-run 路径）
    finally:
        con.close()


def delete_pending(db_path, exec_date: str, code: str, side: str) -> None:
    """删除单笔 pending（一字板作废用）——★2026-11-10：原只打印"作废"不删行，
    load_pending(exec_date<=) 每天重新取到 → 僵尸单无限重试刷日志。"""
    if not Path(db_path).exists():
        return
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("DELETE FROM rotation_pending WHERE exec_date=? AND code=? AND side=?",
                    (exec_date, code, side))
        con.commit()
    finally:
        con.close()


def is_limit_locked(bar: dict) -> bool:
    """一字板（开盘即封死）：open==high==low 且偏离前收 ≥9.8%（含 10%/20% 板块近似）
    任一字段缺失 → 视为非一字板（保守，不拦单）"""
    o, h, low, pc = (float(bar.get(k) or 0)
                     for k in ("open", "high", "low", "preclose"))
    if not (o and h and low and pc):
        return False
    return abs(o - h) < 1e-9 and abs(o - low) < 1e-9 and abs(o / pc - 1) >= 0.098


def compute_sells(holds: list, scores: dict, cfg: dict, ranked_codes: list) -> dict:
    """4 级优先级卖出：stop > time_stop > rank_decay
    （第 4 级「替换式轮动」满仓时由调用方在 compute_buys 前补齐）

    ranked_codes：短线分数降序的 code 列表（前 exit_rank_threshold 名视为仍在榜）
    返回 {"sells": [{"code","reason","qty"}], "kept": [...]}
    """
    top_set = set(ranked_codes[:int(cfg["exit_rank_threshold"])])
    min_hold = int(cfg["min_hold_days"])
    max_hold = int(cfg["max_hold_days"])
    sells, kept = [], []
    for h in holds:
        code = h["code"]
        days = int(h.get("hold_days") or 0)
        plan = h.get("stop_plan") or {}
        last, cost = h.get("last_price"), h.get("entry_price")
        ret = None
        if last is not None and cost:
            ret = float(last) / float(cost) - 1
        reason = None
        # 1) 止损（不受最小持有期约束 —— 风险优先）
        sl = plan.get("stop_loss_pct")
        if sl and ret is not None and ret <= -abs(float(sl)):
            reason = "stop"
        # 2) 时间止损
        elif days > max_hold:
            reason = "time_stop"
        # 3/4) 分数衰减（须满足最小持有期）
        elif days >= min_hold and code not in top_set:
            reason = "rank_decay"
        if reason:
            sells.append({"code": code, "reason": reason, "qty": h.get("qty", 0)})
        else:
            kept.append(h)
    return {"sells": sells, "kept": kept}


def compute_buys(ranked_codes: list, scores: dict, holds: list, cfg: dict,
                 cash: float, equity: float) -> dict:
    """候选 = 排名前 top_n_buy，剔除已持仓；数量 = min(候选, 剩余仓位)
    单票金额 = min(可用现金÷待买只数, 总资产×max_weight_per_stock) —— 天然满足
    「6 只时每只≈16.7%、单票不超 25%」"""
    top_n = int(cfg["top_n_buy"])
    cap = float(cfg["max_weight_per_stock"])
    free_slots = max(0, int(cfg["max_positions"]) - len(holds))
    held = {h["code"] for h in holds}
    cand = [c for c in ranked_codes[:top_n * 3] if c not in held][:top_n][:free_slots]
    if not cand or cash <= 0 or equity <= 0:
        return {"buys": []}
    per = min(cash / len(cand), equity * cap)
    buys, remaining = [], float(cash)
    for c in cand:
        amt = min(per, remaining)
        if amt <= 0:
            break
        buys.append({"code": c, "amount": round(amt, 2), "reason": "new_entry"})
        remaining -= amt
    return {"buys": buys}


BARS_DB = BASE / "data" / "cache" / "bars.db"


def _latest_bars_date() -> str:
    """bars.db 最新股票交易日（晨更链 09:10 时通常为前一交易日）；无数据 → "" """
    if not BARS_DB.exists():
        return ""
    con = sqlite3.connect(f"file:{BARS_DB.as_posix()}?mode=ro&immutable=1",
                          uri=True, timeout=5)
    try:
        r = con.execute(
            "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' "
            "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'").fetchone()
        return str(r[0]) if r and r[0] else ""
    finally:
        con.close()


def _ranked_codes(scores: dict) -> list:
    """短线分数降序的 code 列表"""
    return [c for c, _ in sorted(scores.items(),
                                 key=lambda kv: -float(kv[1].get("score") or 0.0))]


def _bars_for(codes: list, date: str) -> dict:
    """某日行情 {code: {open,high,low,close,preclose}}（qfq；无行情的 code 不在字典中）
    用临时表传 code 列表（静态 SQL，无拼接注入面）"""
    if not codes:
        return {}
    con = sqlite3.connect(f"file:{BARS_DB.as_posix()}?mode=ro&immutable=1",
                          uri=True, timeout=5)
    try:
        con.execute("DROP TABLE IF EXISTS temp.q_codes")
        con.execute("CREATE TEMP TABLE q_codes (code TEXT PRIMARY KEY)")
        con.executemany("INSERT OR IGNORE INTO q_codes (code) VALUES (?)",
                        [(c,) for c in codes])
        cur = con.execute(
            "SELECT code, open, high, low, close, preclose FROM daily_bar "
            "WHERE date=? AND adjust='qfq' AND code IN (SELECT code FROM q_codes)",
            (date,))
        cols = [c[0] for c in cur.description]
        return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}
    finally:
        con.close()


def _trade_days_held(entry_date: str, today: str) -> int:
    """持有交易日数（bars.db 的 distinct date 计数，含首日与当日）"""
    if not entry_date:
        return 0
    con = sqlite3.connect(f"file:{BARS_DB.as_posix()}?mode=ro&immutable=1",
                          uri=True, timeout=5)
    try:
        n = con.execute(
            "SELECT COUNT(DISTINCT date) FROM daily_bar WHERE adjust='qfq' "
            "AND date>=? AND date<=?", (entry_date, today)).fetchone()[0]
        return int(n or 0)
    finally:
        con.close()


def _pending_codes(upto: str) -> set:
    """未成交 pending 涉及的 code（供取行情用）"""
    try:
        return {o["code"] for o in load_pending(PAPER_DB, upto)}
    except Exception:
        return set()


def _to_hold_rows(acc, today: str, bars: dict) -> list:
    """PaperAccount 持仓 → compute_sells 行（附 hold_days / last_price）"""
    rows = []
    for p in (acc.positions() or []):
        code = p.get("code")
        if not code:
            continue
        bar = bars.get(code) or {}
        rows.append({"code": code, "qty": p.get("qty"),
                     "entry_date": p.get("entry_date"),
                     "entry_price": p.get("avg_cost"),
                     "last_price": bar.get("close"),
                     "hold_days": _trade_days_held(p.get("entry_date") or "", today),
                     "stop_plan": p.get("stop_plan") or {"stop_loss_pct": 0.07}})
    return rows


def _execute_pending(acc, dry_run: bool) -> list:
    """执行「意图成交日 <= bars.db 最新日」的 pending —— 用**订单自身 exec_date 的开盘价**
    撮合（晨更链 09:10 拿不到当日行情，故按订单日取价；信号更早，天然无前视）。
    一字板 → 作废；该日无行情（停牌）→ 顺延（仍在队列，下次重试）。
    """
    latest = _latest_bars_date()
    if not latest:
        return []
    done = []
    for o in load_pending(PAPER_DB, latest):
        bar = (_bars_for([o["code"]], o["exec_date"]) or {}).get(o["code"]) or {}
        if is_limit_locked(bar):
            print(f"[paper_rotation] {o['code']} {o['side']} {o['exec_date']} 一字板 → 作废")
            if not dry_run:
                delete_pending(PAPER_DB, o["exec_date"], o["code"], o["side"])
            continue
        px = bar.get("open")
        if not px:
            print(f"[paper_rotation] {o['code']} {o['exec_date']} 无行情 → 顺延")
            continue
        if dry_run:
            done.append({**o, "px": px})
            continue
        if o["side"] == "buy":
            r = acc.buy(o["code"], int(o["qty"]), o["exec_date"], float(px), o.get("reason", ""))
        else:
            r = acc.sell(o["code"], int(o["qty"]), o["exec_date"], float(px), o.get("reason", ""))
        if not (r or {}).get("ok"):
            print(f"[paper_rotation] {o['code']} {o['side']} 撑合失败: {(r or {}).get('msg')}")
        done.append({**o, "px": px})
    return done


def run_once(dry_run: bool = False, today: "str | None" = None) -> dict:
    """每日一次：① 执行 pending（今日开盘价）② 算卖 ③ 算买 ④ 写新 pending（次日开盘）"""
    import datetime as _dt
    cfg = load_rotation_cfg()
    if not cfg.get("enabled") and not dry_run:
        return {"ok": True, "skipped": "disabled"}
    today = today or _dt.date.today().strftime("%Y-%m-%d")
    scores = load_shortline_scores(cfg["tech_source"])
    if not scores:
        print("[paper_rotation] 无短线产物 → 跳过本轮")
        return {"ok": True, "skipped": "no_signal"}
    ranked = _ranked_codes(scores)
    top_n = int(cfg["top_n_buy"])
    min_hold = int(cfg["min_hold_days"])

    from strategy.paper_account import PaperAccount
    acc = PaperAccount(name=str(cfg["account_name"]), cash=float(cfg["initial_cash"]))
    if not dry_run:
        ensure_pending_table(PAPER_DB)

    # 成交/行情基准日 = bars.db 最新股票交易日（晨更链 09:10 时通常为前一交易日）
    bars_date = _latest_bars_date() or today
    # ① 执行 pending（用订单自身 exec_date 的开盘价；数据次日可得 → 天然无前视）
    executed = _execute_pending(acc, dry_run)
    codes = {p.get("code") for p in (acc.positions() or [])}
    codes |= _pending_codes(bars_date)
    codes |= set(ranked[:top_n * 3])
    bars = _bars_for(sorted(c for c in codes if c), bars_date)

    # ② 卖出（4 级优先级）
    holds = _to_hold_rows(acc, bars_date, bars)
    sell_plan = compute_sells(holds, scores, cfg, ranked)
    kept = list(sell_plan["kept"])
    sells = list(sell_plan["sells"])

    # ②b 替换式轮动（第 4 级）：有新高分标的要买但空位不足 → 卖 kept 中分数最低者
    held_now = {h["code"] for h in kept}
    # ★2026-11-11 修复：当日已卖出的代码不得同日再买回（原会生成 sell+buy 同价单，
    #   产生无意义双边成本，dry-run 实测 301699.SZ 同日卖出/买入同价）。
    sold_codes = {s["code"] for s in sells}
    want = [c for c in ranked[:top_n] if c not in held_now and c not in sold_codes]
    slots = max(0, int(cfg["max_positions"]) - len(kept))
    need = max(0, len(want) - slots)
    pool = [h for h in kept if int(h.get("hold_days") or 0) >= min_hold]
    pool.sort(key=lambda h: float(scores.get(h["code"], {}).get("score") or -1.0))
    for h in pool[:need]:
        sells.append({"code": h["code"], "reason": "rotation", "qty": h.get("qty", 0)})
        kept.remove(h)

    # ③ 买入（现金 = 当前现金 + 卖出所得估算）
    snap = acc.snapshot() or {}
    cash_now = float(snap.get("cash") or 0.0)
    for s in sells:
        bar = bars.get(s["code"]) or {}
        px = bar.get("close") or bar.get("open")
        if px:
            cash_now += float(px) * int(s.get("qty") or 0)
    equity_now = cash_now + sum(
        float((bars.get(h["code"]) or {}).get("close") or 0) * int(h.get("qty") or 0)
        for h in kept)
    buy_plan = compute_buys(ranked_codes=ranked, scores=scores, holds=kept, cfg=cfg,
                            cash=cash_now, equity=equity_now)
    buy_plan["buys"] = [b for b in buy_plan.get("buys") or [] if b["code"] not in sold_codes]

    # ④ 写新 pending（次日开盘成交）；qty 由金额/信号日收盘价换算（100 股一手）
    orders = []
    for s in sells:
        orders.append({"code": s["code"], "side": "sell",
                       "qty": int(s.get("qty") or 0), "reason": s["reason"]})
    for b in buy_plan["buys"]:
        px = float((bars.get(b["code"]) or {}).get("close") or 0)
        qty = int(b["amount"] / px / 100) * 100 if px > 0 else 0
        if qty > 0:
            orders.append({"code": b["code"], "side": "buy",
                           "qty": qty, "reason": b["reason"]})
    if not dry_run and orders:
        # exec_date = 出单当日（= 意图成交日）：次日数据落库后按该日开盘价撑合
        write_pending(PAPER_DB, today, today, orders)

    return {"ok": True, "dry_run": dry_run, "date": today,
            "pending_executed": executed, "sells": sells,
            "buys": buy_plan["buys"], "orders_written": (0 if dry_run else len(orders))}


def main() -> int:
    import sys as _s
    r = run_once(dry_run="--dry-run" in _s.argv)
    print(f"[paper_rotation] {r}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
