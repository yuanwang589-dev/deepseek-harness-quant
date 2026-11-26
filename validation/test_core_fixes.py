# -*- coding: utf-8 -*-
"""核心修复回归测试（2026-11-10 审计批次）。

每个用例对应一个已修复缺陷，断言"此前会失败/静默错误"的行为：
- PIT 披露截止日截断（原硬编码 2026-07-01 / period<=end 前视）
- 静态文件路径穿越（原 /dl/、/v2/ 可逃逸）
- 共识加分 5 档缺失（原命中 5 类得 0 分）
- 一字板判定（原 open==high 误杀）
- paper_rotation pending 清理（原一字板僵尸单）
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.pit import disclosure_deadline, latest_disclosed_period
from deck.deck_server import _safe_file
from factors.opportunities.score import CONSENSUS_BONUS
from strategy.paper_rotation import (
    _PENDING_DDL,
    delete_pending,
    is_limit_locked,
    load_pending,
)


def test_pit_cutoff_uses_disclosure_deadline():
    # 8 月中旬：6-30 报告尚未披露完 → 只能用 Q1
    assert latest_disclosed_period("2026-08-15") == "2026-03-31"
    # 11 月：Q3（10-31 截止）已披露
    assert latest_disclosed_period("2026-11-10") == "2026-09-30"
    # 年报次年 4-30 截止；5-1 才可用
    assert disclosure_deadline("2025-12-31").isoformat() == "2026-04-30"
    assert latest_disclosed_period("2026-04-30") == "2025-09-30"


def test_safe_file_blocks_traversal(tmp_path):
    root = tmp_path / "pub"
    root.mkdir()
    (root / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
    assert _safe_file(root, "ok.txt", (".txt",)) is not None
    assert _safe_file(root, "../secret.txt") is None
    assert _safe_file(root, "..%2Fsecret.txt") is None
    assert _safe_file(root, "C:/Windows/win.ini") is None
    assert _safe_file(root, "ok.txt", (".json",)) is None


def test_consensus_bonus_has_five():
    # 原 {2,3,4,6..10} 缺 5 → 命中 5 类得 0（比 4 类还低）
    assert CONSENSUS_BONUS[5] > 0
    assert CONSENSUS_BONUS[5] <= CONSENSUS_BONUS[6]


def test_limit_locked_requires_flat_board():
    locked = {"open": 11.0, "high": 11.0, "low": 11.0, "preclose": 10.0}
    opened = {"open": 11.0, "high": 11.2, "low": 10.9, "preclose": 10.0}
    assert is_limit_locked(locked) is True
    assert is_limit_locked(opened) is False


def test_delete_pending_removes_order(tmp_path):
    db = tmp_path / "paper.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(_PENDING_DDL)
        con.execute(
            "INSERT INTO rotation_pending (signal_date,exec_date,code,side,qty,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-02", "000001.SZ", "buy", 100, "", "2026-01-01"))
        con.commit()
    finally:
        con.close()
    assert len(load_pending(db, "2026-01-02")) == 1
    delete_pending(db, "2026-01-02", "000001.SZ", "buy")
    assert load_pending(db, "2026-01-02") == []