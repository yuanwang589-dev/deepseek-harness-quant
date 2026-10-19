# -*- coding: utf-8 -*-
"""risk/position_stop_check.py — 实盘持仓自动止损检查引擎（B-11 落地建议 #1 · 外包 AI-2 · 2026-08-10）

★背景：B-11 审计结论——止损体系三套脱节，实盘层 ❌（portfolio.py 持仓有 stop 字段但无自动止损检查，
 卖出仅手动 --sell）。本模块为实盘层补上「每日收盘后自动止损扫描」。

★设计（审计报告3 第四节规格 + 研究员《止损策略与买入逻辑匹配研究》六步优先级）：
  ① 硬止损（仅 breakout 10%）→ 无条件优先
  ② 结构止损（跌破止损线，收盘确认）→ T+1 开盘执行
  ③ 时间止损（超期未兑现 time_stop_weeks + min_gain）→ 到期日
  ④ 移动止损（trailing_ma 跌破 MA）→ 每日更新
  ⑤ 最大回撤保护线（max_drawdown_pct）
  ⑥ 逻辑止损（logic_fail_rules：财报/事件证伪）→ 本版输出"建议人工复核"标记（数据源接入 TODO）

★边界（值守铁律）：
  - 只读 portfolio.json / deck_decisions.json / pitch_v2.json / bars.db，不修改任何他人文件
  - 持仓缺 otype 时从 deck_decisions / pitch_v2 反查；查不到 → 降级为"未知类型兜底"并提示
  - 输出 logs/stop_signals_{ts}.json（时间戳文件名，写保护免疫）+ 控制台摘要
  - 接入 daily_pipeline 属主程序，本模块独立可跑（--demo 自测）

用法：
  python risk/position_stop_check.py            # 实盘检查
  python risk/position_stop_check.py --demo     # 3 只模拟持仓自测（验收项）
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

BARS_DB = r"data\cache\bars.db"
PORTFOLIO = BASE / "logs" / "portfolio.json"
DECISIONS = BASE / "logs" / "deck_decisions.json"
PITCH_V2 = BASE / "logs" / "pitch_v2.json"
OUT_DIR = BASE / "logs"


# ---------- 数据 ----------

def _latest_bars(code: str, n: int = 80) -> pd.DataFrame:
    """qfq 日线最近 n 根：date/open/high/low/close/volume（bars.db 只读）"""
    con = sqlite3.connect(BARS_DB, timeout=30)
    try:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close FROM daily_bar "
            "WHERE code=? AND adjust='qfq' ORDER BY date DESC LIMIT ?", con, params=(code, n))
    finally:
        con.close()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _lookup_otype(code: str) -> dict:
    """从 deck_decisions / pitch_v2 反查 (otype, score)；查不到返回 {}
    ★#369 修复：读时间戳 glob 取最新（原读固定名 deck_decisions.json/pitch_v2.json 旧残留，
      反查失败致持仓显示 UNKNOWN——对齐 take_profit_check.py 的 glob 逻辑）"""
    import glob as _g
    for base in (DECISIONS, PITCH_V2):
        fs = sorted([Path(p) for p in _g.glob(str(base).replace(".json", "_*.json"))],
                    key=lambda p: p.stat().st_mtime)
        paths = fs + ([base] if base.exists() else [])
        for p in paths:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict):
                items = d.get("pitch") or d.get("decisions") or []
            else:
                items = d
            for it in items:
                if it.get("code") == code:
                    return {"otype": it.get("otype"), "score": it.get("score"),
                            "src": p.name}
    return {}


def _fill_entry_price(pos: dict, bars: pd.DataFrame) -> float:
    """entry_price 缺失时用 bars 首日（近似入场日）开盘回填；仍无 → None"""
    if pos.get("entry_price") and pos["entry_price"] > 0:
        return float(pos["entry_price"])
    if len(bars):
        return float(bars.iloc[0]["open"])
    return None


# ---------- 止损检查 ----------

def _atr(df: pd.DataFrame, n: int = 14) -> float:
    """ATR(n)：平均真实波幅（日线足够，审计报告第 3 节）"""
    if len(df) < n + 1:
        return np.nan
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.iloc[1:].rolling(n).mean().iloc[-1])


def check_position(pos: dict, ref_date: str) -> dict:
    """单个持仓止损检查 → 信号 dict"""
    code = pos.get("code")
    otype = pos.get("otype")
    score = pos.get("score")
    src = "portfolio"
    if not otype:
        info = _lookup_otype(code)
        otype, score, src = info.get("otype"), info.get("score"), info.get("src", "-")
    bars = _latest_bars(code)
    entry_price = _fill_entry_price(pos, bars)
    last = bars.iloc[-1] if len(bars) else None
    cur_price = float(last["close"]) if last is not None else None

    signals = []
    plan = {}
    atr_value = None
    if otype:
        from risk.type_stop_rules import type_stop_plan
        plan = type_stop_plan(otype, score or 70)
    # ★portfolio 配合项：持仓显式带 stop_plan 则覆盖类型默认（用户保值偏好/审批写入）
    if isinstance(pos.get("stop_plan"), dict):
        for _k in ("stop_loss_pct", "time_stop_weeks", "time_stop_min_gain",
                   "max_drawdown_pct", "trailing_ma", "pivot_check_pct",
                   "atr_stop_mult", "logic_fail_rules"):
            if _k in pos["stop_plan"] and pos["stop_plan"][_k] is not None:
                plan[_k] = pos["stop_plan"][_k]

    # 未知类型兜底（持仓无 otype 且反查不到）
    if not otype:
        signals.append({"rule": "未知类型", "level": "WARN",
                        "detail": f"持仓 {code} 无 otype 且 deck_decisions/pitch_v2 反查失败——"
                                  "建议审批时写入 stop_plan（portfolio 配合项）",
                        "action": "人工复核"})
        # 兜底：15% 回撤保护线
        plan = {"max_drawdown_pct": 0.15, "time_stop_weeks": 8}

    if cur_price is None or not np.isfinite(cur_price) or cur_price <= 0:
        signals.append({"rule": "数据缺失", "level": "WARN",
                        "detail": f"{code} 无最新行情（bars.db 无数据）", "action": "人工复核"})
        return {"code": code, "otype": otype, "cur_price": cur_price, "entry_price": entry_price,
                "plan": plan, "signals": signals, "ref_date": ref_date}

    hold_days = max(0, (last["date"] - pd.Timestamp(pos.get("entry_date") or ref_date)).days)
    drawdown = (cur_price / entry_price - 1) if entry_price and entry_price > 0 else None

    # ① 硬止损（仅 breakout 10%，无条件优先）
    sl = plan.get("stop_loss_pct")
    if sl and entry_price:
        stop_line = entry_price * (1 - sl)
        if cur_price <= stop_line:
            signals.append({"rule": "硬止损", "level": "SELL", "priority": 1,
                            "detail": f"收盘 {cur_price:.2f} ≤ 止损线 {stop_line:.2f}（{sl:.0%}）",
                            "action": "T+1 开盘卖出"})
    # ② 结构止损：跌破止损线收盘确认（若硬止损未触发但跌破进入警戒）
    elif sl and entry_price and cur_price <= entry_price * (1 - sl * 0.5):
        signals.append({"rule": "结构警戒", "level": "WATCH", "priority": 2,
                        "detail": f"距止损线 {(cur_price/(entry_price*(1-sl))-1)*100:.1f}%（收盘确认后触发）",
                        "action": "观察，收盘跌破即卖"})
    # ③ 时间止损
    tsw = plan.get("time_stop_weeks")
    if tsw:
        tg = plan.get("time_stop_min_gain") or 0.0
        over_due = hold_days >= tsw * 7
        gain_ok = drawdown is not None and drawdown >= tg
        if over_due and not gain_ok:
            signals.append({"rule": "时间止损", "level": "SELL", "priority": 3,
                            "detail": f"持有 {hold_days}d ≥ {tsw} 周且涨幅 {drawdown:+.1%} < {tg:+.0%}",
                            "action": "到期卖出"})
    # ④ 移动止损（trailing_ma 跌破）——★#368 基准=入场日之后区间
    #   （对齐 #320/#355 铁律：全历史 MA 会致"今天买入/浮盈0%就触发 SELL"——
    #     中际旭创/万辰集团今天入场、现价=入场价，却因历史 MA 高挂被误判）
    tma = plan.get("trailing_ma")
    if tma:
        try:
            _entry_dt = pd.Timestamp(pos.get("entry_date") or ref_date)
            _post = bars[bars["date"] >= _entry_dt]   # 入场日之后
        except Exception:
            _post = bars
        if len(_post) >= tma:
            ma = float(_post["close"].iloc[-tma:].mean())
            if cur_price < ma:
                signals.append({"rule": "移动止损", "level": "SELL", "priority": 4,
                                "detail": f"收盘 {cur_price:.2f} < MA{tma} {ma:.2f}（入场后）",
                                "action": "卖出"})
    # ④b ATR 宽止损（reversal 类型：ATR 3× 替代固定% —— 研究员 M2 矩阵）
    #   ★2026-10-18 接线：atr_stop_mult 此前只在 type_stop_rules 定义、从未被读取，
    #     导致反转类持仓实际无止损（test_gaps.test_atr_stop_implemented 长期红）。
    atr_mult = plan.get("atr_stop_mult")
    if atr_mult and entry_price:
        atr_value = _atr(bars)
        if np.isfinite(atr_value) and atr_value > 0:
            atr_line = entry_price - atr_mult * atr_value
            if cur_price <= atr_line:
                signals.append({"rule": "ATR止损", "level": "SELL", "priority": 2,
                                "detail": f"收盘 {cur_price:.2f} ≤ 入场 {entry_price:.2f} - "
                                          f"{atr_mult:g}×ATR({atr_value:.2f}) = {atr_line:.2f}",
                                "action": "卖出（宽止损防猎杀）"})
            elif cur_price <= entry_price - atr_mult * 0.6 * atr_value:
                signals.append({"rule": "ATR警戒", "level": "WATCH", "priority": 2,
                                "detail": f"距 ATR 止损线 {(cur_price / atr_line - 1) * 100:+.1f}%"
                                          f"（收盘跌破 {atr_line:.2f} 即卖）",
                                "action": "观察，收盘跌破即卖"})
        else:
            signals.append({"rule": "ATR不可算", "level": "WARN",
                            "detail": f"{code} 有效行情不足 15 根，ATR 止损未生效",
                            "action": "人工复核"})
    # ⑤ 最大回撤保护线
    mdd = plan.get("max_drawdown_pct")
    if mdd and drawdown is not None and drawdown <= -mdd:
        signals.append({"rule": "回撤保护", "level": "SELL", "priority": 5,
                        "detail": f"浮亏 {drawdown:+.1%} ≤ -{mdd:.0%}",
                        "action": "卖出"})
    # ⑥ 逻辑止损（财报/事件证伪）——本版标人工复核（数据源接入 TODO）
    if plan.get("logic_fail_rules"):
        signals.append({"rule": "逻辑止损", "level": "CHECK", "priority": 6,
                        "detail": "需财报/事件数据：[" + "; ".join(
                            f"{r.get('name')}({r.get('action')})" for r in plan["logic_fail_rules"]) + "]",
                        "action": "人工复核（TODO：接 finance_ts.db）"})
    # 假突破分级（breakout）
    if otype == "breakout" and plan.get("pivot_check_pct") and entry_price:
        piv = entry_price * (1 - plan["pivot_check_pct"])
        if cur_price <= piv:
            signals.append({"rule": "假突破", "level": "SELL", "priority": 2,
                            "detail": f"跌破突破点下方 {plan['pivot_check_pct']:.0%}（{piv:.2f}）",
                            "action": "清仓"})

    return {"code": code, "otype": otype or "UNKNOWN", "src": src,
            "cur_price": cur_price, "entry_price": entry_price,
            "hold_days": hold_days, "drawdown": round(drawdown, 4) if drawdown is not None else None,
            "plan": {k: plan.get(k) for k in ("stop_loss_pct", "time_stop_weeks", "trailing_ma",
                                               "max_drawdown_pct", "atr_stop_mult")},
            "atr_value": round(atr_value, 4) if atr_value is not None and np.isfinite(atr_value) else None,
            "signals": signals, "ref_date": ref_date}


def run(demo: bool = False) -> dict:
    ref_date = datetime.now().strftime("%Y-%m-%d")
    positions = []
    if demo:
        # 验收用 3 只模拟持仓（一只触发硬止损、一只时间止损、一只正常）
        positions = [
            {"code": "000001.SZ", "name": "平安银行", "status": "holding",
             "entry_price": 12.0, "entry_date": "2026-07-20", "otype": "breakout", "score": 75},
            {"code": "600519.SH", "name": "贵州茅台", "status": "holding",
             "entry_price": 1400.0, "entry_date": "2025-10-01", "otype": "revalue", "score": 70},
            {"code": "000650.SZ", "name": "仁和药业", "status": "holding",
             "entry_price": None, "entry_date": "2026-08-10", "otype": None, "score": None},
        ]
    else:
        # ★#367 修复：读时间戳 glob 取最新（原读固定名 portfolio.json 是 08-10 旧残留 holding=0，
        #   实盘止损检查每天失效——与 take_profit_check.py 的 glob 逻辑对齐）
        import glob as _g
        _pfs = sorted([Path(p) for p in _g.glob(str(OUT_DIR / "portfolio_*.json"))],
                      key=lambda p: p.stat().st_mtime)
        p = _pfs[-1] if _pfs else PORTFOLIO
        if not p.exists():
            return {"ok": False, "error": "portfolio 时间戳文件不存在", "ref_date": ref_date}
        d = json.loads(p.read_text(encoding="utf-8"))
        positions = [x for x in (d.get("positions") or []) if x.get("status") == "holding"]

    results = [check_position(pos, ref_date) for pos in positions]
    out = {"ok": True, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ref_date": ref_date, "mode": "demo" if demo else "live",
           "n_positions": len(results), "results": results}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"stop_signals_{ts}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已存 {out_path}（{len(results)} 持仓）\n")
    for r in results:
        lvl = "—"
        acts = []
        for s in r["signals"]:
            if s["level"] == "SELL":
                lvl = "SELL"
                acts.append(f"{s['rule']}: {s['action']}")
            elif s["level"] == "WATCH":
                lvl = lvl if lvl != "SELL" else lvl
                acts.append(f"{s['rule']}: {s['action']}")
            elif s["level"] == "CHECK":
                acts.append(f"{s['rule']}: {s['action']}")
            elif s["level"] == "WARN":
                acts.append(f"{s['rule']}: {s['action']}")
        dd = f"{r['drawdown']:+.1%}" if r.get("drawdown") is not None else "—"
        print(f"{r['code']} [{r.get('otype') or 'UNKNOWN'}] 现价 {r.get('cur_price')} 入场 {r.get('entry_price')} "
              f"浮盈 {dd} 持有 {r.get('hold_days')}d → {lvl}")
        for a in acts:
            print(f"    {a}")
        if not acts:
            print("    正常，无触发信号")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="3 只模拟持仓自测")
    args = ap.parse_args()
    run(demo=args.demo)
