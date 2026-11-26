# -*- coding: utf-8 -*-
"""risk/risk_config.py — config/params.yaml(risk) 默认止损/止盈的单一真源（2026-11-20）

为什么需要：stop_matrix_v2.json 缺失时，type_stop_plan / stop_plan_for 返回的止损字段全为
null → 持仓卡显示「—」且止损引擎实际不设防。默认值不允许散落硬编码（7%/8%/3周各写一遍），
统一从 config/params.yaml 的 risk 段读取，按文件 mtime 缓存。
"""
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_CACHE = {"mtime": None, "data": {}}

# params.yaml risk 键 → 止损方案字段
# ★2026-11-20 口径修正：high_drawdown_exit 是「阶段高点回撤」，
#   与引擎 max_drawdown_pct（现价 vs 入仓价回撤）不是一回事，不能互相填。
_KEY_MAP = {
    "stop_loss_pct": "stop_loss_pct",
    "time_stop_weeks": "time_stop_weeks",
    "high_drawdown_exit": "high_drawdown_exit",
    "trailing_ma": "trailing_stop_ma",
}


def risk_defaults() -> dict:
    """{stop_loss_pct, time_stop_weeks, max_drawdown_pct, trailing_ma,
    take_profit_low, take_profit_high, atr_stop_mult}；读取失败返回 {}。"""
    global _CACHE
    p = BASE / "config" / "params.yaml"
    try:
        mt = p.stat().st_mtime
        if _CACHE["data"] and _CACHE["mtime"] == mt:
            return _CACHE["data"]
        import yaml
        r = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("risk") or {}
        out = {k: r.get(src) for k, src in _KEY_MAP.items()}
        out["take_profit_low"] = r.get("profit_take_low")
        out["take_profit_high"] = r.get("profit_take_high")
        out["take_profit_short_types"] = r.get("take_profit_short_types") or ["tech_sentiment"]
        out["atr_stop_mult"] = r.get("atr_stop_mult")
        _CACHE = {"mtime": mt, "data": out}
        return out
    except Exception:
        return {}


def fill_stop_plan(plan: dict, keys=("stop_loss_pct", "time_stop_weeks",
                                     "high_drawdown_exit", "trailing_ma")) -> bool:
    """把 plan 中为 None 的止损字段用配置默认值补齐，返回是否发生填充。
    不覆盖类型方案里已有的值（如 breakout 的 10% 硬止损、quality_gap 的 26 周）。
    注意：max_drawdown_pct（入仓价回撤）不在此列——类型矩阵自己的值才用它。"""
    d = risk_defaults()
    filled = False
    for k in keys:
        if plan.get(k) is None and d.get(k) is not None:
            plan[k] = d[k]
            filled = True
    return filled
