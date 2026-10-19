# -*- coding: utf-8 -*-
"""tests/test_doc_claims.py — docs/资产盘点.md 的事实守卫

为什么需要这个文件：该文档 v3.0 曾出现多处"声称有、仓库里没有"的陈述
（"123+ 因子"实际 6 个、"Beneish 8 指标全量"实际 4 指标降级、"bars.db 1398 万行"实际 846 万）。
文档漂移不是靠"下次注意"能防住的，得靠用例。

本文件做两件事：
  ① 断言已被证伪的陈述**不会回到文档里**（防回退）；
  ② 断言文档里的关键数字**与仓库实际一致**（现实变了就红，提示同步更新文档）。

★现实变了怎么办：如果数据层补齐/删改导致下面 REALITY 里的数字变化，
  这不是"测试写错了"——请先更新 docs/资产盘点.md，再更新这里的 REALITY 常量。
"""
import re
import sqlite3
import sys
import unittest
from pathlib import Path

from tests._env import BASE

DOC = BASE / "docs" / "资产盘点.md"
CACHE = BASE / "data" / "cache"

# 2026-10-15 实测基线（改数据后需同步）
REALITY = {
    "bars_rows": 8477451,          # data/cache/bars.db daily_bar 行数（2026-10-18 补齐 10-16 后复核）
    "bars_codes": 5575,            # 覆盖股票数（2026-10-18 补齐 10-16 后复核）
    "panel_factors": 6,            # factors/factor_engine.FACTOR_FUNCS
    "strong_factors": 27,          # factors/risk/strong_factor_table.STRONG_TABLE
    "pool_rows": 54,               # data/cache/factor_pool.db factors 表（2026-10-15 登记）
    "project_skills": 14,          # assets/skills/
    "template_skills": 8,          # harness/home/skills/
    "beneish_total": 3908,         # risk/beneish.py --scan total
}

# 数据层「受追踪的库」与「当前仍缺失的库」（2026-10-15 晚实测）
# ★补齐新库时：把它从 KNOWN_MISSING_DBS 移出，并同步 docs/资产盘点.md §4.1。
#   本清单是「文档声称 vs 磁盘实际」的对账锚点，不是愿望清单。
ALL_TRACKED_DBS = ["finance_ts.db", "minute.db", "lhb_full.db",
                   "rzrq_full.db", "gdhs_full.db", "shebao.db"]
KNOWN_MISSING_DBS = ["minute.db"]          # 付费边界，未建

# 已证伪的陈述：不得再作为**当前事实**出现在正文里
DISPROVEN = [
    "123+ 因子",
    "123+因子",
    "1398万行",
    "5501 只",
    "factor-pool-methodology",
    "skill-library",
    "`factor_{name}` 注册即自动接入",
]

# 文档的行文约定（见 §0）——旧说法只允许出现在这三类位置：
#   ① 引用块（`>` 开头）：用于"旧写 X → 实测 Y"的对照
#   ② 代码块（``` 围栏内）：核实命令本身会包含旧词
#   ③ §9 问题清单：集中对账区，整段允许引用旧说法原文
# 正文散文里出现旧说法，即视为文档回退。
RECON_HEADING = "## 9."


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _asserted_lines() -> list:
    """返回"正文断言行" [(行号, 内容)]，即必须只说实测结论的那些行。

    为什么按结构判、而不按关键词判：关键词（"原文""声称""实际"…）无法可靠区分
    "引用旧说法并更正"与"把旧说法当事实陈述"——对账文档天然要引用旧说法，
    靠词表只会不停打补丁。改用结构后规则是可解释的，也不依赖措辞。
    """
    out = []
    in_fence = False
    in_recon = False
    for i, ln in enumerate(_doc().splitlines(), 1):
        s = ln.strip()
        if s.startswith("```"):                 # ② 代码块
            in_fence = not in_fence
            continue
        if s.startswith("## "):                 # 顶级小节切换：§9 起为对账区，下一节起退出
            in_recon = s.startswith(RECON_HEADING)
        if in_fence or in_recon:
            continue
        if s.startswith(">"):                   # ① 引用块
            continue
        out.append((i, ln))
    return out


class TestDocHasNoDisprovenClaims(unittest.TestCase):
    """① 防回退：证伪过的说法不能悄悄回来"""

    def test_doc_exists_and_has_status_legend(self):
        self.assertTrue(DOC.exists(), "docs/资产盘点.md 必须存在")
        text = _doc()
        for mark in ("✅", "⚠️", "❌"):
            self.assertIn(mark, text, f"文档应保留三态标记 {mark}（已落地/部分落地/未落地）")

    def test_disproven_claims_absent_from_body(self):
        """引用块 / 代码块 / §9 对账区里可以出现旧说法；正文散文里不行"""
        for lineno, ln in _asserted_lines():
            for bad in DISPROVEN:
                self.assertNotIn(
                    bad, ln,
                    f"第 {lineno} 行把已证伪的说法写进了正文：{bad!r}\n"
                    f"    {ln.strip()[:110]}\n"
                    f"    → 改写为实测结论，或移进引用块 / §9 问题清单")

    def test_reconciliation_section_still_present(self):
        """§9 是对账区，也是上面那条规则的豁免依据——不能被删掉"""
        self.assertIn(RECON_HEADING, _doc(), "§9 问题清单不得删除（它是旧说法的唯一豁免区）")

    def test_problem_list_not_trimmed(self):
        """19 条问题清单是对账的产出，删条目来让文档好看属于回退"""
        text = _doc()
        for n in range(1, 20):
            self.assertIn(f"| {n} |", text, f"§9 问题清单第 {n} 条不见了")


class TestDocNumbersMatchReality(unittest.TestCase):
    """② 文档数字 vs 仓库实际"""

    def test_bars_db_size_documented_correctly(self):
        con = sqlite3.connect(f"file:{CACHE / 'bars.db'}?mode=ro&immutable=1", uri=True)
        try:
            rows = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
            codes = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bar").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(rows, REALITY["bars_rows"],
                         "bars.db 行数变了 —— 请同步更新 docs/资产盘点.md §4.1 与 REALITY")
        self.assertEqual(codes, REALITY["bars_codes"], "bars.db 覆盖股票数变了，请同步文档")
        self.assertIn("846万行", _doc(), "文档应写明实测 846 万行")

    def test_panel_factor_count_documented(self):
        sys.path.insert(0, str(BASE))
        from factors.factor_engine import FACTOR_FUNCS
        self.assertEqual(len(FACTOR_FUNCS), REALITY["panel_factors"],
                         "面板因子数变了 —— 请同步更新文档 §6")
        self.assertIn("面板可算因子", _doc())

    def test_strong_factor_table_count_documented(self):
        sys.path.insert(0, str(BASE))
        from factors.risk.strong_factor_table import STRONG_TABLE
        self.assertEqual(len(STRONG_TABLE), REALITY["strong_factors"],
                         "名义强因子数变了 —— 请同步更新文档 §6.1")
        self.assertIn(f"**{REALITY['strong_factors']}**", _doc(),
                      "文档 §6 应写明名义强因子数")

    def test_factor_pool_registration_disclosed(self):
        """因子池 2026-10-15 从 0 行登记到 54 个 —— 文档必须写真实数量。

        这条原来守的是"文档要披露空池"；池被填上后不能让它静默 skip（守卫会失效），
        而要改成守"文档与池的真实数量一致"。
        """
        con = sqlite3.connect(f"file:{CACHE / 'factor_pool.db'}?mode=ro&immutable=1", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM factors").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(
            n, REALITY["pool_rows"],
            f"因子池登记数 {n} ≠ 文档基线 {REALITY['pool_rows']} —— "
            f"请同步 docs/资产盘点.md §6.3 与这里的 REALITY")
        self.assertIn(f"**{REALITY['pool_rows']}**", _doc(),
                      "文档 §6 应写明因子池已登记的数量")
        self.assertIn("6.3", _doc(), "文档应有 §6.3 记录因子池登记")

    def test_pool_work_does_not_regress_to_empty_claim(self):
        """防止把"已登记"又写回"空表"（那是最容易发生的回退）

        ★2026-10-18 修正作用域：原实现扫全文 `assertNotIn("表 0 行")`，误伤 §9 对账区里
        「`pools.db` 的 `pools` 表 0 行」——那是**另一个库**的真实陈述，不是因子池回退。
        改为与本文件其他用例一致：只扫正文断言行（排除引用块/代码块/§9 对账区），
        并把模式收窄到因子池专属。**不是放宽**：正文里出现"因子池 0 行"仍然会红。
        """
        for lineno, ln in _asserted_lines():
            for bad in ("`factors` 表 **0 行**", "表 0 行"):
                self.assertNotIn(
                    bad, ln,
                    f"第 {lineno} 行把因子池写回空表：{ln.strip()[:110]}")

    def test_missing_databases_disclosed(self):
        """数据层每个库都必须在文档 §4.1 被点名；且"缺失清单"必须与现实一致

        2026-10-15 晚：finance_ts / lhb_full / rzrq_full / gdhs_full / shebao
        五个库已由 data/build_finance_ts.py 与 data/build_event_dbs.py 建出，
        只剩 minute.db（付费边界）。补齐新库时把库名从 KNOWN_MISSING 移出即可。
        """
        text = _doc()
        for f in ALL_TRACKED_DBS:
            self.assertIn(f, text, f"文档 §4.1 应明确列出数据层文件 {f}")
        actually_missing = sorted(f for f in ALL_TRACKED_DBS if not (CACHE / f).exists())
        self.assertEqual(actually_missing, sorted(KNOWN_MISSING_DBS),
                         "缺失库清单变了 —— 请同步更新 docs/资产盘点.md §4.1 与 KNOWN_MISSING_DBS")

    def test_skill_counts_documented(self):
        proj = len([d for d in (BASE / "assets" / "skills").iterdir() if d.is_dir()])
        tmpl = len([d for d in (BASE / "harness" / "home" / "skills").iterdir() if d.is_dir()])
        self.assertEqual(proj, REALITY["project_skills"], "项目 skill 数变了 —— 请同步文档 §8.1")
        self.assertEqual(tmpl, REALITY["template_skills"], "随包模板数变了 —— 请同步文档 §8.2")
        self.assertIn(f"**实测 {proj} 个**", _doc(), "文档 §8.1 应写明实测 skill 数")


class TestDocDisclosesKnownGaps(unittest.TestCase):
    """③ 关键缺口必须在文档里被点名，不能只写优点"""

    def test_discloses_atr_stop_shell(self):
        self.assertIn("ATR 未生效", _doc(), "文档必须点名 ATR 止损是空壳")

    def test_discloses_beneish_degraded(self):
        text = _doc()
        self.assertIn("降级", text, "文档必须说明 Beneish 是降级版")
        self.assertIn("4/8", text, "文档应写明 4/8 指标")

    def test_discloses_broken_evidence_chain(self):
        text = _doc()
        self.assertIn("17年回测验证报告v2_20260810.md", text,
                      "文档应点名证据链引用了一个不存在的文件")
        self.assertFalse((BASE / "logs" / "17年回测验证报告v2_20260810.md").exists(),
                         "该报告若已被找回，请更新文档 §9.3")

    def test_discloses_concentration_gap(self):
        text = _doc()
        self.assertIn("集中度", text, "文档必须点名组合层缺集中度控制")


if __name__ == "__main__":
    unittest.main()
