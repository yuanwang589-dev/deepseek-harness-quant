# -*- coding: utf-8 -*-
"""PIT（Point-in-Time）工具：按法定披露截止日截断财报期。

背景：`finance_report` 只有 `period`（报告期），没有 `ann_date`。若直接用
`period <= 扫描日` 取最新一期，会把尚未披露的财报（如 8 月中旬的 6-30 报告）
提前用于选股，构成前视偏差。本模块用 A 股法定披露截止日做保守截断：

    报告期 03-31 → 04-30 披露截止
    报告期 06-30 → 08-31
    报告期 09-30 → 10-31
    报告期 12-31 → 次年 04-30

严格用 `< 截止日`（当日可能尚未披露完），宁可滞后一期，不使用未来数据。
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
from datetime import date, datetime


def _to_date(asof=None):
    if asof is None:
        return date.today()
    if isinstance(asof, datetime):
        return asof.date()
    if isinstance(asof, date):
        return asof
    try:
        return datetime.strptime(str(asof)[:10], "%Y-%m-%d").date()
    except Exception:
        return date.today()


def disclosure_deadline(period: str) -> date:
    """报告期 → 法定披露截止日。非法 period 返回 date.min。"""
    try:
        y, m, _ = str(period)[:10].split("-")
        y, m = int(y), int(m)
        if m == 3:
            return date(y, 4, 30)
        if m == 6:
            return date(y, 8, 31)
        if m == 9:
            return date(y, 10, 31)
        if m == 12:
            return date(y + 1, 4, 30)
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/pit.py:44 disclosure_deadline")
    return date.min


def latest_disclosed_period(asof=None) -> str:
    """asof 时点**已过披露截止日**的最近报告期（YYYY-MM-DD）；无则返回空串。"""
    d = _to_date(asof)
    candidates = []
    for yy in (d.year, d.year - 1, d.year - 2):
        for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = date(yy, mm, dd)
            if disclosure_deadline(p.isoformat()) < d:
                candidates.append(p)
    return max(candidates).isoformat() if candidates else ""


def is_disclosed(period: str, asof=None) -> bool:
    """报告期在 asof 时点是否已过披露截止日。"""
    return disclosure_deadline(period) < _to_date(asof)