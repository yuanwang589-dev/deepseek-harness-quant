# -*- coding: utf-8 -*-
"""factors/pool/builtin_purify.py — 内建因子净化层（外包 manifest 缺失时的兜底数据源）

背景：因子池全景页（净化分类/当前因子/风控层）依赖外包管线产出的
      data/factorpool/output/factor_manifest_*.json + health/health_*.csv。
      开源部署未接外包管线 → 目录不存在 → 页面"无数据"。

本模块从主系统自有的因子知识（signal_family 信号族映射 + factor_engine 方向实证 +
scan.compute_factors 面板列 + README/代码注释中的 ICIR 实证值）构建内建净化清单，
输出与外包契约同构的 manifest/health 结构，供 deck.live_api.live_factor_dash 在
外包文件缺失时回退。

判定口径与前端 ui_v2/pages/factors.html loadPurify 对齐：
  A 稳定正向 = |icir120| ≥ 0.2 且 status 非 失效/数据不足（可正常入选股）
  B 反转/漂移 = status 含"反向"（direction=-1 的反转族/动态符号，不固定权重）
  C 噪声/弱   = 其余（未实证 / 弱 IC / FRC 排雷 / 剔除）

用法：
  from factors.pool.builtin_purify import build_manifest, build_health
  build_manifest()  # -> {"date", "n", "factors": [...], "categories": {...}, ...}
"""
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from factors.signal_family import SIGNAL_FAMILY, SIGNAL_FAMILY_ORDER, signal_family_of

# ══════════════════════════════════════════════════════════════════
# 内建因子元数据（来源：README 实证表 + signal_family/factor_engine/scan 注释）
#   dir:    +1 正向 / -1 反向（动态符号）/ 0 剔除
#   icir120: 120 日 ICIR 实证值（None = 无实证）
#   icir60:  60 日 ICIR 实证值（None = 无）
#   status:  特殊状态覆盖（None = 按 dir/icir 自动生成）
#   cn:      中文名（前端展示）
# ══════════════════════════════════════════════════════════════════
_BUILTIN_META = {
    # ── A 类实证有效（|icir120|≥0.2 正向；出处见注释）──────────────
    "turn_mid_prox":     {"dir": 1,  "icir120": 0.87,  "cn": "换手率中值代理"},   # README 换手率族 ICIR 0.87
    "limup_ex_ret_20":   {"dir": 1,  "icir120": 1.826, "cn": "涨停次日超额20"},   # signal_family ICIR120 1.826 涨停族最强
    "limup_ex_5":        {"dir": 1,  "icir120": 1.24,  "cn": "涨停次日5日超额"},  # README limup_ex_5 ICIR 1.24
    "amihud":            {"dir": 1,  "icir120": 0.43,  "cn": "Amihud非流动性"},   # README 0.43 多空夏普 1.12
    "max_ret20":         {"dir": 1,  "icir120": 0.64,  "cn": "20日最大收益"},     # README 彩票/偏度族 0.64
    "open_prem_20":      {"dir": 1,  "icir120": 0.96,  "icir60": 0.958, "cn": "开盘溢价20"},  # README 0.96 + signal_family ICIR60 0.958
    "f_score":           {"dir": 1,  "icir120": 0.49,  "cn": "基本面F值"},        # README f_score 120日 0.49
    "sue":               {"dir": 1,  "icir120": 1.193, "cn": "盈余惊喜"},         # FACTOR_ALIAS 注释 sue icir120 1.193
    "sq_nyoy":           {"dir": 1,  "icir120": 1.193, "cn": "单季净利同比"},     # alias→sue 基本面族 1.193
    "inst_surv":         {"dir": 1,  "icir120": 1.193, "cn": "机构调研强度"},     # alias→sue
    "shebao_chg":        {"dir": 1,  "icir120": 1.392, "cn": "社保持仓变化"},     # FACTOR_ALIAS 社保族 1.392
    "inst_hold_chg":     {"dir": 1,  "icir120": 1.392, "cn": "机构持仓变化"},     # alias→shebao_chg
    "lhb_jg_cnt_20":     {"dir": 1,  "icir120": 0.90,  "cn": "龙虎榜机构20日"},   # signal_family 0.900
    "alpha015":          {"dir": 1,  "icir120": 0.76,  "cn": "Alpha015"},         # README 0.76
    "o2c_sum_20":        {"dir": 1,  "icir120": 0.674, "cn": "日内反转提纯20"},   # signal_family ICIR 0.674
    "kline_hammer_cnt":  {"dir": 1,  "icir120": 27.4,  "cn": "锤子线计数"},       # D10 ICIR 27.4 win 78%
    "kline_doji_cnt":    {"dir": 1,  "icir120": 25.1,  "cn": "十字星计数"},       # D10 ICIR 25.1 win 75%
    "kline_shooting_cnt": {"dir": 1, "icir120": 26.5,  "cn": "射击之星计数"},     # D10 ICIR 26.5 win 78%
    "open_gap":          {"dir": 1,  "icir120": 31.4,  "icir60": 31.4, "cn": "开盘缺口"},  # signal_family 缺口动量年正71%
    "limit_down_flag":   {"dir": 1,  "icir120": 0.97,  "cn": "跌停标记"},         # README 跌停排雷 0.97
    "consec_limit_down": {"dir": 1,  "icir120": 0.97,  "cn": "连续跌停"},         # README 跌停排雷族 0.97
    "ind_crowd_60":      {"dir": 1,  "icir120": 0.297, "cn": "行业拥挤度60"},     # signal_family 0.297 最强行业因子
    # ── B 类反转/动态符号（dir=-1，A 股反转实证）───────────────────
    "mom_20":            {"dir": -1, "cn": "20日动量(反转)"},                     # factor_engine A股短期反转显著
    "mom_120":           {"dir": -1, "cn": "120日动量(反转)"},                    # factor_engine 小盘池反转
    "mom20":             {"dir": -1, "cn": "20日动量(反转)"},                     # scan 面板同口径
    "mom120":            {"dir": -1, "cn": "120日动量(反转)"},                    # scan 面板同口径
    "mom60":             {"dir": -1, "cn": "60日动量(反转)"},                     # signal_family 反转族
    "rps_120":           {"dir": -1, "cn": "相对强度120(反转)"},                  # factor_engine 降权反转
    "lowvol":            {"dir": -1, "cn": "低波动(反用)"},                       # 低波=好，README 防守底仓
    "lowvol_60":         {"dir": -1, "cn": "60日低波动(反用)"},                   # factor_engine 低波正用
    "std20":             {"dir": -1, "cn": "20日波动(反用)"},                     # README 低波动族
    "vol60":             {"dir": -1, "cn": "60日波动率(反用)"},                   # scan 面板低波口径
    "downside_vol":      {"dir": -1, "cn": "下行波动(反用)"},                     # README 低波动族
    "reversal20":        {"dir": -1, "cn": "20日反转"},                           # README 反转族
    "reversal5":         {"dir": -1, "cn": "5日反转"},                            # signal_family 反转族
    "drawdown60":        {"dir": -1, "cn": "60日回撤(超跌)"},                     # 超跌反弹反转用
    "drawdown_60d":      {"dir": -1, "cn": "60日回撤(超跌)"},                     # scan 面板同口径
    "ind_rs_20":         {"dir": -1, "icir120": 0.175, "cn": "行业相对强弱反转"},  # signal_family 20日 ICIR 0.175
    "open_vol_share":    {"dir": -1, "cn": "开盘量占比(低换手)"},                  # D7 +9.4% 低换手族 direction=-1
    # ── C 类 FRC 排雷（负 IC 极端选股灾难，只宜排雷）────────────────
    "tail_vol_gain":     {"dir": 0,  "icir120": -17.4, "status": "失效·FRC排雷", "cn": "尾盘放量拉升"},   # ICIR -17.4 诱多
    "intraday_range":    {"dir": 0,  "icir120": -43.8, "status": "失效·FRC排雷", "cn": "日内振幅"},       # ICIR -43.8 全库最强负
    "am_pm_ratio":       {"dir": 0,  "icir120": -21.0, "status": "失效·FRC排雷", "cn": "上下半日量比"},   # ICIR -21 情绪透支
    "close30_ret":       {"dir": 0,  "status": "失效·FRC排雷", "cn": "尾盘30分收益"},                     # 弱 IC 排雷
    "tail60_ret":        {"dir": 0,  "status": "失效·FRC排雷", "cn": "尾盘60分收益"},
    "pm_ret":            {"dir": 0,  "status": "失效·FRC排雷", "cn": "午后收益"},
    "head60_ret":        {"dir": 0,  "status": "失效·FRC排雷", "cn": "开盘60分收益"},
    "first_half_ret":    {"dir": 0,  "status": "失效·FRC排雷", "cn": "上午收益"},
    "tail_vol_share":    {"dir": 0,  "status": "失效·FRC排雷", "cn": "尾盘量占比"},
    "vshape":            {"dir": 0,  "status": "失效·FRC排雷", "cn": "V型反转形态"},
    "open30_ret":        {"dir": 0,  "status": "失效·FRC排雷", "cn": "开盘30分收益"},
    "kline_bear_streak": {"dir": 0,  "icir120": -17.8, "status": "失效·FRC排雷", "cn": "连阴线"},        # D10 ICIR -17.8
    "kline_5m_std":      {"dir": 0,  "icir120": -27.8, "status": "失效·FRC排雷", "cn": "5分钟波动"},      # D10 ICIR -27.8
    "kline_ret_skew":    {"dir": 0,  "status": "失效·FRC排雷", "cn": "收益偏度"},                         # 负 IC 排雷
    "kline_mom_ratio":   {"dir": 0,  "status": "失效·FRC排雷", "cn": "动量比"},
    "kline_last_5m":     {"dir": 0,  "status": "失效·FRC排雷", "cn": "最后5分钟"},
    # ── C 类其他剔除（暂缓/冗余/无方向）────────────────────────────
    "close_to_high":          {"dir": 1,  "status": "弱·暂缓", "cn": "收盘相对高点"},      # #251 暂缓被替代
    "strong_close_quiet_open": {"dir": 0, "status": "失效·冗余剔除", "cn": "强收盘弱开盘"},  # #379 已剔除 0.23 弱+冗余
    "new_high_250":           {"dir": 0,  "status": "失效·无方向", "cn": "52周新高"},        # 各口径均为负
    # ── 主系统 scan 面板补充列（实际可计算，未实证 → 弱·未实证）────
    "gross_margin":     {"cn": "毛利率"},
}

# 族名标签键（非真实因子，仅 signal_family 分类用，剔除出清单）
_FAMILY_TAG_KEYS = {"value", "growth", "quality", "turnover", "sentiment"}


def _status_of(meta: dict) -> str:
    """净化状态：特殊覆盖优先，否则按 dir/icir120 自动生成（与前端判定对齐）"""
    if meta.get("status"):
        return meta["status"]
    _dir = meta.get("dir", 1)
    _ic = meta.get("icir120")
    if _dir == 0:
        return "失效·无方向"
    if _dir == -1:
        return "反向·动态符号"          # 前端 status 含"反向" → B
    if _ic is not None:
        if _ic >= 0.2:
            return "有效·实证"          # 前端 |icir|≥0.2 且非失效 → A
        if _ic >= 0.1:
            return "弱有效·待观察"      # 0.1~0.2 不进 A → C
        return "失效·弱IC"
    return "弱·未实证"                   # 无实证 → C


def _factor_catalog() -> list:
    """内建因子全集：signal_family 全因子 + factor_engine/scan 补充列，去重"""
    names = [k for k in SIGNAL_FAMILY if k not in _FAMILY_TAG_KEYS]
    for extra in ("lowvol_60", "rps_120", "new_high_250", "limup_ex_5",
                  "vol60", "gross_margin", "drawdown_60d", "mom20", "mom120"):
        if extra not in names:
            names.append(extra)
    return names


def _sorted_factors():
    """按信号族展示顺序 + 族内 ICIR 降序排列"""
    cats = {k: [] for k in SIGNAL_FAMILY_ORDER}
    for name in _factor_catalog():
        fam = signal_family_of(name)
        cats.setdefault(fam, []).append(name)
    order = {}
    for fam in SIGNAL_FAMILY_ORDER + [k for k in cats if k not in SIGNAL_FAMILY_ORDER]:
        for name in sorted(cats[fam], key=lambda n: -abs(_BUILTIN_META.get(n, {}).get("icir120") or 0)):
            order[name] = fam
    return order


def build_manifest() -> dict:
    """内建 manifest（与外包 factor_manifest_{date}.json 结构同构）"""
    now = datetime.now().strftime("%Y-%m-%d")
    factors = []
    for name, fam in _sorted_factors().items():
        meta = _BUILTIN_META.get(name, {})
        factors.append({
            "code": name,
            "name_cn": meta.get("cn", name),
            "category": fam,
            "direction": meta.get("dir", 1),
            "icir_60": meta.get("icir60"),
            "icir_120": meta.get("icir120"),
            "status": _status_of(meta),
            "usage": "选股" if meta.get("dir", 1) == 1 and (meta.get("icir120") or 0) >= 0.2 else "观察",
        })
    categories = {}
    for f in factors:
        categories[f["category"]] = categories.get(f["category"], 0) + 1
    return {
        "date": now,
        "n": len(factors),
        "factors": factors,
        "categories": categories,
        "health_date": now,
        "health_stale": False,
        "note": "内建净化清单（外包因子池未接入，由主系统自有因子知识生成）",
    }


def build_health() -> dict:
    """内建 health（与外包 health/health_{date}.csv 结构同构，供净化/风控层渲染）"""
    now = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for name, fam in _sorted_factors().items():
        meta = _BUILTIN_META.get(name, {})
        rows.append({
            "factor": name,
            "family": fam,
            "category": fam,
            "status": _status_of(meta),
            "direction": meta.get("dir", 1),
            "icir120": meta.get("icir120"),
            "icir120_chg": None,
            "note": meta.get("cn", ""),
        })
    return {"date": now, "n": len(rows), "rows": rows, "note": "内建净化清单"}
