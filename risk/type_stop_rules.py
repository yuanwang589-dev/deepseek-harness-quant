# -*- coding: utf-8 -*-
"""risk/type_stop_rules.py — 类型定制止损规则（2026-08-10 用户需求 + 研究员成果固化）

★需求：每个 Pitch 独有止损条件，跟因子/机会逻辑相关（反转看失效、突破强制止损等）。
★原理：止损 = 对应该类机会的"逻辑失效点"——当买入理由被证明错误时卖出。
★研究员成果（2026-08-10 固化）：《止损策略与买入逻辑匹配研究.md》
  - 9 种止损类型全景（固定%/ATR/支撑/均线/时间/移动/逻辑/基本面/阶梯）
  - 7 类买入逻辑 × 止损矩阵（M1 突破=启动K线低点 / M2 反转=MA60判据+宽ATR+时间 / M3 价值=安全垫分级 / M4 趋势=移动 / M5 事件=时间+证伪 / M6 短线=快止损 / M7 防守=无止损）
  - 假突破三阶段退出（预警减半→启动低点清仓→MA60确认） + 洗盘区分（缩量回踩/结构未坏）
  - 防猎杀：避开整数位/共识位 + 1-3% 缓冲 + 收盘触发

用法：
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
  from risk.type_stop_rules import type_stop_plan
  plan = type_stop_plan("breakout", score=74.5)
  → {"stop_loss_pct": 0.10, "pivot_check": 0.08, "logic_fail_rules": [...], "desc": "..."}

输出结构（供 pitch_v2 的 stop_plan 字段）：
  {
    "otype": "breakout",
    "stop_loss_pct": 0.10,          # 硬止损%（类型差异化）
    "time_stop_weeks": null,        # 时间止损（null=不设）
    "pivot_check_pct": 0.08,        # 突破点下方止损%（仅 breakout）
    "trailing_ma": 20,              # 移动止损均线（仅动量类）
    "max_drawdown_pct": 0.10,       # 最大回撤强制线
    "fake_exit_levels": [...],      # ★研究员：假突破分级退出（仅 breakout）
    "logic_fail_rules": [           # ★逻辑失效止损（本类机会的失效信号）
      {"name": "假突破确认", "rule": "收盘跌破突破点 2%", "action": "强制卖出"},
    ],
    "desc": "一句话方案",
  }
"""
from typing import Optional

# 各类型的基础止损参数（基于 17 年回测胜率 + 波动特征定制）
TYPE_STOP_CONFIG = {
    "value": {
        # ★2026-08-10 B-9.1 P2 落地：删 7% 硬止损（P6 实证：防守型 -7% 止损无增益，夏普 1.56=1.56）
        #   M3 矩阵本意：高安全垫标的 = 逻辑止损为主；硬止损降级为可选兜底（默认关，max_drawdown 15% 保护线）
        "stop_loss_pct": None,
        "time_stop_weeks": 26,           # 6 个月估值未修复 → 时间止损
        "time_stop_min_gain": 0.0,       # 6 月涨幅 <0% 卖出
        "max_drawdown_pct": 0.15,        # 可选兜底保护线（默认关，仅极端破位触发）
        "trailing_ma": None,
        "logic_fail_rules": [
            {"name": "财务证伪", "rule": "下期财报 ROE 转负（<0）", "action": "立即卖出"},
            {"name": "估值陷阱", "rule": "PB 继续走低但 ROE 同时恶化", "action": "卖出"},
        ],
        "desc": "财务证伪/估值陷阱触发即离场 · 6 个月未修复离场 · 回撤超 15% 离场",
    },
    "revalue": {
        # ★2026-08-10 B-9.1 P2 落地：删 7% 硬止损（低频重估逻辑，M3 矩阵=逻辑止损为主；P6 实证止损无增益）
        "stop_loss_pct": None,
        "time_stop_weeks": 13,
        "time_stop_min_gain": 0.0,
        "max_drawdown_pct": 0.12,        # 可选兜底保护线（默认关）
        "trailing_ma": 20,
        "logic_fail_rules": [
            {"name": "业绩证伪", "rule": "下期净利同比 <20%（增速掉头；B-8 后升级 SUE<0）", "action": "立即卖出"},
            {"name": "估值兑现", "rule": "PE 分位 >80%（重估到位）", "action": "止盈卖出"},
        ],
        "desc": "业绩证伪触发即离场 · 估值兑现止盈 · 13 周未兑现离场 · 回撤超 12% 离场",
    },
    "quality_gap": {
        # ★2026-08-10 实证修正（外包 Ablation：夏普 0.65→-0.89）：
        #   防守型固定% 硬止损=收益杀手（低波股正常波动被打掉）→ 删除硬止损，逻辑止损为主（M3 高安全垫本意）
        "stop_loss_pct": None,            # ❌ 无硬止损（实证：硬止损摧毁防守型策略）
        "time_stop_weeks": 26,
        "time_stop_min_gain": 0.0,
        "max_drawdown_pct": None,         # 深回撤容忍（无强制线）
        "trailing_ma": None,
        "logic_fail_rules": [
            {"name": "质量破位", "rule": "ROE 跌破 10%（年化）", "action": "卖出"},
            {"name": "现金流恶化", "rule": "CFO/净利润 <0（利润无现金支撑）", "action": "卖出"},
        ],
        "desc": "质量破位（ROE<10%/现金流恶化）触发即离场 · 26 周未修复离场 · 回撤超 20% 离场",
    },
    "pv_consensus": {
        # ★2026-08-10 实证修正（外包 Ablation：夏普 0.40→-1.38，回撤 -28%→-48%）：
        #   硬止损摧毁策略 → 删除硬止损，保留共识瓦解逻辑止损 + MA20 移动（对齐 M7 防守层）
        "stop_loss_pct": None,            # ❌ 无硬止损（实证：低波股正常波动被打掉）
        "time_stop_weeks": 8,
        "time_stop_min_gain": 0.05,
        "max_drawdown_pct": None,
        "trailing_ma": 20,               # 量价股跟 MA20 移动
        "logic_fail_rules": [
            {"name": "共识瓦解", "rule": "量价五强命中数 <4", "action": "卖出"},
            {"name": "量价背离", "rule": "缩量上涨（量能 <5日均 60%）", "action": "预警"},
        ],
        "desc": "共识瓦解（五强命中<4）触发即卖 · 量价背离预警 · 跌破 MA20 关注",
    },
    "event": {
        "stop_loss_pct": 0.06,           # 事件短促，止损收紧
        "time_stop_weeks": 4,            # 事件 1 月不兑现 → 走人
        "time_stop_min_gain": 0.0,
        "max_drawdown_pct": 0.10,
        "trailing_ma": None,
        "logic_fail_rules": [
            {"name": "资金流逆转", "rule": "行业资金流连续 3 日净流出", "action": "卖出"},
            {"name": "事件兑现", "rule": "政策/事件落地后冲高回落", "action": "止盈卖出"},
        ],
        "desc": "6% 硬止损（事件短促）+ 资金流逆转止损 + 4 周时间止损",
    },
    "breakout": {
        "stop_loss_pct": 0.10,           # ★用户指定：突破股强制止损可放宽（波动大）
        "time_stop_weeks": 4,            # 突破 4 周不涨 → 假突破嫌疑
        "time_stop_min_gain": 0.0,
        "max_drawdown_pct": 0.10,        # ★强制最大回撤 10%
        "pivot_check_pct": 0.08,         # 跌破突破点 8% → 强制止损
        "trailing_ma": 10,               # 突破股跟 MA10 移动（趋势快）
        "fake_exit_levels": [            # ★研究员成果（止损研究 4.2）：假突破分级退出
            {"level": 1, "rule": "收盘跌破突破位（前高/颈线）", "action": "减仓 50%，观察 2 日"},
            {"level": 2, "rule": "跌破启动 K 线最低点", "action": "清仓 100%（支点信号失效）"},
            {"level": 3, "rule": "MA60 拐头向下 + 放量下跌", "action": "清仓（趋势确认）"},
        ],
        "anti_hunt": {"avoid_round_numbers": True, "buffer_pct": 0.01, "close_based": True},  # ★防猎杀
        "logic_fail_rules": [
            {"name": "假突破确认", "rule": "收盘跌破突破点 2%（T+3~T+5 确认）", "action": "强制卖出"},
            {"name": "量能衰竭", "rule": "突破日量比>1.5 但 T+2 缩量>30%", "action": "预警减仓"},
            {"name": "冲高回落", "rule": "突破日长上影（上影/实体>1.5）", "action": "降级"},
            {"name": "坟包形态", "rule": "近30日 尖顶+连阴+平底（FS-1）", "action": "30日黑名单"},
            {"name": "无量突破", "rule": "突破日量<1.5×5日均量（FS-12）", "action": "不入池"},
        ],
        "desc": "强制 10% 最大回撤 + 突破点下方 8% + 假突破三级退出（预警→清仓→确认）+ 防猎杀",
    },
    "reversal": {
        # ★2026-08-10 实证修正（外包 Ablation：猎杀率 40% 最高——反转买超跌，7% 正好打在洗盘区）
        #   → 删除固定% 硬止损，改 ATR 3× 宽止损（防猎杀）+ 保留 3 周时间止损 + MA60 判据（M2 矩阵本意）
        "stop_loss_pct": None,            # ❌ 无固定% 硬止损（猎杀 40%）
        "atr_stop_mult": 3.0,             # ✅ ATR 3× 宽止损（替代固定%）
        "time_stop_weeks": 3,            # ★研究员 M2：反转必须 3 周内兑现（均值回归窗口短）
        "time_stop_min_gain": 0.03,
        "max_drawdown_pct": None,
        "trailing_ma": None,
        "ma60_gate": True,               # ★研究员 M2：MA60 走平/向上才允许买入（FS-11 接飞刀否决）
        "logic_fail_rules": [
            {"name": "反转失效", "rule": "跌破入场前 60 日低点（创新低）", "action": "立即卖出"},
            {"name": "动量再转负", "rule": "20 日动量重新 <0", "action": "卖出"},
            {"name": "反弹无量", "rule": "反弹 5 日量能 <前期均量 70%", "action": "预警"},
            {"name": "MA60 判据", "rule": "MA60 向下时超跌=接飞刀（FS-11）", "action": "否决买入"},
        ],
        "desc": "ATR 3× 宽止损 · 3 周未反弹离场 · 创新低/动量转负即卖 · MA60 向下否决买入",
    },
    "event": {
        "stop_loss_pct": 0.06,           # 事件短促，止损收紧
        "time_stop_weeks": 4,            # 事件 1 月不兑现 → 走人
        "time_stop_min_gain": 0.0,
        "max_drawdown_pct": 0.10,
        "trailing_ma": None,
        "logic_fail_rules": [
            {"name": "资金流逆转", "rule": "行业资金流连续 3 日净流出", "action": "卖出"},
            {"name": "事件兑现", "rule": "政策/事件落地后冲高回落", "action": "止盈卖出"},
            {"name": "事件证伪", "rule": "公告不及预期/事件取消", "action": "立即离场（与盈亏无关）"},
            {"name": "涨停诱多", "rule": "换手>50% 涨停或尾盘板（FS-3）", "action": "不参与"},
        ],
        "desc": "6% 硬止损（事件短促）+ 资金流逆转止损 + 事件证伪立即离场 + 4 周时间止损",
    },
}

DEFAULT = {
    "stop_loss_pct": 0.07, "time_stop_weeks": 8, "time_stop_min_gain": 0.0,
    "max_drawdown_pct": 0.12, "trailing_ma": None, "logic_fail_rules": [],
    "desc": "通用 7% 硬止损",
}


def type_stop_plan(otype: str, score: float = None) -> dict:
    """按机会类型返回定制止损方案"""
    cfg = TYPE_STOP_CONFIG.get(otype, DEFAULT)
    plan = {
        "otype": otype,
        "stop_loss_pct": cfg["stop_loss_pct"],
        "time_stop_weeks": cfg.get("time_stop_weeks"),
        "time_stop_min_gain": cfg.get("time_stop_min_gain", 0.0),
        "pivot_check_pct": cfg.get("pivot_check_pct"),
        "atr_stop_mult": cfg.get("atr_stop_mult"),      # ★实证修正：reversal 用 ATR 宽止损替代固定%
        "trailing_ma": cfg.get("trailing_ma"),
        "max_drawdown_pct": cfg["max_drawdown_pct"],
        "high_drawdown_exit": cfg.get("high_drawdown_exit"),   # ★高点回撤（配置默认，fill_stop_plan 补）
        "fake_exit_levels": cfg.get("fake_exit_levels", []),   # ★研究员：假突破分级退出
        "ma60_gate": cfg.get("ma60_gate", False),              # ★研究员：反转 MA60 判据
        "anti_hunt": cfg.get("anti_hunt", {}),                 # ★研究员：防猎杀
        "logic_fail_rules": cfg["logic_fail_rules"],
        "desc": cfg["desc"],
    }
    # 分数调节：高置信（≥80）可略放宽回撤（给足波动空间）；低置信（<65）收紧
    # ★2026-08-10 修复：max_drawdown_pct=None（防守型无硬止损）时跳过调节
    if score is not None and plan["max_drawdown_pct"] is not None:
        if score >= 80:
            plan["max_drawdown_pct"] = round(plan["max_drawdown_pct"] * 1.2, 2)
        elif score < 65:
            plan["max_drawdown_pct"] = round(plan["max_drawdown_pct"] * 0.85, 2)
    # ★2026-11-20 配置默认值兜底：类型方案里显式 None 的字段（原注释「刻意不设」）改由
    #   config/params.yaml(risk) 提供，让止损引擎与持仓页展示同源、不再「显示 — 且不设防」。
    try:
        from risk.risk_config import fill_stop_plan
        fill_stop_plan(plan)
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/type_stop_rules.py:197 type_stop_plan")
    return plan


if __name__ == "__main__":
    import json
    print("=== 类型定制止损方案 ===")
    for ot in ["value", "revalue", "quality_gap", "pv_consensus", "event", "breakout", "reversal"]:
        p = type_stop_plan(ot, score=75)
        print(f"\n[{ot}] {p['desc']}")
        print(f"  硬止损 {p['stop_loss_pct']:.0%} | 时间 {p['time_stop_weeks']}周 | "
              f"最大回撤 {p['max_drawdown_pct']:.0%} | 移动均线 {p['trailing_ma']}")
        for r in p["logic_fail_rules"]:
            print(f"  失效止损: {r['name']} → {r['rule']}（{r['action']}）")
