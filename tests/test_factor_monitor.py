# -*- coding: utf-8 -*-
"""tests/test_factor_monitor.py — IC 序列仓库与因子健康监控的回归护栏（2026-10-18）

立项背景：系统"有池无序列"——因子池 54 个因子只存汇总分，IC 逐窗口序列在
eval_ts._spearman_ic 里算完就扔。本测试锁死三件事：
  ① 序列必须随评估结果返回并落库（防"又被丢弃"）；
  ② 衰减判定规则与 alpha-skills alpha-monitor 对齐（阈值/符号翻转/小样本不判）；
  ③ ic_store 幂等，重复建 schema 不炸、不重列。
"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tests._env import BASE

sys.path.insert(0, str(BASE))


class TestCheckDecay(unittest.TestCase):
    def setUp(self):
        from factors.pool.factor_monitor import check_decay
        self.check = check_decay

    def test_ok_when_stable(self):
        s = pd.Series([0.10] * 24)
        r = self.check(s, window=6)
        self.assertEqual(r["status"], "ok", f"稳定因子不应误报：{r}")

    def test_warning_when_decayed(self):
        s = pd.Series([0.10] * 24 + [0.01] * 6)
        r = self.check(s, window=6)
        self.assertEqual(r["status"], "warning")
        self.assertGreaterEqual(r["decay_pct"], 50, "衰减 90% 必须触发 warning")

    def test_retire_suggested_on_sign_flip(self):
        s = pd.Series([0.10] * 24 + [-0.05] * 6)
        r = self.check(s, window=6)
        self.assertEqual(r["status"], "retire_suggested")
        self.assertTrue(r["sign_flip"])

    def test_insufficient_when_short(self):
        s = pd.Series([0.1, 0.2, 0.05])
        r = self.check(s, window=6)
        self.assertEqual(r["status"], "insufficient_data")

    def test_negative_factor_decay_uses_abs(self):
        """负向因子（IC 长期为负但有效）的衰减要按 |IC| 比，不能因符号误判退役"""
        s = pd.Series([-0.10] * 24 + [-0.01] * 6)
        r = self.check(s, window=6)
        self.assertEqual(r["status"], "warning")
        self.assertFalse(r["sign_flip"])

    def test_none_series(self):
        r = self.check(None, window=6)
        self.assertEqual(r["status"], "insufficient_data")


class TestIcStore(unittest.TestCase):
    def _make_db(self, td):
        from factors.pool import ic_store
        db = Path(td) / "pool.db"
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE factors (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name TEXT UNIQUE NOT NULL, score REAL)")
        con.execute("INSERT INTO factors(name, score) VALUES ('demo_factor', 60.0)")
        con.commit()
        con.close()
        return ic_store.connect(db)

    def test_roundtrip_and_idempotent_schema(self):
        from factors.pool.ic_store import save_series, load_series, ensure_schema
        with tempfile.TemporaryDirectory() as td:
            con = self._make_db(td)
            ensure_schema(con)
            ensure_schema(con)  # 幂等：不炸、不重列
            cols = [r["name"] for r in con.execute("PRAGMA table_info(factors)")]
            for want in ("expression", "ic_mean", "icir", "best_holding_period"):
                self.assertIn(want, cols)
            s = pd.Series({"2026-01": 0.03, "2026-02": 0.05, "2026-03": -0.01})
            n = save_series(con, "demo_factor", s, source="eval_ts")
            self.assertEqual(n, 3)
            save_series(con, "demo_factor", s, source="eval_ts")  # 重复 upsert 不翻倍
            got = load_series(con, "demo_factor", source="eval_ts")
            self.assertEqual(len(got), 3, "upsert 后行数不应增长")
            self.assertAlmostEqual(got["2026-02"], 0.05)
            con.close()

    def test_snapshot_backfills_factor_metrics(self):
        from factors.pool.ic_store import save_snapshot, load_series
        with tempfile.TemporaryDirectory() as td:
            con = self._make_db(td)
            save_snapshot(con, "demo_factor", "2026-10", 0.031, 0.42,
                          source="cross_sectional", best_holding_period=3)
            row = con.execute("SELECT ic_mean, icir, best_holding_period "
                              "FROM factors WHERE name='demo_factor'").fetchone()
            self.assertAlmostEqual(row["ic_mean"], 0.031)
            self.assertAlmostEqual(row["icir"], 0.42)
            self.assertEqual(row["best_holding_period"], 3)
            self.assertEqual(len(load_series(con, "demo_factor")), 1)
            con.close()


class TestEvalTsKeepsSeries(unittest.TestCase):
    def test_spearman_ic_returns_series(self):
        """护栏：_spearman_ic 必须把逐窗口 IC 序列交出来（原来算完就扔）"""
        import numpy as np
        from factors.pool.eval_ts import _spearman_ic
        idx = [f"2020-{m:02d}" for m in range(1, 13)] + [f"2021-{m:02d}" for m in range(1, 13)]
        f = pd.Series(np.linspace(0, 1, 24), index=idx)
        r = pd.Series(np.linspace(0.05, 0.30, 24), index=idx)
        out = _spearman_ic(f, r)
        self.assertIn("ic_series", out)
        self.assertEqual(len(out["ic_series"]), 13, "24 期 × 12 窗口应产出 13 个窗口 IC")

    def test_short_series_returns_empty_dict_not_none(self):
        from factors.pool.eval_ts import _spearman_ic
        out = _spearman_ic(pd.Series([1.0, 2.0]), pd.Series([0.1, 0.2]))
        self.assertEqual(out["ic_series"], {})


if __name__ == "__main__":
    unittest.main()
