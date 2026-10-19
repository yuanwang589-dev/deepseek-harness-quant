# -*- coding: utf-8 -*-
"""scripts/sim_live_1w.py — 1万元模拟实盘（免五、剔除ST/科创/创业板）

用法：
  python scripts/sim_live_1w.py                 # 默认 1w / 万1免五 / 3只 / 等权整手
  python scripts/sim_live_1w.py --capital 20000 --n 5 --comm 0.00015

规则：
  - 佣金率=万1(0.0001)，无5元最低（免五）；卖出另计印花税万5(系统已含)
  - 候选来自 机会池/今日信号/Pitch 全源，过滤 ST(名称含ST)/科创板(688,689)/创业板(300,301)/高风险
  - 等权分配，A股100股/手整手成交，剩现金留仓
  - 写入 paper.db 模拟账户 + 同步 logs/portfolio.json(+时间戳副本) 供止盈/实盘裁决跟踪
"""
import argparse
import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import strategy.paper_account as pa
from risk.type_stop_rules import type_stop_plan
from risk.take_profit_check import TAKE_PROFIT_RULES, DEFAULT_TP
from data.io_utils import read_json, read_text

ACC_NAME = "sim_1w_mianwu"   # paper.db 模拟账户名


def load_latest(pattern):
    fs = sorted(glob.glob(str(BASE / pattern)), key=lambda p: Path(p).stat().st_mtime)
    return fs[-1] if fs else None


def gather_candidates(td):
    """聚合全源候选，返回 [{code,name,price,score,src}]（未过滤）"""
    opp = read_json(load_latest("logs/opp_pool_*.json")) if load_latest("logs/opp_pool_*.json") else {}
    pit = read_json(load_latest("logs/pitch_v2_*.json")) if load_latest("logs/pitch_v2_*.json") else {}
    tech = read_json(load_latest("logs/tech_pitch_*.json")) if load_latest("logs/tech_pitch_*.json") else {}
    dsig = read_json(load_latest("output/daily_signal_*.json")) if load_latest("output/daily_signal_*.json") else {}

    con_sb = sqlite3.connect(f"file:{BASE/'data/cache/stock_basic.db'}?mode=ro&immutable=1", uri=True, timeout=3)
    sb = {c: (n, s) for c, n, s in con_sb.execute("SELECT code,name,status FROM stock_basic")}
    con_sb.close()
    con = sqlite3.connect(f"file:{BASE/'data/cache/bars.db'}?mode=ro&immutable=1", uri=True, timeout=3)

    def lc(code):
        r = con.execute("SELECT close FROM daily_bar WHERE code=? AND adjust='qfq' ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        return r[0] if r else None

    cands = {}
    def add(items, src):
        for it in items:
            if not isinstance(it, dict):
                continue
            code = it.get("code") or it.get("ts_code")
            if not code:
                continue
            sc = it.get("score")
            if not isinstance(sc, (int, float)):
                sc = it.get("signal_score")
            if not isinstance(sc, (int, float)):
                sc = 0
            c = cands.setdefault(code, {"code": code, "src": set(), "score": 0})
            c["src"].add(src)
            c["score"] = max(c["score"], sc)
            for f in ("name", "price", "close", "target", "otype", "upside_est"):
                if f in it and it[f] is not None and f not in c:
                    c[f] = it[f]

    add(opp.get("opportunities", []), "opp")
    add(opp.get("pitch", []), "opp_pitch")
    for v in (pit.values() if isinstance(pit, dict) else []):
        if isinstance(v, list):
            add(v, "pitch")
    for v in (tech.values() if isinstance(tech, dict) else []):
        if isinstance(v, list):
            add(v, "tech")
    for v in (dsig.values() if isinstance(dsig, dict) else []):
        if isinstance(v, list):
            add(v, "signal")

    out = []
    for c in cands.values():
        code = c["code"]
        name = c.get("name") or sb.get(code, (code, None))[0]
        rl = str(c.get("risk_level") or "")
        if "高风险" in rl or rl.upper() in ("HIGH", "DANGER"):
            continue
        px = c.get("price") or c.get("close") or c.get("target") or lc(code)
        if not px or px <= 0:
            continue
        # ★2026-10-15 P0 修复：原此处漏传 otype（上面 :66 已抓到却丢在 out 里），
        #   导致 _mk_pos 的 b.get("otype") 恒为 None → 全部退化成 "quality_gap"。
        out.append({"code": code, "name": name, "price": round(px, 2),
                    "score": round(c["score"], 1), "src": sorted(c["src"]),
                    "otype": c.get("otype")})
    con.close()
    return out


def excluded(code, name):
    if "ST" in (name or "").upper():
        return "ST"
    if code.startswith(("688", "689")):
        return "科创"
    if code.startswith(("300", "301")):
        return "创业"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=10000.0, help="本金(元)")
    ap.add_argument("--n", type=int, default=3, help="持仓只数")
    ap.add_argument("--comm", type=float, default=0.0001, help="佣金率(免五,默认万1)")
    args = ap.parse_args()

    pa.COMMISSION = args.comm          # 覆盖系统默认万2.6 → 免五费率
    CAPITAL = args.capital
    N = args.n

    # 最新交易日
    con = sqlite3.connect(f"file:{BASE/'data/cache/bars.db'}?mode=ro&immutable=1", uri=True, timeout=3)
    td = con.execute("SELECT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 1").fetchone()[0]
    con.close()

    all_c = gather_candidates(td)
    ok = [c for c in all_c if not excluded(c["code"], c["name"])]
    ok.sort(key=lambda x: -x["score"])
    top = ok[:N]

    print(f"=== 1万元模拟实盘（免五 万{args.comm*10000:.0f}，{N}只，等权整手）===")
    print(f"最新交易日 {td} | 全源候选 {len(all_c)} → 剔除ST/科创/创业后 {len(ok)} 可买")
    print(f"入选 Top{N}（按系统评分）:")
    for x in top:
        print(f"  {x['code']} {x['name']:<8} 价{x['price']:<8} score={x['score']} {','.join(x['src'])}")

    acc = pa.PaperAccount(ACC_NAME, cash=CAPITAL)
    target = CAPITAL / N
    fees = 0.0
    bought = []
    for x in top:
        price = x["price"]
        lots = int(target / (price * (1 + args.comm)) // 100)
        qty = lots * 100
        if qty <= 0:
            print(f"  ⚠ {x['code']} 单价{price}超单只预算{target:.0f}，跳过")
            continue
        r = acc.buy(x["code"], qty, td, close=price, reason="模拟实盘-机会池Top信号(免五)")
        if r.get("ok"):
            fees += r.get("fee", 0)
            bought.append({**x, "qty": qty, "cost": r["cost"], "fee": r["fee"]})
            print(f"  买入 {x['code']} {qty}股 @ {price} = {r['cost']:.2f} (佣{r['fee']:.2f})")
        else:
            print(f"  ✗ {x['code']} 下单失败: {r.get('msg')}")

    prices = {b["code"]: b["price"] for b in bought}
    total = acc.mark_to_market(prices, td)
    snap = acc.snapshot()

    print(f"\n--- 组合结果（{td} 建仓）---")
    print(f"投入本金   : {CAPITAL:,.2f}")
    print(f"持仓成本   : {sum(b['cost'] for b in bought):,.2f}")
    print(f"佣金合计   : {fees:,.2f}（万{args.comm*10000:.0f}免五，无5元底）")
    print(f"剩余现金   : {snap['cash']:,.2f}")
    print(f"当日净值   : {total:,.2f}（= 本金 - 佣金，收盘建仓）")
    print(f"持仓 {snap['n_positions']} 只:")
    for b in bought:
        print(f"   {b['code']} {b['name']:<8} {b['qty']}股  成本{b['cost']:.2f}  现价{b['price']}  浮盈亏 {(b['price']*b['qty']-b['cost']):+.2f}")

    # 同步到系统实盘裁决/止盈链
    # 写入真实 otype（机会池/Pitch 反查）+ 显式止损/止盈位（观察池直接显示 + 引擎可 enforcing）
    def _mk_pos(b):
        """按真实 otype 派生止盈/止损（★2026-10-15 P0 修复：原为硬编码 70%/-10%/26周，与类型脱钩）"""
        ep = b["price"]
        otype = b.get("otype") or "quality_gap"
        plan = type_stop_plan(otype, score=b.get("score"))
        # ★保留用户诉求的「保值覆盖」：类型原生无硬止损时，补 -10% 硬止损 + -15% 回撤线
        hard_stop_pct, dd_pct, overrode = plan.get("stop_loss_pct"), plan.get("max_drawdown_pct"), False
        if hard_stop_pct is None:
            hard_stop_pct, overrode = 0.10, True
        if dd_pct is None:
            dd_pct = 0.15
        tp = TAKE_PROFIT_RULES.get(otype, DEFAULT_TP)
        tp_out = {k: tp[k] for k in ("target_pct", "pullback_pct", "time_weeks")}
        sp = dict(plan)
        sp.update({"stop_loss_pct": hard_stop_pct, "max_drawdown_pct": dd_pct})
        if overrode:
            sp["desc"] = (str(plan.get("desc") or "") +
                          "｜保值覆盖：类型原生无硬止损（实证硬止损打掉防守股），按用户诉求补 -10% 硬止损 + -15% 回撤线")
        return {
            "code": b["code"], "name": b["name"], "entry_date": td,
            "entry_price": ep,
            "target": round(ep * (1 + tp_out["target_pct"]), 2),
            "stop": round(ep * (1 - hard_stop_pct), 2),
            "otype": otype,
            "stop_plan": sp,
            "take_profit": tp_out,
            "risk_level": "low", "status": "holding",
        }
    pf = {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_positions": N,
        "source": f"scripts/sim_live_1w.py (模拟实盘, 免五万{args.comm*10000:.0f}, 本金{CAPITAL:.0f})",
        "positions": [_mk_pos(b) for b in bought],
        "history": [],
    }
    (BASE / "logs" / "portfolio.json").write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (BASE / "logs" / f"portfolio_{ts}.json").write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入模拟账户 paper.db[{ACC_NAME}] + logs/portfolio.json(+时间戳副本)")
    print(f"→ 跑 `python risk/take_profit_check.py` 看止盈信号；`python strategy/sim_tracks.py --live` 看实盘估值")


if __name__ == "__main__":
    main()
