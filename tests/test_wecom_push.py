# -*- coding: utf-8 -*-
"""tests/test_wecom_push.py — 企业微信推送脚本（scripts/wecom_push.py）回归护栏

覆盖：消息组装、字节截断（不切断多字节）、webhook 来源优先级、失败优雅降级、
发送错误不抛异常。全部离线（deck 与 webhook 均用桩替换），不产生真实网络请求。
"""
import importlib.util
import json
import os
import sqlite3
import types
import unittest
from pathlib import Path
from unittest import mock

from tests._env import BASE

_spec = importlib.util.spec_from_file_location("wecom_push", BASE / "scripts" / "wecom_push.py")
wq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wq)


def _args(webhook=None):
    return types.SimpleNamespace(webhook=webhook)


class TestTruncate(unittest.TestCase):
    def test_noop_when_under_limit(self):
        s = "短内容"
        self.assertEqual(wq.truncate(s, 100), s, "未超限时不应改动内容")

    def test_respects_byte_limit(self):
        s = "中" * 5000                     # 每字 3 字节 = 15000 字节
        out = wq.truncate(s, 4000)
        self.assertLessEqual(len(out.encode("utf-8")), 4000 + 40, "截断后应回到字节上限附近")
        self.assertTrue(out.endswith("（内容过长已截断）"), "截断应带提示后缀")

    def test_does_not_split_multibyte(self):
        """截断点若落在多字节字符中间会产生乱码 —— 必须按字符边界回退"""
        s = "中" * 5000
        out = wq.truncate(s, 4000)
        out.encode("utf-8").decode("utf-8")     # 能解码即未切断
        self.assertNotIn("\ufffd", out, "出现替换字符说明切断了多字节")


class TestWebhookResolve(unittest.TestCase):
    def test_priority_arg_over_env_over_cfg(self):
        cfg = {"webhook": "cfg-url"}
        with mock.patch.dict(os.environ, {"WECOM_WEBHOOK": "env-url"}):
            self.assertEqual(wq.resolve_webhook(_args("arg-url"), cfg)[0], "arg-url")
            self.assertEqual(wq.resolve_webhook(_args(None), cfg)[0], "env-url")
        self.assertEqual(wq.resolve_webhook(_args(None), cfg)[0], "cfg-url")

    def test_none_when_all_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            url, src = wq.resolve_webhook(_args(None), {})
        self.assertIsNone(url)
        self.assertIsNone(src)

    def test_blank_strings_ignored(self):
        with mock.patch.dict(os.environ, {"WECOM_WEBHOOK": "   "}):
            url, _ = wq.resolve_webhook(_args("  "), {})
        self.assertIsNone(url, "空白字符串不算已配置")


class TestFormatting(unittest.TestCase):
    def test_fmt_holdings_renders_positions(self):
        fixture = {"ok": True,
                   "pnl": {"total_ret": -0.0397, "n_holdings": 1, "rows": [
                       {"code": "001257.SZ", "name": "盛龙股份", "entry_price": 19.98,
                        "last_price": 19.15, "ret": -0.0415, "price_src": "realtime"}]},
                   "portfolio": {"max_positions": 3,
                                 "positions": [{"code": "001257.SZ", "target": 33.97, "stop": 17.98}]}}
        with mock.patch.object(wq, "deck_get", return_value=fixture), \
             mock.patch.object(wq, "paper_account", return_value=None):
            out = wq.fmt_holdings("http://x")
        self.assertIn("盛龙股份", out)
        self.assertIn("001257.SZ", out)
        self.assertIn("-4.15%", out, "应显示个股收益率")
        self.assertIn("🟢", out, "亏损应为绿色（中国习惯：跌=绿）")
        self.assertIn("仅收益率", out, "无股数时应注明只报收益率")

    def test_fmt_holdings_empty(self):
        with mock.patch.object(wq, "deck_get",
                               return_value={"ok": True, "pnl": {"rows": []}, "portfolio": {}}), \
             mock.patch.object(wq, "paper_account", return_value=None):
            self.assertIn("空仓", wq.fmt_holdings("http://x"))

    def test_fmt_pool_removed(self):
        """候选池已按需求下线 —— 推送内容里不该再出现候选池段"""
        self.assertFalse(hasattr(wq, "fmt_pool"), "fmt_pool 应已删除")
        self.assertFalse(hasattr(wq, "_otype_emoji"), "候选池专用 emoji 助手应一并删除")
        self.assertFalse(hasattr(wq, "_rank_emoji"), "候选池专用名次 emoji 应一并删除")
        with self.assertRaises(KeyError):
            wq.build("pool", "http://x")


class TestEmojiHelpers(unittest.TestCase):
    def test_pnl_emoji_used_for_coloring(self):
        self.assertEqual(wq._pnl_emoji(0.01), "🔴")
        self.assertEqual(wq._pnl_emoji(-0.01), "🟢")

    def test_layout_uses_v2_structure(self):
        """手机端排版：## 分节标题 + > 引用块放总账 + - 列表放个股（子项 2 空格嵌套）"""
        fixture = {"ok": True, "pnl": {"total_ret": 0.05, "rows": [
            {"code": "A", "name": "甲", "entry_price": 10.0, "last_price": 10.5,
             "ret": 0.05, "price_src": "realtime"}]},
            "portfolio": {"max_positions": 3,
                          "positions": [{"code": "A", "target": 12.0, "stop": 9.0}]}}
        with mock.patch.object(wq, "deck_get", return_value=fixture), \
             mock.patch.object(wq, "paper_account", return_value=None):
            out = wq.fmt_holdings("http://x")
        self.assertIn("## 💼 持仓", out, "分节应用 ## 标题")
        self.assertIn("> 📊 组合", out, "总账应用 > 引用块")
        self.assertIn("- **甲**", out, "个股应用 - 列表")
        self.assertIn("\n  - ", out, "策略位应 2 空格嵌套在个股下")

    def test_no_markdown_unsupported_syntax(self):
        """WeCom markdown 不支持缩进/列表/表格/分割线 —— 消息里不能出现这些"""
        fixture = {"ok": True, "pnl": {"total_ret": 0.05, "rows": [
            {"code": "A", "name": "甲", "entry_price": 10.0, "last_price": 10.5,
             "ret": 0.05, "price_src": "daily"}]},
            "portfolio": {"max_positions": 3, "positions": [{"code": "A", "target": 12.0, "stop": 9.0}]}}
        with mock.patch.object(wq, "deck_get", return_value=fixture), \
             mock.patch.object(wq, "paper_account", return_value=None):
            out = wq.fmt_holdings("http://x")
        self.assertNotIn("---", out, "分割线在 WeCom markdown 里不支持")
        self.assertNotIn("|", out, "表格在 WeCom markdown 里不支持")
        for ln in out.splitlines():
            self.assertFalse(ln.startswith("    "), f"半角缩进不渲染：{ln!r}")


class TestPnlColoring(unittest.TestCase):
    """盈亏展示：中国习惯（涨/盈利=红，跌/亏损=绿）+ 只报收益率不报金额"""

    def test_profit_red_loss_green(self):
        self.assertEqual(wq._pnl_emoji(0.05), "🔴", "盈利应为红色")
        self.assertEqual(wq._pnl_emoji(-0.05), "🟢", "亏损应为绿色")
        self.assertEqual(wq._pnl_emoji(0), "🔴", "持平按红处理")
        self.assertEqual(wq._pnl_emoji(None), "⚪", "无数据应为中性")

    def test_pct_signed(self):
        self.assertEqual(wq._pct(0.07), "+7.00%")
        self.assertEqual(wq._pct(-0.0415), "-4.15%")
        self.assertEqual(wq._pct(None), "—")

    def test_holdings_shows_portfolio_return_and_source(self):
        fixture = {"ok": True, "pnl": {"total_ret": -0.0397, "rows": [
            {"code": "A", "name": "甲", "entry_price": 10.0, "last_price": 9.6,
             "ret": -0.04, "price_src": "realtime"}]},
            "portfolio": {"max_positions": 3, "positions": [{"code": "A"}]}}
        with mock.patch.object(wq, "deck_get", return_value=fixture), \
             mock.patch.object(wq, "paper_account", return_value=None):
            out = wq.fmt_holdings("http://x")
        self.assertIn("组合", out)
        self.assertIn("-3.97%", out, "应显示组合收益率")
        self.assertIn("实时价", out, "应标注现价来源（实时价/收盘价）")

    def test_holdings_with_paper_account_shows_real_amounts(self):
        """有 paper.db（真实股数+现金）→ 报账户口径金额：净值=市值+现金，对比本金"""
        fixture = {"ok": True, "pnl": {"total_ret": -0.05, "rows": [
            {"code": "A", "name": "甲", "entry_price": 20.0, "last_price": 19.0,
             "ret": -0.05, "price_src": "realtime"}]},
            "portfolio": {"max_positions": 3, "positions": [{"code": "A", "stop": 18.0}]}}
        pa = {"name": "t", "initial_cash": 10000.0, "cash": 5000.0,
              "positions": {"A": {"qty": 200, "avg_cost": 20.0}}}
        with mock.patch.object(wq, "deck_get", return_value=fixture), \
             mock.patch.object(wq, "paper_account", return_value=pa):
            out = wq.fmt_holdings("http://x")
        # 市值 200×19=3800 · 成本 200×20=4000 · 现金 5000 · 净值 8800 · 盈亏 -1200（-12.00%）
        self.assertIn("200股", out, "应显示真实股数")
        self.assertIn("-¥1,200.00", out, "应显示账户金额盈亏")
        self.assertIn("-12.00%", out, "应显示账户收益率")
        self.assertIn("¥8,800.00", out, "应显示净值")
        self.assertNotIn("仅收益率", out, "有股数时不该再走退化文案")


class TestMoney(unittest.TestCase):
    def test_format(self):
        self.assertEqual(wq._money(1234.5), "¥1,234.50")
        self.assertEqual(wq._money(-300.7), "-¥300.70")
        self.assertEqual(wq._money(0), "¥0.00")
        self.assertEqual(wq._money(None), "—")


class TestPaperAccount(unittest.TestCase):
    """模拟账户读取（paper.db 只读）—— 金额盈亏的唯一真源（portfolio.json 不记股数）"""

    def _tmp_db(self):
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(p)
        con.executescript(
            "CREATE TABLE accounts(name TEXT, initial_cash REAL, cash REAL,"
            " start_date TEXT, created_at TEXT);"
            "CREATE TABLE positions(account TEXT, code TEXT, qty INTEGER,"
            " avg_cost REAL, entry_date TEXT);"
            "INSERT INTO accounts VALUES('sim_test',10000.0,3023.3024,'2026-10-15','2026-10-15 13:42:50');"
            "INSERT INTO positions VALUES('sim_test','001257.SZ',100,19.98,'2026-10-14');")
        con.commit()
        con.close()
        return p

    def test_reads_account_and_positions(self):
        p = self._tmp_db()
        try:
            with mock.patch.object(wq, "PAPER_DB", Path(p)):
                pa = wq.paper_account()
            self.assertIsNotNone(pa)
            self.assertEqual(pa["initial_cash"], 10000.0)
            self.assertEqual(pa["cash"], 3023.3024)
            self.assertEqual(pa["positions"]["001257.SZ"]["qty"], 100)
            self.assertEqual(pa["positions"]["001257.SZ"]["avg_cost"], 19.98)
        finally:
            os.unlink(p)

    def test_missing_db_returns_none(self):
        with mock.patch.object(wq, "PAPER_DB", Path("/nonexistent/definitely-nope.db")):
            self.assertIsNone(wq.paper_account(), "paper.db 缺失应返回 None 而非抛异常")


class TestBuildDegradation(unittest.TestCase):
    def test_degrades_when_one_source_fails(self):
        """日报缺失不应让整条推送失败 —— 应推其余内容并注明"""
        def fake_get(base, path, timeout=30):
            if path == "/api/daily_report":
                return {"ok": False, "error": "日报未生成"}
            return {"ok": True,
                    "pnl": {"rows": [{"code": "A", "name": "甲", "entry_price": 10.0,
                                      "last_price": 10.5, "ret": 0.05, "price_src": "daily"}]},
                    "portfolio": {"max_positions": 3, "positions": [{"code": "A"}]}}
        with mock.patch.object(wq, "deck_get", side_effect=fake_get), \
             mock.patch.object(wq, "paper_account", return_value=None):
            body = wq.build("all", "http://x")
        self.assertIn("持仓", body)
        self.assertIn("甲", body, "持仓成功时仍应推出去")
        self.assertNotIn("候选池", body, "候选池已下线")
        self.assertIn("部分内容不可用", body, "失败来源应被注明")
        self.assertIn("daily", body)

    def test_raises_when_all_sources_fail(self):
        with mock.patch.object(wq, "deck_get", side_effect=RuntimeError("deck 未运行")):
            with self.assertRaises(RuntimeError):
                wq.build("all", "http://x")


class TestSend(unittest.TestCase):
    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def read(self):
            return json.dumps(self._p).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def __init__(self, payload=None, exc=None):
            self._payload, self._exc, self.last_req = payload, exc, None

        def open(self, req=None, *a, **k):
            self.last_req = req
            if self._exc:
                raise self._exc
            return TestSend._Resp(self._payload)

    def test_success_on_errcode_0(self):
        with mock.patch.object(wq, "_opener", return_value=self._Opener({"errcode": 0, "errmsg": "ok"})):
            ok, detail = wq.send("http://x", "hi", False)
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")

    def test_defaults_to_markdown_v2(self):
        op = self._Opener({"errcode": 0, "errmsg": "ok"})
        with mock.patch.object(wq, "_opener", return_value=op):
            wq.send("http://x", "hi", False)
        payload = json.loads(op.last_req.data.decode("utf-8"))
        self.assertEqual(payload["msgtype"], "markdown_v2", "默认应为 markdown_v2（手机端真列表）")
        self.assertEqual(payload["markdown_v2"]["content"], "hi")

    def test_can_fall_back_to_legacy_markdown(self):
        op = self._Opener({"errcode": 0, "errmsg": "ok"})
        with mock.patch.object(wq, "_opener", return_value=op):
            wq.send("http://x", "hi", False, "markdown")
        payload = json.loads(op.last_req.data.decode("utf-8"))
        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(payload["markdown"]["content"], "hi")

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            wq.send("http://x", "hi", False, "html")

    def test_reports_error_without_raising(self):
        with mock.patch.object(wq, "_opener", return_value=self._Opener(exc=OSError("boom"))):
            ok, detail = wq.send("http://x", "hi", False)
        self.assertFalse(ok, "发送异常应转成 (False, 说明)，不能冒泡")
        self.assertIn("boom", detail)

    def test_reports_nonzero_errcode(self):
        with mock.patch.object(wq, "_opener", return_value=self._Opener({"errcode": 93000, "errmsg": "invalid webhook"})):
            ok, detail = wq.send("http://x", "hi", False)
        self.assertFalse(ok)
        self.assertIn("93000", detail)


class TestNoInboundSurface(unittest.TestCase):
    def test_script_opens_no_listening_socket(self):
        """方案① 的安全契约：脚本只做出站请求，不得出现任何 bind/listen 调用"""
        from tests._env import code_only
        code = code_only(BASE / "scripts" / "wecom_push.py")
        for bad in ("bind(", "listen(", "HTTPServer", "socketserver"):
            self.assertTrue(bad not in code, f"推送脚本不应出现 {bad}（方案① 零端口暴露）")


if __name__ == "__main__":
    unittest.main()
