"""validation/test_paper_rotation.py — 纸上短线轮换引擎测试（TDD）

对应实施计划 docs/plans/2026-10-16-paper-rotation.md
设计文档 docs/plans/2026-10-16-paper-rotation-design.md
"""
# pyright: reportMissingImports=false
# ↑ strategy.* 由 pytest rootdir 注入 sys.path，静态分析不可见（运行时正常）
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent


def _cfg():
    return yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))


def test_config_paper_rotation_present_and_defaults():
    pr = _cfg().get("paper_rotation")
    assert isinstance(pr, dict)
    assert isinstance(pr["enabled"], bool)  # 可开关（当前已按用户要求启用为 True）
    assert pr["account_name"] == "rotation"  # 独立账户，不碰 default
    assert pr["initial_cash"] == 1000000
    assert pr["max_positions"] == 6
    assert pr["max_weight_per_stock"] == 0.25
    assert pr["top_n_buy"] == 3
    assert pr["exit_rank_threshold"] == 10
    assert pr["min_hold_days"] == 3
    assert pr["max_hold_days"] == 20
    assert pr["execution"] == "next_open"


def test_config_example_has_same_keys():
    a = _cfg()["paper_rotation"].keys()
    b = yaml.safe_load((BASE / "config" / "params.yaml.example")
                       .read_text(encoding="utf-8"))["paper_rotation"].keys()
    assert set(a) == set(b)


def test_load_rotation_cfg_defaults_when_missing(tmp_path):
    import strategy.paper_rotation as pr
    cfg = pr.load_rotation_cfg(path=tmp_path / "nonexistent.yaml")
    assert cfg["max_positions"] == 6 and cfg["enabled"] is False   # 缺省兜底不崩


def test_load_shortline_scores_parses_entries():
    import strategy.paper_rotation as pr
    scores = pr.load_shortline_scores()          # 读 logs/tech_pitch_*.json（取 mtime 最新）
    assert isinstance(scores, dict)              # {code: {"score", "name", ...}}
    for code, v in scores.items():
        assert isinstance(code, str) and code.count(".") == 1
        assert isinstance(v["score"], (int, float))
    # 文件不存在时必须返回空 dict（不抛异常）
    assert pr.load_shortline_scores(pattern="logs/__no_such__*.json") == {}


def test_pending_table_and_roundtrip(tmp_path):
    import strategy.paper_rotation as pr
    db = tmp_path / "paper.db"
    pr.ensure_pending_table(db)
    pr.write_pending(db, "2026-10-15", "2026-10-16",
                     [{"code": "300300.SZ", "side": "buy", "qty": 100,
                       "reason": "new_entry"}])
    rows = pr.load_pending(db, exec_date="2026-10-16")
    assert len(rows) == 1 and rows[0]["code"] == "300300.SZ" and rows[0]["side"] == "buy"


def test_is_limit_locked_detects_one_way_board():
    import strategy.paper_rotation as pr
    # 开盘=最高=最低 且 涨幅≥9.8% → 一字板（买不到/卖不掉）
    assert pr.is_limit_locked({"open": 11.0, "high": 11.0, "low": 11.0,
                               "preclose": 10.0, "close": 11.0}) is True
    assert pr.is_limit_locked({"open": 10.5, "high": 11.0, "low": 10.2,
                               "preclose": 10.0, "close": 10.8}) is False
    assert pr.is_limit_locked({"open": None, "high": None, "low": None,
                               "preclose": None, "close": None}) is False


def _hold(code, entry="2026-10-01", days=5, cost=10.0, last=9.0):
    return {"code": code, "entry_date": entry, "hold_days": days,
            "entry_price": cost, "last_price": last, "stop_plan": {}}


def test_sell_priority_order_and_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3, exit_rank_threshold=10, max_hold_days=20)
    holds = [_hold("A.SZ", days=5), _hold("B.SZ", days=2)]     # B 未满最小持有期
    ranked = ["A.SZ", "B.SZ"] + [f"X{i}.SZ" for i in range(9)]
    plan = pr.compute_sells(holds, {}, cfg, ranked_codes=ranked)
    assert plan["sells"] == []                                 # 都在前 10 → 不触发衰减


def test_sell_triggered_by_rank_decay_after_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3, exit_rank_threshold=10)
    holds = [_hold("Z.SZ", days=5)]
    ranked = [f"X{i}.SZ" for i in range(10)]                   # Z 不在前 10
    plan = pr.compute_sells(holds, {"Z.SZ": {"score": 1}}, cfg, ranked_codes=ranked)
    assert [s["code"] for s in plan["sells"]] == ["Z.SZ"]
    assert plan["sells"][0]["reason"] == "rank_decay"


def test_stop_loss_bypasses_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3)
    h = _hold("S.SZ", days=1, cost=10.0, last=8.5)
    h["stop_plan"] = {"stop_loss_pct": 0.10}                   # 亏 15% > 10% → 止损
    plan = pr.compute_sells([h], {}, cfg, ranked_codes=[])
    assert plan["sells"][0]["reason"] == "stop"                # 止损不受最小持有期约束


def test_time_stop_after_max_hold_days():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3, max_hold_days=20)
    h = _hold("T.SZ", days=21)                                 # 超 20 交易日
    plan = pr.compute_sells([h], {}, cfg, ranked_codes=["T.SZ"])
    assert plan["sells"][0]["reason"] == "time_stop"


def test_buy_picks_top_n_and_respects_cap():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS)                     # top_n_buy=3, cap=0.25, max_positions=6
    ranked = [f"C{i}.SZ" for i in range(5)]
    scores = {c: {"score": 100 - i} for i, c in enumerate(ranked)}
    plan = pr.compute_buys(ranked_codes=ranked, scores=scores, holds=[], cfg=cfg,
                           cash=1_000_000, equity=1_000_000)
    assert len(plan["buys"]) == 3                                    # 只买前 3
    assert [b["code"] for b in plan["buys"]] == ["C0.SZ", "C1.SZ", "C2.SZ"]
    for b in plan["buys"]:
        assert b["amount"] <= 1_000_000 * 0.25 + 1e-6                # 单票不超 25%
    assert sum(b["amount"] for b in plan["buys"]) <= 1_000_000


def test_buy_skips_existing_and_respects_max_positions():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, max_positions=6)
    holds = [{"code": f"H{i}.SZ", "qty": 100} for i in range(6)]      # 已满仓
    plan = pr.compute_buys(ranked_codes=["N.SZ"], scores={"N.SZ": {"score": 99}},
                           holds=holds, cfg=cfg, cash=100_000, equity=1_000_000)
    assert plan["buys"] == []                                        # 满仓不买


def test_buy_excludes_already_held():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS)
    ranked = ["A.SZ", "B.SZ", "C.SZ", "D.SZ"]
    scores = {c: {"score": 90} for c in ranked}
    holds = [{"code": "A.SZ", "qty": 100}]                          # A 已持仓
    plan = pr.compute_buys(ranked_codes=ranked, scores=scores, holds=holds, cfg=cfg,
                           cash=500_000, equity=1_000_000)
    assert "A.SZ" not in [b["code"] for b in plan["buys"]]


def test_pending_exec_date_is_inclusive_and_sorted(tmp_path):
    """load_pending 取 exec_date <= 指定日（含停牌/无行情顺延的旧单）；同日先卖后买"""
    import strategy.paper_rotation as pr
    db = tmp_path / "paper.db"
    pr.ensure_pending_table(db)
    pr.write_pending(db, "2026-10-14", "2026-10-14", [
        {"code": "A.SZ", "side": "buy", "qty": 100, "reason": "new_entry"},
        {"code": "B.SZ", "side": "sell", "qty": 200, "reason": "rank_decay"}])
    pr.write_pending(db, "2026-10-15", "2026-10-15", [
        {"code": "C.SZ", "side": "buy", "qty": 300, "reason": "new_entry"}])
    rows = pr.load_pending(db, "2026-10-15")
    assert len(rows) == 3                                   # 含 10-14 的顺延单
    assert rows[0]["side"] == "sell"                        # 同日先卖后买
    assert {r["exec_date"] for r in rows} == {"2026-10-14", "2026-10-15"}
    assert len(pr.load_pending(db, "2026-10-14")) == 2       # 只取 <= 10-14


def test_run_once_dry_run_writes_nothing():
    import strategy.paper_rotation as pr
    r = pr.run_once(dry_run=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert "sells" in r and "buys" in r and "pending_executed" in r
    assert isinstance(r["sells"], list) and isinstance(r["buys"], list)


def test_run_once_noop_when_disabled(monkeypatch):
    import strategy.paper_rotation as pr
    monkeypatch.setattr(pr, "load_rotation_cfg", lambda **kw: dict(pr.DEFAULTS))
    r = pr.run_once()                            # enabled=False → 直接跳过，零副作用
    assert r["ok"] is True and r.get("skipped") == "disabled"


def test_report_shapes_and_benchmark():
    import strategy.paper_rotation_report as rep
    r = rep.build_report()
    assert set(r) >= {"nav_curve", "turnover", "fees", "max_drawdown", "benchmark"}
    assert isinstance(r["nav_curve"], list)
    assert isinstance(r["turnover"], int)
    assert r["fees"] >= 0 and 0.0 <= r["max_drawdown"] <= 1.0
    assert isinstance(r["benchmark"], dict)
