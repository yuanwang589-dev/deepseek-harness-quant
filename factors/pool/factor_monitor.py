# -*- coding: utf-8 -*-
"""factors/pool/factor_monitor.py — 因子健康监控（2026-10-18 · 立项：补"有池无序列"）

借鉴 VernonOY/alpha-skills（Apache-2.0）alpha-monitor 的衰减检测规则，并适配本项目：
  · 滚动 IC：近期窗口 IC 均值 vs 全历史 IC 均值；
  · 衰减判定：近期 |IC| 相对全历史衰减 ≥ threshold（默认 50%）→ WARNING；
  · ICIR 转负：近期 IC 均值与全历史**符号翻转** → 建议 RETIRE
    （原规则"ICIR 转负直接自动退役"，本项目只出建议、不自动退役——退役是人的决定）；
  · 数据不足（期数 < 2×window）→ insufficient_data，**不误报、不假装**。

时序因子（eval_ts）天然有逐月序列；横截面因子走月末快照，同样按 window_end 时序处理。
两个通道共用一张 factor_ic_series 表（见 ic_store.py）。

用法：
    python factors/pool/factor_monitor.py                 # 全池巡检 + Markdown 报告
    python factors/pool/factor_monitor.py --json          # JSON 输出
    from factors.pool.factor_monitor import check_decay, monitor_all
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

from factors.pool.ic_store import connect, load_series, list_factors

DEFAULT_WINDOW = 6        # 近期窗口（期）
DEFAULT_THRESHOLD = 0.5   # 衰减阈值（50%，对齐 alpha-skills）


def check_decay(series, window: int = DEFAULT_WINDOW,
                threshold: float = DEFAULT_THRESHOLD) -> dict:
    """单因子衰减体检。series: index=window_end, value=IC。"""
    if series is None:
        return {"status": "insufficient_data", "n": 0, "window": window,
                "hint": "无序列数据（该因子还没被评估过，或未落库）"}
    s = pd.Series(series).dropna()
    n = len(s)
    if n < window * 2:
        return {"status": "insufficient_data", "n": n, "window": window,
                "hint": f"序列 {n} 期 < 2×window({window})，不判定（避免小样本误报）"}
    hist_mean = float(s.mean())
    recent = s.iloc[-window:]
    rec_mean = float(recent.mean())
    if hist_mean == 0:
        return {"status": "warning", "n": n, "window": window,
                "hist_mean": hist_mean, "recent_mean": rec_mean,
                "decay_pct": None, "sign_flip": False,
                "hint": "全历史 IC 均值为 0，因子整体无预测力"}
    decay = 1 - abs(rec_mean) / abs(hist_mean)
    sign_flip = (hist_mean > 0) != (rec_mean > 0) and rec_mean != 0
    out = {"n": n, "window": window, "hist_mean": round(hist_mean, 4),
           "recent_mean": round(rec_mean, 4), "decay_pct": round(decay * 100, 1),
           "sign_flip": bool(sign_flip)}
    if sign_flip:
        out["status"] = "retire_suggested"
        out["hint"] = "IC 符号翻转（ICIR 转负）——alpha-skills 规则下的退役信号，请人工复核后决定"
    elif decay >= threshold:
        out["status"] = "warning"
        out["hint"] = f"近期 |IC| 较全历史衰减 {out['decay_pct']}% ≥ {threshold*100:.0f}%，需复核是否失效"
    else:
        out["status"] = "ok"
        out["hint"] = ""
    return out


def monitor_all(con: sqlite3.Connection, window: int = DEFAULT_WINDOW,
                threshold: float = DEFAULT_THRESHOLD) -> dict:
    """全池巡检。返回 {factor_name: 诊断}，判定汇总见 _summary。"""
    out = {}
    for name in list_factors(con):
        out[name] = check_decay(load_series(con, name), window, threshold)
    return out


def _summary(report: dict) -> dict:
    c = {"ok": 0, "warning": 0, "retire_suggested": 0, "insufficient_data": 0}
    for v in report.values():
        c[v["status"]] = c.get(v["status"], 0) + 1
    return c


def render_markdown(report: dict, window: int, threshold: float) -> str:
    summary = _summary(report)
    lines = [
        "# 因子健康监控报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 规则：近期 {window} 期 |IC| 衰减 ≥ {threshold*100:.0f}% → warning；"
        f"IC 符号翻转 → retire_suggested（借鉴 alpha-skills alpha-monitor，退役仅建议不自动）",
        f"- 汇总：🟢 ok {summary['ok']} ｜ 🟡 warning {summary['warning']} ｜ "
        f"🔴 retire_suggested {summary['retire_suggested']} ｜ ⚪ 数据不足 {summary['insufficient_data']}",
        "",
        "| 因子 | 期数 | 全历史 IC | 近期 IC | 衰减% | 符号翻转 | 判定 | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in sorted(report):
        r = report[name]
        if r["status"] == "insufficient_data":
            lines.append(f"| {name} | {r['n']} | - | - | - | - | ⚪ 数据不足 | {r['hint']} |")
        else:
            mark = {"ok": "🟢 ok", "warning": "🟡 warning",
                    "retire_suggested": "🔴 建议退役"}[r["status"]]
            sf = "是" if r.get("sign_flip") else "否"
            lines.append(
                f"| {name} | {r['n']} | {r['hist_mean']:+.4f} | {r['recent_mean']:+.4f} "
                f"| {r['decay_pct']} | {sf} | {mark} | {r.get('hint','')} |")
    lines += ["", "> 注：retire_suggested 是信号不是判决——按本项目纪律，因子退役由人复核后决定。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="因子健康监控（IC 衰减巡检）")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="近期窗口期数")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="衰减阈值 0-1")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    ap.add_argument("--out", default="", help="Markdown 报告输出路径（默认 output/factor_decay_report_YYYYMMDD.md）")
    args = ap.parse_args()

    con = connect()
    report = monitor_all(con, args.window, args.threshold)
    summary = _summary(report)
    if args.json:
        print(json.dumps({"summary": summary, "factors": report},
                         ensure_ascii=False, indent=2, default=str))
    else:
        md = render_markdown(report, args.window, args.threshold)
        out = Path(args.out) if args.out else BASE / "output" / \
            f"factor_decay_report_{datetime.now().strftime('%Y%m%d')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"[factor_monitor] 报告已写入 {out}")
    # 退出码：有 warning/retire 建议 → 1（供 cron/CI 感知）；全 ok/数据不足 → 0
    return 1 if (summary["warning"] or summary["retire_suggested"]) else 0


if __name__ == "__main__":
    sys.exit(main())
