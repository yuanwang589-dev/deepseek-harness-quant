# -*- coding: utf-8 -*-
"""strategy/auto_execute.py — L2 持仓台账自动执行器（2026-11-20）

需求：不必手动点「审批买入 / 卖出」，系统每日自动执行：
  买入：pitch_v2 总分前 N（config auto_execute.buy_n，默认 3），受 portfolio.MAX_POSITIONS 约束
  卖出：① 止损引擎 SELL 级信号  ② 止盈引擎 target/pullback/time 信号
时点：并入晨更链每日一次（信号次日处置，与 paper_rotation 同口径；价格取信号文件里的现价）
开关：config auto_execute.enabled=false → 零副作用直接跳过
产物：logs/auto_execute_<ts>.json（买卖清单 + 错误），持仓页后续可读

注意：只动 L2 台账（logs/portfolio_*.json），不碰 paper.db 的 rotation 账户。
用法：
  python strategy/auto_execute.py --dry-run   # 只打印计划
  python strategy/auto_execute.py             # 实际执行
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

LOGS = BASE / "logs"
SELL_LEVELS = {"SELL", "TRIGGERED", "EXIT"}
TP_TYPES = {"target", "pullback", "time"}


def _cfg() -> dict:
    try:
        import yaml
        d = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8")) or {}
        return d.get("auto_execute") or {}
    except Exception:
        return {}


def _latest(pat: str):
    fs = sorted(LOGS.glob(pat), key=lambda p: p.stat().st_mtime)
    return fs[-1] if fs else None


def _load(f):
    try:
        return json.loads(Path(f).read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect_sells(cfg: dict) -> list:
    """{code: (reason, price)}：止损 SELL 级 + 止盈 target/pullback/time"""
    sells = {}

    if cfg.get("sell_on_stop", True):
        f = _latest("stop_signals_*.json")
        for r in (_load(f).get("results") or []) if f else []:
            code = r.get("code")
            if not code:
                continue
            for s in r.get("signals") or []:
                if str(s.get("level", "")).upper() in SELL_LEVELS:
                    sells[code] = (f"auto_stop:{s.get('rule', '')}", r.get("cur_price"))
                    break

    if cfg.get("sell_on_take_profit", True):
        f = _latest("take_profit_signals_*.json")
        for p in (_load(f).get("positions") or []) if f else []:
            code = p.get("code")
            if not code:
                continue
            hit = next((s for s in (p.get("signals") or []) if s.get("type") in TP_TYPES), None)
            if hit:
                sells[code] = (f"auto_tp:{hit.get('type')}", p.get("close"))
    return [{"code": c, "reason": v[0], "price": v[1]} for c, v in sells.items()]


def collect_buys(cfg: dict, held: set, cooldown: set = None,
                 gate=None, rejected_out=None) -> list:
    """pitch_v2 按 score 降序取前 N（排除已持有 + 冷却期内刚卖出的 + L0 门控拒绝的）

    ★2026-11-26：gate(otype, code) → (ok, reason)；被拒候选写入 rejected_out（不落 auto_decisions）。
    """
    n = int(cfg.get("buy_n", 3) or 3)
    cd = cooldown or set()
    gate = gate or (lambda otype, code: (True, "gate_disabled"))
    f = _latest("pitch_v2_*.json")
    ps = sorted((_load(f).get("pitch") or []),
                key=lambda x: -(x.get("score") or 0))
    out = []
    for p in ps:
        code = p.get("code")
        if not code or code in held or code in cd:
            continue
        otype = p.get("otype")
        _ok, _why = gate(otype, code)
        if not _ok:
            if rejected_out is not None:
                rejected_out.append({"code": code, "otype": otype,
                                     "reason": _why, "layer": "L0"})
            continue
        out.append({
            "code": code,
            "name": p.get("name") or code,
            "date": time.strftime("%Y-%m-%d"),
            "action": "buy",
            "target": p.get("target"),
            "stop": p.get("stop"),
            "pitch_meta": {"otype": p.get("otype"), "score": p.get("score")},
            "source": "auto",
        })
        if len(out) >= n:
            break
    return out


def run(dry: bool = False) -> dict:
    cfg = _cfg()
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "enabled": bool(cfg.get("enabled")),
               "dry_run": dry, "sells": [], "buys": [], "errors": [],
               "l0_rejected": [], "rejected": []}
    if not cfg.get("enabled"):
        summary["skipped"] = "auto_execute.enabled=false"
        return summary

    from strategy import portfolio as PF

    _positions = PF._load().get("positions", [])
    # 候选去重只排除真实持仓：over_limit 允许重新进单，sync 有名额时升格（否则被拒过的候选永远买不进）
    held = {p.get("code") for p in _positions if p.get("status") == "holding"}
    live = held

    sells = [s for s in collect_sells(cfg) if s["code"] in live]   # 幂等：非 holding 一律跳过

    # ★★2026-11-26 卖出侧 StopRules 铁律（叠加而非替换：原 stop/take_profit 信号链保留）
    if cfg.get("sell_on_stop_rules", True):
        try:
            from risk import risk_gate as RG
            for code in live:
                pos = next((p for p in _positions if p.get("code") == code), None)
                if not pos or not pos.get("entry_price"):
                    continue
                cur = PF._latest_close_of(code)
                if not cur:
                    continue
                sd = RG.sell_check({
                    "code": code, "entry_price": pos.get("entry_price"),
                    "current_price": cur, "entry_date": pos.get("entry_date"),
                    "stop": pos.get("stop"),
                })
                if sd.should_sell:
                    tags = ",".join(t.value for t, _ in sd.reasons)
                    sells.append({"code": code, "reason": f"risk_stop:{tags}", "price": cur})
        except Exception as e:
            summary["errors"].append(f"stop_rules: {str(e)[:120]}")
    # 去重（同一 code 既命中信号又命中铁律 → 保留 risk_stop 版本）
    sells = list({s["code"]: s for s in sells}.values())
    summary["sells"] = sells
    if not dry:
        for s in sells:
            try:
                r = PF.sell(s["code"], s.get("price"), s["reason"])
                s["result"] = r
            except Exception as e:
                s["result"] = {"error": str(e)[:200]}
                summary["errors"].append(f"sell {s['code']}: {e}")

    # ★2026-11-20 冷却期：刚卖出的票不在同一轮买回（止损等于没做）——按 history.exit_date 自然日计
    _cd_days = int(cfg.get("rebuy_cooldown_days", 5) or 0)
    _cooldown = set()
    if _cd_days > 0:
        from datetime import datetime as _dt, timedelta as _td
        _cut = (_dt.now() - _td(days=_cd_days)).strftime("%Y-%m-%d")
        _today = _dt.now().strftime("%Y-%m-%d")
        for h in PF._load().get("history") or []:
            _ed = str(h.get("exit_date") or "")[:10]
            if _ed and _cut <= _ed <= _today and h.get("code"):
                _cooldown.add(h["code"])
    summary["cooldown"] = sorted(_cooldown)

    # ★★2026-11-26 买入侧 L0 门控（自动路径也过；被拒候选不写 auto_decisions）
    _gate = None
    if cfg.get("risk_gate_enabled", True):
        try:
            from risk import risk_gate as RG
            _gate = RG.l0_gate
        except Exception as e:
            summary["errors"].append(f"risk_gate 不可用，拒绝所有买入（安全模式）: {str(e)[:120]}")
    buys = collect_buys(cfg, held, _cooldown,
                        gate=_gate, rejected_out=summary["l0_rejected"])
    if _gate is None and cfg.get("risk_gate_enabled", True):
        buys = []   # 门禁不可用 → 安全模式，不放行任何买入
    summary["buys"] = buys
    if buys and not dry:
        try:
            _f = LOGS / f"auto_decisions_{time.strftime('%Y%m%d_%H%M%S')}.json"
            _f.write_text(json.dumps(buys, ensure_ascii=False, indent=1), encoding="utf-8")
            summary["decisions_file"] = _f.name
            summary["sync"] = PF.sync_from_decisions(str(_f))
            summary["rejected"] = (summary["sync"] or {}).get("rejected", [])
        except Exception as e:
            summary["errors"].append(f"buy sync: {e}")

    _out = LOGS / f"auto_execute_{time.strftime('%Y%m%d_%H%M%S')}.json"
    _out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="L2 台账自动买卖（晨更链每日一次）")
    ap.add_argument("--dry-run", action="store_true", help="只输出计划，不写持仓")
    a = ap.parse_args()
    r = run(dry=a.dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1))
