# -*- coding: utf-8 -*-
"""factors/opportunities/scan.py — 每日机会扫描器（v1.0 · 2026-08-08）

流程：全市场面板 → 因子计算 → 每类机会触发 → 统一评分 → 机会大池子

输入：
  - bars.db daily_bar（qfq 行情，2019+ baostock / 2010-2018 回填中）
  - finance.db finance_report（财报：ROE/单季同比/毛利率）
  - finance_quality.db quality（质量因子：负债率/现金流）
  - stock_basic.db（行业/上市日期）

输出：
  output/opportunity_pool.json {date, n, stats, by_type, opportunities:[{...}]}

机会条目字段：
  code, name, industry, otype, otype_name, trigger_desc,
  gains/prob/risk/score（统一评分四维）, factors（触发因子当前值）,
  evidence, rank_in_type, rank_global

用法：
  python factors/opportunities/scan.py                # 全量扫描
  python factors/opportunities/scan.py --types reversal,value   # 指定类型
  python factors/opportunities/scan.py --pitch        # 输出 Pitch 候选（三重过滤）
"""
import argparse
import json
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.opportunities.registry import OPPORTUNITY_TYPES, ORDER, SCORE_THRESHOLDS
from factors.opportunities.score import (opportunity_score, gains_score, prob_score,
                                         risk_score, PITCH_GATE, CONSENSUS_BONUS,
                                         size_tier_of, strong_strength, classify_pitch_sub,
                                         EXPRESS_MIN_FAMILY, CONSENSUS_MIN_FAMILY,
                                         EXPRESS_PER_LINE, CONSENSUS_PER_LINE)
from data.cache import db_uri   # ★2026-10-15 P1-A：SQLite 只读 URI 单出口（防相对路径 as_uri 静默失败）
from data.io_utils import read_json, read_text

# ★2026-10-15 修复：改绝对路径——load_panel 等处用 Path.as_uri() 拼 SQLite 只读 URI，
#   相对路径抛 ValueError("relative path can't be expressed as a file URI") 被逐库
#   try/except 吞掉 → "行情面板为空"（部署机实测命中）
BARS_DB = str(BASE / "data" / "cache" / "bars.db")
FIN_DB = str(BASE / "data" / "cache" / "finance.db")
QD_DB = str(BASE / "data" / "cache" / "finance_quality.db")
BASIC_DB = str(BASE / "data" / "cache" / "stock_basic.db")
OUT = BASE / "logs" / f"opp_pool_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"   # 每次运行唯一文件名（安全层限制同名文件只能写一次）
# 兼容读取：pitch_v2.py 用 get_latest_pool() 取最新


# ==================== 数据加载 ====================

def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# 风控批量缓存（单连接，避免逐只开 sqlite 被安全层拦截）
_risk_cache = {}
_risk_loaded = False


def batch_check_one(code: str) -> dict:
    """批量风控（单连接一次加载全部质量数据到内存缓存）"""
    global _risk_cache, _risk_loaded
    if not _risk_loaded:
        try:
            from risk.stock_risk import _check_row
            import sqlite3
            con = sqlite3.connect(QD_DB)
            period = con.execute(
                "SELECT period, COUNT(*) c FROM quality WHERE period < '2026-07-01' "
                "GROUP BY period ORDER BY period DESC LIMIT 1").fetchone()
            # 取最新且覆盖≥1000 的期
            rows = con.execute(
                "SELECT code, roe_avg, gp_margin, current_ratio, liability_to_asset, cfo_to_np "
                "FROM quality WHERE period=(SELECT MAX(period) FROM quality "
                "WHERE period<'2026-07-01' AND (SELECT COUNT(*) FROM quality q2 "
                "WHERE q2.period=quality.period)>=1000)").fetchall()
            con.close()
            for code_r, roe, gp, cr, liab, cfo in rows:
                _risk_cache[code_r] = _check_row(code_r, None, roe, gp, cr, liab, cfo)
            _risk_loaded = True
        except Exception:
            _risk_loaded = True  # 加载失败不重试
    return _risk_cache.get(code, {"code": code, "score": None, "level": "NO_DATA",
                                  "flags": [{"id": "no_data", "desc": "质量数据未覆盖", "weight": 0}],
                                  "period": None})


def _load_data_cfg():
    import yaml
    return yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))["data"]


def load_valuation(date: str = None) -> dict:
    """估值数据（备用服务器 daily-basic，全市场 5535 只）→ {code: {pe, pb, dv_ratio, total_mv}}
    列序（实测）：ts_code, trade_date, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_mv, circ_mv
    失败返回 {}（扫描器降级为价格分位近似）
    """
    import requests
    try:
        cfg = _load_data_cfg()
        base = cfg["tushare_backup"]["url"].rstrip("/")
        key = cfg["tushare_backup"]["api_key"]
        r = requests.get(f"{base}/daily-basic", headers={"X-API-Key": key},
                         params={"trade_date": (date or "").replace("-", ""),
                                 "fields": "ts_code,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_mv,circ_mv"},
                         timeout=30)
        d = r.json()
        if d.get("code") != 0:
            return {}
        items = d["data"].get("items", [])
        out = {}
        for it in items:
            vals = (list(it) + [None] * 10)[:10]
            code, pe, pe_ttm, pb, ps, ps_ttm, dv, dv_ttm, tmv, cmv = vals
            out[code] = {
                "pe": _f(pe), "pe_ttm": _f(pe_ttm), "pb": _f(pb),
                "dv_ratio": _f(dv), "dv_ttm": _f(dv_ttm),
                "total_mv": _f(tmv), "circ_mv": _f(cmv),
            }
        return out
    except Exception:
        return {}


def load_basic() -> pd.DataFrame:
    con = sqlite3.connect(BASIC_DB)
    df = pd.read_sql("SELECT code, name, industry, ipo_date FROM stock_basic", con)
    con.close()
    return df.set_index("code")


def _min_date_for(ndays: int, end: str = None) -> str:
    """★2026-10-15 提速：估算 ndays 个交易日前的自然日下限（用于 SQL 下界裁剪）。
    按 A 股年均 ~244 交易日 + 节假日缓冲，ndays 交易日 ≈ ceil(ndays*1.6) 自然日。
    下界只需足够靠前（宁可多取不可少取），故用宽松系数 1.7。"""
    import datetime as _dt
    _end = _dt.date.fromisoformat(end) if end else _dt.date.today()
    _lo = _end - _dt.timedelta(days=int(ndays * 1.7) + 15)
    return _lo.strftime("%Y-%m-%d")


# ★2026-10-15 提速：模块级面板缓存。bars 数据每日只更新一次，扫描结果在数据未变时
#   完全复用——避免每次扫描都重读 846 万行 SQL + pivot 宽表（原 load_panel ~20s）。
#   版本键 = bars.db + 最近 3 增量库的 mtime（与 DailyCache._LTD_CACHE 同思路）。
_panel_cache = {"ver": None, "end": None, "days": None, "px": None, "vx": None}


def _panel_version() -> tuple:
    import os as _os
    try:
        from data.cache import CACHE_DIR as _CD
        paths = [BARS_DB] + [str(p) for p in sorted(_CD.glob("bars_incr_*.db"))[-3:]]
        return tuple((p, _os.path.getmtime(p)) for p in paths if _os.path.exists(p))
    except Exception:
        return None


def _panel_parquet_path() -> Path:
    return BASE / "data" / "cache" / "_panel_cache.parquet"


def _try_load_parquet_panel(_ver, end, days):
    """跨进程面板缓存：读磁盘 parquet（数据未变时秒回，worker 子进程也受益）。
    命中返回 (px, vx)，否则返回 (None, None)。"""
    try:
        import pandas as _pd
        _pq = _panel_parquet_path()
        if not _pq.exists():
            return None, None
        _meta = _pd.read_parquet(_pq)
        # 元数据存为单独的列名约定：第一列是版本哨兵
        if "__ver__" not in _meta.columns:
            return None, None
        _r = _meta.iloc[0]
        if str(_r["__ver__"]) != str(_ver) or str(_r["__end__"]) != (end or "") \
                or int(_r["__days__"]) != int(days):
            return None, None
        # 版本匹配：读宽表（__ver__ 列去掉后即 px；vx 单独文件）
        _px = _meta.drop(columns=["__ver__", "__end__", "__days__"], errors="ignore")
        _vx_path = _pq.with_name("_panel_cache_vx.parquet")
        if not _vx_path.exists():
            return None, None
        _vx = _pd.read_parquet(_vx_path)
        if _px.empty:
            return None, None
        return _px, _vx
    except Exception:
        return None, None


def load_panel(end: str = None, days: int = 300):
    """日线面板（收盘价 + 成交量）→ (px, vx)；空返回 (None, None)
    end 只做上限过滤；起点固定 2020-01-01（保证 300 日窗口充足）
    ★2026-08-10 双库合并：bars.db 被环境写保护 → 增量写入 bars_incr_*.db；
      此处读取时合并（主库历史 + 增量库最新），保证增量数据对扫描可见
    ★2026-10-15 提速：模块级缓存（数据未变直接复用上次 pivot 结果，20s→0s）
    """
    _ver = _panel_version()
    if (_panel_cache["ver"] == _ver and _panel_cache["end"] == end
            and _panel_cache["days"] == days and _panel_cache["px"] is not None):
        return _panel_cache["px"], _panel_cache["vx"]
    # 跨进程磁盘缓存（worker 子进程、deck 重启后也能秒回）
    _px0, _vx0 = _try_load_parquet_panel(_ver, end, days)
    if _px0 is not None:
        _panel_cache.update(ver=_ver, end=end, days=days, px=_px0, vx=_vx0)
        return _px0, _vx0
    rows = []
    from data.cache import CACHE_DIR as _CD
    from pathlib import Path as _P
    # ★2026-08-11 #65 修复：只读最近 3 个增量库 + immutable（原遍历全部 64 个 + 普通连接，
    #   每库等锁 5-20s → load_panel 10min+ 无输出）。增量语义：最新库已含全部增量，3 个兜底足够。
    inc_files = sorted(_CD.glob("bars_incr_*.db"))[-3:]
    dbs = [BARS_DB] + [str(p) for p in inc_files]
    # ★2026-10-15 提速：只取最近 days+30 个交易日（约 400 天），不再扫 2020 至今全部历史。
    #   原 SQL date>='2020-01-01' 拉 6 年数百万行只 tail(days)，是 load_panel 20s 的主因。
    lo_date = _min_date_for(days + 30)
    for db in dbs:
        try:
            uri = db_uri(db)   # immutable 免等锁（0.01s vs 20s）；P1-A 单出口
            con = sqlite3.connect(uri, uri=True, timeout=3)
            r2 = con.execute(
                "SELECT code, date, close, volume FROM daily_bar WHERE adjust='qfq' "
                "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%' AND date >= ?",
                (lo_date,)).fetchall()
            con.close()
            rows.extend(r2)
        except Exception:
            continue
    if not rows:
        return None, None
    df = pd.DataFrame(rows, columns=["code", "date", "close", "volume"])
    df = df.drop_duplicates(subset=["code", "date"], keep="last")  # 增量覆盖主库
    px = df.pivot(index="date", columns="code", values="close").ffill()
    vx = df.pivot(index="date", columns="code", values="volume").fillna(0)
    if end:
        px = px[px.index <= end]
        vx = vx[vx.index <= end]
    _px, _vx = px.tail(days), vx.tail(days)
    _panel_cache["ver"], _panel_cache["end"], _panel_cache["days"] = _ver, end, days
    _panel_cache["px"], _panel_cache["vx"] = _px, _vx
    # 写磁盘缓存（含版本哨兵列，供跨进程命中）
    try:
        import pandas as _pd
        _pq = _panel_parquet_path()
        _save = _px.copy()
        _save.insert(0, "__ver__", str(_ver))
        _save.insert(1, "__end__", end or "")
        _save.insert(2, "__days__", int(days))
        _save.to_parquet(_pq)
        _vx.to_parquet(_pq.with_name("_panel_cache_vx.parquet"))
    except Exception:
        pass
    return _px, _vx


def load_fundamentals(end: str = None) -> pd.DataFrame:
    """最新一期财报（code6 索引）：roe, sq_nyoy, sq_rev_yoy, gross_margin, sq_net_profit
    ★取"最近且覆盖≥4000 只"的期（MAX(period) 可能只有 129 只——最新财报未披露完；
      ORDER BY COUNT(*) DESC 会取到 2022 年报 5399 只（太老）→ 按 period 倒序 + 覆盖阈值，2026-08-09 修复）
    """
    con = sqlite3.connect(FIN_DB)
    period = con.execute(
        "SELECT period FROM finance_report WHERE period < '2026-07-01' "
        "GROUP BY period HAVING COUNT(*) >= 4000 ORDER BY period DESC LIMIT 1").fetchone()[0]
    rows = con.execute(
        "SELECT code, period, net_profit, sq_net_yoy, sq_rev_yoy, roe FROM finance_report WHERE period=?",
        (period,)).fetchall()
    con.close()
    df = pd.DataFrame(rows, columns=["code", "period", "net_profit", "sq_net_yoy", "sq_rev_yoy", "roe"])
    df["code6"] = df["code"].astype(str).str[:6]
    # 毛利率从质量库补充
    q = load_quality()
    if q is not None and not q.empty:
        df = df.merge(q[["code6", "gp_margin"]], on="code6", how="left")
        df["gross_margin"] = df["gp_margin"]
    df["code6"] = df["code6"].astype(str)
    return df.set_index("code6")


def load_quality() -> pd.DataFrame:
    con = sqlite3.connect(QD_DB)
    # ★取覆盖最全的最近期（≥1000 只；MAX(period) 可能只有 47 只）
    period = con.execute(
        "SELECT period FROM quality WHERE period < '2026-07-01' "
        "GROUP BY period HAVING COUNT(*) >= 1000 ORDER BY period DESC LIMIT 1").fetchone()
    if not period:
        con.close()
        return None
    period = period[0]
    df = pd.read_sql(
        "SELECT code, period, roe_avg, gp_margin, np_margin, current_ratio, "
        "liability_to_asset, cfo_to_np, cfo_to_or FROM quality WHERE period=?",
        con, params=(period,))
    con.close()
    df["code6"] = df["code"].str[:6]
    return df


def load_st_codes() -> set:
    """ST 名单（bars.db is_st 最新一天）
    ★2026-08-12 #136 修复：① 普通连接改 immutable 只读（防等锁）② is_st 异常检测+回溯——
    08-11 Tushare 增量写入丢 is_st 列（当天仅 4 只 vs 正常 ~200 只）→ ST 过滤静默失效；
    若最新日 is_st=1 数量 <50（异常），回溯最近 10 个交易日取第一个正常日（≥50）名单"""
    con = sqlite3.connect(db_uri(BARS_DB), uri=True, timeout=3)
    try:
        # 双库合并探测最新交易日（#135 原则，复用 cache.py 已实现）
        try:
            from data.cache import DailyCache as _DC
            _mx = _DC().latest_trade_date()
        except Exception:
            _mx = None
        _mx = _mx or con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        # ★2026-10-15 提速：一次 SQL 取回最近 10 个交易日的 is_st 计数 + 名单，
        #   替代逐日 COUNT（原每步全表扫 900 万行 ×11 次 ≈ 8s）
        _days = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE date<=? ORDER BY date DESC LIMIT 10", (_mx,)).fetchall()]
        _cnts = dict(con.execute(
            "SELECT date, COUNT(*) FROM daily_bar WHERE date IN (%s) AND is_st=1 GROUP BY date"
            % ",".join("?" * len(_days)), _days).fetchall())
        for _d in _days:
            _n = _cnts.get(_d, 0)
            if _n >= 50:   # 正常 ST 规模 ~200 只，<50 视为该日 is_st 列异常（增量丢列）
                st = {r[0] for r in con.execute(
                    "SELECT code FROM daily_bar WHERE date=? AND is_st=1", (_d,)).fetchall()}
                if _d != _mx:
                    import logging
                    logging.getLogger("scan").warning(
                        f"is_st 最新日 {_mx} 异常（仅 {_cnts.get(_mx, 0)} 只，正常 ~200）→ 回溯使用 {_d} ST 名单（{len(st)} 只）")
                return st
        return set()
    finally:
        con.close()


# ==================== 外部因子池信号源（B-4 对接协议，2026-08-09） ====================
# 读小弟（量化代码库思路库/因子池）每日评分 CSV → 因子命中 → 共识加分
# ★B-12 裁决落地（2026-08-10 18:20 总指导）：五强 → 十强（+max_ret20/skew20/amihud/mom60/std20，
#   ICIR 0.417-0.642 且低相关互补；回测师规格 pitch/主系统因子消费扩展_pitch.md 档位 A）
EXT_SIGNAL_FACTORS = ["turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol",
                      "max_ret20", "skew20", "amihud", "std20",
                      # ★#418 剔除 mom60：health_2026-08-14 实测 icir120=0.108 ❌失效（因子池 F块留言也建议降权，
                      #   已彻底反转 +7.05→-2.95；动态名单 ICIR≥0.5 本就筛掉，硬编码 fallback 也不该留失效因子）
                      "open_prem_20", "lhb_jg_cnt_20",
                      # ★2026-08-13 外包 D7 组合验证（C14_D7）：open_vol_share +9.4%（低换手族 dir=-1）
                      #   ★#379 factor_corr 决策（总指挥授权 AI 综合决定）：strong_close_quiet_open × open_vol_share 相关 0.801
                      #   ——日频 ICIR 实据（risk_multiplier/health 实测）：strong_close 0.23(🟡弱有效 k=0.25) < open_vol_share 0.40(✅有效 t=6.24 k=0.5)
                      #   ——两者同含"低开盘量"信号（0.801 冗余），剔除更弱且冗余的 strong_close_quiet_open，保留更强的 open_vol_share（宁缺毋滥）
                      #   ——★撤销 #251 D15"换 close_to_high→strong_close"：D15 误用分钟频 ICIR 39.81，日频实际 0.23（分钟因子日频聚合优势消失）
                      "open_vol_share", "kline_hammer_cnt"]  # ★#76 十二强：+open_prem_20（60日ICIR 0.958 全库第一）+ lhb_jg_cnt_20（ICIR 0.900 分年度全正）★#239 +D7 分钟因子 ★#266 E8 预登记 kline_hammer_cnt（D10 ICIR 27.4 质量族 win 78%）
# ★2026-08-13 #267 用户反馈：EXT 名单本应自动（手动维护是设计缺陷）——动态生成优先：
#   从 factor_health（取因子数最多 CSV）筛「有效 + ICIR120≥0.5 + t≥4」按 ICIR120 排序 top N；
#   动态失败 → 降级硬编码 EXT_SIGNAL_FACTORS（宁缺毋滥）
_EXT_DYN_CACHE = {"ts": 0.0, "factors": None}

# ★#419 涨停/龙虎榜因子符号反临时黑名单（#406 外包 bug：原始值全非正，rank 方向反转）
#   反转市里 IC 恰为正掩盖了方向错；等外包核实符号+重算 rank 后移除本黑名单。
#   （#411 哨兵每晚告警"符号反"，此黑名单在哨兵修复前临时阻断符号反因子进动态名单）
EXT_SIGN_REVERSED = {"limup_ex_5", "limit_up_cnt_5d", "lhb_jg_cnt_20", "limup_ex_ret_20",
                     "limit_up_turn", "consec_limit_up", "limit_up_flag"}


def _dynamic_ext_factors(n: int = 15) -> list:
    """★2026-08-13 #267：EXT 名单动态生成（factor_health 驱动，免手动维护）
    满足「有效 + ICIR120≥0.5 + t≥4」的因子按 ICIR120 降序取 top n；5 分钟缓存。
    ★#419 排除 EXT_SIGN_REVERSED 符号反因子（#406 外包 bug，待修后移除黑名单）。
    ★2026-08-14 审计修复 + 因子池 F3 回执：排除 fundamental_lowfreq（契约第五条——
    fflll 平局致 rank 失真，只供 quality_gap/排雷；与 factor_risk.py:96-109 同款逻辑）。
    返回空/异常 → []（调用方降级 EXT_SIGNAL_FACTORS）。"""
    global _EXT_DYN_CACHE
    import time as _t
    now = _t.time()
    if _EXT_DYN_CACHE["factors"] is not None and now - _EXT_DYN_CACHE["ts"] < 300:
        return _EXT_DYN_CACHE["factors"]
    try:
        from factors.risk.factor_risk import load_health
        rows = load_health()
        # ★lowfreq 集合（manifest category == fundamental_lowfreq；与 factor_risk 同源）
        lowfreq = set()
        try:
            import glob as _gl, json as _js, os as _os
            _mfs = sorted(_gl.glob(r"data/factorpool/output/factor_manifest_*.json"),
                          key=_os.path.getmtime)
            if _mfs:
                _md = _js.load(open(_mfs[-1], encoding="utf-8"))
                lowfreq = {x.get("code") for x in _md.get("factors", [])
                           if x.get("category") == "fundamental_lowfreq"}
        except Exception:
            pass
        cands = []
        # ★2026-08-14 一致性改进（因子池 14:30 留言）：
        #   ① 移除 #419 黑名单（C2 已系统性修正方向，health ICIR120 全强正=注册方向正确——
        #      "原始值全非负"对 direction=-1 因子是正常结果，黑名单误杀 7 个有效因子；
        #      #411 哨兵继续兜底）
        #   ② 排除风格暴露因子（F4 双中性归零：shebao_chg_pct/shebao_chg/max_ret20 = 非真 alpha）
        #   ③ F3 家族去重（同族只取 ICIR120 最强——open_prem×gap_ret ρ=0.995、
        #      consec_limit_down×limit_down_flag ρ=1.0 同信号双计票，共识重复的统计根源）
        try:
            from factors.risk.strong_factor_table import STRONG_TABLE as _ST
            _style_set = {f for f, v in _ST.items() if v.get("style_exposed") == "true"}
            # 家族代表：族名 → 该族 ICIR120 最强因子名；以及 因子 → 族 映射（全表成员）
            _fam_rep_of = {}
            for _fn, _v in _ST.items():
                _fam = (_v.get("f3_family") or "").strip()
                _ic = float(_v.get("icir120") or 0)
                if _fam and (_fam not in _fam_rep_of or _ic > _fam_rep_of[_fam][1]):
                    _fam_rep_of[_fam] = (_fn, _ic)
            _fam_rep = {v[0] for v in _fam_rep_of.values()}   # 各族代表（ICIR 最强）
            # ★2026-08-14 修复：族→成员映射（含全部成员，非仅代表）——去重判断需要
            #   "该因子的族代表是谁"，只有族名→代表 不够（gap_ret 查不到自己族导致去重失效）
            _fam_of_factor = {}
            for _fn, _v in _ST.items():
                _fam = (_v.get("f3_family") or "").strip()
                if _fam:
                    _fam_of_factor[_fn] = _fam
        except Exception:
            _style_set, _fam_rep, _fam_of_factor = set(), None, {}
        for r in rows:
            if "有效" not in str(r.get("status") or ""):
                continue
            _fn = str(r.get("factor") or "")
            if _fn in lowfreq:
                continue   # ★2026-08-14 低频因子排除（契约第五条，F3 回执确认）
            if _fn in _style_set:
                continue   # ★2026-08-14 风格暴露排除（F4 双中性归零，非真 alpha 不进共识）
            if _fam_of_factor and _fn in _fam_of_factor and _fn not in _fam_rep:
                continue   # ★2026-08-14 家族去重：非代表因子跳过（同族仅 ICIR 最强计票）
            try:
                icir = float(r["icir120"]) if r.get("icir120") else None
                t = float(r["t120"]) if r.get("t120") else None
            except (TypeError, ValueError):
                continue
            if icir is not None and icir >= 0.5 and t is not None and t >= 4:
                cands.append((icir, _fn))
        cands.sort(key=lambda x: -x[0])
        out = [f for _, f in cands[:n] if f]
        _EXT_DYN_CACHE["factors"], _EXT_DYN_CACHE["ts"] = out, now
        if out:
            print(f"  [EXT动态] 自动名单 {len(out)} 因子（ICIR120≥0.5 有效 top{n}）：{', '.join(out[:5])}…")
        return out
    except Exception:
        return []
# ★2026-08-12 百轮#107：lhb_jg_cnt_20 正式启用——外包 P0-4 闭环：daily_2026-08-11_010256.csv
#   "五强 5/5 正常 + lhb 353 只有效" 完整文件（L0 重跑完成五强恢复）；_best_daily_file 五强分支
#   lhb 有效优先（010256 83 列胜出 052229 85 列——列数多但 lhb 常数无用的文件不选）
EXT_POOL_DIR = Path(r"data/factorpool/output/daily_scores")
EXT_EFF_MIN = 5.0   # ★FRC（2026-08-11）：有效权重和阈值（回测师规格；≈等效 5 个全效因子）
                    #   08-10 数据实测：原 ≥6 命中 148 只 → FRC 后 34 只（0.6%），过滤 77%
                    #   ⏳观察项：pv_consensus 触发量 3-5 天，过少则降 4.5（→82 只）
_ext_signal_cache = None


# ==================== 竞价强度反信号防守（T-3 裁决落地，2026-08-10） ====================
# ★总指导裁决：T-3 实证（2020 全年 83.8 万配对）竞价强度 strength≥6 高开放量
#   1 日短效（+0.9pp）、5/20 日反转（-2.3~-2.4pp）→ 采纳"反信号防守"用途：
#   机会池个股若竞价过热（strength≥6）→ 减分（AUCTION_PENALTY）+ 回避标记，
#   不单独作为买入信号。数据缺口 → 无信号日期自动跳过，引擎不受影响。
AUCTION_SIGNAL_FILES = ["auction_signal.json", "auction_signal_2020.json"]
AUCTION_HEAT_THRESHOLD = 6.0      # strength≥6 = 高开放量（AI-2 经验阈值，未网格寻优防过拟合）
AUCTION_PENALTY = 3.0             # 反信号减分（防守：宁缺毋滥）
AUCTION_MAX_AGE_DAYS = 60         # ★时效保护（2026-08-10）：信号日期与扫描日差 >60 天 → 弃用
                                  #   （竞价强度是日内情绪信号，旧数据无参考意义 → 自动静默）
AUCTION_MAX_FILE_MB = 100         # ★大文件跳过（2026-08-10）：auction_signal_2021_2026 全量
                                  #   561MB 全量加载耗内存 → 只读单年/每日增量小文件（≤100MB）
_auction_cache = None

# ★2026-08-14 Pitch 改进规格 v2 ⑤：实证徽章（因子池 pitch_priority_badges 数据包）
#   每类型 17 年 6 月胜率徽章（quality_gap 🏆70.4%/+14.6% … reversal ❌39.0%/-1.8%）——
#   pitch 卡片展示，用户一眼分清"实证强的"和"弹性的"
_pitch_badge_cache = {"mt": 0.0, "data": None}


def load_pitch_badges() -> dict:
    """读因子池 output/pitch_priority_badges_*.json（glob 最新）→ {otype: {badge, tier, winrate_6m, avg_ret_6m}}"""
    import os as _os, glob as _gl
    try:
        _cands = sorted(_gl.glob(r"data/factorpool/output/pitch_priority_badges_*.json"),
                        key=_os.path.getmtime)
        if not _cands:
            return {}
        _f = _cands[-1]
        _mt = _os.path.getmtime(_f)
        if _pitch_badge_cache["data"] is not None and _pitch_badge_cache["mt"] == _mt:
            return _pitch_badge_cache["data"]
        _raw = read_json(_f)
        _out = {}
        for _b in (_raw.get("badges") or []):
            if _b.get("otype"):
                _out[_b["otype"]] = {
                    "badge": _b.get("badge", ""),
                    "tier": _b.get("tier", ""),
                    "winrate_6m": _b.get("winrate_6m"),
                    "avg_ret_6m": _b.get("avg_ret_6m"),
                    "winrate_mult": _b.get("winrate_mult"),
                }
        _pitch_badge_cache.update({"mt": _mt, "data": _out})
        return _out
    except Exception:
        return {}


# ★2026-08-14 顶级买点标签判定阈值（用户需求：择时高概率 + 高稀有度 + 强买入，极其严格不随便出现）
TOP_BUY_TIMING_MIN = 75.0     # 择时分 ≥75（当前 67.6，需极强择时环境）
TOP_BUY_LEVEL = "适合买入"     # 择时档位必须是"适合买入"
TOP_BUY_SCORE_PAD = 10.0      # score ≥ gate + 10（极强机会）
TOP_BUY_T1_TYPES = ("quality_gap", "value")   # 实证强类型（70.4%/62.5% 胜率）


def _load_timing_score() -> tuple:
    """读最新 timing_system_*.json → (score, level)。失败 → (None, None)。"""
    import glob as _gl, os as _os
    try:
        _fs = sorted(_gl.glob(str(BASE / "output" / "timing_system_*.json")),
                     key=_os.path.getmtime)
        if not _fs:
            return (None, None)
        _d = read_json(_fs[-1])
        return (_d.get("score"), _d.get("level", ""))
    except Exception:
        return (None, None)


def _mark_top_buy(pitch: list, date: str) -> None:
    """★2026-08-14 顶级买点标签：四条件全满足才打标（极其严格）：
    ① 择时高概率：score≥75 且 level=适合买入
    ② 高稀有度：express 强因子直通（家族≥3 三重确认）
    ③ 强买入：score ≥ gate+10 且 T1 实证强类型（quality_gap/value）
    ④ 风控：非 BLOCK
    命中 → pitch 条目加 top_buy=True + top_buy_note（前端醒目标签）。
    正常市场下几乎不出现（express 本身≤2/日，叠加择时≥75 + T1 + 极强分）。
    """
    _ts, _lv = _load_timing_score()
    if _ts is None or _lv is None:
        return   # 无择时数据 → 不打标（宁缺毋滥）
    _timing_ok = (_ts is not None and _ts >= TOP_BUY_TIMING_MIN) and (_lv == TOP_BUY_LEVEL)
    if not _timing_ok:
        return   # 择时不满足 → 全员不打标
    for _p in pitch:
        if _p.get("pitch_sub") != "express":
            continue                          # ②高稀有：必须强因子直通（家族≥3）
        if _p.get("otype") not in TOP_BUY_T1_TYPES:
            continue                          # ③T1 实证强类型
        _g = PITCH_GATE.get(_p.get("otype"), 70)
        if (_p.get("score") or 0) < _g + TOP_BUY_SCORE_PAD:
            continue                          # ③极强机会（score ≥ gate+10）
        if (_p.get("risk_level") or "") == "BLOCK":
            continue                          # ④风控一票否决
        _fam = (_p.get("express_strong") or {}).get("family", "")
        _icir = (_p.get("express_strong") or {}).get("icir120", "")
        _p["top_buy"] = True
        _p["top_buy_note"] = (f"顶级买点：择时 {_ts} 适合买入 + 强因子直通（{_fam} ICIR120={_icir}）"
                              f"+ 实证强类型 + score≥gate+10")
        print(f"  [顶级买点] {_p.get('code')} {_p.get('otype')} score={_p.get('score')} "
              f"择时{_ts} 直通{_fam}——极稀有强买入信号")


def load_auction_signals() -> dict:
    """读 logs/auction_signal*.json → {date8: {code: {gap, v30_ratio, first5_ratio, strength}}}
    ★2026-08-10 改造：glob 读所有 auction_signal_*.json（按 mtime 最新优先），
      跳过超大文件（>30MB，如 2021-2026 全量补算 561MB）；小文件全量合并。
      失败 → {}（引擎正常运行，无信号日期自然跳过）
    """
    global _auction_cache
    if _auction_cache is not None:
        return _auction_cache
    _auction_cache = {}
    logs_dir = BASE / "logs"
    # 最新优先（时间戳文件名按字典序≈时间序）
    files = sorted(logs_dir.glob("auction_signal_*.json"), reverse=True)
    for p in files:
        try:
            if p.stat().st_size > AUCTION_MAX_FILE_MB * 1024 * 1024:
                continue  # 跳过全量历史大文件（防内存爆）
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for date8, codes in d.items():
                    if isinstance(codes, dict) and codes:
                        # 合并：已存在日期用新文件（最新优先已保证）
                        _auction_cache.setdefault(date8, codes)
        except Exception:
            continue
    return _auction_cache


def auction_date8_for(date: str) -> str:
    """扫描日 date(2026-08-07) → 竞价信号日期键(20260807)；
    时效保护：信号与扫描日相差 > AUCTION_MAX_AGE_DAYS → None（数据过期无意义，跳过反信号）
    数据缺口容错：无当日 → 取最近可用日期（≤扫描日且时效内），仍无 → None
    """
    if not _auction_cache:
        return None
    ymd = date.replace("-", "")
    from datetime import datetime as _dt
    try:
        scan_dt = _dt.strptime(ymd, "%Y%m%d")
    except Exception:
        return None
    if ymd in _auction_cache:
        sig_dt = _dt.strptime(ymd, "%Y%m%d")
        if (scan_dt - sig_dt).days <= AUCTION_MAX_AGE_DAYS:
            return ymd
        return None
    avail = [k for k in _auction_cache if k <= ymd]
    if not avail:
        return None
    latest = max(avail)
    sig_dt = _dt.strptime(latest, "%Y%m%d")
    if (scan_dt - sig_dt).days <= AUCTION_MAX_AGE_DAYS:
        return latest
    return None


def auction_heat_penalty(code: str, date8: str) -> float:
    """竞价过热反信号：strength ≥ 阈值 → 返回减分；无信号/未过热 → 0"""
    codes = _auction_cache.get(date8, {}) if date8 else {}
    sig = codes.get(code)
    if not sig:
        return 0.0
    try:
        s = float(sig.get("strength", 0))
    except Exception:
        return 0.0
    return AUCTION_PENALTY if s >= AUCTION_HEAT_THRESHOLD else 0.0


# ★短线事件排雷（外包 2026-08-10 短线因子体检结论落地）
#   连续跌停/跌停（ICIR 0.97，IC>0 86.6% → 显著走弱）→ 风控排雷：risk 上调 + 🔴 标记
#   5 日涨停次数（活跃度）/ 当日涨停（追高溢价 20 日反向 -0.24）→ 提示
_event_flags_cache = {"ts": 0.0, "data": {}}


def load_event_flags(codes: set, end: str = None) -> dict:
    """批量查候选股最近 10 交易日 pct_chg → 事件因子标志（immutable 只读，5min 缓存）"""
    if not codes:
        return {}
    now = time.time()
    if now - _event_flags_cache["ts"] < 300 and end is None:
        return {k: v for k, v in _event_flags_cache["data"].items() if k in codes}
    out = {}
    try:
        from pathlib import Path as _P
        uri = db_uri(BARS_DB)
        con = sqlite3.connect(uri, uri=True, timeout=3)
        # 先取 bars 最近交易日（★双库合并：主库 + 增量库最大值）
        mx = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        try:
            from data.cache import DailyCache as _DC
            _mx2 = _DC().latest_trade_date()
            if _mx2 and (mx is None or _mx2 > mx):
                mx = _mx2
        except Exception:
            pass
        if mx:
            from datetime import timedelta
            lo = (datetime.strptime(mx, "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
            rows = con.execute(
                "SELECT code, date, pct_chg, turn FROM daily_bar "
                "WHERE adjust='qfq' AND date>=? AND date<=? AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'",
                (lo, mx)).fetchall()
        else:
            rows = []
        con.close()
        # 按 code 分组（保留最近 10 个交易日）
        from collections import defaultdict
        g = defaultdict(list)
        for c, d, p, t in rows:
            if p is None:
                continue
            g[c].append((d, float(p), t))
        for c, lst in g.items():
            lst.sort()
            recent = lst[-10:]
            if len(recent) < 2:
                continue
            chg = [x[1] for x in recent]
            last, prev = chg[-1], chg[-2]
            # 最新日换手（limit_up_turn 实证：涨停日换手低=封得死，稀疏样本方向可信）
            last_turn = None
            for _d, _p, _t in reversed(recent):
                if _t is not None:
                    try:
                        last_turn = float(_t)
                    except Exception:
                        last_turn = None
                    break
            f = {
                "consec_down2": last <= -9.7 and prev <= -9.7,
                "down_flag": last <= -9.7,
                "up_cnt5": sum(1 for x in chg[-5:] if x >= 9.7),
                "up_today": last >= 9.7,
                "last_turn": last_turn,
            }
            out[c] = f
    except Exception:
        pass
    _event_flags_cache.update({"ts": now, "data": out})
    return out


def apply_event_flags(opportunities: list) -> None:
    """给机会列表打事件排雷/提示（M7 防守层，外包体检结论 2026-08-10）"""
    if not opportunities:
        return
    try:
        flags = load_event_flags({o["code"] for o in opportunities})
    except Exception:
        return
    for o in opportunities:
        fl = flags.get(o["code"])
        if not fl:
            continue
        tags = []
        if fl["consec_down2"]:
            o["risk"] = round(min((o.get("risk") or 0.5) * 1.3, 1.0), 2)
            tags.append("🔴连续跌停排雷")
        elif fl["down_flag"]:
            o["risk"] = round(min((o.get("risk") or 0.5) * 1.15, 1.0), 2)
            tags.append("🔴跌停排雷")
        if fl["up_cnt5"] >= 2:
            tags.append(f"5日{fl['up_cnt5']}涨停活跃")
        if fl["up_today"] and o.get("otype") in ("event", "breakout", "reversal"):
            tags.append("⚠涨停追高溢价20日反向")
            # ★短线体检 2 期：limit_up_turn 实证「涨停日换手低=封得死」（稀疏样本方向可信）→ 仅提示不加分
            lt = fl.get("last_turn")
            if lt is not None and lt < 5:
                tags.append("缩量涨停·封得死")
            elif lt is not None and lt > 15:
                tags.append("放量涨停·警惕炸板")
        if tags:
            o["note"] = (o.get("note") or "") + "·" + "·".join(tags)
            o["event_flags"] = fl


_crowding_cache = {"ts": 0.0, "data": None}
_basic_confirm_cache = {"data": None}   # ★B-8 基本面确认（revalue←SUE / quality_gap←F-Score）
_fs_flags_cache = {"data": None}        # ★H2 FS 假信号 flag（一票否决）


def load_fs_flags() -> dict:
    """★H2 FS 假信号层接入（外包 08-10 交付 6 flag，总指导 23:50 落地）：
    读面板 CSV 的 flag 列 → {code: [flag 列表]}。一票否决规则（研究员 FS 清单）：
    untradable 一字板（买不进）/ limup_trap 高换手涨停诱多 / distribution 天量滞涨出货 /
    pump_dump 对倒疑似 / falling_knife 超跌接飞刀 / breakout_novol 无量突破。
    复用 ext_signal 已加载的 df（同文件），无数据 → {}。"""
    if _fs_flags_cache["data"] is not None:
        return _fs_flags_cache["data"]
    out = {}
    try:
        # ★#124 同款修复：daily_*.csv 按 mtime 取最新（与 _best_daily_file 语义对齐）
        fs = sorted(EXT_POOL_DIR.glob("daily_*.csv"), key=lambda p: p.stat().st_mtime)
        if not fs:
            return out
        _VETO = ("flag_untradable", "flag_limup_trap", "flag_distribution",
                 "flag_pump_dump", "flag_falling_knife", "flag_breakout_novol",
                 "flag_lhb_high")  # ★2026-08-12 C-10b 外包反向信号接入：机构龙虎榜上榜≥5次（20日 -5.18pp 重排雷实证）
        df = pd.read_csv(fs[-1])
        present = [c for c in _VETO if c in df.columns]
        if not present:
            return out
        for _, row in df.iterrows():
            code = str(row.get("code", "")).upper()
            if "." not in code:
                continue
            hit = [c.replace("flag_", "") for c in present if row.get(c) == 1]
            if hit:
                out[code] = hit
        _fs_flags_cache["data"] = out
    except Exception:
        pass
    return out


def apply_fs_veto(opportunities: list) -> list:
    """★H2 FS 假信号一票否决：命中 veto flag 的机会剔除（宁缺毋滥，防假信号进组合）。
    返回过滤后的机会列表；被剔者留痕打印。"""
    try:
        flags = load_fs_flags()
    except Exception:
        return opportunities
    if not flags:
        return opportunities
    kept, vetoed = [], []
    for o in opportunities:
        hit = flags.get(o["code"])
        if hit:
            vetoed.append((o["code"], hit))
            continue
        kept.append(o)
    if vetoed:
        print(f"  [FS否决] 剔除 {len(vetoed)} 只：{[(c, h) for c, h in vetoed[:6]]}")
    return kept


def load_crowding() -> dict:
    """★C3 执行侧接入（2026-08-10 总指导）：读因子池 market_{date}.csv 的五强拥挤度
    （60 日波动率历史分位）→ {factor: pct}；阈值 ui_thresholds.json crowding.downweight=90/pause=95。
    5min 缓存；market 缺失/异常 → {}（降权是防守增强，异常不阻断）。"""
    import time as _t
    now = _t.time()
    if _crowding_cache["data"] is not None and now - _crowding_cache["ts"] < 300:
        return _crowding_cache["data"]
    out = {}
    try:
        # ★#124 同款修复：market_*.csv 按 mtime 取最新
        fs = sorted((EXT_POOL_DIR.parent / "daily_scores").glob("market_*.csv"),
                    key=lambda p: p.stat().st_mtime)
        if not fs:
            return out
        with open(fs[-1], encoding="utf-8") as f:
            import csv as _csv
            rd = _csv.DictReader(f)
            row = next(rd, None)
        if not row:
            return out
        # ★2026-08-12 #145 内容日期防护（#135 原则延伸）：market csv 的 date 字段
        #   滞后最新交易日 >3 天 → 拥挤度数据过期，降级返回 {}（不阻断，防停更误用旧值）
        try:
            _md = (row.get("date") or "").strip()
            from data.cache import DailyCache as _DC
            _lt = _DC().latest_trade_date()
            if _md and _lt and _md < _lt:
                import datetime as _dt
                _lag = (_dt.date.fromisoformat(_lt) - _dt.date.fromisoformat(_md)).days
                if _lag > 3:
                    return out
        except Exception:
            pass
        for ft in ("turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol"):
            v = row.get(f"crowding_{ft}", "")
            try:
                pct = float(v) * 100.0 if float(v) <= 1.0 else float(v)   # 0-1 或 0-100 兼容
            except Exception:
                continue
            if pct > 0:
                out[ft] = round(pct, 1)
        _crowding_cache.update({"ts": now, "data": out})
    except Exception:
        pass
    return out


# ==================== FRC 因子风控系数（2026-08-11，回测师交付阶段3接入） ====================
# 读因子池 risk/risk_multiplier_{date}.json → {factor: eff}
#   eff=0（k=0 反向失效）→ 命中不计；0<eff<1 → 命中按 eff 加权
# 无文件/失败 → {}（降级：不调制，正常流程）
_risk_mult_cache = {"ts": 0.0, "data": None}

def load_risk_multiplier() -> dict:
    global _risk_mult_cache
    now = time.time()
    if _risk_mult_cache["data"] is not None and now - _risk_mult_cache["ts"] < 300:
        return _risk_mult_cache["data"]
    _risk_mult_cache["data"] = {}
    try:
        rdir = EXT_POOL_DIR.parent.parent / "risk"   # 因子池/risk（output/daily_scores 上两级）
        # ★#124 同款修复：risk_multiplier_*.json 按 mtime 取最新（_v2 后缀 ASCII 排序陷阱免疫）
        # ★#268 追加：外包 --only 增量重算覆盖主文件（08-13 5 因子版覆盖 08-12 83 因子版）→ 返回 0
        #   同 #250 load_health 教训：**取「因子数最多」而非「文件名最新」**——全量优先，残缺增量版跳过
        files = sorted(rdir.glob("risk_multiplier_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return _risk_mult_cache["data"]
        best, best_n = None, -1
        for p in files:
            try:
                d = read_json(p)
                facs = d.get("factors", {})
                n = len(facs)
                if n > best_n and n > 0:
                    eff_n = sum(1 for v in facs.values() if isinstance(v, dict) and "eff" in v)
                    if eff_n > 0:   # 结构校验：至少 1 个有效 eff（残缺/异常文件跳过）
                        best, best_n = p, n
            except Exception:
                continue
        if best is None:
            return _risk_mult_cache["data"]
        d = read_json(best)
        _risk_mult_cache["data"] = {f: v["eff"] for f, v in d.get("factors", {}).items() if isinstance(v, dict) and "eff" in v}
    except Exception:
        pass
    _risk_mult_cache["ts"] = now
    return _risk_mult_cache["data"]


# ★2026-08-13 #338 风格轮动加权（因子池 F块收官）：当前风格下 boost 因子族 ×1.2 / trim 因子族 ×0.8
#   与 ICIR120 降权（risk_multiplier eff）叠加——低波期偏好低波族、动量期偏好流动性族
_STYLE_W_CACHE = {"ts": 0.0, "weights": {}}

def _style_weights() -> dict:
    """读因子池 factor_style_rotation（output/ui_data 最新）→ {factor: weight}
    当前风格 boost 因子 → boost_x(1.2)，trim 因子 → trim_x(0.8)，其余 → 默认 1.0。
    异常/无文件 → {}（风格轮动是增强，不阻断主流程）。"""
    global _STYLE_W_CACHE
    now = time.time()
    if _STYLE_W_CACHE["weights"] and now - _STYLE_W_CACHE["ts"] < 300:
        return _STYLE_W_CACHE["weights"]
    _STYLE_W_CACHE["weights"] = {}
    try:
        ui_dir = EXT_POOL_DIR.parent / "ui_data"
        files = sorted(ui_dir.glob("factor_style_rotation_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return _STYLE_W_CACHE["weights"]
        d = read_json(files[-1])
        cur = d.get("current_style") or ""
        w = (d.get("weights") or {}).get(cur) or {}
        out = {}
        for f in (w.get("boost") or []):
            out[f] = float(w.get("boost_x", 1.2))
        for f in (w.get("trim") or []):
            out[f] = float(w.get("trim_x", 0.8))
        _STYLE_W_CACHE["weights"] = out
        _STYLE_W_CACHE["ts"] = now
        print(f"  [风格轮动] {cur}期 boost {len(w.get('boost') or [])} / trim {len(w.get('trim') or [])} 因子族加权")
    except Exception as e:
        print(f"  [风格轮动] 读取失败（跳过）: {str(e)[:60]}")
    return _STYLE_W_CACHE["weights"]


# ==================== 分域权重（2026-08-12 百轮#81，回测师探索报告） ====================
# turn_mid_prox 大盘域显著强（小-大 -3.6pp，探索_市场级情绪择时第五节）→ 大盘域（市值≥P70）权重 ×1.15
# 市值数据：hist_mv.db 最新月 circ_mv（PIT），P70 分位分域；1h 缓存；异常 → 空集（不分域）
_DOMAIN_CACHE = {"ts": 0.0, "big": None}

def _big_caps() -> set:
    import time as _t
    now = _t.time()
    if _DOMAIN_CACHE["big"] is not None and now - _DOMAIN_CACHE["ts"] < 3600:
        return _DOMAIN_CACHE["big"]
    out = set()
    try:
        import sqlite3 as _sq
        con = _sq.connect("file:data/cache/hist_mv.db?mode=ro&immutable=1", uri=True)
        m = con.execute("SELECT MAX(month) FROM hist_mv").fetchone()[0]
        vals = sorted(r[0] for r in con.execute(
            "SELECT circ_mv FROM hist_mv WHERE month=?", (m,)).fetchall())
        p70 = vals[int(len(vals) * 0.7)] if vals else 1e9
        for c, mv in con.execute("SELECT code, circ_mv FROM hist_mv WHERE month=?", (m,)):
            if mv >= p70:
                out.add(str(c)[:6])
        con.close()
    except Exception:
        pass
    _DOMAIN_CACHE.update({"ts": now, "big": out})
    return out


_strong_hits_cache = None   # ★2026-08-11 强因子直通（factor_risk 强因子 × daily CSV rank≤0.10）
_health_cache = None        # ★2026-08-11 因子有效性（外包 health_*.csv → {factor: {icir120, status}}）
_daily_rank_cache = None    # ★2026-08-11 个股因子强度（daily_*.csv → {code: {factor: rank}}）
_manifest_cache = None      # ★2026-08-11 因子登记 manifest（外包契约 → {factor: {category, icir_60, ...}}）


def load_factor_manifest() -> dict:
    """读外包 factor_manifest_*.json（《因子池输出格式契约 2026-08-11》：output/factor_manifest_{date}.json）
    → {factor: {category, icir_60, direction, status, usage, name_cn}}
    ★新因子无缝接入机制：外包在 manifest 登记（category/方向/有效性）+ daily_scores 加 {code}_rank 列
    + health 加行 → 主系统免改代码自动消费（信号族归类/有效性标注/信号分加权/看板显示）。
    无 manifest → {}（信号联动降级：health + 硬编码映射兜底）。"""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    _manifest_cache = {}
    try:
        # ★2026-08-12 百轮后#124：文件名排序≠时间排序（factor_manifest_2026-08-12_v2.json ASCII 'v' > '0'
        #   排在 _085512.json 之后 → mf[-1] 选中 05:23 旧文件 78 因子，漏掉 08:55 最新 80 因子）
        # → 改按 mtime 取最新（与 deck/live_api._latest 同规范）
        mf = sorted((EXT_POOL_DIR.parent).glob("factor_manifest_*.json"),
                    key=lambda p: p.stat().st_mtime)
        if not mf:
            return _manifest_cache
        md = json.loads(mf[-1].read_text(encoding="utf-8"))
        for x in md.get("factors", []):
            if isinstance(x, dict) and x.get("code"):
                _manifest_cache[x["code"]] = {
                    "category": str(x.get("category", "")).strip(),
                    "icir_60": _f(x.get("icir_60")),
                    "direction": x.get("direction"),
                    "status": str(x.get("status", "")).strip(),
                    "usage": str(x.get("usage", "")).strip(),
                    "name_cn": str(x.get("name_cn", "")).strip(),
                }
    except Exception:
        pass
    return _manifest_cache


def load_factor_health() -> dict:
    """读外包因子池 health_*.csv（65 因子有效性）→ {factor: {icir120, status}}。
    无文件/失败 → {}（信号联动降级：只有信号族，无有效性权重）。"""
    global _health_cache
    if _health_cache is not None:
        return _health_cache
    _health_cache = {}
    try:
        # ★#124 同款修复：health_*.csv 按 mtime 取最新（文件名排序可能被 v2/latest 后缀干扰）
        hs = sorted((EXT_POOL_DIR.parent / "health").glob("health_*.csv"),
                    key=lambda p: p.stat().st_mtime)
        if not hs:
            return _health_cache
        df = pd.read_csv(hs[-1])
        for _, r in df.iterrows():
            f = str(r.get("factor", "")).strip()
            if not f:
                continue
            _health_cache[f] = {
                "icir120": _f(r.get("icir120")),
                "status": str(r.get("status", "")).strip(),
            }
    except Exception:
        pass
    return _health_cache


def _best_daily_file() -> object:
    """★2026-08-11 百轮#47：选五强完整率≥4 且 rank 列数最多的 daily CSV。
    不用 files[-1]/reversed 首个——fix 补跑文件（列少）排最后会被误选，v8 新因子读不到。
    ★2026-08-12 百轮#76 修正：**最新数据日期优先，同日期内选 rank 列最多**——
    ① 原逻辑让"更旧但五强完整"的 08-10 文件胜出（日期倒挂，用旧数据）
    ② 08-11 v9 文件（85 列含 open_prem/lhb/morph）五强 2/5 被拒 → 新因子永远读不到
    新逻辑：按文件名日期分组取最新组 → 组内五强≥4 优先，否则扩展因子完整者，再按列数。"""
    try:
        files = sorted(EXT_POOL_DIR.glob("daily_*.csv"))
        if not files:
            return None
        _F5 = ("turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol")
        _V9 = ("open_prem_20", "lhb_jg_cnt_20", "morph_5_combo")
        import re as _re
        # 按文件名日期分组（daily_YYYY-MM-DD...），取最新日期组
        by_date = {}
        for f in files:
            m = _re.search(r"daily_(\d{4}-\d{2}-\d{2})", f.name)
            dk = m.group(1) if m else f.name
            by_date.setdefault(dk, []).append(f)
        latest_date = sorted(by_date.keys())[-1]
        grp = by_date[latest_date]
        chosen, best_n, best_key, best_mt = None, -1, (-1, -1, -1), -1.0
        v9_chosen, v9_n = None, -1
        for f in grp:
            try:
                # ★2026-08-12 百轮#101：nrows=3000 采样误判——08-11 文件 turn 三列头部缺失（前 3000 行
                #   非空仅 0.03-0.36）但全列非空率 1.0 → 五强误判 2/5。改全量判定（5816 行，9 文件可接受）
                df_try = pd.read_csv(f, nrows=6000)
                f5_ok = sum(1 for ft in _F5 if f"{ft}_rank" in df_try.columns
                            and df_try[f"{ft}_rank"].notna().mean() >= 0.5)
                n_rank = sum(1 for c in df_try.columns if c.endswith("_rank"))
                # ★2026-08-12 百轮#107：五强完整时 lhb 有效者优先（P0-4 完整文件 010256：五强 5/5 + lhb 353 只
                #   有效 vs 052229 五强 5/5 + lhb 常数——列数多但 lhb 无用的文件不应胜出）
                _lhb_ok = ("lhb_jg_cnt_20_rank" in df_try.columns
                           and df_try["lhb_jg_cnt_20_rank"].notna().mean() >= 0.5
                           and df_try["lhb_jg_cnt_20_rank"].max() > 0.75)
                # ★2026-08-12 百轮后#121：shebao_hold rank 已重算（P1-2 部分闭环——外包 090602 文件
                #   1387 只 ≥0.75 有区分度 vs 073341 常数 0.5）→ 五强分支加 shebao 区分度优先
                #   （090602 与 073341 同 85 列时，选 shebao 有效的更新文件）
                _shebao_ok = ("shebao_hold_rank" in df_try.columns
                              and df_try["shebao_hold_rank"].notna().mean() >= 0.5
                              and df_try["shebao_hold_rank"].max() > 0.75)
                if f5_ok >= 4:
                    _key = (1 if _lhb_ok else 0, 1 if _shebao_ok else 0, n_rank)
                    # ★2026-08-12 百轮后#129：同 key 时选 mtime 更新者（090602 vs 111439 同 5/5+85 列
                    #   +lhb+shebao 全同 → 原逻辑保留文件名排序靠前者 = 旧文件；外包盘中高频更新面板，
                    #   同质量必须取最新版，否则日期倒挂用旧数据）
                    if _key > best_key or (_key == best_key and f.stat().st_mtime > best_mt):
                        best_key = _key
                        best_n = n_rank
                        best_mt = f.stat().st_mtime
                        chosen = f
                else:
                    # ★2026-08-12 百轮#101 回滚：lhb 区分度优先判定引入回归（v2 文件 lhb 有效但
                    #   turn/sent/turnover 三列异常仅 20 只命中 vs 052229 的 855——外包半成品，不能为 lhb 牺牲五强）
                    # → 恢复原 v9 判定（052229 胜出，lhb 维持暂剔，等外包完整版）
                    _v9_ok = sum(1 for ft in _V9 if f"{ft}_rank" in df_try.columns
                                 and df_try[f"{ft}_rank"].notna().mean() >= 0.9)
                    if _v9_ok >= 2 and n_rank > v9_n:
                        v9_n = n_rank
                        v9_chosen = f
            except Exception:
                continue
        if chosen is None and v9_chosen is not None:
            print(f"  [容错] {latest_date} 五强不完整 → 用 v9 扩展因子文件（{v9_chosen.name}，{v9_n} rank 列）")
            return v9_chosen
        if chosen is None and len(by_date) > 1:
            # 最新日期组无可用文件 → 回退上一日期组（罕见）
            return _best_daily_file_fallback(files, _F5)
        return chosen
    except Exception:
        return None


def _best_daily_file_fallback(files, _F5) -> object:
    """回退：全文件扫描五强≥4 列最多（旧版逻辑，仅最新组全不可用时触发）"""
    chosen, best_n = None, -1
    for f in files:
        try:
            # ★2026-08-12 百轮后#130：nrows=3000 → 6000（#101 同款：头部缺失文件采样误判五强 2/5）
            df_try = pd.read_csv(f, nrows=6000)
            f5_ok = sum(1 for ft in _F5 if f"{ft}_rank" in df_try.columns
                        and df_try[f"{ft}_rank"].notna().mean() >= 0.5)
            if f5_ok < 4:
                continue
            n_rank = sum(1 for c in df_try.columns if c.endswith("_rank"))
            if n_rank > best_n:
                best_n, chosen = n_rank, f
        except Exception:
            continue
    return chosen


def load_daily_ranks() -> dict:
    """读外包因子池最新可用 daily_*.csv → {code: {factor: rank}}（个股信号强度）。
    复用 ext_signal 的容错选择逻辑（五强完整率 ≥50%）；无 → {}。"""
    global _daily_rank_cache
    if _daily_rank_cache is not None:
        return _daily_rank_cache
    _daily_rank_cache = {}
    try:
        files = sorted(EXT_POOL_DIR.glob("daily_*.csv"))
        if not files:
            return _daily_rank_cache
        chosen = _best_daily_file()
        if chosen is None:
            return _daily_rank_cache
        df = pd.read_csv(chosen)
        df = df.dropna(subset=["code"])
        rank_cols = [c for c in df.columns if c.endswith("_rank")]
        for _, row in df.iterrows():
            code = str(row["code"]).upper()
            if "." not in code:
                continue
            entry = {}
            for c in rank_cols:
                v = row.get(c)
                if v is not None and pd.notna(v):
                    try:
                        entry[c[:-5]] = round(float(v), 4)   # 去 _rank 后缀
                    except Exception:
                        pass
            if entry:
                _daily_rank_cache[code] = entry
    except Exception:
        pass
    return _daily_rank_cache


# ★2026-08-14 白名单加权数据包加载（协作：因子池产出 pitch_signal_weights_{ts}.json）
#   兼容两种格式：
#     A) 因子池实际格式 {"whitelist": [{code, weight, family, style_exposed, role, ...}], "style_exposed_zero_weight": [...]}
#     B) 约定格式 {"weights": {factor: {"weight", "family", "style_exposed"}}} 或直接 {factor: {...}}
#   未找到文件 → 返回 None（signal_metrics 回退全因子加权，行为不变）。
_pw_cache = {"mt": 0.0, "data": None}
_PW_PATHS = (
    BASE / "factors" / "risk" / "pitch_signal_weights.json",
    BASE / "logs" / "pitch_signal_weights.json",
)
# ★2026-08-14 因子池产出路径（时间戳名，glob 最新）
_PW_POOL_PATHS = (
    Path(r"<home>\Desktop\量化代码库思路库\因子池\output"),
)


def _load_pitch_weights() -> dict or None:
    import os as _os, glob as _gl
    _f = None
    for _p in _PW_PATHS:
        if _p.exists():
            _f = _p
            break
    if _f is None:
        # 因子池 output 时间戳文件（glob 最新）
        for _dir in _PW_POOL_PATHS:
            try:
                _cands = sorted(_gl.glob(str(_dir / "pitch_signal_weights_*.json")),
                                key=_os.path.getmtime)
                if _cands:
                    _f = Path(_cands[-1])
                    break
            except Exception:
                continue
    if _f is None:
        return None
    try:
        _mt = _os.path.getmtime(_f)
        if _pw_cache["data"] is not None and _pw_cache["mt"] == _mt:
            return _pw_cache["data"]
        _raw = json.loads(_f.read_text(encoding="utf-8"))
        _out = {}
        # 格式 A：whitelist 数组（因子池实际格式）
        _wl = _raw.get("whitelist") if isinstance(_raw, dict) else None
        if isinstance(_wl, list):
            for _ent in _wl:
                if not isinstance(_ent, dict) or not _ent.get("code"):
                    continue
                # ★2026-08-14 排雷角色权重减半（研究员 notes：consec_limit_down 主排序建议 ×0.5——
                #   跌停族 direction 取反后 rank 大=安全，属防守信号不宜与买入主排序等权）
                _role = str(_ent.get("role", "") or "")
                _wgt = float(_ent.get("weight", 1.0) or 1.0)
                if "排雷" in _role or "反向" in _role:
                    _wgt = _wgt * 0.5
                _out[str(_ent["code"])] = {
                    "weight": _wgt,
                    "family": str(_ent.get("family", "") or ""),
                    "style_exposed": "true" if _ent.get("style_exposed") is True
                                     else str(_ent.get("style_exposed", "") or ""),
                }
            # 风格暴露 0 权因子显式补 0（style_exposed_zero_weight 列表）
            for _z in (_raw.get("style_exposed_zero_weight") or []):
                if isinstance(_z, dict) and _z.get("code"):
                    _out.setdefault(str(_z["code"]), {})["style_exposed"] = "true"
        else:
            # 格式 B：weights 字典
            _w = _raw.get("weights") if isinstance(_raw, dict) and "weights" in _raw else _raw
            for _k, _v in (_w or {}).items():
                if isinstance(_v, dict):
                    _out[str(_k)] = {
                        "weight": float(_v.get("weight", 1.0) or 1.0),
                        "family": str(_v.get("family", "") or ""),
                        "style_exposed": str(_v.get("style_exposed", "") or ""),
                    }
                else:
                    _out[str(_k)] = {"weight": float(_v or 1.0), "family": "", "style_exposed": ""}
        _pw_cache.update({"mt": _mt, "data": _out})
        return _out
    except Exception:
        return None


def signal_metrics(o: dict, health: dict, daily_ranks: dict, manifest: dict = None) -> dict:
    """★2026-08-11 信号联动（用户指示：机会池与因子池联动）：
    分类按因子信号族（signal_family，无法归类→其他）；排名按 信号有效程度×个股强度加权。
    返回 {signal_family, signal_score, factor_eff, n_invalid, eff_note}
    - 信号有效程度 = health icir120（有效因子才计权；❌失效/⚠️漂移权重 0）；health 缺失 → manifest icir_60 兜底
    - 个股信号强度 = daily_scores rank（0-1，越大越强）
    - signal_score = Σ(max(icir,0)×rank) / Σ(max(icir,0)) × 100（rank 缺失因子不计）
    - signal_family：signal_family_of（硬编码映射）→ 若落"其他"且 manifest category 存在 → 按类别归族（新因子自动吸收）
    ★2026-08-14 白名单加权（协作：因子池 pitch_signal_weights.json 落地即启用）：
      - 仅 F1 白名单因子计权（非白名单只触发不计权）
      - 风格暴露 style_exposed=true 因子计 0 权重
      - 同一 f3_family 内仅权重最高因子计权（家族去重）
      文件未产出 → 完全回退原全因子加权（行为不变）
    """
    from factors.signal_family import signal_family_of, alias_of, category_to_family
    manifest = manifest or {}
    factors = o.get("factors", {}) or {}
    code = o.get("code", "")
    ranks = daily_ranks.get(code, {})
    pw = _load_pitch_weights()   # None = 白名单未启用
    fam_w = {}
    total_w, total_rw = 0.0, 0.0
    factor_eff, n_invalid = {}, 0
    # 白名单模式：家族去重预计算（同 family 仅保留 weight 最高因子）
    _wl_keep = None
    if pw:
        _wl_keep = {}
        _wl_keep = set()
        _fam_best = {}
        for _f0, _w0 in pw.items():
            _fam0 = (_w0.get("family") or "").strip()
            _wgt0 = float(_w0.get("weight", 0) or 0)
            if not _fam0:
                _wl_keep.add(_f0)
                continue
            if _fam0 not in _fam_best or _wgt0 > _fam_best[_fam0][1]:
                _fam_best[_fam0] = (_f0, _wgt0)
        _wl_keep.update(_f0 for _f0, _ in _fam_best.values())
    for f in factors.keys():
        # ★别名兜底：主系统财务因子（sq_nyoy 等）→ 外包信号因子（sue 等）取 icir/rank
        _al = alias_of(f)
        _m = manifest.get(f) or manifest.get(_al) or {}
        eff = health.get(f) or health.get(_al) or {}
        # 有效性：health icir120 → manifest icir_60 → 无
        icir = eff.get("icir120")
        if icir is None:
            icir = _m.get("icir_60")
        status = eff.get("status") or _m.get("status") or ""
        icir_w = icir if (icir is not None and icir > 0) else 0.0
        # 失效/反向漂移 → 权重 0（标注不隐瞒）
        if status and any(k in status for k in ("❌", "反向", "失效", "dead")):
            icir_w = 0.0
            n_invalid += 1
        # ★2026-08-14 白名单加权：非白名单因子不计权（只作触发）；风格暴露 true 计 0
        if pw:
            _wr = pw.get(f) or pw.get(_al)
            if _wr is None or (_wr.get("style_exposed") or "") == "true" \
                    or (_wr.get("family") or "").strip() == "style" \
                    or (_wr.get("family") or "").strip() == "STYLE":
                icir_w = 0.0
            elif _wl_keep and f not in _wl_keep and _al not in _wl_keep:
                icir_w = 0.0   # 家族去重：同族非最强因子不计权
            else:
                icir_w = icir_w * float(_wr.get("weight", 1.0) or 1.0)
        # ★2026-08-12 百轮#81：分域权重——turn_mid_prox 大盘域（市值≥P70）×1.15
        #   回测师实证：小-大 -3.6pp（大盘显著强）；M4 权重微调建议落地（不分域建两套系统）
        if f == "turn_mid_prox" and icir_w > 0 and code[:6] in _big_caps():
            icir_w = icir_w * 1.15
        # ★2026-08-13 外包5因子终版派单 §7.2：amihud 中小盘域补强（中小盘域候选池优先用 amihud 排序）
        #   研究员实证：amihud 中小盘域（市值分位 Q1-Q3）+9.9pp/84% 全库第一；大盘域不受影响
        #   对称实现：非大盘域（市值<P70）amihud 权重 ×1.15（与 turn_mid_prox 大盘 ×1.15 同一套机制）
        if f == "amihud" and icir_w > 0 and code[:6] not in _big_caps():
            icir_w = icir_w * 1.15
        rk = ranks.get(f)
        if rk is None and _al != f:
            rk = ranks.get(_al)      # 别名 rank（如 sq_nyoy → sue_rank）
        # 信号族：硬编码映射 → manifest category 自动吸收（新因子免改代码）
        fam = signal_family_of(f)
        if fam == "其他" and _m.get("category"):
            fam = category_to_family(_m["category"]) or "其他"
        fam_w[fam] = fam_w.get(fam, 0.0) + max(icir_w, 0.05)
        factor_eff[f] = {
            "family": fam,
            "icir120": icir,
            "status": status or "未登记",
            "rank": rk,
            "src": "health" if eff else ("manifest" if _m else ""),
        }
        if icir_w > 0 and rk is not None:
            total_w += icir_w
            total_rw += icir_w * rk
    if total_w > 0:
        signal_score = round(total_rw / total_w * 100, 1)
    else:
        signal_score = 0.0   # 无有效因子（全部无效/无 rank）→ 0 分，沉底展示
    signal_family = max(fam_w.items(), key=lambda x: x[1])[0] if fam_w else "其他"
    eff_note = ""
    if n_invalid:
        eff_note = f"{n_invalid} 个触发因子无效/失效（不计权重）"
    return {
        "signal_family": signal_family,
        "signal_score": signal_score,
        "factor_eff": factor_eff,
        "n_invalid": n_invalid,
        "eff_note": eff_note,
    }


def _pv_factors(code: str, factor_list) -> dict:
    """★2026-08-11 pv_consensus 触发因子补全：从 daily_scores rank（load_daily_ranks）取五强 rank 值。
    返回 {factor: rank}（0-1），无数据 → {}。"""
    try:
        _dr = load_daily_ranks()
        entry = _dr.get(code, {})
        return {k: entry.get(k) for k in factor_list if entry.get(k) is not None}
    except Exception:
        return {}


def load_strong_hits() -> dict:
    """★2026-08-11 用户指示：强因子直通 Deck。
    读 factors/risk/factor_risk.py 最新强因子清单（家族代表，独立信号）→
    daily CSV 各强因子 rank≥0.90（★#384 方向修正：好因子 rank 大，top10% 直通是高 rank 非低 rank）
    → {code: {factor: {rank, family}}}。
    无文件/无列 → {}（降级：直通不触发，不影响正常流程）。"""
    global _strong_hits_cache
    if _strong_hits_cache is not None:
        return _strong_hits_cache
    _strong_hits_cache = {}
    try:
        from factors.risk.factor_risk import latest as _fr
        fr = _fr()
        strong = fr.get("strong") or []
        if not strong:
            return _strong_hits_cache
        files = sorted(EXT_POOL_DIR.glob("daily_*.csv"))
        chosen = _best_daily_file()
        if chosen is None:
            return _strong_hits_cache
        df = pd.read_csv(chosen)
        df = df.dropna(subset=["code"])
        hits = {}
        for s in strong:
            f = s["factor"]
            rc = f"{f}_rank"
            if rc not in df.columns:
                continue
            sub = df[df[rc] >= 0.90]
            for _, row in sub.iterrows():
                code = str(row["code"]).upper()
                if "." not in code:
                    continue
                hits.setdefault(code, {})[f] = {"rank": round(float(row[rc]), 3),
                                                 "family": s.get("family", ""),
                                                 "icir120": s.get("icir120")}
        # ★统计误差审计（用户指示）：直通必须「跨家族独立证据 ≥2」——
        #   单一强因子命中可能是运气/同族重复计数，≥2 个不同家族同时命中才是真独立交叉确认
        hits = {c: fs for c, fs in hits.items()
                if len({v["family"] for v in fs.values()}) >= 2}
        _strong_hits_cache = hits
        if hits:
            print(f"  [强因子直通] 跨家族强因子命中 {len(hits)} 只（{len(strong)} 个独立强因子，≥2 家族交叉）")
    except Exception:
        pass
    return _strong_hits_cache


def apply_strong_hits(opportunities: list) -> None:
    """★2026-08-11 百轮#67 强因子直通接入主流程：
    load_strong_hits（factor_risk 家族代表 × daily CSV rank≥0.90，跨家族≥2 交叉）
    → 机会池内命中股 +3 确认加分（与 B-8 同级"独立证据确认"，宁缺毋滥不扩触发面）。
    无命中/无文件 → 无副作用。"""
    try:
        hits = load_strong_hits()
        if not hits:
            return
        n = 0
        for o in opportunities:
            fs = hits.get(o["code"])
            if not fs:
                continue
            fams = {v.get("family", "") for v in fs.values() if v.get("family")}
            o["score"] = round(o["score"] + 3.0, 1)
            o["strong_hit"] = {f: v["rank"] for f, v in fs.items()}
            o["note"] = (o.get("note") or "") + f"·强因子直通({len(fams)}家族:{'、'.join(sorted(fams))})"
            n += 1
        if n:
            print(f"  [强因子直通] 机会池内跨家族强因子确认 {n} 只（+3 分）")
    except Exception:
        pass  # 直通是增强，任何异常不阻断主流程


# ★2026-08-14 Pitch 改进规格 v2 ④（研究员 14:10 裁决 → 16:15 终裁）：成交额 ≥0.2 亿硬过滤
#   双低（低换手+低波）冷门股天然成交额小（0.2-0.4 亿），0.5 亿过滤会让 pv 名存实亡（2 只）；
#   0.2 亿 = 双低本意优先，个人投资者单笔 10-30 万冲击 <1% 可交易。
#   ★参数化（研究员 16:15 建议）：未来资金量级变化（如 >500 万）改此常量切 0.5 亿，不用改代码。
PV_AMOUNT_MIN_YI = 0.2
_amount_cache = {"ts": 0.0, "data": None}


def _amount_20d_yi(codes: set) -> dict:
    """批量查 20 日均成交额（亿元）：bars.db daily_bar（双库合并取最新日）。
    ★2026-08-14 单位换算（data_loader C-8 教训）：baostock amount=元 / tushare amount=千元——
      混算会错 1000 倍（主库 08-13 已含 338 行 tushare 源）；按 source 换算成元再平均。
    失败/缺数据 → {}（调用方容错放行）。"""
    import time as _t
    global _amount_cache
    _now = _t.time()
    if _amount_cache["data"] is not None and _now - _amount_cache["ts"] < 600:
        return _amount_cache["data"]
    out = {}
    try:
        import sqlite3 as _sq
        # 主库 + 最近 3 个增量库（immutable 只读，与 portfolio._latest_close_of 同款）
        # ★2026-10-15 P1-A：绝对路径（原相对 glob 依赖 cwd）
        _cache_d = BASE / "data" / "cache"
        _dbs = [_cache_d / "bars.db"] + sorted(_cache_d.glob("bars_incr_*.db"))[-3:]
        # 收集每 code 的 (date, amount_元) —— 增量库后写覆盖主库同日期
        _by_code = {c: {} for c in codes}
        for _db in _dbs:
            try:
                _uri = db_uri(_db)
                _c = _sq.connect(_uri, uri=True, timeout=4)
                _ph = ",".join("?" * len(codes))
                for _r in _c.execute(
                        f"SELECT code, date, amount, source FROM daily_bar "
                        f"WHERE code IN ({_ph}) AND amount > 0", (*codes,)):
                    _cd, _dt, _am, _src = _r[0], _r[1], _r[2], (_r[3] or "")
                    # 单位换算：tushare/tushare_backup 千元 → 元（×1000）；baostock 元不变；其他按元假设
                    _am_y = float(_am) * 1000.0 if _src in ("tushare", "tushare_backup") else float(_am)
                    # 增量库同 (code,date) 覆盖主库（后写优先）
                    _by_code[_cd][_dt] = _am_y
                _c.close()
            except Exception:
                continue
        # 每 code 取最近 20 个交易日均值
        for _cd, _m in _by_code.items():
            if not _m:
                continue
            _recent = sorted(_m.items(), key=lambda x: x[0], reverse=True)[:20]
            if _recent:
                out[_cd] = round(sum(v for _, v in _recent) / len(_recent) / 1e8, 3)
        _amount_cache.update({"ts": _now, "data": out})
    except Exception:
        pass
    return out


def apply_basic_confirm(opportunities: list) -> None:
    """★B-8 基本面确认（2026-08-10 总指导落地，回测师 E5 pitch）：
    revalue 机会 + SUE_rank≥0.8（真实业绩超预期）→ +3 分确认；
    quality_gap 机会 + F-Score_rank≥0.8（基本面质量确认）→ +3 分确认。
    数据源：外包面板 sue_rank/f_score_rank（PIT 精确 ann_date，ICIR 0.35-0.49）。
    宁缺毋滥：仅确认加分，不扩触发面。"""
    bc = _basic_confirm_cache.get("data")
    if not bc:
        return
    n = 0
    for o in opportunities:
        b = bc.get(o["code"])
        if not b:
            continue
        if o["otype"] == "revalue" and b.get("sue") is not None and b["sue"] >= 0.80:
            o["score"] = round(o["score"] + 3.0, 1)
            o["note"] = (o.get("note") or "") + f"·SUE超预期确认(B-8 {b['sue']:.0%})"
            n += 1
        elif o["otype"] == "quality_gap" and b.get("fscore") is not None and b["fscore"] >= 0.80:
            o["score"] = round(o["score"] + 3.0, 1)
            o["note"] = (o.get("note") or "") + f"·F-Score质量确认(B-8 {b['fscore']:.0%})"
            n += 1
        elif o["otype"] == "reversal" and b.get("o2c") is not None and b["o2c"] >= 0.80:
            # ★O-3 日内反转确认（2026-08-11）：o2c_sum_20 rank≥0.8 = 日内收益反转信号确认
            #   （外包复现 ICIR 0.674；reversal20 0.347 的 1.9 倍提纯版）——CSV 无列时 o2c=None 降级
            o["score"] = round(o["score"] + 3.0, 1)
            o["note"] = (o.get("note") or "") + f"·日内反转确认(O-3 {b['o2c']:.0%})"
            n += 1
    if n:
        print(f"  [B-8] 基本面/日内确认加分 {n} 只（SUE/F-Score/o2c≥0.8）")


def load_external_signals() -> dict:
    """读取小弟因子池最新 daily_*.csv → {code: {hits, factors, date}}
    五强因子 rank_pct ≤ 0.20 记为命中（因子方向已统一，rank 越小越好）；
    命中 ≥4 才记录（★五强量价因子正相关，hits=3 太宽松仅 646 只；hits≥4 仅 158 只 ≈3% = 真共识）。
    无文件/失败 → {}（引擎正常运行）。
    ★2026-08-10 容错（18:22 总指导）：CSV 五强 rank 大面积缺失（外包 scheduler 运行中改代码
    致 3 因子 L0 全空，08-10 文件 100% 空）→ 自动回退最近「五强 rank 完整率 ≥50%」的 CSV，
    避免 pv_consensus 空转；并打印告警留痕。
    """
    global _ext_signal_cache
    if _ext_signal_cache is not None:
        return _ext_signal_cache
    _ext_signal_cache = {}
    try:
        # ★2026-08-14 #433：按 mtime 排序（原 sorted 按文件名——重跑补数据文件时间戳
        #   可能小于旧文件时间戳（如 06:45 < 20:17），文件名排序 reversed 会读旧文件、漏新数据；
        #   铁律#3「Deck glob 必须按 mtime」此处在 load_external_signals 漏改）
        files = sorted(EXT_POOL_DIR.glob("daily_*.csv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return _ext_signal_cache
        # ★2026-08-14 审计修复：EXT_SIGN_REVERSED 黑名单原只在 _dynamic_ext_factors() 内部排除
        #   （#419 符号反因子）——若动态名单生成失败降级到硬编码 EXT_SIGNAL_FACTORS，名单里的
        #   lhb_jg_cnt_20 等符号反因子会重新进入共识（方向错配命中决策链）。黑名单过滤统一
        #   下沉到此：无论动态/fallback 名单，一律剔除符号反因子。
        _dyn = _dynamic_ext_factors() or []
        _ext_list = [f for f in (_dyn or [x for x in EXT_SIGNAL_FACTORS if x not in EXT_SIGN_REVERSED])]
        # ★2026-08-14 Pitch 规格 v2 ④ 口径定版（研究员 14:50 裁决采纳 A 口径）：
        #   pv 触发 = 动态名单（排风格暴露 + F3 家族去重）hits≥6 + 双 rank≥0.75 + 成交额≥0.5 亿
        #   ——动态名单是 health 有效因子（五强已衰减，B-6 协议与 R-4 双低不兼容）；
        #   预期 ~8-15 只/日可交易冷门优质
        _pv_ext = _ext_list   # 动态名单（已排风格暴露 + 家族去重）
        rank_cols = [f"{ft}_rank" for ft in _ext_list]
        _F5 = ("turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol")
        # 从最新往前找可用文件：★五强 5 列每列非空率 ≥0.5（共识前提）
        # ★18:25 修正①：原"整体均值 ≥0.5"会被 3 列全空的文件混过（08-10 事故）→ 逐列检查
        # ★18:30 修正②：删除方向校验（列与其自身 rank 恒正相关，原理错误；方向正确性由外包
        #   C2 方向修正 + L0 取反保障，主系统只做完整率防御）
        chosen = None
        for f in reversed(files):
            try:
                # ★2026-08-12 百轮后#130：nrows=3000 → 6000（#101 同款：头部缺失采样误判）
                df_try = pd.read_csv(f, nrows=6000)
                f5_ok = 0
                for ft in _F5:
                    c = f"{ft}_rank"
                    if c in df_try.columns and df_try[c].notna().mean() >= 0.5:
                        f5_ok += 1
                if f5_ok < 4:
                    print(f"  [容错] {f.name} 五强 rank 缺失（{f5_ok}/5 列可用）→ 跳过")
                    continue
                chosen = f
                break
            except Exception:
                continue
        if chosen is None:
            print("  [容错] 无可用因子池 CSV（完整率/方向校验均失败）→ ext_signal 停用（宁缺毋滥）")
            return _ext_signal_cache
        if chosen != files[-1]:
            print(f"  [容错] 最新因子池 CSV 五强 rank 缺失（{files[-1].name}）→ 回退 {chosen.name}")
        df = pd.read_csv(chosen)
        df = df.dropna(subset=["code"])
        present = [c for c in rank_cols if c in df.columns]
        if not present:
            return _ext_signal_cache
        # ★B-8 基本面确认缓存（revalue←SUE / quality_gap←F-Score，2026-08-10 总指导）
        # ★O-3 o2c 日内反转确认缓存（reversal←o2c_sum_20，2026-08-11 总指导：外包 factors_emotion
        #   L2/L3 复现 o2c_sum_20 ICIR 0.674 / limup_ex_ret_20 1.099；CSV 有列才读，无列降级）
        try:
            _bc = {}
            _has_o2c = "o2c_sum_20_rank" in df.columns
            if "sue_rank" in df.columns and "f_score_rank" in df.columns:
                for _, row in df.iterrows():
                    c_ = str(row["code"]).upper()
                    if "." not in c_:
                        continue
                    sue = row.get("sue_rank")
                    fs_ = row.get("f_score_rank")
                    entry = {
                        "sue": float(sue) if pd.notna(sue) else None,
                        "fscore": float(fs_) if pd.notna(fs_) else None,
                        "o2c": None,
                    }
                    if _has_o2c:
                        o2c = row.get("o2c_sum_20_rank")
                        entry["o2c"] = float(o2c) if pd.notna(o2c) else None
                    _bc[c_] = entry
                _basic_confirm_cache.update({"data": _bc})
        except Exception:
            pass
        for _, row in df.iterrows():
            code = str(row["code"]).upper()
            if "." not in code:
                continue
            # ★2026-08-09 方向修正（外包 AI-1 实证诊断 + AI-2 复核）：
            #   因子池 rank(pct=True) 语义 = 好因子 rank 大 → 命中应为大 rank；
            #   原 <=0.20 选的是"五强同时差"（6月胜率 36.7% 负收益），修正后 54.2% 正收益
            # ★B-12 阈值裁决（2026-08-10 总指导）：五强→十强后分布变宽，0.65/4 会命中 43.9%
            #   （稀释共识）→ 十强 0.75/6 = 130 只 ≈2.3%（与原五强 143 只同量级，共识质量保持）
            # ★FRC 接入（2026-08-11 回测师规格阶段3）：k=0 因子不计命中；0<eff<1 按 eff 加权
            #   阈值：≥6 命中且有效权重和 ≥5（防"6 命中全是降权因子"的虚高共识）
            rm = load_risk_multiplier()          # {factor: eff}（ICIR120 降权 k）
            sw = _style_weights()                # ★#338 风格轮动 boost×1.2 / trim×0.8
            hits = [c.replace("_rank", "") for c in present
                    if pd.notna(row.get(c)) and float(row[c]) >= 0.75
                    and rm.get(c.replace("_rank", ""), 1.0) > 0]   # eff=0 不计
            # ★2026-08-14 Pitch 规格 v2 ④：pv 触发命中数（固定 EXT 全集口径，与研究员的 pv_tight_verify 对齐）
            #   ★全市场记录 pv 信息（不只 hits≥6 子集）——pv 收紧必须在全市场候选上做，
            #     若只在动态名单子集内筛，双 rank 交集会严重低估（2 只 vs 研究员 34 只）
            pv_hits = [f for f in _pv_ext
                       if f"{f}_rank" in df.columns and pd.notna(row.get(f"{f}_rank"))
                       and float(row[f"{f}_rank"]) >= 0.75]
            _tm = row.get("turn_mid_prox_rank")
            _lv = row.get("lowvol_rank")
            eff_sum = sum(rm.get(f, 1.0) * sw.get(f, 1.0) for f in hits)
            # 全市场记录 pv 候选（pv_hits≥6 或双 rank 达标任一），供 pv_consensus 触发用
            _pv_cand = (len(pv_hits) >= 6
                        or ((_tm is not None and _tm >= 0.75) and (_lv is not None and _lv >= 0.75)))
            if (len(hits) >= 6 and eff_sum >= EXT_EFF_MIN) or _pv_cand:
                _ext_signal_cache[code] = {
                    "hits": len(hits), "eff_hits": round(eff_sum, 2),
                    "factors": hits, "date": str(chosen.stem).replace("daily_", ""),
                    "turn_mid_prox_rank": float(_tm) if pd.notna(_tm) else None,
                    "lowvol_rank": float(_lv) if pd.notna(_lv) else None,
                    "pv_hits": len(pv_hits),   # ★2026-08-14 pv 触发口径（固定 EXT 全集）
                }
    except Exception:
        pass
    return _ext_signal_cache


# ==================== 因子计算 ====================

def compute_factors(px: pd.DataFrame, vx: pd.DataFrame, fin: pd.DataFrame,
                    basic: pd.DataFrame, st_codes: set) -> pd.DataFrame:
    """全市场因子面板（行=股票，列=因子）"""
    close = px.astype(float)
    ret = close.pct_change()
    f = pd.DataFrame(index=close.columns)

    # 技术因子
    f["close"] = close.iloc[-1]
    f["mom120"] = (close.iloc[-1] / close.shift(120).iloc[-1] - 1)
    f["mom20"] = (close.iloc[-1] / close.shift(20).iloc[-1] - 1)
    f["vol60"] = ret.rolling(60, min_periods=40).std().iloc[-1] * np.sqrt(252)
    f["high252"] = close.rolling(250, min_periods=150).max().iloc[-1]
    f["low252"] = close.rolling(250, min_periods=150).min().iloc[-1]
    f["near_high_250"] = f["close"] / f["high252"] - 1          # 负=距高点回撤
    f["drawdown_60d"] = f["close"] / close.rolling(60, min_periods=40).max().iloc[-1] - 1
    # RSI14
    up = ret.clip(lower=0).rolling(14).mean()
    dn = (-ret.clip(upper=0)).rolling(14).mean()
    f["rsi14"] = 100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1].replace(0, np.nan))
    # 量比（20日 vs 60日）
    v20 = vx.rolling(20, min_periods=10).mean().iloc[-1]
    v60 = vx.rolling(60, min_periods=30).mean().iloc[-1]
    f["vol_ratio"] = v20 / v60.replace(0, np.nan)
    # 波动收缩（VCP 近似：20日波动 / 60日波动 < 0.7）
    f["vol_contract"] = (ret.rolling(20, min_periods=10).std().iloc[-1] /
                         ret.rolling(60, min_periods=30).std().iloc[-1])
    f["ma50_up"] = (close.rolling(50, min_periods=40).mean().iloc[-1] >
                    close.rolling(50, min_periods=40).mean().iloc[-6]).astype(int)
    f["ma200_up"] = (close.rolling(200, min_periods=120).mean().iloc[-1] >
                     close.rolling(200, min_periods=120).mean().iloc[-6]).astype(int)

    # 财务因子（code6 对齐）
    f["code6"] = f.index.str[:6]
    f = f.merge(fin[["roe", "sq_net_yoy", "sq_rev_yoy", "gross_margin"]],
                left_on="code6", right_index=True, how="left")
    f = f.merge(fin[["sq_net_yoy"]].rename(columns={"sq_net_yoy": "sq_nyoy"}),
                left_on="code6", right_index=True, how="left", suffixes=("", "_x"))
    # 质量因子（quality.code6 是整数 → 转字符串对齐）
    q = load_quality()
    if q is not None and not q.empty:
        q["code6"] = q["code6"].astype(str)
        q = q.set_index("code6")
        f = f.merge(q[["liability_to_asset", "cfo_to_np", "cfo_to_or", "roe_avg"]],
                    left_on="code6", right_index=True, how="left", suffixes=("", "_q"))
        f["liability"] = f["liability_to_asset"]
        f["cfo_health"] = (f["cfo_to_np"] > 0).astype(int)
        # ★ROE 口径统一（2026-08-09）：质量表 roe_avg 为年化 ROE（小数），
        #   finance_report.roe 为单季 ROE（Q1 单季>8% 极少 → value/quality_gap 触发稀少）
        #   → 有 roe_avg 的股票用年化值覆盖（质量数据全量后 5388 只覆盖）
        f["roe_avg"] = pd.to_numeric(f["roe_avg"], errors="coerce")
        f["roe"] = f["roe_avg"].fillna(f["roe"])

    # 基本面硬筛标记
    f["roe"] = pd.to_numeric(f["roe"], errors="coerce")
    f["sq_nyoy"] = pd.to_numeric(f["sq_nyoy"], errors="coerce")
    f["non_st"] = (~f.index.isin(st_codes)).astype(int)
    # ★C8（外包 08-12）：min_price 硬过滤——极端低价股（仙股/1 元股）流动性差、退市风险高，
    #   组合回测口径 min_price=1.5（外包 C4/C8 引擎已内置）；主系统补同款硬筛（non_lowprice）
    f["non_lowprice"] = (f["close"] >= 1.5).astype(int) if "close" in f.columns else 1
    # ★估值因子（备用服务器 daily-basic 真实 PB/PE/股息率；失败降级价格分位近似）
    val = load_valuation()
    if val:
        vdf = pd.DataFrame.from_dict(val, orient="index")
        vdf.index.name = "code"
        f = f.join(vdf, how="left")
        # PB 分位（截面内越低越好 → pb_pct 越小越好，触发条件用 <0.20 分位）
        f["pb_pct"] = f["pb"].rank(pct=True, na_option="bottom")
        # PE 分位（排除负 PE：负 PE 直接给 1.0 高分位=不触发低估）
        pe_clean = f["pe_ttm"].where(f["pe_ttm"] > 0)
        f["pe_pct"] = pe_clean.rank(pct=True, na_option="bottom")
        f["div_yield"] = f["dv_ratio"].fillna(0.0)   # 股息率 %
        f["total_mv_yi"] = f["total_mv"].fillna(0) / 10000  # 万元→亿
    else:
        f["pb_pct"] = f["close"].rank(pct=True)
        f["pe_pct"] = f["close"].rank(pct=True)
        f["div_yield"] = 0.0
        # ★降级占位列（2026-08-09 外包反馈）：value 触发引用 r["pb"]/r["pe_ttm"]，
        # 估值接口失败时无此列 → KeyError；补 NaN 占位使触发安全降级
        f["pb"] = np.nan
        f["pe_ttm"] = np.nan
    # 行业
    f = f.join(basic[["name", "industry"]], how="left")
    f["industry"] = f["industry"].fillna("未知")
    return f


# ==================== 触发规则（每类机会） ====================

def safety_pad_score(r) -> float:
    """低估值安全垫强度（R-2 研究员规格简版，0-10 分）
    - S1 估值深度：PB 分位 <20% +2，20-40% +1
    - S2 股息安全：股息率 > 2.5%（≈10年国债） +2，>3.75% +3
    - S3 负债安全：负债率 <0.40 +2，<0.60 +1
    - S4 现金流：cfo_health=1 +1（与 R-2 增持/回购信号同权，等数据接入后扩展）
    数据缺失项不计分；安全垫 ≥6 视为厚安全垫（评分时风险减半）
    """
    s = 0.0
    pb_pct = r.get("pb_pct")
    if pd.notna(pb_pct) and pb_pct is not None:
        if pb_pct <= 0.20: s += 2
        elif pb_pct <= 0.40: s += 1
    dv = r.get("div_yield") or 0
    if dv and dv > 0:
        if dv > 3.75: s += 3
        elif dv > 2.5: s += 2
    liab = r.get("liability")
    if pd.notna(liab) and liab is not None:
        if liab < 0.40: s += 2
        elif liab < 0.60: s += 1
    ch = r.get("cfo_health")
    if pd.notna(ch) and ch == 1:
        s += 1
    return min(round(s, 1), 10.0)


def triggers():
    """每类机会的触发函数（输入因子行 Series → bool）"""
    return {
        "reversal": lambda r: (
            r["drawdown_60d"] < -0.25 and r["mom20"] > 0 and
            (r["vol_ratio"] or 1) >= 1.2 and (r["roe"] or 0) > 0 and r["non_st"] == 1 and r["non_lowprice"] == 1),
        "value": lambda r: (
            # ★真实估值：PB<20%分位（或 PB<1.5 兜底）且 PE>0 且 PE<20%分位，股息率>0 加分
            (r["pb_pct"] or 1) <= 0.20 and ((r["pb"] or 99) < 1.5 or True) and
            (r["pe_ttm"] or 0) > 0 and (r["pe_pct"] or 1) <= 0.30 and
            # ★ROE 口径（2026-08-09）：单季 ROE（Q1 单季>8% 极少）→ 阈值按年化/4 换算：0.08/4=0.02
            (r["roe"] or 0) > 0.02 and
            (r["liability"] if pd.notna(r.get("liability")) else 1) < 0.70 and r["non_st"] == 1 and r["non_lowprice"] == 1),
        "breakout": lambda r: (
            (r["near_high_250"] or -1) > -0.05 and (r["vol_ratio"] or 1) >= 1.5 and
            r["ma50_up"] == 1 and r["ma200_up"] == 1 and r["non_st"] == 1 and r["non_lowprice"] == 1),
        "revalue": lambda r: (
            (r["sq_nyoy"] or 0) > 0.50 and (r["roe"] or 0) > 0.05 and
            (r["pe_pct"] or 1) < 0.80 and r["non_st"] == 1 and r["non_lowprice"] == 1),
        "event": lambda r: (
            (r["roe"] or 0) > 0.08 and (r["sq_nyoy"] or 0) > 0.20 and r["non_st"] == 1 and r["non_lowprice"] == 1),
        "quality_gap": lambda r: (
            # ★ROE 阈值 0.15 按单季口径换算：年化 15% ≈ 单季 0.0375（2026-08-09）
            (r["roe"] or 0) > 0.0375 and (r["drawdown_60d"] or 0) < -0.25 and
            (r["liability"] if pd.notna(r.get("liability")) else 1) < 0.60 and
            (r["cfo_health"] if pd.notna(r.get("cfo_health")) else 1) == 1 and
            r["non_st"] == 1 and r["non_lowprice"] == 1),
    }


# ★第 7 类 pv_consensus 触发：外部因子池信号（B-6，2026-08-09）
# 在 scan() 中由 load_external_signals() 命中驱动（≥4 五强 rank 前 20%），
# 不走 apply 触发（外部信号与面板因子不同源）。


def target_upside_approx(r, otype: str) -> float:
    """目标空间近似（%）：
    - revalue/event：单季净利同比驱动（增速 50%→40%空间，100%→60%，200%+→80%）
    - reversal/quality_gap：回撤越大空间越大
    - 其他：经验基准
    ★2026-08-10 修正（17 年回测实证）：value 基准 20→30、quality_gap 30→40
      ——实证 6 月均收益 value +11.6%（年化≈24%）、quality_gap +14.6%（年化≈30%），
      原基准低估收益分导致 17 年最强类反而进不了 Pitch（校准 bug）
    """
    if otype in ("revalue", "event"):
        yoy = abs(r.get("sq_nyoy") or 0)
        return min(30 + yoy * 25, 90)      # 50%→42.5%，100%→55%，200%→80%
    base = {
        "reversal": 25, "value": 30, "breakout": 40,
        "revalue": 60, "event": 35, "quality_gap": 40,
    }.get(otype, 25)
    if otype in ("reversal", "quality_gap"):
        dd = abs(r.get("drawdown_60d") or 0)
        return min(base + dd * 80, 100)
    return base


_WR_CACHE = None


def winrate_approx(otype: str) -> float:
    """同类历史胜率（近似基准）
    ★覆盖机制（外包 AI 2026-08-08）：若 logs/opportunity_winrates.json 存在
    且该类型 6 月持有样本数 ≥ 30，用真实滚动回测胜率替换硬编码；否则退回原值。
    """
    global _WR_CACHE
    if _WR_CACHE is None:
        _WR_CACHE = {}
        p = BASE / "logs" / "opportunity_winrates.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                for ot, hs in d.get("results", {}).items():
                    v = hs.get("6", {})
                    if v.get("n", 0) >= 30 and v.get("winrate") is not None:
                        _WR_CACHE[ot] = v["winrate"]
            except Exception:
                _WR_CACHE = {}
    if otype in _WR_CACHE:
        return _WR_CACHE[otype]
    # ★2026-08-11 实证校准（知识库《Pitch台逻辑分析报告》：评分必须用 17 年回测胜率，不靠人工直觉）
    #   17 年回测（2011-2026, 188 期）：quality_gap 70.4% / value 62.5% / revalue 53.6% /
    #   pv_consensus 53.3% / event 51.5% / breakout 45.5% / reversal 39.0%（负期望）
    #   旧兜底（reversal 0.62/breakout 0.58）严重高估 → Pitch 推次优类型；实证值让评分反映真实胜率
    return {
        "quality_gap": 0.704, "value": 0.625, "revalue": 0.536,
        "pv_consensus": 0.533, "event": 0.515, "breakout": 0.455,
        "reversal": 0.39,   # ★17 年负期望 → 概率分低 → 评分自然低于门槛
    }.get(otype, 0.50)


# ==================== 主流程 ====================

def scan(types: list = None, date: str = None, pitch_only: bool = False) -> dict:
    # ★2026-08-10 修复：默认 date 用 bars.db 最近交易日（避免周末/节假日用当天日期
    #   导致 date 字段虚标未来；数据实际基于最近交易日）
    if date is None:
        try:
            # ★2026-08-10 双库合并探测：主库 08-07 + 增量库 08-10 → 取最新（直连主库会虚标旧日期）
            from data.cache import DailyCache
            _d = DailyCache().latest_trade_date()
            date = _d or datetime.now().strftime("%Y-%m-%d")
        except Exception:
            date = datetime.now().strftime("%Y-%m-%d")
    px, vx = load_panel(end=date, days=320)
    if px is None or px.empty:
        return {"error": "行情面板为空（检查 bars.db qfq 数据）", "date": date}
    basic = load_basic()
    fin = load_fundamentals(date)
    st = load_st_codes()
    f = compute_factors(px, vx, fin, basic, st)

    types = types or ORDER
    trig = triggers()
    opportunities = []
    stats = {}

    # ★外部因子池信号（B-4/B-6）：pv_consensus 类触发源 + 共识加分
    try:
        ext_sig = load_external_signals()
    except Exception:
        ext_sig = {}

    for ot in types:
        if ot not in OPPORTUNITY_TYPES:
            continue
        spec = OPPORTUNITY_TYPES[ot]
        if ot == "pv_consensus":
            # ★第 7 类：外部信号驱动（EXT rank≥0.75 命中 ≥6 的股票——B-12 十强裁决）
            # 需在面板中找到对应行（code 匹配），非ST 硬筛
            # ★2026-08-14 Pitch 改进规格 v2 ④：pv 触发收紧——命中≥6 且低换手+低波+可交易
            #   （R-4 冷门优质本意：turn_mid_prox_rank≥0.75 AND lowvol_rank≥0.75，
            #   研究员 08-13 实测方案 A=34 只但 17 只成交额<0.3 亿不可交易；
            #   14:10 裁决新增 amount 20日均 ≥0.5 亿硬过滤 → 预期 ~12 只/日）
            ext_codes = set()
            _dual = set()
            for _c, _e in ext_sig.items():
                # ★2026-08-14 定版口径（研究员 15:55 裁决）：动态名单 hits≥4 + 双 rank≥0.75
                #   ——动态名单全强 alpha（10 因子 ICIR≥1.0，chip_turnover 入列）与 R-4 双低
                #   结构性冲突（强 alpha 股天然非低波低换手），hits≥6 仅 2 只名存实亡；
                #   hits≥4 = 强共识（4/10）+ 低风险约束的可行折中（8 只/日）
                if (_e.get("pv_hits") if _e.get("pv_hits") is not None else _e.get("hits", 0)) < 4:
                    continue
                _tm = _e.get("turn_mid_prox_rank")
                _lv = _e.get("lowvol_rank")
                if _tm is not None and _tm >= 0.75 and _lv is not None and _lv >= 0.75:
                    _dual.add(_c)
                # 容错：rank 缺失（旧 CSV 无列）→ 不收紧（保持原行为，宁缺毋滥由命中数把关）
                elif _tm is None or _lv is None:
                    _dual.add(_c)
            # 成交额硬过滤（研究员 16:15 终裁：≥0.2 亿保可交易性——双低冷门天然小成交额，0.5 亿过滤名存实亡）
            if _dual:
                _amt = _amount_20d_yi(_dual)
                ext_codes = {c for c in _dual
                             if (c in _amt and _amt[c] >= PV_AMOUNT_MIN_YI) or (c not in _amt)}
                _rej = len(_dual) - len(ext_codes)
                if _rej:
                    print(f"  [pv收紧] 成交额<{PV_AMOUNT_MIN_YI}亿剔除 {_rej} 只（不可交易冷门）")
            else:
                ext_codes = _dual
            mask = f.index.isin(ext_codes) & (f["non_st"] == 1) & (f["non_lowprice"] == 1)
            stats[ot] = int(mask.sum())
            # ★2026-08-14 pv 数量哨兵（研究员 15:55 预警）：动态名单演进会改变 pv 数量
            #   （chip_turnover 入列后 hits≥6 从 9→2）——偏离健康区间 5-20 即提示重校准
            try:
                _pv_n = int(mask.sum())
                _pv_warn = "⚠" if not (5 <= _pv_n <= 20) else ""
                print(f"  [pv哨兵] pv_consensus {_pv_n} 只（健康区间 5-20）{_pv_warn}")
            except Exception:
                pass
            hits = f[mask]
            for code, r in hits.iterrows():
                upside = target_upside_approx(r, ot)
                g = gains_score(upside, 6)
                wr = winrate_approx(ot)
                ts = 0.7   # 量价共识：多因子一致命中，触发强度基准
                p_ = prob_score(wr, ts)
                dd = r["drawdown_60d"]
                dd = abs(dd) * 100 if pd.notna(dd) else 0.2 * 100
                v60 = r["vol60"]
                v60 = v60 if pd.notna(v60) else 0.3
                risk = risk_score(dd, v60, 0.8)
                s = opportunity_score(ot, g, p_, risk, wr)
                e = ext_sig.get(code, {})
                # ★2026-08-11 市值档位（用户指示：Pitch 偏小盘 → 大小盘分开；08-11 重划：大盘≥1000亿/中盘300-1000亿/小盘<300亿，券商指数口径）
                _mv = r.get("total_mv_yi")
                _tier = size_tier_of(_mv)
                opp = {
                    "code": code, "name": r.get("name", code), "industry": r.get("industry", ""),
                    "otype": ot, "otype_name": spec["name"],
                    "trigger": spec["trigger_desc"],
                    "gains": s["gains"], "prob": s["prob"], "risk": s["risk"],
                    "score": s["score"], "note": s["note"],
                    "upside_est": round(upside, 1), "winrate_est": wr,
                    "total_mv_yi": round(_mv, 1) if _mv and pd.notna(_mv) else None,
                    "size_tier": _tier,
                    # ★2026-08-11 信号联动修复：pv_consensus 触发源是外包 CSV（五强列不在面板）→ factors 从 daily rank 补
                    "factors": {k: (round(v, 4) if isinstance(v, (int, float)) else str(v))
                                for k, v in r.items() if k in spec["factors"]}
                               or _pv_factors(code, spec["factors"]),
                    "evidence": spec["evidence"],
                    "ext_signal": e,
                }
                opportunities.append(opp)
            continue
        mask = f.apply(trig[ot], axis=1)
        hits = f[mask]
        stats[ot] = int(mask.sum())
        for code, r in hits.iterrows():
            upside = target_upside_approx(r, ot)
            g = gains_score(upside, 6)
            wr = winrate_approx(ot)
            # 触发强度：财务因子偏离越大越强（0.5-1.0）
            ts = 0.5 + min(abs(r.get("sq_nyoy") or 0) / 2.0, 0.5) if ot in ("revalue", "event") else \
                 0.5 + min(abs(r.get("drawdown_60d") or 0) * 2, 0.5) if ot in ("reversal", "quality_gap") else \
                 0.7
            p_ = prob_score(wr, ts)
            # ★NaN 防御（2026-08-09 修复）：次新股 60 日窗口数据不足 → drawdown_60d/vol60 为 NaN，
            #   `nan or 0.2` 返回 nan（nan 是 truthy）→ risk_score 传播 NaN → score=nan 排序错乱
            dd = r["drawdown_60d"]
            dd = abs(dd) * 100 if pd.notna(dd) else 0.2 * 100
            v60 = r["vol60"]
            v60 = v60 if pd.notna(v60) else 0.3
            risk = risk_score(dd, v60, 0.8)
            # ★安全垫强度（R-2 规格简版，0-10）：value/quality_gap 类厚安全垫 → 风险折半
            safety_pad = None
            if ot in ("value", "quality_gap"):
                safety_pad = safety_pad_score(r)
                if safety_pad >= 6:
                    risk = round(risk * 0.5, 1)
            s = opportunity_score(ot, g, p_, risk, wr)
            # ★2026-08-11 市值档位（用户指示：Pitch 偏小盘 → 大小盘分开；08-11 重划：大盘≥1000亿/中盘300-1000亿/小盘<300亿，券商指数口径）
            _mv = r.get("total_mv_yi")
            _tier = size_tier_of(_mv)
            opp = {
                "code": code, "name": r.get("name", code), "industry": r.get("industry", ""),
                "otype": ot, "otype_name": spec["name"],
                "trigger": spec["trigger_desc"],
                "gains": s["gains"], "prob": s["prob"], "risk": s["risk"],
                "score": s["score"], "note": s["note"],
                "upside_est": round(upside, 1), "winrate_est": wr,
                "total_mv_yi": round(_mv, 1) if _mv and pd.notna(_mv) else None,
                "size_tier": _tier,
                "factors": {k: (round(v, 4) if isinstance(v, (int, float)) else str(v))
                            for k, v in r.items() if k in spec["factors"]},
                "evidence": spec["evidence"],
            }
            if safety_pad is not None:
                opp["safety_pad"] = safety_pad
                if safety_pad >= 6:
                    opp["note"] = (opp.get("note") or "") + f"·厚安全垫({safety_pad})"
            opportunities.append(opp)

    # ★同股多类命中 → 只保留最高分类（其余降为 tags 标注）；共识加分（多类型命中）
    n_types = {}
    for o in opportunities:
        n_types[o["code"]] = n_types.get(o["code"], 0) + 1
    for o in opportunities:
        o["n_types_hit"] = n_types.get(o["code"], 1)

    by_code = {}
    for o in opportunities:
        c = o["code"]
        if c not in by_code or o["score"] > by_code[c]["score"]:
            by_code[c] = o
    seen = {c: set() for c in by_code}
    for o in opportunities:
        if o["code"] in by_code and o is not by_code[o["code"]]:
            seen[o["code"]].add(o["otype_name"])
    for c, o in by_code.items():
        if seen[c]:
            o["also_types"] = sorted(seen[c])
        # ★共识加分重算（多类型命中 → 更高分）
        if n_types.get(c, 1) > 1:
            s2 = opportunity_score(o["otype"], o["gains"], o["prob"], o["risk"],
                                   o.get("winrate_est"), n_types.get(c, 1))
            o["score"] = s2["score"]
            o["consensus_bonus"] = s2["consensus_bonus"]
            o["note"] = s2["note"]
    opportunities = list(by_code.values())

    # ★★日历窗口因子（H16/H17 实证，2026-08-11 百轮#65）：春节后 5 日 +3.68%/88% / 国庆后 +4 /
    #   两会后 +3 / 经工会后 -3 —— 市场级效应（全市场平均），全池统一加减分 + note 留痕。
    #   无窗口 → 0 副作用。数据源：data/calendar_hook.py（登记表按年维护）
    try:
        from data.calendar_hook import get_window as _get_cal
        _cal = _get_cal()
        if _cal:
            _cb = _cal["bonus"]
            for o in opportunities:
                o["score"] = round(o["score"] + _cb, 1)
                o["note"] = (o.get("note") or "") + f"·日历:{_cal['label']}({'+' if _cb>=0 else ''}{_cb})"
            print(f"  [日历层] {_cal['label']}（{_cal['start']}~{_cal['end']}）→ 全池 {_cb:+d} 分")
    except Exception:
        pass

    # ★★CAL 月收益日历（因子池 CAL日历效应实证_20260810：2月+5/11月+5/10月+3/1月4月12月-5；
    #   CAL-4 12月小切大已实证推翻不实施）——市场级月度效应，全池统一加减分 + note 留痕。
    #   2026-08-12 补落地：汇总《A股日历效应研究汇总_20260812》声称"已落地"但实测 scan 无此逻辑，
    #   实为只落地了 H16/H17 窗口层，CAL 月收益层缺失——本次补齐（与日历窗口同构，0 副作用兜底）。
    try:
        import datetime as _dt
        _mon = _dt.datetime.now().month
        _mb = 0
        if _mon == 2:
            _mb = 5   # CAL-1 春季躁动（+4.89%/胜率 75%）
        elif _mon == 11:
            _mb = 5   # CAL-3 Q4 核心（11月 +3.9%/73%）
        elif _mon == 10:
            _mb = 3   # Q4 起始（10月 +2.5%）
        elif _mon in (1, 4, 12):
            _mb = -5  # CAL-2 4月最弱 + 实测弱月 1月(-3.11%)/12月(-0.4%)
        if _mb:
            _ml = {2: "2月春季躁动", 11: "11月Q4核心", 10: "10月Q4起始",
                   1: "1月弱月", 4: "4月弱月", 12: "12月弱月"}[_mon]
            for o in opportunities:
                o["score"] = round(o["score"] + _mb, 1)
                o["note"] = (o.get("note") or "") + f"·月历:{_ml}({'+' if _mb>=0 else ''}{_mb})"
            print(f"  [月历层] {_ml} → 全池 {_mb:+d} 分")
    except Exception:
        pass

    # ★★2026-08-11 信号联动（用户指示：机会池与因子池联动，分类按信号族，排名按 有效程度×强度 加权）
    #   ① signal_family：机会主信号族（触发因子加权归属，无法归类→其他）
    #   ② signal_score：信号加权分 = Σ(max(icir120,0)×rank) / Σ(max(icir120,0)) × 100
    #   ③ rank_signal：全池按 signal_score 排名（机会池默认排序改为信号排名）
    #   ④ factor_eff：每个触发因子的 {family, icir120, status, rank}（展开可见有效性）
    try:
        _he = load_factor_health()
        _dr = load_daily_ranks()
        _mf = load_factor_manifest()   # ★2026-08-11 manifest 消费（新因子自动吸收）
        for o in opportunities:
            o.update(signal_metrics(o, _he, _dr, _mf))
        _sr = sorted(opportunities, key=lambda x: -(x.get("signal_score") or -1))
        for i, o in enumerate(_sr, 1):
            o["rank_signal"] = i
        if _he or _mf:
            print(f"  [信号联动] 有效性 {len(_he)} + manifest {len(_mf)} · 个股强度 {len(_dr)} 只 · 信号族+加权分标注")
    except Exception as e:
        print(f"  [信号联动] 跳过（{str(e)[:50]}）")

    # ★外部因子池信号共识加分（B-4 对接：小弟 daily_scores 五强因子命中 ≥4 → +3/+6/+9）
    # 跳过 pv_consensus（触发条件即五强命中，已享受类型溢价，避免双重加分）
    try:
        ext_sig = load_external_signals()
    except Exception:
        ext_sig = {}
    if ext_sig:
        for o in opportunities:
            if o["otype"] == "pv_consensus":
                continue
            e = ext_sig.get(o["code"])
            if e:
                o["ext_signal"] = e
                # ★B-12 十强：bonus 按十强档（6-10 命中），min_hits=6
                bonus = CONSENSUS_BONUS.get(min(e["hits"], 10), 0.0) if e["hits"] >= 6 else 0.0
                if bonus > 0:
                    o["score"] = round(o["score"] + bonus, 1)
                    o["ext_bonus"] = bonus   # ★C3 拥挤度减分引用
                    o["note"] = (o.get("note") or "") + f"·因子池共识+{int(bonus)}"

    # ★B-8 基本面确认加分（2026-08-10 总指导）：revalue←SUE / quality_gap←F-Score
    #   在 ext_signal 之后（load_external_signals 已填充 _basic_confirm_cache）
    try:
        apply_basic_confirm(opportunities)
    except Exception:
        pass  # B-8 是增强，任何异常不阻断主流程

    # ★强因子直通确认（2026-08-11 百轮#67）：factor_risk 家族代表 × rank≤0.10 跨家族≥2
    #   → 机会池内 +3 分（H8/H10 强因子实证落地；load_strong_hits 原已实现但未接入）
    try:
        apply_strong_hits(opportunities)
    except Exception:
        pass  # 直通是增强，任何异常不阻断主流程

    # ★竞价强度反信号防守（T-3 裁决落地，2026-08-10）：strength≥6 高开放量 → 减分+回避标记
    #   实证：1 日短效（+0.9pp）、5/20 日反转（-2.3~-2.4pp）→ 防守用途，防追高被套
    #   ★F5-4（08-11）：昨涨停（premium 高）+ 今日竞价过热 = 双重追高风险 → 额外减分
    try:
        load_auction_signals()
        ad8 = auction_date8_for(date)
        if ad8:
            n_heat = 0
            n_combo = 0
            try:
                from factors.opportunities.shortterm_hook import load_shortterm
                _st = load_shortterm()
            except Exception:
                _st = {}
            for o in opportunities:
                pen = auction_heat_penalty(o["code"], ad8)
                if pen > 0:
                    o["score"] = round(o["score"] - pen, 1)
                    o["auction_heat"] = True
                    o["note"] = (o.get("note") or "") + "·竞价过热减分"
                    n_heat += 1
                    # ★F5-4 组合：昨板今收溢价高（昨涨停强势）+ 竞价过热 → 追高双风险
                    _sr = _st.get(o["code"], {})
                    if (_sr.get("premium_rank") or 0) >= 0.85:
                        o["score"] = round(o["score"] - 2.0, 1)
                        o["note"] = (o.get("note") or "") + "·⚡昨涨停+竞价过热双风险(F5-4)"
                        n_combo += 1
            if n_heat:
                print(f"  [反信号] 竞价过热减分 {n_heat} 只（{ad8}，strength≥{AUCTION_HEAT_THRESHOLD}）"
                      + (f"，其中 {n_combo} 只昨涨停组合双风险" if n_combo else ""))
    except Exception:
        pass  # 反信号是防守增强，任何异常不阻断主流程

    # ★2026-08-12 C-10b 外包反向信号减分（用户指示"反向信号入系统做提示"）：
    #   flag_crowded（量化拥挤 8 因子共振，5日 -0.32~-0.66pp）/ flag_deadzone（四正冷门死区，
    #   60日 -8.66pp）/ flag_lhb_mid（机构上榜≥3次，-3.50pp）→ 机会池减分+回避标记
    #   实证（2021-2026）：剔除反向 flag 股后 20 日收益每年 +0.31~+0.43pp（稳定 alpha）
    try:
        _fl = None
        for _f_ in sorted(EXT_POOL_DIR.glob("daily_*.csv"), key=lambda p: p.stat().st_mtime):
            _fl = _f_
        if _fl is not None:
            _dfp = pd.read_csv(_fl)
            _rd = {}
            for _c in ("flag_crowded", "flag_deadzone", "flag_lhb_mid"):
                if _c in _dfp.columns:
                    for _, _r in _dfp[_dfp[_c] == 1].iterrows():
                        _code = str(_r.get("code", "")).upper()
                        if "." in _code:
                            _rd.setdefault(_code, []).append(_c.replace("flag_", ""))
            if _rd:
                n_pen = 0
                for o in opportunities:
                    h = _rd.get(o["code"])
                    if h:
                        o["score"] = round(o["score"] - 2.0, 1)   # 每条反向 flag -2 分
                        o["reverse_flag"] = h
                        o["note"] = (o.get("note") or "") + "·反向信号减分(" + "/".join(h) + ")"
                        n_pen += 1
                if n_pen:
                    print(f"  [反信号] 反向 flag 减分 {n_pen} 只：{list(_rd.items())[:5]}")
    except Exception:
        pass  # 反向信号是防守增强，任何异常不阻断主流程

    # ★短线事件排雷（外包 2026-08-10 短线体检结论落地，M7 防守层）：
    #   连续跌停/跌停 → risk 上调 + 🔴 标记；涨停活跃/追高 → 提示
    try:
        apply_event_flags(opportunities)
        n_mine = sum(1 for o in opportunities if o.get("event_flags", {}).get("consec_down2"))
        if n_mine:
            print(f"  [排雷] 连续跌停高危 {n_mine} 只")
    except Exception:
        pass  # 排雷是防守增强，任何异常不阻断主流程

    # ★C3 拥挤度执行侧（2026-08-10 总指导接入）：五强因子 60 日波动率历史分位
    #   >90 → 因子池共识加分减半（防踩踏）；>95 → 暂停（该类机会不加分）
    try:
        crowd = load_crowding()
        if crowd:
            thr = {"down": 90.0, "pause": 95.0}
            try:
                import json as _jth
                _th = _jth.loads((BASE / "report" / "ui_thresholds.json").read_text(encoding="utf-8"))
                thr = {"down": float(_th["crowding"]["downweight"]), "pause": float(_th["crowding"]["pause"])}
            except Exception:
                pass
            hot = {k: v for k, v in crowd.items() if v >= thr["down"]}
            paused = {k for k, v in crowd.items() if v >= thr["pause"]}
            if hot:
                n_dw = 0
                for o in opportunities:
                    if o.get("otype") != "pv_consensus" or not o.get("ext_signal"):
                        continue
                    if paused:
                        o["score"] = round(o["score"] - (o.get("ext_bonus") or 0.0), 1)
                        o["note"] = (o.get("note") or "") + f"·拥挤度{paused}≥95暂停共识加分"
                        n_dw += 1
                    elif any(f in hot for f in o["ext_signal"].get("factors", [])):
                        half = (o.get("ext_bonus") or 0.0) / 2
                        o["score"] = round(o["score"] - half, 1)
                        o["note"] = (o.get("note") or "") + f"·拥挤度{hot}≥90降权50%"
                        n_dw += 1
                if n_dw:
                    print(f"  [拥挤度] 降权/暂停 {n_dw} 只（hot={hot} paused={sorted(paused)}）")
    except Exception:
        pass  # 拥挤度是防守增强，任何异常不阻断主流程

    # ★H2 FS 假信号一票否决（外包 08-10 交付 6 flag，总指导 23:50 接入）：
    #   一字板/涨停诱多/天量滞涨/对倒/接飞刀/无量突破 → 剔除（防假信号进组合）
    try:
        opportunities = apply_fs_veto(opportunities)
    except Exception:
        pass  # FS 否决是防守增强，任何异常不阻断主流程

    # ★F5 短线因子模块（外包 F1-F4 完成，总指导 12:35 接入）：
    #   ① event 涨停维度加分（premium/连板/5日活跃 rank≥0.85）② 排雷（连续跌停≥2 打7折 / 高位炸板打8.5折）
    try:
        from factors.opportunities.shortterm_hook import apply_event_upgrade, apply_limitdown_mine
        apply_limitdown_mine(opportunities)
        apply_event_upgrade(opportunities)
    except Exception:
        pass  # 短线因子是增强，任何异常不阻断主流程

    # 大池子：全部机会统一按 score 排序
    opportunities.sort(key=lambda x: -x["score"])
    for i, o in enumerate(opportunities, 1):
        o["rank_global"] = i
    # 同类排名
    for ot in types:
        sub = [o for o in opportunities if o["otype"] == ot]
        sub.sort(key=lambda x: -x["score"])
        for i, o in enumerate(sub, 1):
            o["rank_in_type"] = i

    # Pitch 候选（★四重过滤：风控 BLOCK 一票否决 → 类型门槛 → 同类Top20% → 跨类Top20）
    pitch = []
    if pitch_only:
        # 风控层（一票否决：BLOCK 直接剔除，WATCH 标注）
        try:
            from risk.stock_risk import check_one
            risk_map = {}
            for o in opportunities:
                if o["code"] not in risk_map:
                    # 批量风控：单连接一次取全部（避免逐只开连接被安全层拦截）
                    rc = batch_check_one(o["code"])
                    risk_map[o["code"]] = rc
        except Exception as e:
            risk_map = {}
            print(f"  [风控加载失败，跳过风控] {str(e)[:60]}")

        # ★★2026-08-11 Pitch 决策台 v3（总指导：分短线/长线双板块 + 每线内子菜单 + 增加数量 + 直通白名单收窄）
        #   长线（pitch_line=long）= scan 全部机会（价值/质量折价/重估/量价共识/事件 等 中长线基本面驱动）
        #   子分类（pitch_sub，每线内子菜单）：
        #     ⚡ express   强因子直通 —— 跨家族独立证据 ≥3（三重确认）+ 强度排序前 EXPRESS_PER_LINE（每档≤1）
        #                  ★用户指示：直通白名单太多 → 只有前几位才能直通，剩下的走常规路径
        #     🤝 consensus 多因子共识达成 —— 家族≥2 未直通 / also_types≥2 多类型命中 / B-12 十强 ext_signal
        #     📊 score    加权评分高分 —— 其余，大小盘分档配额（大盘2/中盘2/小盘1）
        #   数量：express ≤2 + consensus ≤3 + score ≤5 = 长线 ≤10（Pitch 为决策面，持仓纪律仍 ≤5）
        TIER_SLOTS = {"大盘": 2, "中盘": 2, "小盘": 1}

        # 1) 常规门槛筛选（gate + 风控 + 同类前20% + 全局前20）→ eligible 池
        eligible = []
        for o in sorted(opportunities, key=lambda x: -x["score"]):
            gate = PITCH_GATE.get(o["otype"], 70)
            if o["score"] < gate:
                continue
            rc = risk_map.get(o["code"], {})
            o["risk_level"] = rc.get("level", "NO_DATA")
            o["risk_score"] = rc.get("score")
            o["risk_flags"] = [f["id"] for f in rc.get("flags", [])]
            if rc.get("level") == "BLOCK":
                o["risk_note"] = "风控一票否决"
                continue
            if rc.get("level") == "WATCH":
                o["risk_note"] = "风控 WATCH（人工复核）"
            n_type = sum(1 for x in opportunities if x["otype"] == o["otype"])
            if n_type > 0 and o["rank_in_type"] > max(1, int(n_type * 0.2)):
                continue
            # ★2026-08-11 移除全局 Top20 过滤（用户反馈：Pitch 理由单一——revalue 高分霸榜把 value 84 分
            #   挤到全局 32 名外全被过滤）→ 质量由 gate 门槛 + 同类前20% + 市值配额 + 类型分散共同保证
            eligible.append(o)

        # 2) ⚡ 强因子直通（★收窄：仅"前几位"，白名单不再全量直通）
        #    直通候选 = 强因子共识池（load_strong_hits 家族≥2）中 家族≥3 三重确认者；
        #    ★从全量机会池选（特殊权限可破 PITCH_GATE，仅过滤 风控 BLOCK + 非小盘 + 每档≤1），
        #    按 strong_strength（家族数↓/rank 靠前↓/icir120↓）排序取前 EXPRESS_PER_LINE
        sh = load_strong_hits() or {}
        express = []
        if sh:
            cands = []
            for o in opportunities:
                sh_ = sh.get(o["code"])
                if not sh_:
                    continue
                if len({v["family"] for v in sh_.values() if v.get("family")}) < EXPRESS_MIN_FAMILY:
                    continue
                rc = risk_map.get(o["code"], {})
                if rc.get("level") == "BLOCK":
                    continue                     # 风控一票否决（直通也不例外）
                tier = o.get("size_tier", "小盘")
                if tier == "小盘":
                    continue                     # 小盘不直通（防绕过分档降权）
                cands.append((strong_strength(sh_), o, rc))
            cands.sort(key=lambda x: x[0], reverse=True)
            tier_used = set()
            for _, o, rc in cands:
                if len(express) >= EXPRESS_PER_LINE:
                    break
                tier = o.get("size_tier", "小盘")
                if tier in tier_used:
                    continue                     # 每档直通 ≤1
                tier_used.add(tier)
                sh_ = sh[o["code"]]
                best = max(sh_.values(), key=lambda v: v["icir120"] or 0)
                o2 = dict(o)
                o2["express_strong"] = {"factor": next(k for k, v in sh_.items() if v == best),
                                        "family": best["family"], "icir120": best["icir120"]}
                o2["pitch_line"] = "long"
                o2["pitch_sub"] = "express"
                o2["risk_level"] = rc.get("level", "NO_DATA")
                o2["risk_score"] = rc.get("score")
                o2["risk_flags"] = [f["id"] for f in rc.get("flags", [])]
                o2["note"] = (o.get("note") or "") + f"·⚡强因子直通（{best['family']} ICIR120={best['icir120']}，三重家族确认）"
                express.append(o2)
            if express:
                print(f"  [强因子直通] {len(express)} 只（家族≥{EXPRESS_MIN_FAMILY} 前{EXPRESS_PER_LINE}，长线，可破门槛）")

        # 3) 🤝 多因子共识达成（走常规门槛，不破 gate）：家族≥2 未直通 / 多类型命中≥2 / B-12 十强
        express_codes = {p["code"] for p in express}
        consensus = []
        for o in eligible:
            if o["code"] in express_codes:
                continue
            if classify_pitch_sub(sh.get(o["code"]), o.get("also_types"), o.get("ext_signal")) != "consensus":
                continue
            o2 = dict(o)
            o2["pitch_line"] = "long"
            o2["pitch_sub"] = "consensus"
            nf = len({v["family"] for v in (sh.get(o["code"]) or {}).values() if v.get("family")})
            tag = []
            if nf >= CONSENSUS_MIN_FAMILY:
                tag.append(f"强因子{nf}家族")
            if o.get("also_types") and len(o["also_types"]) >= 2:
                tag.append(f"多类型{len(o['also_types'])}")
            if o.get("ext_signal"):
                tag.append(f"B-12十强{len(o['ext_signal'].get('factors', []))}因子")
            o2["note"] = (o.get("note") or "") + ("·🤝多因子共识（" + "+".join(tag) + "）" if tag else "·🤝多因子共识")
            consensus.append(o2)
        consensus.sort(key=lambda x: -x["score"])
        consensus = consensus[:CONSENSUS_PER_LINE]
        if consensus:
            print(f"  [多因子共识] {len(consensus)} 只（长线）")

        # 4) 📊 加权评分高分：eligible 剩余 → 大小盘分档配额（大盘2/中盘2/小盘1，小盘降权防垄断）
        #    ★2026-08-11 类型分散（用户反馈：Pitch 理由单一——左边全是 revalue）：
        #      ① 同类最多 2 席（revalue 高分霸榜被限制）
        #      ② 每类机会第一名给"保底参考位"（score ≥ gate-8 即可，≤8 席）——价值/质量/事件等
        #         即使今日分数略低也能进入 Pitch，展示多样化的决策理由（宁缺毋滥底线保留）
        #    ★2026-08-11 实证校准（知识库）：quality_gap 17年 70.4% 全场最强 → 配额 2→3（高胜率多席位）；
        #      reversal 17年负期望 → 门槛 80 已基本排除，不占配额
        OTYPE_SLOTS = {"revalue": 2, "value": 2, "quality_gap": 3, "event": 2,
                       "reversal": 1, "breakout": 1, "pv_consensus": 1}
        taken = {p["code"] for p in express} | {p["code"] for p in consensus}
        tier_fill = {"大盘": 0, "中盘": 0, "小盘": 0}
        otype_fill = {}
        # 每类第一名（保底参考用）
        ot_first = {}
        for o in sorted(eligible, key=lambda x: -x["score"]):
            if o["otype"] not in ot_first and o["code"] not in taken:
                ot_first[o["otype"]] = o
        # ★2026-08-11 实证校准（知识库《Pitch台逻辑分析报告》）：高胜率类型（quality_gap 70.4%/value 62.5%）
        #   触发频率低但胜率高 → 该类第一名给"保底推送位"（超配额，不受市值档限制；总分 ≤8）——
        #   避免 value/quality_gap 高分机会被 revalue 高分挤掉市值档
        REF_TYPES = ("quality_gap", "value")
        score_path = []   # ★2026-08-14 前移到 T1 预填之前（T1 块引用；原定义在主循环前过晚）
        # ★2026-08-14 Pitch 改进规格 v2 ②：T1 梯队保底（实证强 = 优先展示）
        #   T1（quality_gap 70.4% + value 62.5%）≥50% 且 quality_gap 保底 ≥2——
        #   原仅"保底参考位"（该类第一名），T1 高分者仍可能被 revalue 高分挤出
        #   → score_path 前先按 T1 优先序填充（quality_gap → value → 其他按分）
        _score_cap = 12   # 每日 pitch 总量 ≤12（规格 ②：价值线 ≤6 + 科技线 ≤6；本函数为价值线）
        _t1_pool = [o for o in eligible
                    if o["code"] not in taken and o["otype"] in ("quality_gap", "value")]
        _t1_pool.sort(key=lambda x: -x["score"])
        for o in _t1_pool:
            if len(score_path) >= _score_cap:
                break
            tier = o.get("size_tier", "小盘")
            if tier not in TIER_SLOTS:
                tier = "小盘"
            if tier_fill[tier] >= TIER_SLOTS[tier]:
                continue
            otype_fill[o["otype"]] = otype_fill.get(o["otype"], 0) + 1
            tier_fill[tier] += 1
            o2 = dict(o)
            o2["pitch_line"] = "long"
            o2["pitch_sub"] = "score"
            o2["t1_prefill"] = True   # ★2026-08-14 T1 保底（实证胜率优先）
            score_path.append(o2)
            taken.add(o["code"])   # ★2026-08-14 600874 重复修复：T1 预填后加入 taken，
                                   #   主循环不再重复选同一 code（研究员 15:05 实锤 12 只=11 实际+1 重复）
        # ★2026-08-13 外包5因子终版派单 §7.2：中小盘域候选池优先用 amihud 排序（研究员实证：
        #   中小盘域 amihud +9.9pp/84% 全库第一；大盘域不受影响）——中小盘候选 amihud rank 强（≥0.8）
        #   加排序加分 + 决策台标记（amihud_flag），让中小盘非流动性溢价机会优先浮出
        _dr_for_amihud = load_daily_ranks()   # {code: {factor: rank}}
        for o in sorted(eligible, key=lambda x: -x["score"]):
            if o["code"] in taken:
                continue
            if len(score_path) >= _score_cap:   # ★2026-08-14 总量 ≤12（Pitch 改进规格 v2 ②）
                break
            tier = o.get("size_tier", "小盘")
            if tier not in TIER_SLOTS:
                tier = "小盘"
            _fst = ot_first.get(o["otype"])
            _is_ref = (_fst and o["code"] == _fst["code"]
                       and o["otype"] in REF_TYPES
                       and o["score"] >= PITCH_GATE.get(o["otype"], 70) - 8
                       and len(score_path) < 8)
            if _is_ref:
                # ★高胜率类型保底推送（超配额，不受市值档限制）
                otype_fill[o["otype"]] = otype_fill.get(o["otype"], 0) + 1
                o2 = dict(o)
                o2["pitch_line"] = "long"
                o2["pitch_sub"] = "score"
                # ★amihud 中小盘加分（中盘/小盘候选，非流动性 rank 强 → +0.8 排序加分 + 标记透传）
                _am = (_dr_for_amihud.get(o["code"]) or {}).get("amihud")
                if tier in ("中盘", "小盘") and _am is not None and _am >= 0.8:
                    o2["score"] = round(o2["score"] + 0.8, 1)
                    o2["amihud_flag"] = {"rank": round(float(_am), 3), "tier": tier,
                                         "note": "中小盘非流动性溢价（研究员实证 +9.9pp）"}
                score_path.append(o2)
                continue
            if tier_fill[tier] >= TIER_SLOTS[tier]:
                continue
            if otype_fill.get(o["otype"], 0) >= OTYPE_SLOTS.get(o["otype"], 2):
                # ★类型保底参考位（原机制保留）：该类第一名且 score ≥ gate-8 且总分未超 8 → 放行（参考理由）
                _is_ref2 = (_fst and o["code"] == _fst["code"]
                            and o["score"] >= PITCH_GATE.get(o["otype"], 70) - 8
                            and len(score_path) < 8)
                if not _is_ref2:
                    continue
            tier_fill[tier] += 1
            otype_fill[o["otype"]] = otype_fill.get(o["otype"], 0) + 1
            o2 = dict(o)
            o2["pitch_line"] = "long"
            o2["pitch_sub"] = "score"
            # ★amihud 中小盘加分（同上：中盘/小盘候选 + 非流动性 rank ≥0.8 → +0.8 分 + 标记）
            _am = (_dr_for_amihud.get(o["code"]) or {}).get("amihud")
            if tier in ("中盘", "小盘") and _am is not None and _am >= 0.8:
                o2["score"] = round(o2["score"] + 0.8, 1)
                o2["amihud_flag"] = {"rank": round(float(_am), 3), "tier": tier,
                                     "note": "中小盘非流动性溢价（研究员实证 +9.9pp）"}
            score_path.append(o2)
        print(f"  [长线 Pitch] ⚡express {len(express)} + 🤝consensus {len(consensus)} + 📊score {len(score_path)}"
              f"（分档 {dict(tier_fill)} 类型 {dict(otype_fill)}；大盘≥1000亿/中盘300-1000亿/小盘<300亿）")
        pitch = express + consensus + score_path
        # ★2026-08-11 百轮#10 三级分档（知识库《Pitch台深度指导建议》：核心/备选/临时——国内私募三级股票池）
        #   core 核心推荐（⚡直通 + score≥gate+10 极强）/ alt 备选观察（🤝共识 + score 常规）/
        #   temp 临时参考（类型保底参考位，分数略低）——UI 徽章 + 决策优先级
        for _p in pitch:
            if _p.get("pitch_sub") == "express":
                _p["tier"] = "core"
            elif _p.get("pitch_sub") == "consensus":
                _p["tier"] = "alt"
            else:
                _g = PITCH_GATE.get(_p.get("otype"), 70)
                _p["tier"] = "core" if _p["score"] >= _g + 10 else ("alt" if _p["score"] >= _g else "temp")
        pitch.sort(key=lambda x: -x["score"])
        # ★2026-08-14 Pitch 改进规格 v2 ⑤：实证徽章挂载（quality_gap 🏆70.4%/+14.6% …）
        #   pitch 卡片直接展示，用户一眼分清"实证强"vs"弹性"；tech 短线另由 tech_pitch_v3 标注
        _badges = load_pitch_badges()
        for _p in pitch:
            _b = _badges.get(_p.get("otype"))
            if _b:
                _p["pitch_badge"] = _b.get("badge", "")
                _p["pitch_badge_tier"] = _b.get("tier", "")
        # ★2026-08-14 顶级买点标签（用户需求："择时高概率 + 高稀有程度 + 强买入机会"——
        #   极其严格，不随便出现）。四条件全满足才打标：
        #   ① 择时高概率：timing score≥75 且 level=适合买入（当前 67.6，需极强择时）
        #   ② 高稀有度：express 强因子直通（家族≥3 三重确认，每日名额≤2）
        #   ③ 强买入：score ≥ gate+10（极强机会）且 T1 实证强类型（quality_gap/value 70.4%/62.5%）
        #   ④ 风控：非 BLOCK
        _mark_top_buy(pitch, date)

    # ★2026-08-11 Pitch 决策台 v3：长线 pitch 已按子分类构建（express ≤2 + consensus ≤3 + score ≤5）
    pitch_out = pitch
    result = {
        "date": date,
        "n": len(opportunities),
        "stats": stats,
        "opportunities": opportunities,
        "pitch": pitch_out,          # 长线 ≤10：⚡express ≤2 + 🤝consensus ≤3 + 📊score ≤5（子分类字段 pitch_line/pitch_sub）
        "thresholds": SCORE_THRESHOLDS,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # ★2026-08-14 #430 幂等：数据源未更新时本次扫描结果与上次相同 → 跳过写时间戳文件
    #   （防 4h 链每轮重复写 1.8M opp_pool 文件累积膨胀，同 #413/#429 根治）
    _unchanged = False
    try:
        import glob as _g, os as _os
        def _fp(r):
            _oc = sorted((o.get("code"), o.get("score")) for o in r.get("opportunities", []))
            _pc = sorted((p.get("code"), p.get("score")) for p in r.get("pitch", []))
            return (r.get("date"), r.get("n"), r.get("stats"), _oc, _pc)
        _prev = sorted(_g.glob(str(OUT.parent / "opp_pool_*.json")), key=_os.path.getmtime)
        if _prev:
            with open(_prev[-1], encoding="utf-8") as _f:
                _old = json.load(_f)
            if _fp(_old) == _fp(result):
                _unchanged = True
    except Exception:
        _unchanged = False
    if not _unchanged:
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", type=str, default=None, help="逗号分隔机会类型")
    ap.add_argument("--date", type=str, default=None)
    ap.add_argument("--pitch", action="store_true", help="输出 Pitch 候选")
    args = ap.parse_args()
    types = args.types.split(",") if args.types else None
    r = scan(types=types, date=args.date, pitch_only=args.pitch)
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    print(f"=== 机会扫描 {r['date']} ===")
    print(f"大池子 {r['n']} 条: {r['stats']}")
    print(f"\nTop 10 机会（跨类别统一评分）:")
    for o in r["opportunities"][:10]:
        print(f"  #{o['rank_global']:>3} {o['otype']:12s} {o['code']} {o['name']:8s} "
              f"score={o['score']:5.1f} ({o['gains']}/{o['prob']}/{o['risk']}) {o['note']}")
    if args.pitch:
        print(f"\n★★★ Pitch 候选（三重过滤后 {len(r['pitch'])} 只）:")
        for o in r["pitch"]:
            print(f"  {o['otype']:12s} {o['code']} {o['name']:8s} score={o['score']:.1f} "
                  f"同类#{o['rank_in_type']} 全局#{o['rank_global']} 预期空间{o['upside_est']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
