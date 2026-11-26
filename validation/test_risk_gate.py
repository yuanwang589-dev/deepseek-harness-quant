# -*- coding: utf-8 -*-
"""风控接线回归测试（2026-11-26）。

覆盖：
- pretrade_check：APPROVE / REDUCE / 行业超限 REJECT / 熔断 REJECT / 回撤控制线 REJECT
- l0_gate：防御期拒绝 / 数据缺失全 fail-closed / 非高风险放行
- sell_check：硬止损 / 止盈
- 熔断状态跨进程持久化与冷静期自动恢复
- 集成：sync_from_decisions 在择时数据缺失时拒绝高风险买入

不依赖 pytest（环境未安装）：用标准库 tempfile + __main__ 入口；
pytest 装了以后也能被 `test_*` 函数发现。
"""
import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from risk.risk_agent import Decision
from risk.risk_gate import (
    l0_gate,
    load_circuit_state,
    pretrade_check,
    save_circuit_state,
    sell_check,
)


def _cfg():
    return {"risk": {
        "max_position_pct": 0.20,
        "max_symbol_exposure": 0.20,
        "max_industry_pct": 0.30,
        "max_total_exposure": 0.80,
        "drawdown_levels": {"warning": 0.05, "control": 0.10, "circuit_breaker": 0.15},
        "circuit_breaker_position": 0.30,
        "circuit_breaker_cooldown_days": 5,
    }}


def _state(tmp):
    return str(Path(tmp) / "risk_circuit_state.json")


# ------------------------------------------------------------------
def test_pretrade_approve_normal():
    with tempfile.TemporaryDirectory() as td:
        r = pretrade_check("600519.SH", 0.20, {}, 0.0,
                           cfg=_cfg(), state_path=_state(td))
        assert r.decision == Decision.APPROVE, r.reason


def test_pretrade_reduce_single_over():
    with tempfile.TemporaryDirectory() as td:
        r = pretrade_check("600519.SH", 0.25, {}, 0.0,
                           cfg=_cfg(), state_path=_state(td))
        assert r.decision == Decision.REDUCE, r.reason
        assert abs(r.adjusted_size - 0.20) < 1e-6, r.adjusted_size


def test_pretrade_reject_sector_over():
    with tempfile.TemporaryDirectory() as td:
        weights = {"000001.SZ": 0.15, "000002.SZ": 0.15}   # 同行业已 30%
        smap = {"000001.SZ": "银行", "000002.SZ": "银行", "600519.SH": "银行"}
        r = pretrade_check("600519.SH", 0.20, weights, 0.0,
                           sector="银行", sector_map=smap,
                           cfg=_cfg(), state_path=_state(td))
        assert r.decision == Decision.REJECT, r.reason


def test_pretrade_reject_circuit():
    with tempfile.TemporaryDirectory() as td:
        p = Path(_state(td))
        save_circuit_state({"active": True, "at": datetime.now().isoformat(),
                            "cooldown_days": 5, "updated_at": datetime.now().isoformat()}, p)
        r = pretrade_check("600519.SH", 0.20, {}, 0.0,
                           cfg=_cfg(), state_path=p)
        assert r.decision == Decision.REJECT, r.reason
        assert "熔断" in r.reason


def test_pretrade_reject_control_line():
    with tempfile.TemporaryDirectory() as td:
        r = pretrade_check("600519.SH", 0.20, {}, 0.12,   # 回撤 12% > 控制线 10%
                           cfg=_cfg(), state_path=_state(td))
        assert r.decision == Decision.REJECT, r.reason


def test_l0_defensive_reject():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "output").mkdir()
        (root / "output" / "timing_system_20261126.json").write_text(
            json.dumps({"score": 38, "level": "不适合"}), encoding="utf-8")
        ok, why = l0_gate("tech_sentiment", "600519.SH", base=root)
        assert not ok and "L0" in why, why


def test_l0_missing_timing_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)   # 故意不建 output/timing_system_*.json
        ok, why = l0_gate("revalue", "600519.SH", base=root)
        assert not ok and "FAIL_CLOSED" in why, why
        # 坏 JSON 同样 fail-closed
        (root / "output").mkdir()
        (root / "output" / "timing_system_bad.json").write_text("{broken", encoding="utf-8")
        ok2, why2 = l0_gate("revalue", "600519.SH", base=root)
        assert not ok2, why2


def test_l0_non_high_risk_pass():
    with tempfile.TemporaryDirectory() as td:
        ok, _ = l0_gate("value", "600519.SH", base=Path(td))
        assert ok


def test_sell_check_hard_loss():
    d = sell_check({"code": "600519.SH", "entry_price": 10.0,
                    "current_price": 9.2, "entry_date": datetime.now().strftime("%Y-%m-%d")},
                   cfg=_cfg())
    assert d.should_sell and any(t.value == "hard_loss" for t, _ in d.reasons)


def test_sell_check_take_profit():
    d = sell_check({"code": "600519.SH", "entry_price": 10.0,
                    "current_price": 12.8, "entry_date": datetime.now().strftime("%Y-%m-%d")},
                   cfg=_cfg())
    assert d.should_sell and "止盈" in d.action


def test_circuit_state_persist_cross_instance():
    with tempfile.TemporaryDirectory() as td:
        p = Path(_state(td))
        save_circuit_state({"active": True, "at": datetime.now().isoformat(),
                            "cooldown_days": 5}, p)
        loaded = load_circuit_state(p)
        assert loaded["active"] is True
        # 新实例读到 active → REJECT
        r = pretrade_check("600519.SH", 0.20, {}, 0.0, cfg=_cfg(), state_path=p)
        assert r.decision == Decision.REJECT
        # cooldown 到期 → 自动 RESET → APPROVE
        save_circuit_state({"active": True,
                            "at": (datetime.now() - timedelta(days=10)).isoformat(),
                            "cooldown_days": 5}, p)
        r2 = pretrade_check("600519.SH", 0.20, {}, 0.0, cfg=_cfg(), state_path=p)
        assert r2.decision == Decision.APPROVE, r2.reason


def test_sync_rejects_high_risk_when_timing_missing():
    """集成：sync_from_decisions 在择时数据缺失时拒绝高风险买入（L0 fail-closed 落到台账）。"""
    from strategy import portfolio as PF
    from risk import risk_gate as RG

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir()
        (root / "output").mkdir()   # 无 timing_system_*.json
        old = (PF.BASE, PF.PORTFOLIO_JSON, PF.DECISIONS_JSON,
               PF.RISK_JSON, PF.STOP_MATRIX_JSON, RG.BASE, RG.AUDIT_LOG)
        try:
            PF.BASE = root
            PF.PORTFOLIO_JSON = root / "logs" / "portfolio.json"
            PF.DECISIONS_JSON = root / "logs" / "deck_decisions.json"
            PF.RISK_JSON = root / "logs" / "stock_risk_map_v2.json"
            PF.STOP_MATRIX_JSON = root / "stop_matrix.json"
            RG.BASE = root
            RG.AUDIT_LOG = root / "logs" / "risk_audit.log"
            dec = root / "logs" / "deck_decisions_test.json"
            dec.write_text(json.dumps([{
                "code": "600519.SH", "action": "buy",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "pitch_meta": {"otype": "tech_sentiment"},
            }], ensure_ascii=False), encoding="utf-8")
            res = PF.sync_from_decisions(str(dec))
            assert res["added"] == [], res
            assert any(r.get("layer") == "L0" for r in res["rejected"]), res
        finally:
            (PF.BASE, PF.PORTFOLIO_JSON, PF.DECISIONS_JSON,
             PF.RISK_JSON, PF.STOP_MATRIX_JSON, RG.BASE, RG.AUDIT_LOG) = old


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
