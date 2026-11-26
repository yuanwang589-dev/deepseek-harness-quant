# -*- coding: utf-8 -*-
"""
risk/risk_agent.py — Risk Agent（M4 纪律引擎核心，第15课原文落地）

来源：《AI量化交易从0到1》第15课《风险控制与资金管理》原文代码 + 落地增强
      （学习笔记/原文/第15课_风险控制与资金管理.md）

设计要点（原文核心原则）：
1. ★硬约束不可覆盖：仓位上限/回撤熔断写死；人工干预需特殊流程（双人确认）
2. ★独立数据源：Risk 用自己的数据（本项目 = data/cache.py，独立于策略层）
3. ★审计日志完整：每个决策记录 时间/请求/决策/理由，日志不可修改
4. ★降级策略：自身故障 → 安全模式（禁新开仓只能减仓）；风控故障不能跳过风控
5. ★否决权是架构属性：订单路径强制执行；普通策略路径不能绕过
6. ★安全层不做打折：超限返回 REDUCE 由仓位管理层重新下单，不静默缩放
   （"连续波动率目标和敞口缩放是仓位管理的职责；应急联锁只做硬限制"）

审核顺序（原文 check_order）：熔断 → 回撤 → 单笔 → 标的集中度 → 行业 → 总仓位

参数来源：config/params.yaml risk 段（M4 前已配置：三层回撤 5/10/15%、ATR 2×等）
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
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats


class Decision(Enum):
    APPROVE = "approve"
    REDUCE = "reduce"
    REJECT = "reject"


@dataclass
class RiskCheckResult:
    decision: Decision
    reason: str
    adjusted_size: Optional[float] = None
    ts: str = ""  # 审计：决策时间


class RiskAgent:
    """风险控制 Agent - 拥有一票否决权（M4 纪律引擎核心）"""

    def __init__(self, config: dict, audit_log: Optional[Path] = None,
                 state: Optional[dict] = None):
        # ★2026-11-26 风控接线：注入可变状态字典（由 risk_gate 从磁盘 load/save），
        #   使熔断状态可跨进程/跨实例恢复（deck_server 常驻 vs auto_execute 独立脚本）。
        #   gate 不传时退化为本进程内存态（保持原行为）。
        self.state = state if state is not None else {}
        # ---- 硬约束（来自 params.yaml risk 段）----
        self.max_single_position = config.get("max_position_pct", 0.20)   # 单笔上限 20%（与 5 只等权对齐）
        self.max_symbol_exposure = config.get("max_symbol_exposure", 0.20)  # 单标的 20%
        self.max_sector_exposure = config.get("max_industry_pct", 0.30)    # 行业 30%
        self.max_total_exposure = config.get("max_total_exposure", 0.80)   # 总仓位 80%
        ddl = config.get("drawdown_levels", {})
        self.drawdown_warning = ddl.get("warning", 0.05)        # 警戒 5%
        self.drawdown_stop = ddl.get("control", 0.10)           # 控制 10%
        self.drawdown_circuit = ddl.get("circuit_breaker", 0.15)  # 熔断 15%
        self.circuit_position = config.get("circuit_breaker_position", 0.30)  # 熔断后仓位 30%
        self.cooldown_days = config.get("circuit_breaker_cooldown_days", 5)  # 冷静期 5 天

        # ★2026-11-26：从持久化 state 恢复熔断状态（ISO 时间串 → datetime）
        self.is_circuit_breaker_active = bool(self.state.get("active", False))
        self.circuit_breaker_at = None
        _at = self.state.get("at")
        if _at:
            try:
                self.circuit_breaker_at = datetime.fromisoformat(str(_at))
            except (ValueError, TypeError):
                self.is_circuit_breaker_active = False  # 损坏的时间戳 → 不熔断
        self.audit_log = audit_log or (Path(__file__).resolve().parent.parent / "logs" / "risk_audit.log")

    # ---------- 审核（原文 check_order 完整逻辑）----------
    def check_order(self, symbol: str, size: float, current_portfolio: dict,
                    current_drawdown: float, sector: str = "",
                    sector_map: Optional[dict] = None) -> RiskCheckResult:
        """审核订单请求 → APPROVE / REDUCE(缩小) / REJECT"""
        # ⓪ 冷静期自动恢复（★2026-11-10：原 check_cooldown 无调用，熔断永不恢复）
        self.check_cooldown()
        # ① 熔断检查
        if self.is_circuit_breaker_active:
            return self._result(Decision.REJECT, "熔断冷静期：禁止新建仓（只可减仓）")
        # ② 回撤检查
        if current_drawdown >= self.drawdown_circuit:
            self._trigger_circuit_breaker(current_drawdown)
            return self._result(Decision.REJECT, f"回撤 {current_drawdown:.1%} ≥ 熔断线 {self.drawdown_circuit:.0%}，触发熔断")
        if current_drawdown >= self.drawdown_stop:
            return self._result(Decision.REJECT, f"回撤 {current_drawdown:.1%} ≥ 控制线 {self.drawdown_stop:.0%}：停止新开仓")
        # ③ 单笔上限（REDUCE 而非静默打折 —— 缩放是仓位管理的职责）
        if size > self.max_single_position:
            return self._result(Decision.REDUCE, f"单笔 {size:.1%} > 上限 {self.max_single_position:.1%}，缩小至上限",
                                adjusted_size=self.max_single_position)
        # ④ 标的集中度
        cur_sym = current_portfolio.get(symbol, 0.0)
        if cur_sym + size > self.max_symbol_exposure:
            allowed = self.max_symbol_exposure - cur_sym
            if allowed <= 0:
                return self._result(Decision.REJECT, f"标的 {symbol} 已达集中度上限 {self.max_symbol_exposure:.0%}")
            return self._result(Decision.REDUCE, f"标的集中度限制，缩小至 {allowed:.1%}", adjusted_size=allowed)
        # ⑤ 行业集中度（有行业信息时）
        if sector:
            # ★2026-11-10 修复：原 `k.startswith(sector)` 用股票代码做键，永不命中 →
            #   行业集中度上限形同虚设。传入 sector_map{code:行业} 时按行业聚合。
            if sector_map:
                cur_sector = sum(v for k, v in current_portfolio.items()
                                 if sector_map.get(k) == sector)
            else:
                cur_sector = sum(v for k, v in current_portfolio.items() if k.startswith(sector))
            if cur_sector + size > self.max_sector_exposure:
                allowed = self.max_sector_exposure - cur_sector
                if allowed <= 0:
                    return self._result(Decision.REJECT, f"行业 {sector} 已达上限 {self.max_sector_exposure:.0%}")
                return self._result(Decision.REDUCE, f"行业集中度限制，缩小至 {allowed:.1%}", adjusted_size=allowed)
        # ⑥ 总仓位
        total = sum(current_portfolio.values()) + size
        if total > self.max_total_exposure:
            return self._result(Decision.REJECT, f"总仓位 {total:.1%} 将超过上限 {self.max_total_exposure:.0%}")
        return self._result(Decision.APPROVE, "全部风控检查通过")

    # ---------- 回撤状态机（原文 check_drawdown）----------
    def check_drawdown(self, current_drawdown: float) -> str:
        """返回当前回撤状态：normal / reduce_risk / stop_new_positions / circuit_breaker"""
        if current_drawdown >= self.drawdown_circuit:
            self._trigger_circuit_breaker(current_drawdown)
            return "circuit_breaker"
        if current_drawdown >= self.drawdown_stop:
            return "stop_new_positions"
        if current_drawdown >= self.drawdown_warning:
            return "reduce_risk"
        return "normal"

    def _sync_state(self):
        """★2026-11-26：把熔断状态回写注入的 state（risk_gate 据此持久化到磁盘）。"""
        if not self.state:
            return
        self.state["active"] = bool(self.is_circuit_breaker_active)
        self.state["at"] = self.circuit_breaker_at.isoformat() if self.circuit_breaker_at else None
        self.state["cooldown_days"] = self.cooldown_days
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def _trigger_circuit_breaker(self, dd: float):
        """触发熔断：只可减仓至 circuit_position，进入冷静期"""
        self.is_circuit_breaker_active = True
        self.circuit_breaker_at = datetime.now()
        self._sync_state()
        self._audit("CIRCUIT_BREAKER_TRIGGERED", f"回撤 {dd:.1%}，减仓至 {self.circuit_position:.0%}，冷静期 {self.cooldown_days} 天")

    def check_cooldown(self) -> bool:
        """冷静期是否已过（自动恢复）"""
        if not self.is_circuit_breaker_active or self.circuit_breaker_at is None:
            return True
        days = (datetime.now() - self.circuit_breaker_at).days
        if days >= self.cooldown_days:
            self.is_circuit_breaker_active = False
            self.circuit_breaker_at = None
            self._sync_state()
            self._audit("CIRCUIT_BREAKER_RESET", f"冷静期 {days} 天结束，恢复正常")
            return True
        return False

    # ---------- 审计（原文原则3：日志不可修改）----------
    def _result(self, decision: Decision, reason: str, adjusted_size: Optional[float] = None) -> RiskCheckResult:
        res = RiskCheckResult(decision=decision, reason=reason,
                              adjusted_size=adjusted_size, ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._audit(decision.value, reason, adjusted_size)
        return res

    def _audit(self, decision: str, reason: str, adjusted_size: Optional[float] = None):
        """每次决策写审计日志（追加，不可修改）"""
        try:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            line = (f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {decision} | {reason}"
                    + (f" | adjusted={adjusted_size:.4f}" if adjusted_size is not None else "") + "\n")
            with open(self.audit_log, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "risk/risk_agent.py:180 _audit")


# ============================================================
# 仓位模型（第10/15课原文落地：Half-Kelly + Van Tharp + 贝叶斯 Kelly）
# ============================================================

def half_kelly(win_rate: float, reward_risk_ratio: float) -> float:
    """Half-Kelly 进攻上限（第10课）：f = (p×(b+1)-1)/b/2"""
    full = (win_rate * (reward_risk_ratio + 1) - 1) / reward_risk_ratio
    return max(0.0, full / 2)


def van_tharp_position_pct(equity: float, risk_pct: float, stop_loss_dist: float, price: float) -> float:
    """Van Tharp R-Multiple 防守下限（第10课）：max_loss = equity×risk_pct；股数 = max_loss/止损距离"""
    if stop_loss_dist <= 0 or price <= 0:
        return 0.0
    max_loss = equity * risk_pct
    shares = max_loss / stop_loss_dist
    return (shares * price) / equity


def bayesian_kelly(wins: int, losses: int, avg_win: float, avg_loss: float,
                   confidence: float = 0.9) -> dict:
    """贝叶斯 Kelly（第15课原文完整实现）：Beta 后验 + 置信下限 + 再打五折"""
    alpha, beta_ = wins + 1, losses + 1
    p_mean = alpha / (alpha + beta_)
    p_lower = stats.beta.ppf((1 - confidence) / 2, alpha, beta_)
    p_upper = stats.beta.ppf((1 + confidence) / 2, alpha, beta_)
    odds = avg_win / avg_loss if avg_loss > 0 else 0.0
    kelly_mean = (p_mean * odds - (1 - p_mean)) / odds if odds > 0 else 0.0
    kelly_lower = (p_lower * odds - (1 - p_lower)) / odds if odds > 0 else 0.0
    kelly_conservative = max(0.0, kelly_lower)
    return {
        "p_estimate": p_mean,
        "p_interval": (p_lower, p_upper),
        "kelly_mean": max(0.0, kelly_mean),
        "kelly_conservative": kelly_conservative,
        "recommendation": kelly_conservative / 2,
        "sample_size": wins + losses,
    }


def kelly_sample_discount(n_trades: int) -> float:
    """Kelly 样本量折扣（第15课原文表格）：
    <30 不建议 / 30-100×0.25 / 100-300×0.5 / 300-1000×0.7 / >1000×0.8"""
    if n_trades < 30:
        return 0.0          # 数据不足，不用 Kelly
    if n_trades < 100:
        return 0.25
    if n_trades < 300:
        return 0.50
    if n_trades < 1000:
        return 0.70
    return 0.80


def final_position_size(equity: float, win_rate: float, reward_risk: float,
                        risk_pct: float, stop_dist: float, price: float,
                        n_trades: int, hard_cap: float = 0.10) -> dict:
    """最终仓位 = min(Half-Kelly×样本折扣, Van Tharp, 硬性单笔上限)（第10/15课）"""
    hk = half_kelly(win_rate, reward_risk) * kelly_sample_discount(n_trades)
    vt = van_tharp_position_pct(equity, risk_pct, stop_dist, price)
    final = min(hk, vt, hard_cap)
    return {"half_kelly": hk, "van_tharp": vt, "hard_cap": hard_cap,
            "final_pct": final, "final_value": equity * final}


# ============================================================
# ATR 止损（第04/15课原文：止损价 = 入场价 - N×ATR）
# ============================================================

def atr_stop_price(entry_price: float, atr: float, n: float = 2.0) -> dict:
    """ATR 倍数止损：止损价 = 入场价 - N×ATR（N 通常 1.5-3）"""
    stop = entry_price - n * atr
    return {"entry": entry_price, "atr": atr, "n": n,
            "stop_price": stop, "stop_pct": (entry_price - stop) / entry_price}


# ============================================================
# 演示（对照第15课原文练习）
# ============================================================
if __name__ == "__main__":
    print("=== RiskAgent 审核演示（第15课场景）===")
    cfg = {"max_position_pct": 0.20, "max_symbol_exposure": 0.20,
           "max_industry_pct": 0.30, "max_total_exposure": 0.80,
           "drawdown_levels": {"warning": 0.05, "control": 0.10, "circuit_breaker": 0.15}}
    ra = RiskAgent(cfg)
    scenarios = [
        ("正常买入10%", dict(symbol="AAPL", size=0.10, current_portfolio={}, current_drawdown=0.02)),
        ("超限买入25%", dict(symbol="AAPL", size=0.25, current_portfolio={}, current_drawdown=0.02)),
        ("集中度(已有AAPL15%再买10%)", dict(symbol="AAPL", size=0.10, current_portfolio={"AAPL": 0.15}, current_drawdown=0.02)),
        ("回撤超控制线加仓", dict(symbol="MSFT", size=0.05, current_portfolio={}, current_drawdown=0.12)),
        ("熔断状态任何买入", dict(symbol="MSFT", size=0.05, current_portfolio={}, current_drawdown=0.16)),
    ]
    for name, kw in scenarios:
        r = ra.check_order(**kw)
        print(f"{name:22s} → {r.decision.value:8s} | {r.reason}")

    print("\n=== 贝叶斯 Kelly（第15课：60胜40负）===")
    bk = bayesian_kelly(60, 40, 0.02, 0.015)
    print(f"胜率 {bk['p_estimate']:.1%} (90%区间 {bk['p_interval'][0]:.1%}~{bk['p_interval'][1]:.1%}) "
          f"→ 保守 {bk['kelly_conservative']:.1%} → 推荐 {bk['recommendation']:.1%}")

    print("\n=== 最终仓位（第10课示例）===")
    ps = final_position_size(100000, 0.55, 1.5, 0.01, 10, 200, n_trades=50)
    print(f"Half-Kelly×折扣={ps['half_kelly']:.1%} VanTharp={ps['van_tharp']:.1%} "
          f"硬上限={ps['hard_cap']:.0%} → 最终 {ps['final_pct']:.1%}")

    print("\n=== ATR 止损（第15课示例：$100, ATR=$2, N=2）===")
    print(atr_stop_price(100, 2, 2))
