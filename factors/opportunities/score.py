# -*- coding: utf-8 -*-
"""factors/opportunities/score.py — 大池子统一评分系统 v2.0（2026-08-08 固化）

★核心创新（用户定调）：不同机会类型 → 不同加权评分标准
  - 低估值/反转类：本身有极高安全垫 → 安全性权重更高
  - 突破/重估类：收益弹性大 → 收益权重更高、风险权重更严
  - 事件类：时效性强 → 概率（触发质量）权重更高

评分公式（每类独立权重）：
    score = 收益分×W_gains + 概率分×W_prob + 安全分×W_safety   （0-100）
    其中 安全分 = 10 - 风险分

借鉴（开源调研 2026-08-08）：
  - 腾讯 skillhub 股票筛选器：11 策略每类独立 0-100 评分 + AND/OR/SCORE 组合
  - 多模型选股系统：同票多模型命中 → 共识加分（本系统 = also_types 多类命中加分）
  - 均值回归 vs 动量双策略：不同市场状态不同策略权重（Regime 联动预留）

用法：
  from factors.opportunities.score import opportunity_score, gains_score, prob_score, risk_score
  s = opportunity_score("value", gains=6, prob=7, risk=3)   # 低估值：安全权重高
  s = opportunity_score("breakout", gains=8, prob=6, risk=5) # 突破：收益权重高
"""
import json
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent.parent


def _load_scoring_cfg() -> dict:
    """读取 config/strategies.yaml 的 scoring 段（缺失回退 .example）。

    ★2026-11-11：阈值/权重/配额从代码迁到配置（AGENTS 铁律：会变的东西不写死）。"""
    for name in ("strategies.yaml", "strategies.yaml.example"):
        p = BASE / "config" / name
        if p.exists():
            try:
                return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("scoring") or {}
            except Exception:
                return {}
    return {}


SCORING_CFG = _load_scoring_cfg()

# ---------------------------------------------------------------
# ★类型差异化权重（核心配置，改这里 = 改系统风格）
# W_gains 收益 / W_prob 概率 / W_safety 安全（三者之和 = 1.0）
# 设计逻辑：
#   value/reversal/quality_gap：安全垫类 → 安全权重 ≥0.35（优先不亏钱）
#   breakout/revalue：弹性类 → 收益权重 ≥0.45（赚空间）
#   event：时效类 → 概率权重 0.40（触发质量决定成败）
# ---------------------------------------------------------------
TYPE_WEIGHTS = {
    "reversal":     {"w_gains": 0.30, "w_prob": 0.35, "w_safety": 0.35},  # 反转：安全+概率（★17年负期望，门槛80基本不推）
    "value":        {"w_gains": 0.30, "w_prob": 0.30, "w_safety": 0.40},  # ★低估值：安全最高（17年 62.5% 基本盘）
    "breakout":     {"w_gains": 0.45, "w_prob": 0.25, "w_safety": 0.30},  # ★突破：胜率低（45.5%）环境敏感 → 收益 0.50→0.45 + 安全 0.20→0.30（2026-08-11 实证校准：盈亏比 1.83 需更厚安全垫）
    "revalue":      {"w_gains": 0.45, "w_prob": 0.35, "w_safety": 0.20},  # 重估：收益高（17年 53.6%）
    "event":        {"w_gains": 0.30, "w_prob": 0.40, "w_safety": 0.30},  # ★事件：概率最高（17年 51.5%）
    "quality_gap":  {"w_gains": 0.30, "w_prob": 0.30, "w_safety": 0.40},  # 质量折价：安全高（★17年 70.4% 全场最强）
    "pv_consensus": {"w_gains": 0.25, "w_prob": 0.35, "w_safety": 0.40},  # ★R-4 精化（2026-08-10 总指导裁决采纳）：五强共识=稳态修复（低波低换冷门股），安全权重应最高 40%；17 年实证 53.3% 胜率/+4.3% 均收益 = 概率型+安全型（原 30/35/35 → 25/35/40）
}

# 默认权重（未注册类型）
DEFAULT_WEIGHTS = {"w_gains": 0.40, "w_prob": 0.35, "w_safety": 0.25}

# 多类型命中加分（同票被多类机会命中 = 共识确认）
# ★B-12 十强扩展（2026-08-10 总指导）：命中 6+ 档（十强 rank≥0.75 命中≥6 = 真共识）
CONSENSUS_BONUS = {2: 3.0, 3: 6.0, 4: 9.0, 5: 3.0,
                   6: 6.0, 7: 8.0, 8: 10.0, 9: 12.0, 10: 14.0}

# Pitch 门槛（每类独立，v2.0：安全类门槛略低但风控更严，弹性类门槛高）
PITCH_GATE = {
    "reversal": 80, "value": 62, "breakout": 72,   # ★17年实证 reversal 负期望（-1.8%/6月 39%胜率）→ 门槛 65→68→80（2026-08-11 知识库强烈建议：负期望类型不推，宁缺毋滥）
    "revalue": 70, "event": 68, "quality_gap": 62,
    "pv_consensus": 68,   # B-6 量价共识（安全类，门槛中位；R-4 精化后调整）
}

# ★2026-08-14 Pitch 改进规格 v2 ①：实证胜率校正（17 年 6 月胜率 / 全场均值 53.75%）
#   因子池 pitch_priority_badges_20260814 数据包：quality_gap 70.4%→×1.31、value 62.5%→×1.16、
#   revalue 53.6%→×1.00、event 51.5%→×0.96、pv_consensus 53.3%→×0.99、breakout 45.5%→×0.85、
#   reversal 39.0%→×0.73（负期望不推，乘后进一步沉底）
#   作用：机会分与实证胜率重新对齐——最高胜率类（quality_gap）不再被弹性类（revalue）高分挤出。
WINRATE_MULT = {
    "quality_gap": 1.311, "value": 1.164, "revalue": 0.999,
    "pv_consensus": 0.993, "event": 0.959, "breakout": 0.848, "reversal": 0.726,
}

# ---------------------------------------------------------------
# ★大中小盘划分（2026-08-11 总指导指示重划：150亿算大盘不合理，至少千亿）
# 券商/指数公司口径（据 中证指数公司 + 建信基金赵云煜访谈 + 富国基金 公开资料）：
#   - 大盘：总市值 ≥ 1000 亿   沪深300 成分中位数市值 ≈1100 亿（蓝筹门槛，"超级大盘股"）
#   - 中盘：300 亿 ~ 1000 亿    中证500 成分中位数市值 ≈300 亿
#   - 小盘：< 300 亿            中证1000 成分中位数市值 ≈140 亿及以下（含微盘 <40 亿）
# 依据指数成分市值中位数锚定，而非拍脑袋阈值；未来市场扩容可同步上调。
# ---------------------------------------------------------------
SIZE_TIER_LARGE = 1000.0   # 大盘下限（亿）
SIZE_TIER_MID   = 300.0    # 中盘下限（亿）

# ★2026-11-11 配置覆盖（config/strategies.yaml scoring 段；缺省保留上方默认值）
for _k, _v in (SCORING_CFG.get("type_weights") or {}).items():
    if isinstance(_v, dict):
        TYPE_WEIGHTS[_k] = {**TYPE_WEIGHTS.get(_k, DEFAULT_WEIGHTS), **_v}
PITCH_GATE.update({k: int(v) for k, v in (SCORING_CFG.get("pitch_gate") or {}).items()})
WINRATE_MULT.update({k: float(v) for k, v in (SCORING_CFG.get("winrate_mult") or {}).items()})
CONSENSUS_BONUS.update({int(k): float(v) for k, v in (SCORING_CFG.get("consensus_bonus") or {}).items()})
SIZE_TIER_LARGE = float(SCORING_CFG.get("size_tier_large", SIZE_TIER_LARGE))
SIZE_TIER_MID = float(SCORING_CFG.get("size_tier_mid", SIZE_TIER_MID))


def size_tier_of(mv_yi):
    """按券商指数口径把总市值（亿元）映射到大/中/小盘。
    缺失或 NaN → None（调用方 UI 缺省进小盘分组展示，但不在卡片上谎报档位）。
    """
    if mv_yi is None:
        return None
    try:
        mv = float(mv_yi)
    except (TypeError, ValueError):
        return None
    if mv != mv:   # NaN
        return None
    if mv >= SIZE_TIER_LARGE:
        return "大盘"
    if mv >= SIZE_TIER_MID:
        return "中盘"
    return "小盘"


# ---------------------------------------------------------------
# ★Pitch 子分类（2026-08-11 总指导：短线/长线双板块，每板块内子菜单）
#   ⚡ express   强因子直通   —— 跨家族独立证据 ≥3（三重确认）→ 真特殊权限（名额每线≤2，调用方控制）
#   🤝 consensus 多因子共识达成 —— 跨家族 ≥2 未直通 / also_types≥2 多类型命中 / B-12 十强 ext_signal
#   📊 score    加权评分高分   —— 其余：单因子/无交叉确认，纯评分排序
# 判定函数只做基础归类；express 的"前几位"名额由调用方按强度排序截取。
# ---------------------------------------------------------------
EXPRESS_MIN_FAMILY = 3     # 直通需 ≥3 个独立家族交叉（统计误差审计收紧：2 家族仍可能运气）
CONSENSUS_MIN_FAMILY = 2   # 共识需 ≥2 个独立家族
EXPRESS_PER_LINE = 2       # 每线直通名额（"只有前几位才能直通，剩下的走常规路径"）
CONSENSUS_PER_LINE = 3     # 每线共识名额
SCORE_PER_LINE = 5         # 长线评分高分名额（含大小盘分档 2/2/1）


def strong_strength(sh_):
    """强因子命中强度排序键（元组，越大越强）：
    (家族数, 最小rank越大越强, 最高icir120)
    sh_ = {factor: {rank, family, icir120}}
    ★2026-08-14 审计修复：原 `-min_rank` 使"命中因子中最小 rank 越小者排前"——与铁律
      "好因子 rank 大"（load_strong_hits 用 ≥0.90）相悖；rank 0.90 应优先于 0.75。
      改 `min_rank`：最小 rank 越大 = 全部命中因子都强 = 更优先。
    """
    fams = {v["family"] for v in sh_.values() if v.get("family")}
    n_fam = len(fams)
    min_rank = min((v.get("rank") or 1.0) for v in sh_.values())
    max_icir = max((v.get("icir120") or 0) for v in sh_.values())
    return (n_fam, min_rank, max_icir)


def classify_pitch_sub(sh_, also_types=None, ext_signal=None) -> str:
    """返回 'express_candidate' | 'consensus' | 'score'。
    express 仅为候选标记（是否拿到名额由调用方按 strong_strength 排序截取）。
    """
    n_fam = 0
    if sh_:
        n_fam = len({v["family"] for v in sh_.values() if v.get("family")})
    if n_fam >= EXPRESS_MIN_FAMILY:
        return "express_candidate"
    if n_fam >= CONSENSUS_MIN_FAMILY:
        return "consensus"
    if also_types and isinstance(also_types, (list, tuple)) and len(also_types) >= 2:
        return "consensus"          # 多机会类型同时命中 = 共识
    if ext_signal:
        return "consensus"          # B-12 十强命中≥6（外包共识信号）
    return "score"


def weights_for(otype: str) -> dict:
    """获取类型权重；未注册类型用默认"""
    return TYPE_WEIGHTS.get(otype, DEFAULT_WEIGHTS)


def opportunity_score(otype: str, gains: float, prob: float, risk: float,
                      same_type_winrate: float = None,
                      n_types_hit: int = 1) -> dict:
    """统一机会评分（★类型差异化加权，0-100）
    Args:
        otype: 机会类型 key
        gains: 预期收益分 0-10（目标空间 × 时间效率）
        prob:  概率分 0-10（胜率证据 × 触发强度）
        risk:  风险分 1-10（回撤×波动×流动性；越高越差）
        same_type_winrate: 同类历史胜率 0-1（诚实披露）
        n_types_hit: 该股被几种机会类型命中（共识加分）
    Returns:
        {otype, gains, prob, risk, safety, score, note, weights, consensus}
    """
    risk = max(risk, 1.0)
    safety = 10 - min(risk, 10)
    w = weights_for(otype)
    base = gains * w["w_gains"] + prob * w["w_prob"] + safety * w["w_safety"]
    bonus = CONSENSUS_BONUS.get(n_types_hit, 0.0) if n_types_hit > 1 else 0.0
    score = round(base * 10 + bonus, 1)
    # ★2026-08-14 Pitch 改进规格 v2 ①：实证胜率校正（17 年 6 月胜率 / 全场均值 53.75%）
    #   quality_gap ×1.31（71.9→94.2 进第一梯队）、revalue ×1.00（不变）、breakout ×0.85（回落）
    #   —— 分数与实证胜率对齐：高分 = 实证高胜率，弹性类不再霸榜
    wm = WINRATE_MULT.get(otype, 1.0)
    if wm != 1.0:
        score = round(min(100.0, score * wm), 1)

    gate = PITCH_GATE.get(otype, 70)
    if score >= gate + 10:
        note = "极强机会"
    elif score >= gate:
        note = "强机会"
    elif score >= gate - 10:
        note = "机会"
    else:
        note = "弱机会"
    return {
        "otype": otype,
        "gains": round(gains, 1),
        "prob": round(prob, 1),
        "risk": round(risk, 1),
        "safety": round(safety, 1),
        "score": score,
        "weights": w,
        "consensus_bonus": bonus,
        "n_types_hit": n_types_hit,
        "gate": gate,
        "winrate": same_type_winrate,
        "note": note,
    }


def gains_score(target_upside: float, horizon_months: float = 6.0) -> float:
    """预期收益分：目标空间(0-100%+) × 时间效率
    - 空间 15%→5分，25%→7分，40%→8.5分，60%+→10分
    - 时间效率：6 个月内可达打满，12 个月打 0.8
    """
    space = min(target_upside / 15.0, 1.0) * 5 + min(max(target_upside - 15, 0) / 45.0, 1.0) * 5
    space = min(space, 10)
    time_eff = max(0.8, 1.0 - max(horizon_months - 6, 0) / 30.0)
    return round(min(space * time_eff, 10), 1)


def prob_score(winrate: float, trigger_strength: float) -> float:
    """概率分：同类历史胜率(0-1) × 触发强度(0-1)
    winrate 0.55→5.5分，0.65→7分，0.75→8.5分
    """
    base = winrate * 10
    bonus = trigger_strength * 2.5
    return round(min(base + bonus, 10), 1)


def risk_score(max_drawdown_expected: float, vol_annual: float, liquidity_pct: float) -> float:
    """风险分（越高越差）：
    - 预期回撤 10%→2分，20%→3.5分，30%→5分，50%→8分
    - 波动率 30%→1.2分，50%→2.4分
    - 流动性：日均成交 <0.3亿 +1.5，<0.1亿 +3
    """
    dd = max_drawdown_expected / 6.0 + 1.0
    v = max(vol_annual - 0.20, 0) * 8
    liq = 3.0 if liquidity_pct < 0.1 else (1.5 if liquidity_pct < 0.3 else 0.0)
    return round(min(dd + v + liq, 10), 1)


def weights_summary() -> str:
    lines = ["## 类型差异化权重（v2.0）", ""]
    lines.append("| 类型 | 收益 | 概率 | 安全 | Pitch门槛 | 设计逻辑 |")
    lines.append("|---|---|---|---|---|---|")
    logic = {
        "reversal": "安全+概率：超跌反弹不亏钱优先",
        "value": "★安全最高：低估值=安全垫",
        "breakout": "★收益最高：突破赚弹性",
        "revalue": "收益高：双击吃空间",
        "event": "★概率最高：事件成败看触发",
        "quality_gap": "安全高：白马错杀防守",
    }
    for ot, w in TYPE_WEIGHTS.items():
        lines.append(f"| {ot} | {w['w_gains']:.0%} | {w['w_prob']:.0%} | {w['w_safety']:.0%} | "
                     f"{PITCH_GATE.get(ot, 70)} | {logic.get(ot, '')} |")
    lines.append("")
    lines.append("共识加分：同票多类命中 +3/+6/+9（多模型交叉验证，抄自多模型选股系统）")
    return "\n".join(lines)


if __name__ == "__main__":
    print(weights_summary())
    print()
    # 自测：同机会质量，不同类型分数差异（体现差异化权重）
    cases = [
        ("value", 5.0, 6.5, 3.0),      # 低估值：安全垫高 → 分数应较高
        ("reversal", 6.0, 6.5, 3.5),
        ("breakout", 8.0, 6.5, 5.0),
        ("revalue", 8.5, 6.5, 5.5),
        ("event", 6.0, 7.0, 4.0),
        ("quality_gap", 6.0, 6.0, 3.0),
    ]
    print("差异化权重自测（安全类应获安全权重红利）：")
    for ot, g, p, r in cases:
        s = opportunity_score(ot, g, p, r)
        print(f"  {ot:12s} 收益{g:>4.1f} 概率{p:>4.1f} 风险{r:>4.1f} → {s['score']:5.1f} ({s['note']}) "
              f"w={s['weights']['w_gains']:.0%}/{s['weights']['w_prob']:.0%}/{s['weights']['w_safety']:.0%}")
    # 共识加分测试
    s1 = opportunity_score("value", 6, 6.5, 3, n_types_hit=1)
    s2 = opportunity_score("value", 6, 6.5, 3, n_types_hit=2)
    print(f"\n共识加分测试: 单类命中 {s1['score']} → 双类命中 {s2['score']} (+{s2['consensus_bonus']})")
