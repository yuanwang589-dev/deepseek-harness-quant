# -*- coding: utf-8 -*-
"""risk/data_audit.py — 数据审计（风控前置闸门 · 长期架构）

定位（主文档 4.5③ 数据不可信则策略不可信）：
  策略/回测的错误若源于数据幻觉（未来函数 / 幸存者偏差 / 价格错误 / ST 标记失效），
  风控层无法兜底 —— 数据审计是第一道防线，与 risk_agent（第二道）级联：

      dev_auto 每日调度 ──> DataAuditor.run() ──> 健康度评分
                                   │ FAIL（strict 模式）
                                   ▼
                            阻断策略/回测执行（写 STOP.md 熔断）

设计原则：
1. ★只读审计：绝不修改任何数据文件（修复走独立脚本）
2. ★SQL 聚合 / 集合运算：禁止在 828 万行上做逐行循环（2026-08-07 事故：361 次 LIKE
   全表扫描导致 2 分钟超时被杀）→ 所有检查项在 SQLite 侧聚合或一次性取集合
3. ★检查项注册制：每项独立方法 + 元数据，新增检查只需加一个方法
4. ★阈值全部走 params.yaml data_audit 段，改配置不改代码
5. ★审计即文档：每次运行输出 report/data_audit_report.{md,json}，可追溯历史健康度

用法：
  python risk/data_audit.py                 # 全量审计 + 报告 + 闸门结论
  python risk/data_audit.py --quick         # 轻量审计（dev_auto 每轮调用，只跑关键项）
  python risk/data_audit.py --json          # 只输出 JSON 供程序消费

已知数据缺陷（2026-08-07 首次审计确认，修复清单见 data/fix_st_flags.py 与待办队列）：
  F-1 [P0] is_st 全 0：baostock 返回 '0'/'1'，fetcher map 字典用 'True'/'False' → 全部填 0
           → filter_st 形同虚设。代码已修复，数据待全量重拉（data/fix_st_flags.py）
  F-2 [P0] 幸存者偏差：2019 年后终止上市 148 只全部缺失（600068 等 2019-2021 在市无数据）
           → 回测池只剩"活着的股票"，收益虚高。待补拉（待办队列 ★项）
  F-3 [P1] backtest.start=2010 但数据 2019-01-01 起（bulk_loader START_DATE=2019-01-01）
           → 2010-2018 基本空转。已同步改 params（见 data_audit 备注），待补拉或保持 2019
  F-4 [P1] bar_meta.rows 记录"最后写入段"而非累计覆盖 → covers() 覆盖率判断不可信（16 只漂移）
  F-5 [P2] 财报 PIT 用固定延迟近似（无 ann_date 披露日），设计为 tushare 源但实现用同花顺
  F-6 [P2] qfq 前复权以查询日为基准 → 历史价随分红再查询而变，回测不可精确复现（验收用 hfq/none 对照）
"""
# ★2026-11-26 静默异常统一记录（自动注入）
try:
    from dsh_log import swallow as _dsh_swallow
except ImportError:
    import sys as _dsh_sys
    from pathlib import Path as _dsh_Path
    for _d in [_dsh_Path(__file__).resolve().parent] + list(_dsh_Path(__file__).resolve().parents):
        if (_d / 'dsh_log.py').exists():
            _dsh_sys.path.insert(0, str(_d))
            break
    from dsh_log import swallow as _dsh_swallow
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _load_config() -> dict:
    """读 params.yaml 的 data_audit 段（不存在则用默认）"""
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
        return (cfg or {}).get("data_audit", {}) or {}
    except Exception:
        return {}


_DEFAULT_CFG = {
    "enabled": True,
    "strict": True,                 # 任一 FAIL → 阻断策略/回测
    "warn_block": False,            # 可选：WARN 数过多也阻断
    "warn_block_count": 6,
    "report_dir": "report",
    "thresholds": {
        "min_rows_per_stock": 100,      # A3: 少于该行数视为短数据
        "max_pct_deviation": 0.5,       # C2: pct_chg 与 close/preclose 允许偏差(pp)
        "price_limit_over": 21.0,       # C4: 超过该涨跌幅视为超限（A股任何板块任何时期上限）
        "st_ratio_min": 0.01,           # C5: is_st 非零占比低于该值 → 标记失效
        "amount_ratio_range": [0.9, 1.5],  # D1: amount/(volume*close) 中位数合理区间
        "finance_null_pct_max": 1.0,    # E2: 核心字段缺失率上限(%)
        "sq_calc_coverage_min": 90.0,   # E3: 单季自算覆盖率下限(%)
        "delisted_coverage_min": 95.0,  # A2: 2019后退市股缓存覆盖率下限(%)
        "c1_ohlc_warn_max": 100,        # ★H18（外包 08-11）：C1 OHLC 违规行数 ≤ 该值 → WARN（历史噪音容忍，
                                        #   如 920489.BJ 老三板 2014 年 4 行）；> 该值 → FAIL 阻断
    },
    "data_start_note": "缓存数据起点 2019-01-01（bulk_loader START_DATE）；backtest.start 已同步为 2019-01-01",
}

_STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}


class AuditItem:
    """单项检查结果"""

    def __init__(self, check_id, category, name, status, detail, suggestion=""):
        self.id = check_id
        self.category = category
        self.name = name
        self.status = status
        self.detail = detail
        self.suggestion = suggestion

    def to_dict(self):
        return {"id": self.id, "category": self.category, "name": self.name,
                "status": self.status, "detail": self.detail, "suggestion": self.suggestion}


class DataAuditor:
    """数据审计器：检查项注册制，全部只读"""

    def __init__(self, config: dict = None):
        self.cfg = {**_DEFAULT_CFG, **(config or {})}
        th = self.cfg.setdefault("thresholds", {})
        self.th = {**_DEFAULT_CFG["thresholds"], **th}
        cache_dir = self._resolve_cache_dir()
        self.bars_db = cache_dir / "bars.db"
        self.fin_db = cache_dir / "finance.db"
        self.hist_mv_db = cache_dir / "hist_mv.db"
        self.delisted_csv = cache_dir / "delisted_list.csv"
        self.items: list[AuditItem] = []
        self._start_ts = time.time()

    # ---------------- 基础设施 ----------------
    @staticmethod
    def _resolve_cache_dir() -> Path:
        env = os.environ.get("LWQUANT_CACHE_DIR")
        if env:
            return Path(env)
        try:
            import yaml
            cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
            d = (cfg or {}).get("data", {}).get("cache_dir")
            if d:
                p = Path(str(d))
                return p if p.is_absolute() else BASE / p
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "risk/data_audit.py:134 _resolve_cache_dir")
        return BASE / "data" / "cache"

    def _conn(self, db: Path):
        return sqlite3.connect(str(db))

    def _q(self, cur, sql, args=()):
        """单值查询；异常返回 None 不抛"""
        try:
            return cur.execute(sql, args).fetchone()[0]
        except Exception as e:
            return f"ERR:{type(e).__name__}:{e}"

    def _add(self, check_id, category, name, status, detail, suggestion=""):
        self.items.append(AuditItem(check_id, category, name, status, detail, suggestion))

    @staticmethod
    def _norm6(code: str) -> str:
        """'600519.SH'/'sh.600519'/'600519' → '600519'；指数 'SH.000300' → 'SH.000300'"""
        s = str(code).strip().upper()
        if s.startswith(("SH.", "SZ.", "BJ.")):
            return s
        if "." in s:
            left, right = s.split(".", 1)
            return left.zfill(6) if right in ("SH", "SZ", "BJ") else right.zfill(6)
        return s.zfill(6)

    # ---------------- A. 完整性 ----------------
    def check_completeness(self):
        con = self._conn(self.bars_db)
        cur = con.cursor()
        n_total = self._q(cur, "SELECT COUNT(*) FROM daily_bar")
        n_codes = self._q(cur, "SELECT COUNT(DISTINCT code) FROM daily_bar")
        n_meta = self._q(cur, "SELECT COUNT(*) FROM bar_meta")
        self._add("A1", "完整性", "全表规模", "PASS" if n_total and n_total > 5_000_000 else "WARN",
                  f"daily_bar {n_total:,} 行 / {n_codes} 只 / bar_meta {n_meta} 条")

        diff = self._q(cur, """SELECT COUNT(*) FROM bar_meta m WHERE m.rows !=
            (SELECT COUNT(*) FROM daily_bar b WHERE b.code=m.code AND b.adjust=m.adjust)""") or 0
        self._add("A2", "完整性", "bar_meta 行数漂移",
                  "WARN" if diff else "PASS",
                  f"{diff} 只 meta.rows 与实际不符（meta=最后写入段，非累计覆盖）→ covers() 不可全信",
                  "F-4: 修改 put_daily 累计 meta 或标注语义")

        # A3 退市股覆盖（集合一次比对，禁止逐只 LIKE）
        recent_delisted = set()
        n_delist = 0
        if self.delisted_csv.exists():
            with open(self.delisted_csv, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    end = (row.get("终止上市日期") or "").strip()
                    if end and end >= "2019-01-01":
                        recent_delisted.add(self._norm6(row.get("code") or row.get("公司代码") or ""))
                    n_delist += 1
        cached6 = set()
        if n_codes:
            rows = cur.execute("SELECT DISTINCT code FROM daily_bar").fetchall()
            cached6 = {self._norm6(r[0]) for r in rows}
        missing = recent_delisted - cached6
        cov = (len(recent_delisted) - len(missing)) / len(recent_delisted) * 100 if recent_delisted else 100.0
        status = "PASS" if cov >= self.th["delisted_coverage_min"] else "WARN"
        self._add("A3", "完整性", "退市股覆盖（幸存者偏差）", status,
                  f"2019 后终止上市 {len(recent_delisted)} 只，缓存覆盖 {cov:.1f}%（缺 {len(missing)} 只，"
                  f"样本: {sorted(list(missing))[:5]}）→ 回测收益虚高风险",
                  "F-2: 补拉 148 只退市股（2019-退市日）后重跑 v3 验收")

        few = cur.execute("SELECT COUNT(*) FROM (SELECT code FROM daily_bar GROUP BY code HAVING COUNT(*)<?)",
                          (self.th["min_rows_per_stock"],)).fetchone()[0]
        self._add("A4", "完整性", "短数据股票", "WARN" if few > 5 else "PASS",
                  f"行数 <{self.th['min_rows_per_stock']} 的股票 {few} 只（多为次新股/退市边缘）")

        y0 = self._q(cur, "SELECT COUNT(*) FROM (SELECT code, MIN(date) md FROM daily_bar GROUP BY code) WHERE md < '2019-01-01'") or 0
        pct_pre2019 = y0 / n_codes * 100 if n_codes else 0
        self._add("A5", "完整性", "数据起点覆盖", "PASS" if pct_pre2019 >= 90 else "WARN",
                  f"起点早于 2019-01-01 的股票 {y0} 只（{pct_pre2019:.0f}%）→ 2010-2018 基本空转，"
                  f"backtest.start 需与实际一致", self.cfg.get("data_start_note", ""))
        con.close()

    # ---------------- B. 一致性 ----------------
    def check_consistency(self):
        con = self._conn(self.bars_db)
        cur = con.cursor()
        dup = self._q(cur, "SELECT COUNT(*) FROM (SELECT code,date,adjust FROM daily_bar GROUP BY 1,2,3 HAVING COUNT(*)>1)")
        self._add("B1", "一致性", "重复行", "FAIL" if dup else "PASS", f"{dup} 组 (code,date,adjust) 重复")

        idx = cur.execute("""SELECT DISTINCT code FROM daily_bar WHERE code LIKE '399%'
            OR code LIKE 'sh.%' OR code LIKE 'sz.%' OR code LIKE 'SH.%' OR code LIKE 'SZ.%'
            OR code LIKE '000300%'""").fetchall()
        self._add("B2", "一致性", "指数混入", "WARN" if len(idx) > 2 else "PASS",
                  f"{len(idx)} 只非股票代码: {[i[0] for i in idx[:5]]}")

        wknd = self._q(cur, "SELECT COUNT(*) FROM daily_bar WHERE strftime('%w', date) IN ('0','6')") or 0
        self._add("B3", "一致性", "周末日期", "FAIL" if wknd else "PASS", f"{wknd} 行落在周末")

        future = self._q(cur, "SELECT COUNT(*) FROM daily_bar WHERE date > ?", (datetime.now().strftime("%Y-%m-%d"),)) or 0
        self._add("B4", "一致性", "未来日期", "FAIL" if future else "PASS", f"{future} 行 date 晚于今天")

        adj = dict(cur.execute("SELECT adjust, COUNT(*) FROM daily_bar GROUP BY adjust").fetchall())
        self._add("B5", "一致性", "adjust 分布", "PASS",
                  "; ".join(f"{k}={v:,}" for k, v in adj.items()))
        con.close()

    # ---------------- C. 价格合理性 ----------------
    def check_price(self):
        con = self._conn(self.bars_db)
        cur = con.cursor()
        bad = self._q(cur, """SELECT COUNT(*) FROM daily_bar WHERE high < max(open,close)
            OR low > min(open,close) OR high < low OR open<=0 OR high<=0 OR low<=0 OR close<=0""") or 0
        # ★H18（外包 08-11 诊断）：全表 strict FAIL 被 4 行老三板历史噪音（920489.BJ 2014）
        #   一刀切阻断——按违规行数阈值化：微量（≤c1_ohlc_warn_max）→ WARN（历史容忍），大量 → FAIL
        _c1_max = self.th.get("c1_ohlc_warn_max", 100)
        _status = "FAIL" if bad > _c1_max else ("WARN" if bad else "PASS")
        self._add("C1", "价格", "OHLC 关系/正数", _status,
                  f"{bad} 行违反 OHLC 或价格非正（阈值 {_c1_max} 行内为 WARN 容忍，F-7 老三板历史噪音）")

        dev = self._q(cur, """SELECT COUNT(*) FROM daily_bar WHERE preclose>0 AND pct_chg IS NOT NULL
            AND abs(pct_chg - (close/preclose-1)*100) > ?""", (self.th["max_pct_deviation"],)) or 0
        self._add("C2", "价格", "pct_chg 一致性", "WARN" if dev else "PASS",
                  f"{dev} 行 pct_chg 与 close/preclose 偏差 >{self.th['max_pct_deviation']}pp")

        neg = self._q(cur, "SELECT COUNT(*) FROM daily_bar WHERE volume<0 OR amount<0 OR turn<0") or 0
        self._add("C3", "价格", "负量额", "FAIL" if neg else "PASS", f"{neg} 行 volume/amount/turn 为负")

        # C4 涨跌幅超限：新股上市前 5 日无涨跌幅限制（创业板 2020-08/科创板 2019-07/主板注册制 2023-02），
        # 恢复上市首日亦无限制（停牌数月至数年）→ 双条件：此前 ≥10 个真实交易日 且 前一日真实交易在 5 自然日内
        # ★2026-08-12 十轮#173：preclose < 0.05 视为源数据异常（920139.BJ 2015-03 复牌 preclose=0.01 假值，
        #   pct_chg 算 153300%——非真实涨跌，排除后 WARN 收敛）
        lim = self.th["price_limit_over"]
        over = cur.execute(f"""SELECT code,date,pct_chg FROM daily_bar d WHERE abs(pct_chg)>{lim}
            AND preclose >= 0.05
            AND (SELECT COUNT(*) FROM daily_bar
                 WHERE code=d.code AND adjust=d.adjust AND date<d.date AND volume>0) >= 10
            AND (SELECT MAX(date) FROM daily_bar
                 WHERE code=d.code AND adjust=d.adjust AND date<d.date AND volume>0) >= date(d.date, '-5 day')
            ORDER BY abs(pct_chg) DESC LIMIT 8""").fetchall()
        self._add("C4", "价格", "涨跌幅超限", "WARN" if over else "PASS",
                  f"上市满10日后仍 |pct_chg|>{lim}% 共 {len(over)} 只（样本: {[f'{c}@{d}:{p:.0f}%' for c,d,p in over]}）"
                  if over else "0 行异常（原始 202 行超限均为上市/恢复上市初期无涨跌幅限制期，正常）")

        # C6 停牌填充 bar（baostock 停牌日填充：close 不变、量额=0；pct_chg 有 NULL 和 0.0 两种形态）
        n_none = self._q(cur, "SELECT COUNT(*) FROM daily_bar WHERE volume=0 OR pct_chg IS NULL") or 0
        n_total2 = self._q(cur, "SELECT COUNT(*) FROM daily_bar") or 1
        self._add("C6", "价格", "停牌填充 bar", "PASS" if n_none / n_total2 < 0.01 else "WARN",
                  f"{n_none} 行（{n_none/n_total2*100:.2f}%）为停牌填充（量额=0、pct_chg=NULL/0.0）"
                  f"→ 因子计算需跳过 volume=0 的日子")

        st = self._q(cur, "SELECT COUNT(*) FROM daily_bar WHERE is_st != 0") or 0
        st_ratio = st / 8282007 if False else (st / (self._q(cur, "SELECT COUNT(*) FROM daily_bar") or 1))
        status = "FAIL" if st_ratio < self.th["st_ratio_min"] else "PASS"
        self._add("C5", "价格", "ST 标记有效性", status,
                  f"is_st≠0 共 {st} 行（占比 {st_ratio*100:.4f}%，阈值 {self.th['st_ratio_min']*100:.0f}%）"
                  f"→ ST 标记疑似失效，filter_st 形同虚设",
                  "F-1: fetcher map 字典 bug 已修，待 data/fix_st_flags.py 全量重拉")
        con.close()

    # ---------------- D. 量价一致性 ----------------
    def check_volume(self):
        con = self._conn(self.bars_db)
        cur = con.cursor()
        lo, hi = self.th["amount_ratio_range"]
        # 量价比受除权日影响（qfq 价×原始量≠amount）→ 过滤极端值(0.5~3)后求均值抗噪
        med = self._q(cur, """SELECT AVG(r) FROM (
            SELECT amount/(volume*close) r FROM daily_bar
            WHERE amount>0 AND volume>0 AND close>0 AND adjust='qfq'
            AND date>='2024-01-01' AND amount/(volume*close) BETWEEN 0.5 AND 3.0)""")
        status = "PASS" if med and lo <= float(med) <= hi else "WARN"
        self._add("D1", "量价", "amount 单位校验", status,
                  f"近 3 年量价比均值 {float(med):.3f}（合理区间 {lo}~{hi}，超出即疑似单位幻觉）"
                  f"→ amount 为元/volume 为股，v3 成交额过滤可用" if med else "无法计算")
        con.close()

    # ---------------- E. 财报审计 ----------------
    def check_finance(self):
        if not self.fin_db.exists():
            self._add("E1", "财报", "财报库存在", "WARN", "finance.db 不存在")
            return
        con = self._conn(self.fin_db)
        cur = con.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        n = self._q(cur, "SELECT COUNT(*) FROM finance_report") or 0
        codes = self._q(cur, "SELECT COUNT(DISTINCT code) FROM finance_report") or 0
        self._add("E1", "财报", "规模", "PASS" if n else "WARN", f"{n:,} 行 / {codes} 只")

        future = self._q(cur, "SELECT COUNT(*) FROM finance_report WHERE period > ?", (today,)) or 0
        self._add("E2", "财报", "未来报告期", "FAIL" if future else "PASS", f"{future} 行 period 晚于今天")

        nulls = self._q(cur, "SELECT COUNT(*) FROM finance_report WHERE net_profit IS NULL OR revenue IS NULL") or 0
        pct = nulls / n * 100 if n else 0
        self._add("E3", "财报", "核心字段缺失", "WARN" if pct > self.th["finance_null_pct_max"] else "PASS",
                  f"{nulls} 行（{pct:.2f}%）net_profit/revenue 为空")

        sqz = self._q(cur, """SELECT COUNT(*) FROM finance_report
            WHERE (sq_net_profit IS NULL OR sq_net_profit=0) AND net_profit IS NOT NULL AND net_profit!=0""") or 0
        cov = (1 - sqz / n) * 100 if n else 0
        self._add("E4", "财报", "单季自算覆盖率", "WARN" if cov < self.th["sq_calc_coverage_min"] else "PASS",
                  f"自算覆盖率 {cov:.1f}%（{sqz} 行有累计无单季）")

        # E5 报告期格式：合法为 03-31/06-30/09-30/12-31（中报/三季报为 30 日，勿用 31 判定）
        bad5y = self._q(cur, """SELECT COUNT(*) FROM finance_report WHERE period >= '2021-01-01'
            AND substr(period,6,5) NOT IN ('03-31','06-30','09-30','12-31')""") or 0
        self._add("E5", "财报", "近5年报告期格式", "WARN" if bad5y else "PASS",
                  f"{bad5y} 行非季度末（2021+，历史 2002 前半年报已豁免）")

        # E6 PIT 披露日提示（非阻断）
        self._add("E6", "财报", "PIT 披露日", "WARN",
                  "无 ann_date 披露日字段，用固定延迟近似（一季报4-30/中报8-31/三季报10-31/年报4-30）",
                  "F-5: 切 tushare 源（含 ann_date）或维持近似并文档化")
        con.close()

    # ---------------- F. PIT 状态 ----------------
    def check_pit(self):
        if self.hist_mv_db.exists():
            con = self._conn(self.hist_mv_db)
            cur = con.cursor()
            n = self._q(cur, "SELECT COUNT(*) FROM hist_mv") or 0
            months = self._q(cur, "SELECT COUNT(DISTINCT month) FROM hist_mv") or 0
            con.close()
            status = "WARN" if n == 0 else "PASS"
            self._add("F1", "PIT", "历史市值", status,
                      f"hist_mv {n} 行 / {months} 个月 → 拉取{'进行中，v3 仍为快照口径' if n==0 else '完成，可验收'}"
                      if n == 0 else f"{n} 行 / {months} 个月")
        else:
            self._add("F1", "PIT", "历史市值", "WARN", "hist_mv.db 不存在")

        cache_dir = self.bars_db.parent
        mv = {p.name: p.stat().st_size for p in cache_dir.glob("circ_mv*.csv")}
        self._add("F2", "PIT", "市值映射文件", "PASS" if mv else "WARN", f"{mv}")

    # ---------------- 主流程 ----------------
    def run(self, quick=False):
        self.items = []
        self.check_completeness()
        self.check_consistency()
        self.check_price()
        self.check_volume()
        if not quick:
            self.check_finance()
            self.check_pit()

        n = len(self.items)
        score_map = {"PASS": 1.0, "WARN": 0.5, "FAIL": 0.0}
        total = sum(score_map[i.status] for i in self.items)
        health = total / n * 100 if n else 0
        n_pass = sum(1 for i in self.items if i.status == "PASS")
        n_warn = sum(1 for i in self.items if i.status == "WARN")
        n_fail = sum(1 for i in self.items if i.status == "FAIL")
        fails = [i.name for i in self.items if i.status == "FAIL"]

        # 闸门判定
        blocked = False
        reason = ""
        if n_fail and self.cfg.get("strict", True):
            blocked = True
            reason = f"数据审计 FAIL {n_fail} 项（{fails}）→ 数据不可信，阻断策略/回测执行"
        elif self.cfg.get("warn_block") and n_warn >= self.cfg.get("warn_block_count", 6):
            blocked = True
            reason = f"数据审计 WARN {n_warn} 项 ≥ 阈值 → 阻断"

        result = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - self._start_ts, 1),
            "quick": quick,
            "health": round(health, 1),
            "n_items": n,
            "n_pass": n_pass, "n_warn": n_warn, "n_fail": n_fail,
            "fails": fails,
            "blocked": blocked,
            "block_reason": reason,
            "items": [i.to_dict() for i in self.items],
        }
        return result

    def gate(self):
        """供策略/回测前置调用：False 表示数据不可信，必须阻断"""
        r = self.run(quick=True)
        return not r["blocked"], r

    def save_report(self, result: dict) -> tuple[Path, Path]:
        out_dir = BASE / self.cfg.get("report_dir", "report")
        out_dir.mkdir(parents=True, exist_ok=True)
        # ★2026-08-10 写保护修复：固定名多次写被环境锁 → 时间戳文件名（读取方 glob 取最新）
        _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = out_dir / f"data_audit_report_{_ts}.md"
        json_path = out_dir / f"data_audit_report_{_ts}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [f"# 数据审计报告 · {result['ts']}",
                 "",
                 f"- 健康度: **{result['health']} / 100**（{result['n_items']} 项: "
                 f"PASS {result['n_pass']} / WARN {result['n_warn']} / FAIL {result['n_fail']}）",
                 f"- 耗时: {result['elapsed_sec']}s（quick={result['quick']}）",
                 f"- 闸门: {'🔴 阻断' if result['blocked'] else '🟢 放行'} — {result['block_reason'] or '数据可信'}",
                 ""]
        cur_cat = None
        for i in result["items"]:
            if i["category"] != cur_cat:
                cur_cat = i["category"]
                lines.append(f"\n## {cur_cat}")
                lines.append("")
            lines.append(f"- [{i['status']}] **{i['name']}**: {i['detail']}")
            if i["suggestion"]:
                lines.append(f"  - 建议: {i['suggestion']}")
        lines.append("\n---\n\n*本报告由 risk/data_audit.py 自动生成，阈值见 config/params.yaml data_audit 段*")
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return md_path, json_path


def main():
    ap = argparse.ArgumentParser(description="数据审计（风控前置闸门）")
    ap.add_argument("--quick", action="store_true", help="轻量审计（dev_auto 每轮用）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    auditor = DataAuditor(_load_config())
    result = auditor.run(quick=args.quick)
    md_path, json_path = auditor.save_report(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"数据审计完成: 健康度 {result['health']}/100 "
              f"(PASS {result['n_pass']}/WARN {result['n_warn']}/FAIL {result['n_fail']}) "
              f"[{result['elapsed_sec']}s]")
        for i in result["items"]:
            print(f"  [{i['status']:4}] {i['name']}: {i['detail']}")
        print(f"闸门: {'🔴 阻断 - ' + result['block_reason'] if result['blocked'] else '🟢 放行'}")
        print(f"报告: {md_path}\n      {json_path}")
    sys.exit(1 if result["blocked"] else 0)


if __name__ == "__main__":
    main()
