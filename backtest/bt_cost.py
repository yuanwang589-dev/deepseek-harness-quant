# -*- coding: utf-8 -*-
"""backtest/bt_cost.py — 回测交易成本唯一口径（2026-11-26）

把此前散落在 bt_runner / bt_engine 里的硬编码成本
`0.00026 + 0.0005 + 0.001` 收敛到一处，并修正"印花税双边收取"的错误：
A 股印花税仅卖出方缴纳，买入只有佣金 + 滑点。

参数来源：config/params.yaml backtest 段
    commission_bps: 2.6     # 双边佣金（万 2.6 = 0.00026）
    stamp_tax: 0.0005       # 卖出印花税 0.05%
    slippage: 0.001         # 双边滑点 0.1%

口径：
    one_way_cost("buy")  = 佣金 + 滑点
    one_way_cost("sell") = 佣金 + 滑点 + 印花税
    round_trip_cost()    = buy + sell = 2×(佣金+滑点) + 印花税
"""
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_DEFAULTS = {"commission_bps": 2.6, "stamp_tax": 0.0005, "slippage": 0.001}


def _params(cfg=None) -> dict:
    """取成本参数：显式 cfg > config/params.yaml > 内置默认值。"""
    out = dict(_DEFAULTS)
    if cfg is None:
        try:
            import yaml
            p = _BASE / "config" / "params.yaml"
            if p.exists():
                cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("backtest", {})
        except Exception:
            cfg = {}
    for k in out:
        if isinstance(cfg, dict) and cfg.get(k) is not None:
            try:
                out[k] = float(cfg[k])
            except (ValueError, TypeError):
                pass
    return out


def one_way_cost(cfg=None, side: str = "buy") -> float:
    """单边成本比例：buy=佣金+滑点；sell=佣金+滑点+印花税。"""
    p = _params(cfg)
    c = p["commission_bps"] / 10000.0 + p["slippage"]
    if str(side).lower() == "sell":
        c += p["stamp_tax"]
    return c


def round_trip_cost(cfg=None) -> float:
    """一次全换仓（买+卖）成本比例，默认 0.00302（原 0.00352 多扣一次印花税）。"""
    return one_way_cost(cfg, "buy") + one_way_cost(cfg, "sell")
