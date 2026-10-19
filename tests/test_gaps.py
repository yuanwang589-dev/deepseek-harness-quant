# -*- coding: utf-8 -*-
"""tests/test_gaps.py — 待修清单（红 = 该缺口尚未修复）

每条用例断言「应有的状态」。现在红，修好后自动变绿 —— 这就是一份可执行的 TODO。
来源：docs/资产盘点.md（项目自述）、output/短线适配性审查_2026-10-15.html（审查结论）。

★ 本文件中的失败不是"测试写错"，是"系统缺东西"。修完请把用例保留为回归护栏。
"""
import sys
import tempfile
import unittest
from pathlib import Path

from tests._env import BASE, CACHE, code_only


class TestMissingDataSources(unittest.TestCase):
    """被代码引用、但磁盘上不存在的数据库"""

    def test_finance_ts_db_present(self):
        """PIT 财务三表：bt_runner/_load_fin、pitch_replay、stop_monitor 都依赖它"""
        self.assertTrue((CACHE / "finance_ts.db").exists(),
                        "data/cache/finance_ts.db 缺失 → 回测的基本面因子路径直接崩溃")

    @unittest.skipUnless((CACHE / "minute.db").exists(),
                         "付费边界：分钟数据不随包分发，见 docs/数据说明.md（库补齐后自动恢复断言）")
    def test_minute_db_present(self):
        """分钟数据（属付费边界，见 docs/数据说明.md）"""
        self.assertTrue((CACHE / "minute.db").exists(),
                        "data/cache/minute.db 缺失 → 竞价强度信号不可用（今日流水线已因此失败）")

    def test_lhb_rzrq_gdhs_shebao_present(self):
        """龙虎榜/两融/股东户数/社保 —— 资产盘点 §3 声称的源头数据层"""
        want = ["lhb_full.db", "rzrq_full.db", "gdhs_full.db", "shebao.db"]
        missing = [n for n in want if not (CACHE / n).exists()]
        self.assertEqual(missing, [], f"缺失 {missing} → 资产盘点声称的源头数据层不完整")

    def test_factor_pool_populated(self):
        """因子池应有实际入池记录 —— 资产盘点 §6.3 声称「已登记 54 个因子 + 生命周期管理」

        ★2026-10-15 已修复：新增 `factors/pool/seed_pool.py` 把四类因子登记入池（0 → 54 个）。
          本用例从"待修红"转为**回归护栏**（按 tests/README 约定：修好后不删用例）。
          更细的池内守卫（每个因子有分或有说明、retired 有理有据、派生分可复算）见
          `tests/test_factor_pool.py`。

        注意：只查文件存在是无效断言（`FactorRegistry()` 会自动建空库）。必须查表内容。
        """
        import sqlite3
        p = CACHE / "factor_pool.db"
        if not p.exists():
            self.fail("data/cache/factor_pool.db 不存在 → 因子池无持久化载体")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            n = con.execute("SELECT COUNT(*) FROM factors").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(
            n, 50,
            f"因子池 factors 表只有 {n} 行（基线 54）→ 入池记录被清掉了，"
            "跑 `python factors/pool/seed_pool.py` 重新登记")


class TestBrokenCodePaths(unittest.TestCase):
    """能复现的崩溃路径"""

    def test_backtest_fundamental_path_loads(self):
        """bt_runner._load_fin() 缺库时必须抛**带补数指引的 MissingDataError**，
        而不是裸 sqlite3.OperationalError。

        原缺陷：finance_ts.db 缺失 → OperationalError("unable to open database file")
        直接冒到 /api/live/backtest_run，500 + traceback 刷屏，且不告诉你是哪个库、
        怎么补。该崩溃路径已修复（2026-10-15），现在这是回归护栏：库在则加载出数据，
        库缺则抛 typed error 且 hint 可操作。库缺失本身仍是数据缺口，
        由 TestMissingDataSources 单独盯。
        """
        import backtest.bt_runner as B
        if (CACHE / "finance_ts.db").exists():
            fin = B._load_fin()
            self.assertGreater(len(fin), 0)
            return
        with self.assertRaises(B.MissingDataError) as cm:
            B._load_fin()
        self.assertTrue(cm.exception.hint, "MissingDataError 缺补数指引——退回成不可诊断的裸错")
        self.assertIn("build_unified_db.py", cm.exception.hint)

    def test_backtest_script1_runs(self):
        """script1（大市值三因子）应可跑通 —— 它是注册策略之一

        注意：run_backtest 现在对「数据缺失」返回 ok:False 的结构化结果而不是抛异常，
        所以断言点从「不抛异常」改为「ok 为真」。当前仍红 = finance_ts.db 未补齐。
        """
        import backtest.bt_runner as B
        r = B.run_backtest(strategy="script1", topn=5, stocks=100,
                           start="2023-01-01", end="2024-12-31")
        self.assertTrue(r.get("ok"), f"script1 回测未成功：{r.get('error')}")


class TestBoardLimitRules(unittest.TestCase):
    """涨跌停限幅未区分板块 / ST 档位过期（paper_account.py:30-31, 127, 190）

    A股现行规则：主板 ±10%；创业板(300/301)、科创板(688/689) ±20%；
                 北交所 ±30%；ST 自 2026-07-06 起 ±10%。
    现状：LIMIT_PCT 恒为 0.10、ST_LIMIT_PCT 恒为 0.05，与代码/板块无关
          → 创业板/科创板 10%~20% 的正常涨跌被误判为涨跌停而拒单；
             ST 5%~10% 的正常涨跌同样被误判。
    """

    def _fresh(self):
        import strategy.paper_account as pa
        return pa.PaperAccount("__test__", cash=1_000_000,
                               db_path=str(Path(tempfile.mkdtemp()) / "t.db"))

    def test_gem_board_limit_is_20pct(self):
        """创业板 +11% 属正常交易，不应被拒买"""
        r = self._fresh().buy("300001.SZ", 100, "2026-10-14", close=11.0,
                              prev_close=10.0, day_volume=10 ** 7, is_st=False)
        self.assertTrue(r.get("ok"), f"创业板 +11% 被拒（{r.get('msg')}）—— 限幅被当成 ±10%")

    def test_star_board_limit_is_20pct(self):
        """科创板 +11% 属正常交易，不应被拒买"""
        r = self._fresh().buy("688001.SH", 100, "2026-10-14", close=11.0,
                              prev_close=10.0, day_volume=10 ** 7, is_st=False)
        self.assertTrue(r.get("ok"), f"科创板 +11% 被拒（{r.get('msg')}）—— 限幅被当成 ±10%")

    def test_st_limit_is_10pct(self):
        """ST 自 2026-07-06 起为 ±10%，+5% 不应被拒"""
        r = self._fresh().buy("600000.SH", 100, "2026-10-14", close=10.5,
                              prev_close=10.0, day_volume=10 ** 7, is_st=True)
        self.assertTrue(r.get("ok"), f"ST +5% 被拒（{r.get('msg')}）—— ST 限幅仍按旧的 ±5%")


class TestLogicGaps(unittest.TestCase):
    """逻辑口径缺陷（P1/P2）"""

    def test_backtest_cost_tied_to_rebalance(self):
        """回测成本年化必须随策略实际调仓频率变化，不能硬编码 60 次/年。
        用 code_only 剥注释：描述历史缺陷的注释里必然提老公式，字面搜全文会误报。"""
        code = code_only(BASE / "backtest" / "bt_runner.py")
        self.assertTrue(
            "cost * 2 * 60" not in code,
            "bt_runner 仍硬编码 `cost * 2 * 60`：所有策略一律按 60 次/年（≈月频）摊销成本，"
            "对 turn_low(40 交易日)、factor_all(季度) 会高估成本。应按调仓频率折算。")

    def test_atr_stop_implemented(self):
        """ATR 移动止损应真正参与出信号（现仅定义 _atr() 且从未调用）"""
        code = code_only(BASE / "risk" / "position_stop_check.py")
        self.assertGreater(
            code.count("_atr("), 1,
            "position_stop_check._atr() 定义后从未被调用 → atr_stop_mult 配置是空壳"
            "（reversal 类型的 ATR 3× 宽止损未生效）")


class TestAuditFixes20261018(unittest.TestCase):
    """2026-10-18 审查修复的回归护栏（修好不删，反向验证）。

    每一条都对应一次实测失败，不是预防性猜想：
      - /api/live/funnel 全表回表 72~102s → 拖死整个看板（/help 页触发）
      - `python deck/deck_server.py` 直接启动 → 36 个 /api/live/* 全 500
      - data/health_check.py 遇 schtasks 拒绝访问 → 整份巡检崩溃
      - /api/live/data_assets 库不存在却返回 ok:true（假成功）
    """

    def test_funnel_bars_rows_uses_covering_index(self):
        """源头行数必须走覆盖索引，且 2 次调用稳态 <5s（原写法 72~102s/次）"""
        import time
        sys.path.insert(0, str(BASE))
        sys.path.insert(0, str(BASE / "deck"))
        import live_api
        t = time.time()
        n1 = live_api._bars_qfq_rows()
        first = time.time() - t
        t = time.time()
        n2 = live_api._bars_qfq_rows()
        second = time.time() - t
        self.assertIsNotNone(n1, "bars.db 行数取不到 → 说明库/索引异常")
        self.assertEqual(n1, n2, "两次调用结果不一致 → mtime 缓存失效逻辑有 bug")
        self.assertGreater(n1, 5_000_000, "qfq 全市场行数量级异常")
        self.assertLess(first, 20, f"首次 {first:.1f}s 仍过慢（应 ~1.2s，覆盖索引是否生效？）")
        self.assertLess(second, 1.0, f"缓存后仍 {second:.2f}s → 缓存未命中")

    def test_deck_server_bootstraps_repo_root(self):
        """deck_server 被直接 import 时须把仓库根放进 sys.path（否则 live_api 导入失败）"""
        import subprocess
        py = sys.executable
        code = ("import sys; sys.path.insert(0, r'%s'); import deck_server; "
                "assert r'%s' in sys.path, 'repo root missing'; print('ok')"
                % (BASE / "deck", BASE))
        r = subprocess.run([py, "-X", "utf8", "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, f"deck_server 自举失败：{r.stderr[-400:]}")
        self.assertIn("ok", r.stdout)

    def test_tasks_degrade_to_unverified_when_query_denied(self):
        """计划任务查询工具被拒时必须降级为"未验证"，不能整份崩掉、也不能伪装成"任务缺失"

        ★2026-10-18 修订历史：本测试原 mock 全局 subprocess.run 模拟 schtasks 被拒、
          断言降级返回 6 条。check_tasks 已改走 task_registry（后缀解析，不再用
          schtasks），降级路径统一返回 1 条 "(全部)" unverified——测试期望值还停留在
          旧实现，挂了 1 天。现改为精准 mock task_registry 的两个查询后端
          （不污染全局 subprocess，避免影响并行用例）。
        """
        sys.path.insert(0, str(BASE))
        from data import health_check, task_registry
        orig_ps = task_registry._query_powershell
        orig_st = task_registry._query_schtasks

        def denied(*a, **k):
            raise PermissionError(5, "拒绝访问")

        task_registry._query_powershell = denied
        task_registry._query_schtasks = denied
        try:
            rows = health_check.check_tasks()
        finally:
            task_registry._query_powershell = orig_ps
            task_registry._query_schtasks = orig_st
        self.assertEqual(len(rows), 1, "查询失败应统一降级为 1 条总览")
        self.assertTrue(all(r.get("unverified") for r in rows),
                        "无法查询时应标记 unverified，而不是伪装成'计划任务缺失'")
        self.assertFalse(rows[0].get("ok"), "未验证不能算通过")

    def test_data_assets_does_not_fake_success(self):
        """unified.db 不存在时必须 ok:false，不能 ok:true + 空表（假成功）"""
        sys.path.insert(0, str(BASE))
        sys.path.insert(0, str(BASE / "deck"))
        import live_api
        if (CACHE / "unified.db").exists():
            self.skipTest("unified.db 已建成，假成功路径不触发")
        live_api._data_assets_cache.update({"ts": 0.0, "data": None})
        out = live_api.live_data_assets()
        self.assertFalse(out.get("ok"), "unified.db 缺失却返回 ok:true → 前端会显示成'有库无数据'")
        self.assertIn("hint", out, "失败时必须给可执行提示")


class TestProbeContract20261018(unittest.TestCase):
    """存活/就绪探针契约（借 Kubernetes 探针语义）的回归护栏。

    背景：watchdog 原用业务接口 /api/live/opp 当存活探针，且**一次探不通就 kill**。
    2026-10-18 实测因果链：funnel 慢查询 72s → 线程占满 → 探针超时 → 立刻重启 →
    全站 30~60s 不可用。k8s 官方文档对此有明确警告（存活探针不得查下游依赖，
    且应设 failureThreshold 先观察再杀）。
    """

    def test_liveness_is_cheap_and_io_free(self):
        """/healthz 必须秒回且不碰数据库（否则探针会跟着业务一起卡）"""
        import time
        sys.path.insert(0, str(BASE))
        from deck.health import liveness
        t = time.time()
        d = liveness()
        dt = time.time() - t
        self.assertEqual(d["status"], "ok")
        self.assertIn("pid", d)
        self.assertIn("uptime_s", d)
        self.assertLess(dt, 0.05, f"liveness 耗时 {dt*1000:.1f}ms → 里面干了活，探针会被业务拖死")
        # 关键：不得出现任何依赖字段（一旦出现说明有人往里加了 I/O）
        self.assertNotIn("checks", d, "liveness 不应包含依赖检查（那是 readiness 的职责）")

    def test_readiness_three_states(self):
        """/readyz 必须是 ok/degraded/failed 三态，且 failed 只由依赖不可用触发"""
        sys.path.insert(0, str(BASE))
        from deck.health import readiness
        d = readiness()
        self.assertIn(d["status"], ("ok", "degraded", "failed"))
        self.assertIn("checks", d)
        self.assertIn("warnings", d)
        self.assertIn("errors", d)
        # bars.db 存在时不应判 failed（产物陈旧只能算 degraded）
        self.assertTrue((CACHE / "bars.db").exists(), "本用例假定 bars.db 存在")
        self.assertNotEqual(d["status"], "failed",
                            "bars.db 可读却判 failed → 会把'数据旧'误判成'服务挂'")

    def test_watchdog_probes_liveness_and_has_threshold(self):
        """watchdog 必须探 /healthz 且具备连续失败阈值（不能一次不通就杀）"""
        import re
        # code_only() 会 ast.unparse，字符串字面量统一成单引号 → 断言不能依赖引号形式
        src = code_only(BASE / "scripts" / "watchdog.py")
        m = re.search(r"HEALTH_PATH\s*=\s*['\"]([^'\"]+)['\"]", src)
        self.assertIsNotNone(m, "watchdog 里找不到 HEALTH_PATH 定义")
        self.assertEqual(m.group(1), "/healthz",
                         f"watchdog 探针指向 {m.group(1)} → 业务接口慢会触发重启风暴")
        m2 = re.search(r"FAIL_THRESHOLD\s*=\s*(\d+)", src)
        self.assertIsNotNone(m2, "watchdog 缺 FAIL_THRESHOLD → 一次探不通就 kill")
        self.assertGreaterEqual(int(m2.group(1)), 2, "failureThreshold 至少 2，否则等于没设")

    def test_slow_request_logging_wired(self):
        """do_GET 必须被计时包裹（慢接口要能在日志里被看见）"""
        src = code_only(BASE / "deck" / "deck_server.py")
        self.assertIn("def _do_GET_impl", src, "do_GET 未被拆分，无法加计时")
        self.assertIn("_log_slow", src, "缺慢请求日志函数")
        self.assertIn("SLOW_S", src, "缺慢请求阈值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
