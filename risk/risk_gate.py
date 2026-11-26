# -*- coding: utf-8 -*-
"""risk/risk_gate.py — 统一风控门禁（2026-11-26 风控接线）

把此前"实例化但从未调用"的 RiskAgent / StopRules 真正接入决策链，
并把 L0 择时硬门控从 deck_server 提取为共享函数，让手动/自动两条买入路径
都经过同一套门禁。

依赖单向（杜绝循环 import）：
    portfolio / auto_execute / deck_server  →  risk_gate
    risk_gate  →  risk_agent / stop_rules / position_monitor._industry_map
持仓权重、现价、回撤等数据一律由调用方传入，gate 不反向 import portfolio。

三层门禁：
    l0_gate()          L0 择时硬门控（防御期禁高风险类型）——全 fail-closed
    pretrade_check()   下单前 RiskAgent 一票否决 + 熔断状态持久化
    sell_check()       卖出侧 StopRules 止损止盈铁律

审计：logs/risk_audit.log（追加）；熔断状态：logs/risk_circuit_state.json。
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
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent.parent
AUDIT_LOG = BASE / "logs" / "risk_audit.log"
STATE_LOCK = threading.Lock()


def _load_cfg() -> dict:
    try:
        import yaml
        p = BASE / "config" / "params.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/risk_gate.py:38 _load_cfg")
    return {}


def audit(event: str, detail: str, **kw) -> None:
    """风控决策审计（追加写，失败不阻塞风控）。"""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        extra = " ".join(f"{k}={v}" for k, v in kw.items() if v is not None)
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {event} | {detail}"
        if extra:
            line += f" | {extra}"
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/risk_gate.py:53 audit")


# ------------------------------------------------------------------
# 熔断状态持久化（跨进程：deck_server 常驻 vs auto_execute 独立脚本）
# ------------------------------------------------------------------
def load_circuit_state(path: Path) -> dict:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_circuit_state(state: dict, path: Path) -> None:
    """原子写（tmp.<pid> + os.replace），进程内加锁。跨进程靠时间戳重算冷却期。"""
    with STATE_LOCK:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(p))
        except Exception as e:
            audit("CIRCUIT_STATE_SAVE_FAIL", str(e)[:120])


# ------------------------------------------------------------------
# L0 择时硬门控（全 fail-closed）
# ------------------------------------------------------------------
def l0_gate(otype, code, *, base=None, cfg=None,
            high_risk=None, defensive_score=None, fail_closed=None):
    """防御期禁止高风险类型买入。

    返回 (ok: bool, reason: str)。失败一律审计。
    ★全 fail-closed：择时文件缺失/坏 JSON/score 非数值 → 拒绝高风险类型。
    """
    base = Path(base) if base else BASE
    cfg = cfg if cfg is not None else _load_cfg()
    risk = cfg.get("risk", {}) if isinstance(cfg, dict) else {}

    high_risk = tuple(high_risk or risk.get("l0_high_risk_otypes",
                                            ("revalue", "tech_sentiment")))
    defensive_score = defensive_score if defensive_score is not None else float(
        risk.get("l0_defensive_score", 40))
    fail_closed = fail_closed if fail_closed is not None else bool(
        risk.get("l0_fail_closed", True))

    otype = str(otype or "")
    if otype not in high_risk:
        return True, "not_high_risk"

    score = None
    err = None
    try:
        import glob as _g
        fs = sorted(_g.glob(str(base / "output" / "timing_system_*.json")),
                    key=lambda p: os.path.getmtime(p))
        if not fs:
            err = "timing_file_missing"
        else:
            d = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
            v = d.get("score")
            if v is None:
                err = "score_missing"
            else:
                score = float(v)
    except Exception as e:
        err = f"read_error:{str(e)[:80]}"

    if score is None:
        if fail_closed:
            detail = err or "score_unavailable"
            audit("L0_FAIL_CLOSED", detail, code=code, otype=otype)
            return False, f"L0_FAIL_CLOSED:{detail}——无法确认非防御期，拒绝 {otype} 买入"
        audit("L0_UNAVAILABLE_ALLOW", err or "score_unavailable", code=code, otype=otype)
        return True, "timing_unavailable_allowed"

    if score < defensive_score:
        detail = f"score={score:.0f} < {defensive_score:.0f}"
        audit("L0_REJECT", detail, code=code, otype=otype)
        return False, f"L0 防御期（{detail}）禁止 {otype} 买入——实盘归因下跌日放大亏损，硬规则不放开"

    audit("L0_PASS", f"score={score:.0f} >= {defensive_score:.0f}", code=code, otype=otype)
    return True, f"score={score:.0f} pass"


# ------------------------------------------------------------------
# 下单前 RiskAgent 一票否决
# ------------------------------------------------------------------
def pretrade_check(code, size, weights, current_drawdown, *, sector="",
                   sector_map=None, dd_available=True, cfg=None, state_path=None):
    """下单前风控检查 → RiskCheckResult（REJECT 真拦截 / REDUCE 按 adjusted_size 生效）。

    - weights: {code: weight} 当前持仓权重（由调用方算好传入）
    - current_drawdown: 当前组合回撤（正小数，如 0.12 表示回撤 12%）
    - dd_available: 回撤数据是否可信；False 时 size 折半并审计（唯一不 fail-closed 点）
    - state_path: 熔断状态文件路径；None 用 config risk.circuit_state_file
    """
    from risk.risk_agent import RiskAgent

    cfg = cfg if cfg is not None else _load_cfg()
    risk = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
    state_path = Path(state_path) if state_path else BASE / risk.get(
        "circuit_state_file", "logs/risk_circuit_state.json")

    # 每次先 load → 跨进程恢复熔断状态
    state = load_circuit_state(state_path)
    before = dict(state)
    ra = RiskAgent(risk, state=state)

    size = float(size or 0)
    if not dd_available:
        size = size / 2.0
        audit("DRAWDOWN_UNAVAILABLE", f"size halved to {size:.4f}", code=code)

    try:
        result = ra.check_order(
            symbol=str(code), size=size,
            current_portfolio=dict(weights or {}),
            current_drawdown=float(current_drawdown or 0),
            sector=sector or "", sector_map=sector_map,
        )
        audit("PRETRADE", f"{result.decision.value} | {result.reason}",
              code=code, size=f"{size:.4f}", dd=f"{float(current_drawdown or 0):.4f}")
    except Exception as e:
        # 风控自身异常 → 安全模式（禁新开仓），绝不 fail-open
        from risk.risk_agent import Decision, RiskCheckResult
        audit("PRETRADE_ERROR", str(e)[:160], code=code)
        result = RiskCheckResult(
            decision=Decision.REJECT,
            reason=f"风控检查异常（安全模式禁新开仓）: {str(e)[:100]}",
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    # 熔断状态变更（触发/冷静期恢复）→ 持久化
    if state != before:
        save_circuit_state(state, state_path)
    return result


# ------------------------------------------------------------------
# 卖出侧 StopRules 止损止盈铁律
# ------------------------------------------------------------------
def sell_check(position, bars=None, *, cfg=None):
    """检查单个持仓是否触发止损止盈铁律 → StopDecision。

    position 至少含 entry_price/current_price；其余参数可由 bars 补齐或由调用方预置：
      pivot_price / atr / ma_50 / peak_price / hold_weeks。
    缺失项传 None 让 StopRules 跳过对应规则，硬止损始终有效。
    """
    from risk.stop_rules import StopDecision, StopRules

    cfg = cfg if cfg is not None else _load_cfg()
    risk = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
    sr = StopRules(risk)
    d = dict(position or {})

    # bars 补齐技术指标（entry 之后的高点 / 50 均线 / 14 日 ATR）
    if bars is not None and len(bars):
        try:
            close = bars["close"] if "close" in getattr(bars, "columns", []) else None
            high = bars["high"] if "high" in getattr(bars, "columns", []) else None
            low = bars["low"] if "low" in getattr(bars, "columns", []) else None
            if close is not None and len(close):
                if not d.get("current_price"):
                    d["current_price"] = float(close.iloc[-1])
                if not d.get("ma_50"):
                    d["ma_50"] = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
                if not d.get("peak_price"):
                    ed = str(d.get("entry_date") or "")[:10]
                    seg = close[close.index.astype(str) >= ed] if ed else close
                    d["peak_price"] = float(seg.max()) if len(seg) else None
                if high is not None and low is not None and not d.get("atr"):
                    tr = (high - low).to_frame("a")
                    tr["b"] = (high - close.shift()).abs()
                    tr["c"] = (low - close.shift()).abs()
                    atr = tr.max(axis=1).rolling(14).mean().iloc[-1]
                    d["atr"] = float(atr) if atr == atr else None
        except Exception as e:
            audit("SELL_BARS_ENRICH_FAIL", str(e)[:120], code=d.get("code"))

    entry = float(d.get("entry_price") or 0)
    cur = float(d.get("current_price") or 0)
    if entry <= 0 or cur <= 0:
        return StopDecision(should_sell=False)

    hold_weeks = float(d.get("hold_weeks") or 0)
    if not d.get("hold_weeks"):
        ed = str(d.get("entry_date") or "")[:10]
        if ed:
            try:
                hold_weeks = (datetime.now() - datetime.strptime(ed, "%Y-%m-%d")).days / 7.0
            except (ValueError, TypeError):
                pass

    pivot = d.get("pivot_price")
    if pivot is None:
        # 台账 stop 字段可能是突破点价；stop_plan 不存价格，只存百分比
        raw = d.get("stop")
        try:
            pivot = float(raw) if raw not in (None, "") else None
        except (ValueError, TypeError):
            pivot = None

    decision = sr.check(
        entry_price=entry, current_price=cur,
        pivot_price=pivot, atr=d.get("atr"),
        ma_50=d.get("ma_50"), peak_price=d.get("peak_price"),
        hold_weeks=hold_weeks,
    )
    if decision.should_sell:
        tags = ",".join(t.value for t, _ in decision.reasons)
        audit("SELL_RULE", f"{decision.action} | {tags}", code=d.get("code"),
              pnl=f"{cur / entry - 1:.4f}")
    return decision
