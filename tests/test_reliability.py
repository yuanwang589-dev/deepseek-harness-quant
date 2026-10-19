# -*- coding: utf-8 -*-
"""tests/test_reliability.py — 可靠性与资源管理回归（★2026-10-15）

守住本轮加固，防回退：
  TestServerRobustness   deck 服务器：守护线程 / 未捕获异常回 500 / 大 backlog / 退出不阻塞
  TestIoUtils            data/io_utils：无 ResourceWarning、原子写无残留、sqlite 异常也关
  TestLimitMatrix        撮合涨跌停矩阵（板块 + ST 全组合，买/卖两侧）
  TestCostModel          回测成本按调仓频率折算，不与窗口长度耦合

设计说明：Windows 上无 /proc/self/fd，所以**不用句柄计数**（会退化成 0 而变成空断言）。
泄漏检测改用两个确定性信号：
  ① warnings 捕获 ResourceWarning —— 未及时关闭的对象由 gc 在销毁时告警，必现且可断言；
  ② 直接调用 handle_error / with 退出后的 sqlite 状态 —— 不依赖平台 API。
"""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

from tests._env import BASE, code_only as _code_only


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _stub_request():
    """handle_error 只用得到 request.sendall。真实 socket 未连接时 sendall 必抛，
    这里用必然抛错的桩代替，免得每次调用都留一个未关闭的 socket（ResourceWarning）。"""
    class _Req:
        @staticmethod
        def sendall(data):
            raise OSError("stub request has no connection")
    return _Req()


def _server_module():
    """干净地导入 deck_server（避免跨用例共享模块状态）"""
    sys.path.insert(0, str(BASE / "deck"))
    sys.modules.pop("deck_server", None)
    import deck_server as D
    return D


class TestServerRobustness(unittest.TestCase):
    """deck 服务器可靠性（原裸用 ThreadingHTTPServer 的三个隐患）"""

    def setUp(self):
        self.D = _server_module()

    def _srv(self, handler=None):
        """构造测试用的 DeckServer，并把合成异常重定向到临时日志。

        不重定向的话，每轮测试都会往真实观测面 logs/deck_server_errors.log 里写
        boom/NoneType 之类的噪音，运维就分不清哪些是真异常了。
        """
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), handler or self.D.Handler)
        srv.err_log = Path(tempfile.mkdtemp()) / "synthetic_errors.log"
        return srv

    def test_daemon_threads_on(self):
        """卡住的请求线程不得阻塞进程退出（否则重启撞端口占用）"""
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), self.D.Handler)
        try:
            self.assertTrue(srv.daemon_threads, "daemon_threads 未开——卡死线程会挂住整个进程")
        finally:
            srv.server_close()

    def test_allow_reuse_address(self):
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), self.D.Handler)
        try:
            self.assertTrue(srv.allow_reuse_address, "未开地址复用——重启会撞 WSAEADDRINUSE")
        finally:
            srv.server_close()

    def test_large_backlog(self):
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), self.D.Handler)
        try:
            self.assertGreaterEqual(srv.request_queue_size, 32, "listen backlog 太小，集中刷新会拒连")
        finally:
            srv.server_close()

    def test_handler_timeout(self):
        self.assertGreater(self.D.Handler.timeout, 0,
                           "handler 未设 socket 超时——半开连接会长期占用工作线程")

    def test_uncaught_exception_returns_500_not_hang(self):
        """工作线程未捕获异常必须回 500，让客户端立刻失败而不是悬挂"""
        class Boom(self.D.Handler):
            def do_GET(self):
                raise RuntimeError("boom")

        srv = self._srv(Boom)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            op.open(f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=6)
            self.fail("异常请求竟然返回 200")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 500, "未捕获异常未回 500 → 客户端会悬挂到自身超时")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_real_error_counted_in_window(self):
        """真异常必须进错误窗口（可观测），且不向外抛"""
        self.D.DeckServer._err_window.clear()
        srv = self._srv()
        try:
            # 造一个真异常上下文
            try:
                raise ValueError("db locked")
            except Exception:
                srv.handle_error(_stub_request(), ("127.0.0.1", 12345))
            self.assertEqual(len(self.D.DeckServer._err_window), 1)
        finally:
            srv.server_close()

    def test_handle_error_tolerates_empty_exc_info(self):
        """无异常上下文时被调用也不能炸（exc 为 None 时自身要稳）"""
        self.D.DeckServer._err_window.clear()
        srv = self._srv()
        try:
            srv.handle_error(_stub_request(), ("127.0.0.1", 12345))   # 不抛即通过
        finally:
            srv.server_close()

    def test_synthetic_errors_do_not_touch_real_log(self):
        """覆盖 err_log 后，合成异常必须只进临时文件，不污染真实观测面。
        （否则每轮测试都往 logs/deck_server_errors.log 写 boom/NoneType 噪音）"""
        real = self.D.LOGS / "deck_server_errors.log"
        before = real.read_text(encoding="utf-8") if real.exists() else ""
        srv = self._srv()
        tmp = srv.err_log
        try:
            try:
                raise ValueError("synthetic-only")
            except Exception:
                srv.handle_error(_stub_request(), ("127.0.0.1", 1))
        finally:
            srv.server_close()
        after = real.read_text(encoding="utf-8") if real.exists() else ""
        self.assertEqual(before, after, "合成异常写进了真实观测面 logs/deck_server_errors.log")
        self.assertIn("synthetic-only", tmp.read_text(encoding="utf-8"),
                      "覆盖 err_log 后异常仍没进临时文件")

    def test_port_already_served_detects_live_instance(self):
        """双开自检：端口上有存活实例在应答 → True（拒绝启动的依据）"""
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), self.D.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        try:
            self.assertTrue(self.D._port_already_served(srv.server_address[1]))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_port_already_served_false_when_free(self):
        """空端口 → False（旧实例已死，允许安全接管，watchdog 故障转移不受阻）"""
        self.assertFalse(self.D._port_already_served(_free_port()))

    def test_port_already_served_true_when_server_busy(self):
        """活着但忙的实例 → True（不能因为探测超时就放行双开）。

        本轮线上事故的直接复现：deck 重端点把 GIL 打满时探测超过 timeout 不应答，
        原自检把"超时"和"拒连"一样当成空端口 → 第二个实例双开成功 → 端口劫持。
        """
        class Slow(self.D.Handler):
            def do_GET(self):
                time.sleep(4)

        srv = self.D.DeckServer(("127.0.0.1", _free_port()), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        try:
            t0 = time.time()
            self.assertTrue(self.D._port_already_served(srv.server_address[1], timeout=1.0),
                            "忙着的实例被误判成空端口——双开劫持的成因没堵住")
            self.assertGreaterEqual(time.time() - t0, 4.0,
                                    "忙实例应重试到能应答为止，而不是一次超时就说空")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_port_already_served_fast_when_free(self):
        """空端口判定必须远快于"忙实例"的最坏路径（watchdog 故障转移不能被拖慢）。

        注意：_free_port() 刚 bind/close 的端口首次连接可能撞一次 reset 触发一轮
        1.5s 重试（真实场景里旧实例早死、端口干净，是即时拒连），所以阈值取 3.5s；
        要守的是"别走满 3×timeout 的忙等路径"（见上一条用例，忙路径 ≥4s）。
        """
        t0 = time.time()
        self.assertFalse(self.D._port_already_served(_free_port()))
        self.assertLess(time.time() - t0, 3.5, "空端口判定走了完整重试路径")

    def test_client_abort_not_counted_as_error(self):
        """客户端主动断连不算服务缺陷，不进错误窗口（否则健康面被刷假警）"""
        srv = self.D.DeckServer(("127.0.0.1", _free_port()), self.D.Handler)
        try:
            self.D.DeckServer._err_window.clear()
            try:
                raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有连接")
            except Exception:
                srv.handle_error(_stub_request(), ("127.0.0.1", 12345))
            self.assertEqual(len(self.D.DeckServer._err_window), 0,
                             "客户端断连被计成服务错误——健康面会刷假警")
        finally:
            srv.server_close()

    def test_shutdown_not_blocked_by_hung_handler(self):
        """卡死的 handler 不得让 shutdown() 卡住（守护线程的意义所在）"""
        class Hang(self.D.Handler):
            def do_GET(self):
                time.sleep(999)

        srv = self.D.DeckServer(("127.0.0.1", _free_port()), Hang)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)

        def _probe():
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=1)
            except Exception:
                pass   # 探活超时正是本用例预期，别让线程 traceback 刷屏

        threading.Thread(target=_probe, daemon=True).start()
        time.sleep(0.5)
        t0 = time.time()
        try:
            srv.shutdown()
        finally:
            srv.server_close()
        self.assertLess(time.time() - t0, 3.0, "shutdown 被卡死的 handler 阻塞了")


class TestIoUtils(unittest.TestCase):
    """data/io_utils.py —— 修复的资源管理问题"""

    def setUp(self):
        sys.path.insert(0, str(BASE))
        from data import io_utils
        self.m = io_utils

    def test_read_json_no_resource_warning(self):
        """read_json 必须不留未关闭文件对象。

        原写法 json.load(open(f)) 的对象生命周期依赖 gc；在有 traceback 引用残留的
        错误路径上会长时间不关。这里以 ResourceWarning 作为确定性信号（Windows 上无
        /proc/self/fd 可用，用这个比句柄计数可靠）。
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            p.write_text('{"k": 1}', encoding="utf-8")
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always", ResourceWarning)
                for _ in range(50):
                    self.assertEqual(self.m.read_json(p), {"k": 1})
            leaked = [x for x in w if issubclass(x.category, ResourceWarning)]
            self.assertEqual(leaked, [], f"read_json 产生 {len(leaked)} 条 ResourceWarning")

    def test_read_json_old_pattern_did_warn(self):
        """反向基线：确认老写法确实会告警（否则上面的断言是空转）"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            p.write_text('{"k": 1}', encoding="utf-8")
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always", ResourceWarning)
                for _ in range(50):
                    self.assertEqual(json.load(open(p, encoding="utf-8")), {"k": 1})
            self.assertTrue(any(issubclass(x.category, ResourceWarning) for x in w),
                            "老写法居然不告警——ResourceWarning 基线失效，上面的用例可能空转")

    def test_read_json_semantics_preserved(self):
        """语义等价性：缺文件/坏 JSON 必须照样抛（不能悄悄变 None 掩盖问题）"""
        with tempfile.TemporaryDirectory() as d:
            self.assertRaises(FileNotFoundError, self.m.read_json, Path(d) / "nope.json")
            bad = Path(d) / "bad.json"
            bad.write_text("{oops", encoding="utf-8")
            self.assertRaises(json.JSONDecodeError, self.m.read_json, bad)

    def test_read_json_safe_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self.m.read_json_safe(Path(d) / "nope.json"))
            self.assertEqual(self.m.read_json_safe(Path(d) / "nope.json", default={}), {})

    def test_sqlite_ro_closes_after_exception(self):
        """查询抛异常后连接必须关闭（原写法 close 在 try 内 → 异常即泄漏）。

        验证方式不依赖平台 API：异常退出后再开一个正常连接读同一库，
        若旧连接还占着 Windows 文件锁，新连接的写/删会失败。
        """
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            c = sqlite3.connect(str(db))
            c.execute("CREATE TABLE t(a)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
            c.close()
            for _ in range(50):
                with self.assertRaises(Exception):
                    with self.m.sqlite_ro(db) as con:
                        con.execute("SELECT * FROM 不存在的表")
            with self.m.sqlite_ro(db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_sqlite_ro_reads(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            c = sqlite3.connect(str(db))
            c.execute("CREATE TABLE t(a)")
            c.execute("INSERT INTO t VALUES (7)")
            c.commit()
            c.close()
            with self.m.sqlite_ro(db, immutable=True) as con:
                self.assertEqual(con.execute("SELECT a FROM t").fetchone()[0], 7)
            with self.m.sqlite_ro(db, immutable=False) as con:
                self.assertEqual(con.execute("SELECT a FROM t").fetchone()[0], 7)

    def test_write_json_atomic_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.json"
            self.m.write_json_atomic(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})
            self.m.write_json_atomic(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 2})
            self.assertEqual(list(Path(d).glob("*.tmp")), [], "原子写留下临时文件")


class TestLimitMatrix(unittest.TestCase):
    """撮合涨跌停矩阵（板块 + ST）—— 2026-10-15 修复的回归护栏"""

    def _acc(self):
        import tempfile as _tf
        sys.path.insert(0, str(BASE))
        import strategy.paper_account as pa
        return pa, pa.PaperAccount("__mt__", cash=10 ** 6,
                                   db_path=str(Path(tempfile.mkdtemp()) / "t.db"))

    def _buy(self, code, close, is_st=False, prev=10.0):
        pa, acc = self._acc()
        return acc.buy(code, 100, "2026-10-14", close=close, prev_close=prev,
                       day_volume=10 ** 7, is_st=is_st)

    def test_limit_helper_matrix(self):
        sys.path.insert(0, str(BASE))
        import strategy.paper_account as pa
        for code, want in [("600000.SH", 0.10), ("000001.SZ", 0.10), ("002594.SZ", 0.10),
                           ("300001.SZ", 0.20), ("301001.SZ", 0.20),
                           ("688001.SH", 0.20), ("689009.SH", 0.20),
                           ("430047.BJ", 0.30), ("830799.BJ", 0.30), ("920002.BJ", 0.30)]:
            self.assertAlmostEqual(pa._limit_pct(code), want, places=6, msg=f"{code} 限幅错误")
        # ST：自 2026-07-06 起与所在板块同幅
        self.assertAlmostEqual(pa._limit_pct("600000.SH", is_st=True), 0.10)
        self.assertAlmostEqual(pa._limit_pct("300001.SZ", is_st=True), 0.20)
        self.assertAlmostEqual(pa._limit_pct("688001.SH", is_st=True), 0.20)

    def test_main_board_rejects_at_10pct(self):
        self.assertFalse(self._buy("600000.SH", 11.0).get("ok"), "主板 +11% 应被拒")

    def test_gem_allows_10_to_20pct(self):
        for px in (10.9, 11.5, 11.9):
            self.assertTrue(self._buy("300001.SZ", px).get("ok"),
                            f"创业板 {px}（+{(px / 10 - 1):.0%}）属正常交易却被拒")

    def test_gem_rejects_at_20pct(self):
        self.assertFalse(self._buy("300001.SZ", 12.0).get("ok"), "创业板 +20% 应被拒")

    def test_star_allows_10_to_20pct(self):
        for px in (10.9, 11.5, 11.9):
            self.assertTrue(self._buy("688001.SH", px).get("ok"),
                            f"科创板 {px}（+{(px / 10 - 1):.0%}）属正常交易却被拒")

    def test_st_main_board_allows_5_to_10pct(self):
        """原 ST_LIMIT_PCT=0.05 的过期规则会把这一段全部误拒"""
        for px in (10.5, 10.8, 10.9):
            self.assertTrue(self._buy("600000.SH", px, is_st=True).get("ok"),
                            f"主板 ST {px}（+{(px / 10 - 1):.0%}）被误拒——ST 仍按旧的 ±5%")

    def test_bse_allows_up_to_30pct(self):
        self.assertTrue(self._buy("430047.BJ", 12.5).get("ok"), "北交所 +25% 应允许")

    def test_sell_side_uses_same_board_rules(self):
        """卖出侧跌停必须与买入侧同一套板块规则（原两处各抄一遍，改一处漏一处）"""
        pa, acc = self._acc()
        acc.buy("300001.SZ", 100, "2026-10-14", close=10.0, prev_close=10.0,
                day_volume=10 ** 7, is_st=False)
        # 创业板 -15%（在 ±20% 内）应允许卖出；旧规则按 -10% 会误拒
        r = acc.sell("300001.SZ", 100, "2026-10-15", close=8.5, prev_close=10.0, is_st=False)
        self.assertTrue(r.get("ok"), f"创业板 -15% 卖出被误拒：{r.get('msg')}")
        # -21%（超出 ±20%）必须拒
        r2 = acc.sell("300001.SZ", 100, "2026-10-16", close=7.9, prev_close=10.0, is_st=False)
        self.assertFalse(r2.get("ok"), "创业板 -21% 卖出未被拒")

    def test_board_rules_uniform_across_buy_and_sell(self):
        """buy 用的 _limit_pct 与 sell 用的必须是同一个函数（结构性保证）。

        用 _code_only 而非全文搜索：解释历史缺陷的注释里必然要提老常量名，
        字面搜全文会误报；assertTrue 的 failure message 也不会 dump 整份源码。
        """
        code = _code_only(BASE / "strategy" / "paper_account.py")
        self.assertTrue("ST_LIMIT_PCT" not in code, "又出现 ST_LIMIT_PCT 老常量（可执行代码里）")
        self.assertEqual(code.count("_limit_pct(code, is_st)"), 2,
                         "买/卖两处应各自调用一次 _limit_pct")


class TestCostModel(unittest.TestCase):
    """回测成本折算：按调仓频率，且不与回测窗口长度耦合"""

    def setUp(self):
        sys.path.insert(0, str(BASE))
        import backtest.bt_runner as B
        self.B = B

    def test_rebalance_days_mapping(self):
        self.assertEqual(self.B._rebalance_days("M"), 20)
        self.assertEqual(self.B._rebalance_days("Q"), 60)
        self.assertEqual(self.B._rebalance_days("W"), 5)
        self.assertEqual(self.B._rebalance_days(40), 40)

    def test_cost_not_hardcoded(self):
        code = _code_only(BASE / "backtest" / "bt_runner.py")
        self.assertTrue("cost * 2 * 60" not in code, "成本又变回硬编码 60 次/年")

    def test_cost_independent_of_window_length(self):
        code = _code_only(BASE / "backtest" / "bt_runner.py")
        self.assertTrue("cost * 2 * 60 / max(len(ret), 1)" not in code,
                        "成本又与 len(ret) 耦合——窗口长短会改变日均成本")

    def test_cost_drag_varies_with_rebalance(self):
        """换仓越频，日均成本拖累越大：季度 < 40日 < 月频 < 周频。
        （间隔 60 交易日 > 40 > 20 > 5，拖累 = 2×cost/间隔，故顺序相反）"""
        d = {rb: 2.0 * 0.00176 / self.B._rebalance_days(rb) for rb in ("Q", "M", 40, "W")}
        self.assertLess(d["Q"], d[40], "季度成本拖累竟大于 40 交易日")
        self.assertLess(d[40], d["M"], "40 交易日成本拖累竟大于月频")
        self.assertLess(d["M"], d["W"], "月频成本拖累竟大于周频")


class TestMissingDataError(unittest.TestCase):
    """bt_runner 数据缺失必须变成结构化结果，不是裸异常 + traceback 刷屏"""

    def setUp(self):
        sys.path.insert(0, str(BASE))
        import backtest.bt_runner as B
        self.B = B

    def test_missing_data_error_carries_locator(self):
        e = self.B.MissingDataError("/x/y.db", "financials_ts", "跑 zzz.py")
        self.assertEqual(e.db, "/x/y.db")
        self.assertEqual(e.table, "financials_ts")
        self.assertEqual(e.hint, "跑 zzz.py")
        self.assertIn("/x/y.db", str(e))
        self.assertIn("跑 zzz.py", str(e))

    def test_require_db_raises_typed_error(self):
        with self.assertRaises(self.B.MissingDataError):
            self.B._require_db("/definitely/not/here.db", "t", "hint")

    def test_require_db_passes_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "real.db"
            p.write_bytes(b"")
            self.B._require_db(str(p), "t", "hint")   # 不抛即通过

    def test_script1_returns_structured_failure(self):
        """script1 缺数据时：返回 ok:False + missing_db + hint，而不是抛异常"""
        r = self.B.run_backtest(strategy="script1", topn=5, stocks=50,
                                start="2023-01-01", end="2024-12-31")
        if r.get("ok"):
            self.skipTest("finance_ts.db 已补齐，script1 可跑")
        self.assertFalse(r.get("ok"))
        self.assertIn("hint", r, "失败结果缺补数指引——退回成不可诊断的裸错")
        self.assertTrue(r["hint"], "hint 为空")
        self.assertIn("missing_db", r, "失败结果缺 missing_db 定位")


if __name__ == "__main__":
    unittest.main(verbosity=2)
