# -*- coding: utf-8 -*-
"""scripts/auto_rebalance.py — 1w 模拟仓自动再平衡（信号 → 自动执行闭环）

★目的：把"只出信号不执行"补成闭环。收盘后跑：
  1. 调 position_stop_check（硬止损/回撤/时间/移动/逻辑）→ 命中 SELL 级 → paper_account.sell_all 平仓
  2. 调 take_profit_check（目标价/移动回撤/时间）→ 命中止盈 → 全平锁定收益
  3. 更新 logs/portfolio.json(+最新时间戳)：status=exit + history
  4. 重新跑 stop_monitor 刷新观察池触发/临近徽标

★边界（值守铁律）：
  - 仅作用于 sim_1w_mianwu 模拟账户；不碰真实账户。
  - T+1 双重强制：调用方按成交日跳过当日买入，paper_account.sell 账本层亦拒绝当日买入（P0-1）。
  - 卖出价=最新日线收盘（bars.db qfq），要求 bar 日期==成交日（逐只新鲜度，P0-2）。
  - 只读/写 logs/portfolio*.json 与 data/cache/paper.db；不碰他人文件。

★★★ 2026-10-15 P0 修复：成交日唯一真源 = bars.db 数据日，默认只出意向单 ★★★
  原缺陷（已修）：
    - 成交日取 datetime.now()（墙上时钟），价格取 bars.db 最新一根 → 一旦当日流水线失败/未跑，
      就会「用昨天的收盘价、记成今天的成交」并永久写入 portfolio.history。
    - 一运行就写盘，没有"先看意向再决定"的中间态。
  现行铁律：
    1. 成交日 trade_date 恒等于 bars.db 的 MAX(date)（数据日），不再取墙上时钟 → 日期与价格必然同源。
    2. 新鲜度门禁：数据日 ≠ 今天 → 拒绝成交（默认），仅生成意向单；需人工确认后显式 --allow-stale 放行。
    3. 默认 dry-run：只写 logs/rebalance_intents_{ts}.json，不碰 portfolio/paper.db；
       真正落账必须显式加 --execute。
    4. T+1 判定基准同步改为数据日（原用墙上时钟，与撮合日不一致）。
    5. 逐只新鲜度：每只持仓的 bar 日期必须 == 成交日，否则跳过该只（防「全局新鲜但个股陈旧」仍按旧价记账）。

用法：
  python scripts/auto_rebalance.py                  # 默认：只生成意向单（安全，可随时跑）
  python scripts/auto_rebalance.py --execute        # 数据新鲜时才真正平仓落账
  python scripts/auto_rebalance.py --execute --allow-stale   # 数据不新鲜但已人工确认，放行（仍按数据日记账）
（建议经每日定时任务在 19:00 触发，确保 bars.db 已写入当日收盘）
"""
import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import strategy.paper_account as pa
import risk.position_stop_check as psc
import risk.take_profit_check as tpc
import risk.stop_monitor as sm

ACC = "sim_1w_mianwu"
BARS_DB = str(BASE / "data" / "cache" / "bars.db")


def _latest_bar(code: str):
    """最新 qfq 日线 (close, bar_date)；bars.db 异常软失败返回 (None, None)"""
    try:
        con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        r = con.execute(
            "SELECT close, date FROM daily_bar WHERE code=? AND adjust='qfq' ORDER BY date DESC LIMIT 1",
            (code,)).fetchone()
        con.close()
        if r:
            return float(r[0]), r[1]
    except Exception:
        pass
    return None, None


def _latest_bar_date():
    """bars.db 最新交易日（qfq）—— 成交日唯一真源；无数据返回 None"""
    try:
        con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        r = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()
        con.close()
        return r[0] if r and r[0] else None
    except Exception:
        return None


def _load_portfolio() -> dict | None:
    fs = sorted(glob.glob(str(BASE / "logs" / "portfolio_*.json")),
                key=lambda x: Path(x).stat().st_mtime)
    if not fs:
        return None
    try:
        return json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_json(pat: str) -> dict:
    fs = sorted(glob.glob(str(BASE / "logs" / pat)),
                key=lambda x: Path(x).stat().st_mtime)
    if not fs:
        return {}
    try:
        return json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_portfolio(d: dict):
    d["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (BASE / "logs" / "portfolio.json").write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (BASE / "logs" / f"portfolio_{ts}.json").write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def main(execute: bool = False, allow_stale: bool = False) -> int:
    """execute=False（默认）：只生成意向单，不落账；execute=True：真正平仓写 portfolio/paper.db"""
    wall = datetime.now().strftime("%Y-%m-%d")
    bar_date = _latest_bar_date()
    if not bar_date:
        print("bars.db 无日线数据，跳过")
        return 1

    # ★P0-1 成交日唯一真源 = 数据日（与卖出价同源，杜绝「昨日价格记今日成交」）
    trade_date = bar_date
    fresh = (bar_date == wall)
    exec_ok = True
    if not fresh:
        print(f"⚠ 数据不新鲜：bars.db 最新交易日 {bar_date} ≠ 今天 {wall}")
        if execute and not allow_stale:
            print(f"  → 拒绝成交（否则会以 {bar_date} 收盘价记成 {wall} 成交）；本轮仅生成意向单")
            print(f"  → 已人工确认数据无误时：--execute --allow-stale（成交仍按数据日 {trade_date} 记账）")
            exec_ok = False
        elif execute:
            print(f"  → --allow-stale 显式放行；成交按数据日 {trade_date} 记账（日期与价格同源，不会错配）")
        else:
            print("  → 未指定 --execute，本轮仅生成意向单")

    pf = _load_portfolio()
    if not pf:
        print("无 portfolio 文件，跳过")
        return 1
    holdings = [p for p in pf.get("positions", []) if p.get("status") == "holding"]
    if not holdings:
        print("无 holding 持仓，跳过")
        return 0

    # 1) 止损检查（写 stop_signals_*.json）
    psc.run()
    # 2) 止盈检查（写 take_profit_signals_*.json）
    #   ★2026-10-15 修复：改调 run()。原 tpc.main() 内部 ap.parse_args() 会吞掉本脚本的
    #   --execute / --allow-stale，导致 "unrecognized arguments" 直接 SystemExit(2)。
    tpc.run(verbose=False)

    stop_d = _latest_json("stop_signals_*.json")
    tp_d = _latest_json("take_profit_signals_*.json")
    stop_by_code = {r["code"]: r for r in stop_d.get("results", [])}
    tp_by_code = {r["code"]: r for r in tp_d.get("positions", [])}

    intents, skipped = [], []
    for p in holdings:
        code = p["code"]
        # ★T+1 基准 = 数据日（ledger 层 paper_account.sell 亦强制）
        if str(p.get("entry_date")) == trade_date:
            skipped.append({"code": code, "why": f"T+1：{trade_date} 当日买入不可卖"})
            continue
        # ★P0-2 逐只新鲜度：该标的最新 bar 日期必须等于成交日，否则跳过以防按旧价错误记账
        close, stock_bar_date = _latest_bar(code)
        if close is None:
            skipped.append({"code": code, "why": "bars.db 无该标的收盘价"})
            continue
        if stock_bar_date != trade_date:
            skipped.append({"code": code,
                            "why": f"数据非最新交易日(bar={stock_bar_date}, 成交日={trade_date})，跳过以防错误记账"})
            continue
        reason, src = "", ""
        sr = stop_by_code.get(code)
        if sr:
            for s in sr.get("signals", []):
                if s.get("level") == "SELL":
                    reason, src = f"止损触发：{s.get('rule')}", "position_stop_check"
                    break
        if not reason:
            tr = tp_by_code.get(code)
            if tr:
                for s in tr.get("signals", []):
                    if s.get("type") in ("target", "pullback"):
                        reason, src = f"止盈触发：{s.get('type')}", "take_profit_check"
                        break
        if reason:
            intents.append({"code": code, "name": p.get("name") or code, "action": "SELL",
                            "qty_hint": "all", "price_ref": round(close, 3),
                            "price_date": trade_date, "reason": reason, "source": src})

    doc = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "wall_date": wall,
           "trade_date": trade_date, "fresh": fresh, "executed": False,
           "n_holdings": len(holdings), "intents": intents, "skipped": skipped}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = BASE / "logs"
    (outdir / f"rebalance_intents_{ts}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    if not intents:
        (outdir / "rebalance_intents.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"自动再平衡：无触发，持仓不动（数据日 {trade_date}）")
        return 0

    print(f"再平衡意向单：{len(intents)} 笔卖出意向（数据日 {trade_date}）")
    for it in intents:
        print(f"  {it['code']} {it['name'][:6]} {it['reason']} @ {it['price_ref']}（{it['source']}）")

    if not (execute and exec_ok):
        (outdir / "rebalance_intents.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ 未成交（意向单已写入 logs/rebalance_intents.json）；要落账请加 --execute")
        return 0

    # ★P0-1：只有显式 --execute 且门禁通过，才动 paper.db / portfolio
    acc = pa.PaperAccount(ACC)
    done = []
    for it in intents:
        r = acc.sell_all(it["code"], trade_date, it["price_ref"], reason=it["reason"])
        if not r.get("ok"):
            print(f"  ⚠ {it['code']} 平仓失败: {r.get('msg')}")
            continue
        done.append(it)
        for pp in pf["positions"]:
            if pp["code"] == it["code"]:
                pp["status"] = "exit"
                pp["exit"] = {"date": trade_date, "price": it["price_ref"], "reason": it["reason"]}
                pf.setdefault("history", []).append({
                    "code": it["code"], "name": pp.get("name", it["code"]),
                    "entry_date": pp.get("entry_date"), "exit_date": trade_date,
                    "entry_price": pp.get("entry_price"), "exit_price": it["price_ref"],
                    "reason": it["reason"], "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    if done:
        doc["executed"] = True
        doc["executed_codes"] = [d["code"] for d in done]
        (outdir / "rebalance_intents.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        _save_portfolio(pf)
        sm.run()  # 刷新观察池触发/临近徽标
        print(f"自动再平衡：已落账平仓 {len(done)} 只（数据日 {trade_date}）")
        for it in done:
            print(f"  {it['code']} {it['reason']} @ {it['price_ref']:.2f}")
    else:
        print("自动再平衡：全部平仓失败，未改动 portfolio")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="1w 模拟仓自动再平衡（默认只出意向单）")
    ap.add_argument("--execute", action="store_true",
                    help="真正平仓落账（写 paper.db + portfolio）；缺省仅生成意向单")
    ap.add_argument("--allow-stale", action="store_true",
                    help="数据日≠今天时也允许落账（仍按数据日记账；需已人工确认数据无误）")
    _a = ap.parse_args()
    sys.exit(main(execute=_a.execute, allow_stale=_a.allow_stale))
