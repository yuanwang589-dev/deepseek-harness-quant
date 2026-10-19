# -*- coding: utf-8 -*-
"""tests/test_health.py — 健康基线（应当全绿）

红了 = 系统退化，不是测试写错。三类：
  TestDataAssets      数据资产与数据质量
  TestP0Regressions   已修复项的回归护栏（2026-10-15 P0）
  TestAccountRules    模拟账户账本层强制（T+1/整手/涨跌停/成交量）
  TestDeckService     看板服务（未启动则 skip）
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout

from tests._env import CACHE, LOGS, ro, bars_max_date, today_str, days_between


class TestDataAssets(unittest.TestCase):
    """核心数据资产必须存在、可读、新鲜、无脏值"""

    CORE_DBS = ["bars.db", "stock_basic.db", "finance.db", "finance_quality.db", "hist_mv.db"]

    def test_core_dbs_exist(self):
        missing = [n for n in self.CORE_DBS if not (CACHE / n).exists()]
        self.assertEqual(missing, [], f"核心库缺失: {missing}")

    def test_bars_readable(self):
        self.assertIsNotNone(bars_max_date(), "bars.db 读不到最新交易日")

    def test_bars_freshness(self):
        """最新交易日距今 ≤7 个自然日（容忍长假）"""
        d = bars_max_date()
        age = days_between(d, today_str())
        self.assertGreaterEqual(age, 0, f"bars.db 最新交易日 {d} 晚于今天 {today_str()}")
        self.assertLessEqual(age, 7, f"bars.db 最新交易日 {d}，距今 {age} 天 → 数据陈旧，全链结论不可信")

    def test_daily_coverage(self):
        """最新交易日覆盖 ≥4000 只（数据审计 gate 的完整性门槛）"""
        d = bars_max_date()
        n = ro("bars.db", "SELECT COUNT(*) FROM daily_bar WHERE adjust='qfq' AND date=?", (d,))[0][0]
        self.assertGreaterEqual(n, 4000, f"{d} 仅 {n} 只，低于完整性门槛 4000")

    def test_recent_price_sanity(self):
        """近 30 日无 0/负价、无 high<low"""
        d = bars_max_date()
        rows, bad_px, bad_hl = ro("bars.db", """
            SELECT COUNT(*),
                   SUM(CASE WHEN close<=0 OR open<=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN high<low THEN 1 ELSE 0 END)
            FROM daily_bar WHERE adjust='qfq' AND date >= date(?, '-30 day')""", (d,))[0]
        self.assertGreater(rows, 0, "近 30 日无数据")
        self.assertEqual(bad_px or 0, 0, f"存在 {bad_px} 条非正价格")
        self.assertEqual(bad_hl or 0, 0, f"存在 {bad_hl} 条 high<low")

    def test_stock_basic_size(self):
        n = ro("stock_basic.db", "SELECT COUNT(*) FROM stock_basic")[0][0]
        self.assertGreaterEqual(n, 4000, "stock_basic 代码表过小")


class TestP0Regressions(unittest.TestCase):
    """2026-10-15 P0 修复护栏。任一条变红 = 修复被回退，查 CHANGELOG。"""

    def test_rebalance_default_is_dry_run(self):
        """默认（无 --execute）跑再平衡，不得改动 logs/portfolio.json"""
        import scripts.auto_rebalance as ar
        pf = LOGS / "portfolio.json"
        before = (pf.stat().st_mtime_ns, pf.stat().st_size) if pf.exists() else None
        with redirect_stdout(io.StringIO()):
            ar.main(execute=False)
        after = (pf.stat().st_mtime_ns, pf.stat().st_size) if pf.exists() else None
        self.assertEqual(before, after, "dry-run 改动了 portfolio.json —— 冻结成交被破坏")

    def test_rebalance_trade_date_equals_data_date(self):
        """意向单 trade_date 必须等于 bars.db 数据日，不能是墙上时钟"""
        import scripts.auto_rebalance as ar
        with redirect_stdout(io.StringIO()):
            ar.main(execute=False)
        f = LOGS / "rebalance_intents.json"
        self.assertTrue(f.exists(), "未生成 logs/rebalance_intents.json")
        doc = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(doc["trade_date"], bars_max_date(), "trade_date 与数据日不一致 → 会用错日期记账")
        self.assertEqual(doc["wall_date"], today_str())
        self.assertFalse(doc["executed"], "dry-run 不得标记 executed")

    def test_rebalance_gate_blocks_stale_execute(self):
        """数据不新鲜时 --execute 必须被拒绝"""
        import scripts.auto_rebalance as ar
        if ar._latest_bar_date() == today_str():
            self.skipTest("今日数据已入库，门禁无法触发（属正常）")
        with redirect_stdout(io.StringIO()):
            ar.main(execute=True, allow_stale=False)
        doc = json.loads((LOGS / "rebalance_intents.json").read_text(encoding="utf-8"))
        self.assertFalse(doc["executed"], "数据不新鲜却成交 —— 新鲜度门禁失效")

    def test_candidates_carry_otype(self):
        """gather_candidates 必须传出 otype（原缺陷：漏传 → 全部退化 quality_gap）"""
        import scripts.sim_live_1w as sl
        cands = sl.gather_candidates(bars_max_date())
        self.assertGreater(len(cands), 0, "候选池为空，无法验证")
        no_ot = [c["code"] for c in cands if not c.get("otype")]
        self.assertEqual(no_ot, [], f"{len(no_ot)} 只候选 otype 为空，例如 {no_ot[:5]}")

    def test_take_profit_position_override_wins(self):
        """持仓级 take_profit 必须覆盖类型档（原缺陷：该字段被完全忽略）"""
        import risk.take_profit_check as tpc
        r = tpc.check_position({"code": "600519.SH", "otype": "reversal", "entry_date": "2026-09-01",
                                "entry_price": 20.0,
                                "take_profit": {"target_pct": 0.08, "pullback_pct": 0.03, "time_weeks": 2}})
        self.assertEqual(r["tp_source"], "position")
        self.assertAlmostEqual(r["tp"]["target_pct"], 0.08, places=6,
                               msg="持仓级 target_pct 未生效（被类型档 0.35 覆盖）")

    def test_take_profit_partial_override_merges_with_type(self):
        import risk.take_profit_check as tpc
        r = tpc.check_position({"code": "600519.SH", "otype": "reversal", "entry_date": "2026-09-01",
                                "entry_price": 20.0, "take_profit": {"target_pct": 0.08}})
        self.assertEqual(r["tp_source"], "position+type")
        self.assertAlmostEqual(r["tp"]["target_pct"], 0.08, places=6)
        self.assertAlmostEqual(r["tp"]["pullback_pct"], 0.06, places=6)   # reversal 类型档

    def test_take_profit_run_does_not_consume_argv(self):
        """tpc.run() 不得解析 sys.argv（原缺陷：吞掉调用方的 --execute 直接 SystemExit）"""
        import risk.take_profit_check as tpc
        saved = sys.argv[:]
        try:
            sys.argv = ["caller.py", "--execute", "--allow-stale"]
            with redirect_stdout(io.StringIO()):
                tpc.run(verbose=False)
        except SystemExit as e:
            self.fail(f"tpc.run() 消费了调用方参数并退出: {e}")
        finally:
            sys.argv = saved


class TestAccountRules(unittest.TestCase):
    """模拟账户账本层强制（P0-1 已实现，此处为回归护栏）"""

    def _fresh(self):
        import tempfile
        from pathlib import Path
        import strategy.paper_account as pa
        return pa.PaperAccount("__test__", cash=1_000_000,
                               db_path=str(Path(tempfile.mkdtemp()) / "t.db"))

    def test_lot_size_enforced(self):
        r = self._fresh().buy("600000.SH", 150, "2026-10-14", close=10.0,
                              prev_close=10.0, day_volume=10 ** 7, is_st=False)
        self.assertFalse(r.get("ok"), "非 100 股整数手被接受")

    def test_t1_enforced(self):
        a = self._fresh()
        self.assertTrue(a.buy("600000.SH", 100, "2026-10-14", close=10.0,
                              prev_close=10.0, day_volume=10 ** 7, is_st=False).get("ok"))
        s = a.sell("600000.SH", 100, "2026-10-14", close=10.5, prev_close=10.0, is_st=False)
        self.assertFalse(s.get("ok"), "同日卖出被接受 —— T+1 未强制")

    def test_limit_up_buy_rejected(self):
        r = self._fresh().buy("600000.SH", 100, "2026-10-14", close=11.0,
                              prev_close=10.0, day_volume=10 ** 7, is_st=False)
        self.assertFalse(r.get("ok"), "主板涨停买入未被拒")

    def test_limit_down_sell_rejected(self):
        a = self._fresh()
        a.buy("600000.SH", 100, "2026-10-14", close=10.0, prev_close=10.0,
              day_volume=10 ** 7, is_st=False)
        s = a.sell("600000.SH", 100, "2026-10-15", close=9.0, prev_close=10.0, is_st=False)
        self.assertFalse(s.get("ok"), "主板跌停卖出未被拒")

    def test_volume_cap_enforced(self):
        r = self._fresh().buy("600000.SH", 100, "2026-10-14", close=10.0,
                              prev_close=10.0, day_volume=50, is_st=False)
        self.assertFalse(r.get("ok"), "买入量超过当日成交量却未被拒")


class TestDeckService(unittest.TestCase):
    """看板服务健康（未启动则 skip，不算失败）"""

    def test_deck_health(self):
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 显式绕开代理
        try:
            with op.open("http://127.0.0.1:8787/api/live/opp", timeout=5) as r:
                status = r.status
        except Exception as e:
            self.skipTest(f"deck 未启动（{type(e).__name__}）—— 跳过")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
