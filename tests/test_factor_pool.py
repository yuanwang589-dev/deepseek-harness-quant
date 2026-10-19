# -*- coding: utf-8 -*-
"""tests/test_factor_pool.py — 因子池守卫

为什么需要这个文件：`data/cache/factor_pool.db` 的 `factors` 表长期 **0 行**——
挖→抓→测→决→报 机制齐全、`params.yaml` 阈值配好，但**治理逻辑没有对象**，
状态机 / 漂移巡检 / 报告全是空转。而"空池"不会报错，只会静默地什么都不做。

本文件守四件事：
  ① 池不能退回空池（登记数下限）；
  ② 每个因子要么有分、要么有话说（不允许"既没分、又不解释"）；
  ③ `retired` 必须有理有据（不许被误判淘汰）；
  ④ 评估通路本身可用——守住 2026-10-15 修掉的两个真 bug。

★现实变了怎么办：补了因子/跑了评估导致数字变化，先更新
  `factors/pool/seed_pool.py`，再更新这里的 EXPECTED。
"""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._env import BASE, CACHE

POOL_DB = CACHE / "factor_pool.db"

# 2026-10-15 登记后的基线（改 seed_pool.py 后需同步）
EXPECTED = {
    "engine": 6,          # factors/factor_engine.py::FACTOR_FUNCS
    "strong": 27,         # factors/risk/strong_factor_table.py::STRONG_TABLE
    "epu": 6,             # factors/policy/epu_factors.py::EPU_FAMILY
    "min_total": 50,      # 池规模下限（防回退到空池）
}


def _rows(sql, params=()):
    """只读打开池（不锁业务库）"""
    if not POOL_DB.exists():
        return None
    con = sqlite3.connect(f"file:{POOL_DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _all():
    return _rows("SELECT * FROM factors") or []


class TestPoolIsPopulated(unittest.TestCase):
    """① 池不能退回空池"""

    def test_pool_db_exists(self):
        self.assertTrue(POOL_DB.exists(), "data/cache/factor_pool.db 必须存在")

    def test_pool_not_empty(self):
        n = len(_all())
        self.assertGreaterEqual(
            n, EXPECTED["min_total"],
            f"因子池只有 {n} 个因子（下限 {EXPECTED['min_total']}）。"
            f"空池 = 因子治理逻辑没有对象，跑 `python factors/pool/seed_pool.py` 登记")

    def test_all_engine_factors_registered(self):
        sys.path.insert(0, str(BASE))
        from factors.factor_engine import FACTOR_FUNCS
        have = {r["name"] for r in _all()}
        missing = sorted(set(FACTOR_FUNCS) - have)
        self.assertEqual(missing, [], f"引擎因子未登记入池：{missing}")

    def test_all_strong_factors_registered(self):
        sys.path.insert(0, str(BASE))
        from factors.risk.strong_factor_table import STRONG_TABLE
        have = {r["name"] for r in _all()}
        missing = sorted(set(STRONG_TABLE) - have)
        self.assertEqual(missing, [], f"STRONG_TABLE 因子未登记入池：{missing}")

    def test_all_epu_factors_registered(self):
        sys.path.insert(0, str(BASE))
        try:
            from factors.policy.epu_factors import EPU_FAMILY
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"EPU_FAMILY 不可读：{e}")
        have = {r["name"] for r in _all()}
        missing = sorted(set(EPU_FAMILY) - have)
        self.assertEqual(missing, [], f"EPU 因子未登记入池：{missing}")

    def test_no_duplicate_names(self):
        names = [r["name"] for r in _all()]
        dupes = {n for n in names if names.count(n) > 1}
        self.assertEqual(dupes, set(), f"池内因子名重复：{dupes}")


class TestEveryFactorIsAccountedFor(unittest.TestCase):
    """② 每个因子要么有分、要么有话说"""

    def test_every_factor_has_score_or_explanation(self):
        """不允许"既没分、又不解释为何没分"——那正是空池换了个样子"""
        bad = []
        for r in _all():
            has_score = r.get("score") is not None
            note = (r.get("note") or "").strip()
            detail = (r.get("last_eval_detail") or "").strip()
            if not has_score and not note and not detail:
                bad.append(r["name"])
        self.assertEqual(bad, [], f"这些因子既无 score 也无 note/detail，无从判断质量：{bad}")

    def test_score_in_valid_range(self):
        for r in _all():
            s = r.get("score")
            if s is None:
                continue
            self.assertGreaterEqual(s, 0, f"{r['name']} score 越界：{s}")
            self.assertLessEqual(s, 100, f"{r['name']} score 越界：{s}")

    def test_status_in_known_set(self):
        from factors.pool.registry import STATUSES
        for r in _all():
            self.assertIn(r["status"], STATUSES,
                          f"{r['name']} 状态非法：{r['status']}（应 ∈ {STATUSES}）")

    def test_kind_in_known_set(self):
        from factors.pool.registry import KINDS
        for r in _all():
            self.assertIn(r["kind"], KINDS,
                          f"{r['name']} kind 非法：{r['kind']}（应 ∈ {KINDS}）")

    def test_source_is_traceable(self):
        """每个因子必须写清来源，不能留空——否则无从复现"""
        bad = [r["name"] for r in _all() if not (r.get("source") or "").strip()]
        self.assertEqual(bad, [], f"这些因子没写 source，无法溯源：{bad}")


class TestRetiredIsJustified(unittest.TestCase):
    """③ retired 必须有理有据"""

    def test_retired_has_reason(self):
        """不许出现"无评估记录却被淘汰"——那说明是被误判，不是被判定"""
        bad = []
        for r in _all():
            if r["status"] != "retired":
                continue
            note = (r.get("note") or "").strip()
            detail = (r.get("last_eval_detail") or "").strip()
            if not note and not detail:
                bad.append(r["name"])
        self.assertEqual(bad, [], f"这些因子被标 retired 却没有评估记录或说明：{bad}")

    def test_direction_zero_factor_is_retired(self):
        """方向=0 表示已剔除，必须 retired 而不是留在 active/candidate"""
        for r in _all():
            if r.get("direction") == 0:
                self.assertEqual(
                    r["status"], "retired",
                    f"{r['name']} direction=0（已剔除）却仍是 {r['status']}")

    def test_express_banned_never_active(self):
        """★截面 ICIR 高 ≠ 组合层有效。

        lhb_jg_cnt_20 / limup_ex_5 的截面 ICIR 全库前三，但组合层实证为负超额
        （-5.63pp / 6-7 个负年），已被 STRONG_TABLE 的 EXPRESS_BANNED 移出直通白名单。
        派生分会给它们满分，若按分数自动流转就会标成 active（=可接入策略）——这是错的。
        """
        sys.path.insert(0, str(BASE))
        from factors.risk.strong_factor_table import EXPRESS_BANNED
        by_name = {r["name"]: r for r in _all()}
        for name in EXPRESS_BANNED:
            if name not in by_name:
                self.fail(f"EXPRESS_BANNED 的 {name} 未登记入池")
            self.assertNotEqual(
                by_name[name]["status"], "active",
                f"{name} 在 EXPRESS_BANNED 里（组合层负超额），不得标 active")


class TestActiveSetIsHonest(unittest.TestCase):
    """`active` 的语义在文档 §6.3 有明确定义，别让它悄悄漂移"""

    def test_active_factors_are_all_external(self):
        """★当前 17 个 active **全部**来自外部因子池（本仓算不出来）。

        文档 §6.3 把这条写成了明确口径："active 不代表可立即使用，
        只代表外部研究判定有效"。如果哪天本仓可算的因子也进了 active，
        说明评估通路接通了 —— 那就该同步更新 §6.3 的表述。
        """
        active = [r for r in _all() if r["status"] == "active"]
        self.assertGreater(len(active), 0, "不应出现没有任何 active 因子的池")
        non_external = [r["name"] for r in active
                        if "strong_factor_table" not in (r["source"] or "")]
        self.assertEqual(
            non_external, [],
            f"active 里出现了非外部因子池的因子：{non_external}。"
            f"若评估通路已接通（本仓可算的因子拿到分数并转 active），"
            f"请同步更新 docs/资产盘点.md §6.3 中『active 全是本仓算不出来的』这一表述")

    def test_source_counts_match_expectation(self):
        """各来源登记数必须与 seed_pool 的四类清单一致"""
        from collections import Counter
        cnt = Counter((r["source"] or "").split("::")[0] for r in _all())
        self.assertEqual(cnt.get("factors/factor_engine.py"), EXPECTED["engine"],
                         "引擎因子登记数不符")
        self.assertEqual(cnt.get("factors/risk/strong_factor_table.py"),
                         EXPECTED["strong"] - 1,
                         "STRONG_TABLE 登记数不符（vol_contract 与扫描面板合并为一条）")
        self.assertEqual(sum(v for k, v in cnt.items() if k.startswith("factors/policy")
                             or "FRED" in k or "Huang" in k), EXPECTED["epu"],
                         "EPU 因子登记数不符")


class TestEvaluatorPathWorks(unittest.TestCase):
    """④ 守住 2026-10-15 修掉的两个真 bug"""

    def test_parse_score_from_real_output_format(self):
        """真实输出行没有冒号，原实现用 split(':') 解析必然失败"""
        from factors.pool.lifecycle import parse_evaluator_score
        real = "  评分 54.0 ｜ 反向弱有效 ｜ 低权重（10-30%，反用）"
        self.assertEqual(parse_evaluator_score(real), 54.0,
                         "必须能解析 factor_evaluator 的真实输出行")

    def test_parse_score_handles_variants(self):
        from factors.pool.lifecycle import parse_evaluator_score
        self.assertEqual(parse_evaluator_score("  评分 70.0 ｜ 强有效 ｜ 主权重"), 70.0)
        self.assertEqual(parse_evaluator_score("评分：88.5"), 88.5)
        self.assertIsNone(parse_evaluator_score("=== 评估 mom_20 ===\n  未注册，跳过"))

    def test_missing_score_does_not_retire(self):
        """★核心回归：评估拿不到分时**不得**把因子判成 retired。

        原实现：score=None → `"active" if score and ... else "retired"` → 一律 retired。
        后果是「评估器输出格式一变」或「因子无评估通路」就把好因子静默淘汰。
        """
        from factors.pool import lifecycle
        from factors.pool.registry import FactorRegistry

        with tempfile.TemporaryDirectory() as td:
            reg = FactorRegistry(db_path=Path(td) / "t.db")
            reg.register(name="demo", family="技术", kind="cross_sectional")
            reg.set_status("demo", "active", score=80.0, detail={"seed": True})

            fake = mock.Mock(returncode=0, stdout="=== 评估 demo ===\n  未注册，跳过\n", stderr="")
            with mock.patch.object(subprocess, "run", return_value=fake):
                lifecycle.evaluate_factor(reg, {"name": "demo", "kind": "cross_sectional"})

            after = reg.get("demo")
            self.assertEqual(after["status"], "active",
                             "拿不到分数时状态必须保持不变，不得判 retired")
            self.assertEqual(after["score"], 80.0, "原有分数不得被 None 覆盖")

    def test_real_score_still_transitions(self):
        """有分时仍要正常流转（别把上面的修复做成"永不流转"）"""
        from factors.pool import lifecycle
        from factors.pool.registry import FactorRegistry

        with tempfile.TemporaryDirectory() as td:
            reg = FactorRegistry(db_path=Path(td) / "t.db")
            reg.register(name="demo", family="技术", kind="cross_sectional")
            fake = mock.Mock(returncode=0, stdout="  评分 72.5 ｜ 强有效 ｜ 主权重\n", stderr="")
            with mock.patch.object(subprocess, "run", return_value=fake):
                lifecycle.evaluate_factor(reg, {"name": "demo", "kind": "cross_sectional"})
            after = reg.get("demo")
            self.assertEqual(after["score"], 72.5)
            self.assertEqual(after["status"], "active")

            fake2 = mock.Mock(returncode=0, stdout="  评分 20.0 ｜ 无效 ｜ 剔除\n", stderr="")
            with mock.patch.object(subprocess, "run", return_value=fake2):
                lifecycle.evaluate_factor(reg, {"name": "demo", "kind": "cross_sectional"})
            self.assertEqual(reg.get("demo")["status"], "retired",
                             "低分因子该被淘汰还是要淘汰")


class TestDerivedScore(unittest.TestCase):
    """派生分的确定性（STRONG_TABLE 那 27 个没有可跑通路，分数是派生的）"""

    def test_deterministic(self):
        from factors.pool.seed_pool import derive_score
        a = derive_score(1.857, 25.0, 0.874, 365)
        b = derive_score(1.857, 25.0, 0.874, 365)
        self.assertEqual(a, b, "同一输入必须给同一分数")

    def test_bounds(self):
        from factors.pool.seed_pool import derive_score
        self.assertGreaterEqual(derive_score(0, 0, 0, 0), 0)
        self.assertLessEqual(derive_score(99, 99, 1.0, 9999), 100)

    def test_monotonic_in_icir(self):
        from factors.pool.seed_pool import derive_score
        lo = derive_score(0.5, 20, 0.8, 365)
        hi = derive_score(1.5, 20, 0.8, 365)
        self.assertGreater(hi, lo, "ICIR 越高派生分应越高")

    def test_strong_table_scores_match_formula(self):
        """池里的分数必须真能由 STRONG_TABLE 指标复算出来（防止手改分数）"""
        sys.path.insert(0, str(BASE))
        from factors.risk.strong_factor_table import STRONG_TABLE
        from factors.pool.seed_pool import derive_score
        by_name = {r["name"]: r for r in _all()}
        checked = 0
        for name, m in STRONG_TABLE.items():
            r = by_name.get(name)
            if r is None or r["score"] is None:
                continue
            expect = derive_score(m["icir120"], m["t120"], m["win_rate"], m["n_days"])
            self.assertAlmostEqual(
                r["score"], expect, places=1,
                msg=f"{name} 池内分数 {r['score']} ≠ 由 F1 指标复算的 {expect}")
            checked += 1
        self.assertEqual(checked, EXPECTED["strong"],
                         f"应能复算全部 {EXPECTED['strong']} 个 STRONG_TABLE 因子的分数"
                         f"（实际复算 {checked} 个；同名因子合并时分数不能被丢掉）")


class TestSeedIsIdempotent(unittest.TestCase):
    """重复登记不应产生重复行或改坏分数"""

    def test_seed_idempotent_on_temp_db(self):
        from factors.pool.registry import FactorRegistry
        from factors.pool import seed_pool

        with tempfile.TemporaryDirectory() as td:
            reg = FactorRegistry(db_path=Path(td) / "t.db")
            seed_pool.seed(reg, quiet=True)
            n1 = reg.stats()["total"]
            snap1 = {r["name"]: r["score"] for r in reg.list_factors()}
            seed_pool.seed(reg, quiet=True)
            n2 = reg.stats()["total"]
            snap2 = {r["name"]: r["score"] for r in reg.list_factors()}
            self.assertEqual(n1, n2, "重复登记不得改变因子数量")
            self.assertEqual(snap1, snap2, "重复登记不得改变分数")


if __name__ == "__main__":
    unittest.main()
