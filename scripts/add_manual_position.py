# -*- coding: utf-8 -*-
"""scripts/add_manual_position.py — 手动/历史持仓录入（★2026-10-15 #剩余非代码项）

用途：把一笔历史或线下持仓登记进系统实盘裁决/止盈/止损链（logs/portfolio + 时间戳副本），
      与 sim_live_1w.py 写入的模拟仓共享同一套 schema，观察页 / 自动再平衡 / 止损监测通用。

与 strategy/portfolio.py 的 sync_from_decisions（只认 deck 审批买入）互补：
      本脚本面向"审批记录之外"的持仓（如早期手动建仓、外部账户迁移、回测标注）。

用法：
  # 最简：只给代码，价格自动回填审批日收盘，otype 自动反查机会池
  python scripts/add_manual_position.py 000001.SZ

  # 完整：指定建仓日/价格/类型/止损止盈
  python scripts/add_manual_position.py 600519.SH --name 贵州茅台 \\
        --entry-date 2026-09-01 --entry-price 1500 \\
        --otype quality_gap --stop-pct 0.10 --time-weeks 26 --target-pct 0.70

  # 同时记入模拟账户 paper.db（需给 --qty）
  python scripts/add_manual_position.py 000001.SZ --entry-price 12.5 --qty 1000 \\
        --paper sim_1w_mianwu

字段说明：
  target / stop  由 entry_price ×(1±pct) 推导（与 sim_live_1w 一致：+70% / -10%）
  stop_plan      --stop-pct/--time-weeks/--max-dd 任一显式传入则覆盖类型默认（保值诉求）
                 否则按 otype 从止损矩阵 v2 生成
  take_profit    target_pct / pullback_pct / time_weeks
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from strategy.portfolio import (
    _load, _save, _ensure_legacy_fields, _otype_of, _name_of,
    _entry_price_of, MAX_POSITIONS, stop_plan_for,
)


def _add(code, entry_date, entry_price, otype, name,
         target_pct, stop_pct, time_weeks, max_dd, tp_pullback,
         risk_level, note, force, paper, qty):
    # ---- 基础字段补全 ----
    name = name or _name_of(code) or code
    otype = otype or _otype_of(code) or "quality_gap"
    if entry_price is None:
        entry_price = _entry_price_of(code, entry_date)
    if entry_price is None or entry_price <= 0:
        raise SystemExit(f"[错误] {code} 未能取得建仓价（请显式传 --entry-price，"
                         f"或确认 bars.db 含 {entry_date} 数据）")
    entry_price = round(float(entry_price), 2)

    target = round(entry_price * (1 + target_pct), 2)
    stop = round(entry_price * (1 - stop_pct), 2)

    # 显式止损参数 → 覆盖类型默认（保值诉求）；否则按矩阵生成
    if any(v is not None for v in (stop_pct, time_weeks, max_dd)):
        stop_plan = {
            "stop_loss_pct": round(stop_pct, 4) if stop_pct is not None else None,
            "time_stop_weeks": time_weeks,
            "max_drawdown_pct": round(max_dd, 4) if max_dd is not None else None,
            "trailing_ma": None,
            "desc": "手动录入显式覆盖（保值诉求：-{:.0f}% 硬止损 / -{:.0f}% 回撤 / {} 周时间止损)".format(
                (stop_pct or 0) * 100, (max_dd or 0) * 100, time_weeks or 0),
        }
    else:
        stop_plan = stop_plan_for(otype)

    take_profit = {
        "target_pct": round(target_pct, 4),
        "pullback_pct": round(tp_pullback, 4),
        "time_weeks": time_weeks,
    }

    d = _load()
    d = _ensure_legacy_fields(d)
    existing = {p["code"] for p in d["positions"]
                if p["status"] in ("holding", "over_limit")}
    if code in existing and not force:
        raise SystemExit(f"[跳过] {code} 已在持仓中（holding/over_limit）。"
                         f"如需覆盖请加 --force 后重新录入。")

    n_holding = len([p for p in d["positions"] if p["status"] == "holding"])
    status = "holding" if n_holding < MAX_POSITIONS else "over_limit"

    pos = {
        "code": code, "name": name, "entry_date": entry_date,
        "entry_price": entry_price, "target": target, "stop": stop,
        "otype": otype,
        "stop_plan": stop_plan,
        "take_profit": take_profit,
        "risk_level": risk_level or "MANUAL",
        "status": status,
        "source": "scripts/add_manual_position.py",
        "note": note or "",
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if status == "over_limit":
        print(f"  ⚠ 当前持仓已达 {n_holding}/{MAX_POSITIONS}，本笔录为 over_limit（纪律拒绝，不计入活跃持仓）")
    d["positions"].append(pos)
    _save(d)

    # ---- 可选：同步记入模拟账户 paper.db ----
    if paper:
        import strategy.paper_account as pa
        if not qty:
            print(f"  ⚠ 已指定 --paper {paper} 但未给 --qty，跳过 paper.db 写入")
        else:
            acc = pa.PaperAccount(paper)
            r = acc.buy(code, int(qty), entry_date, close=entry_price,
                        reason="手动录入(脚本)")
            if r.get("ok"):
                print(f"  ✓ 已写入模拟账户 paper.db[{paper}]: {qty}股 @ {entry_price}")
            else:
                print(f"  ✗ paper.db 写入失败: {r.get('msg')}")

    print(f"  ✓ 录入 {code} {name} | 类型 {otype} | 入{entry_date} @ {entry_price}")
    print(f"    目标 {target} (+{target_pct*100:.0f}%) · 止损 {stop} (-{stop_pct*100:.0f}%)")
    print(f"    止损计划: 硬止损 {stop_plan.get('stop_loss_pct')} · "
          f"时间 {stop_plan.get('time_stop_weeks')}周 · 回撤 {stop_plan.get('max_drawdown_pct')}")
    print(f"    止盈: +{take_profit['target_pct']*100:.0f}% / 回落{take_profit['pullback_pct']*100:.0f}% / {take_profit['time_weeks']}周")
    print(f"    状态 {status} | 已写入 logs/portfolio_*.json")
    print(f"  → 跑 `python risk/stop_monitor.py` 看监测；`python strategy/portfolio.py --status` 查看")


def main():
    ap = argparse.ArgumentParser(description="手动/历史持仓录入（写入 logs/portfolio）")
    ap.add_argument("code", help="6 位代码或 ts_code，如 000001.SZ")
    ap.add_argument("--name", default=None)
    ap.add_argument("--entry-date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="建仓日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--entry-price", type=float, default=None,
                    help="建仓价（默认自动取 bars.db 当日收盘）")
    ap.add_argument("--otype", default=None, help="机会类型，如 quality_gap / breakout / value")
    ap.add_argument("--target-pct", type=float, default=0.70, help="止盈目标%%（默认 70）")
    ap.add_argument("--stop-pct", type=float, default=0.10, help="硬止损%%（默认 10）")
    ap.add_argument("--time-weeks", type=int, default=26, help="时间止损周数（默认 26）")
    ap.add_argument("--max-dd", type=float, default=0.15, help="最大回撤线%%（默认 15）")
    ap.add_argument("--tp-pullback", type=float, default=0.10, help="止盈回落%%（默认 10）")
    ap.add_argument("--risk-level", default=None, help="风控等级（默认 MANUAL）")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--force", action="store_true", help="已存在也强制重新录入")
    ap.add_argument("--paper", default=None, help="同时写入模拟账户 paper.db 的账户名")
    ap.add_argument("--qty", type=int, default=None, help="--paper 时的买入股数")
    args = ap.parse_args()

    _add(args.code, args.entry_date, args.entry_price, args.otype, args.name,
         args.target_pct, args.stop_pct, args.time_weeks, args.max_dd,
         args.tp_pullback, args.risk_level, args.note, args.force,
         args.paper, args.qty)


if __name__ == "__main__":
    main()
