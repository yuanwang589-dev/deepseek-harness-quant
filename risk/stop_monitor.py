# -*- coding: utf-8 -*-
"""risk/stop_monitor.py — 止损条件自动检测器（2026-08-10 用户需求）

★需求：不只展示止损方案，而是**自动检测止损条件是否达成**（回撤保护线/时间止损/逻辑失效）。

监测对象：pitch_track 远期池入池股（真实 Pitch 入池，含 entry_date/entry_close/otype）
          + portfolio.json 真实持仓（若有，按类型匹配方案）

检测项（每只股票按其机会类型的定制方案）：
  ① 回撤保护线：当前价 vs 入池价回撤 ≥ max_drawdown_pct → TRIGGERED
  ② 时间止损：入池 ≥ time_stop_weeks 且收益 < time_stop_min_gain → TRIGGERED
  ③ 逻辑失效（可计算项）：
     - 财务证伪（value/revalue）：最新已披露财报 ROE < 0 → TRIGGERED
     - 动量再转负（reversal/pv_consensus）：20 日动量 < 0 → TRIGGERED
     - 量能衰竭（breakout）：量比 < 0.7 → WARN
     - 突破点下方（breakout）：价 < 入池价 × (1-8%) → TRIGGERED
     - 均线失守（pv_consensus）：价 < MA20 → NEAR

输出：logs/stop_alerts_{ts}.json（触发清单 + 汇总）+ deck/ 双写
  → 待处理面板 /dashboard_actions.html 消费
"""
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # risk/ → deepseek-harness-quant
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

BARS_DB = str(BASE / "data" / "cache" / "bars.db")   # ★2026-10-15 P1-A 绝对路径（原相对依赖 cwd）
FIN_TS_DB = r"data/cache/finance_ts.db"


def _ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=3)


def _latest(files):
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _single_series(code: str):
    """单股行情（2024 起）→ DataFrame"""
    import pandas as pd
    con = _ro(BARS_DB)
    try:
        df = pd.read_sql_query(
            "SELECT date, close, volume FROM daily_bar WHERE code=? AND adjust='qfq' "
            "AND date >= '2024-01-01' ORDER BY date", con, params=(code,))
    except Exception:
        df = pd.DataFrame()
    con.close()
    return df


def _latest_roe(code: str):
    """最新已披露财报 ROE（ann_date ≤ 今日；n_income/equity）"""
    try:
        con = _ro(FIN_TS_DB)
        r = con.execute(
            "SELECT ann_date, n_income, total_hldr_eqy_exc_min_int FROM financials_ts "
            "WHERE code=? AND ann_date IS NOT NULL AND ann_date != '' "
            "ORDER BY ann_date DESC LIMIT 1", (code,)).fetchone()
        con.close()
        if r and r[1] is not None and r[2] and float(r[2]) > 0:
            return {"ann_date": str(r[0])[:10], "roe": float(r[1]) / float(r[2])}
    except Exception:
        pass
    return None


def _check_one(e: dict, plan: dict) -> dict:
    """单只入池股止损检测 → {status, alerts, metrics}"""
    code = e.get("code", "")
    otype = e.get("otype", "value")
    entry_close = e.get("entry_close")
    entry_date = e.get("entry_date", "")
    age_days = e.get("age_days", 0)
    df = _single_series(code)
    alerts = []
    metrics = {"entry_close": entry_close, "age_days": age_days}
    if df is None or df.empty or len(df) < 20:
        return {"code": code, "name": e.get("name", code), "otype": otype,
                "status": "NO_DATA", "alerts": [{"rule": "行情数据不足", "action": "无法检测"}],
                "metrics": metrics}

    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    last = float(close.iloc[-1])
    mom20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else None
    vol_ratio = float(vol.iloc[-1] / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else None
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
    hi = float(close.max())
    # ★#355 止损基准=入池日之后的区间（对齐 #320 移动止盈铁律）：
    #   MA20 失守应判「现价跌破入池日那天的 MA20 支撑」，而非「现价 < 全历史最新 MA20」
    #   （后者=几乎所有人池股都"失守"——MA20 是历史均价，现价本就常在其上下波动，
    #     今天刚买入也误报"失守"；且最新 MA20 含入池后的价格，基准被现价污染）
    ma20_at_entry = None
    if entry_date:
        try:
            _idx = df.index[df["date"] <= entry_date]
            if len(_idx) > 0:
                _upto = close.loc[:_idx[-1]]
                if len(_upto) >= 20:
                    ma20_at_entry = float(_upto.tail(20).mean())
        except Exception:
            ma20_at_entry = None
    metrics.update({"last": round(last, 3), "mom20": round(mom20, 4) if mom20 is not None else None,
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
                    "ma20": round(ma20, 2), "ma60": round(ma60, 2) if ma60 else None,
                    "ma20_at_entry": round(ma20_at_entry, 2) if ma20_at_entry else None})

    status = "NORMAL"
    # ①-bis 硬止损（显式 stop_loss_pct，portfolio 配合项 / 模拟仓保值覆盖）
    sl = plan.get("stop_loss_pct")
    if sl and entry_close:
        _sl_line = entry_close * (1 - sl)
        if last <= _sl_line:
            alerts.append({"rule": f"硬止损（-{sl*100:.0f}%）",
                           "detail": f"现价 {last:.2f} ≤ 止损线 {_sl_line:.2f}",
                           "action": "离场"})
            status = "TRIGGERED"
    # ① 回撤保护线（vs 入池价）
    dd_protect = plan.get("max_drawdown_pct")
    if dd_protect and entry_close:
        dd = last / entry_close - 1
        metrics["dd_from_entry"] = round(dd, 4)
        if dd <= -dd_protect:
            alerts.append({"rule": f"回撤保护线（-{dd_protect*100:.0f}%）",
                           "detail": f"现价 {last:.2f}，入池 {entry_close:.2f}，回撤 {dd*100:.1f}%",
                           "action": "离场"})
            status = "TRIGGERED"
    # ② 时间止损
    ts_weeks = plan.get("time_stop_weeks")
    ts_min = plan.get("time_stop_min_gain", 0.0)
    if ts_weeks and age_days >= ts_weeks * 7 and entry_close:
        ret = last / entry_close - 1
        if ret < ts_min:
            alerts.append({"rule": f"时间止损（{ts_weeks} 周未达标）",
                           "detail": f"入池 {age_days} 天，收益 {ret*100:.1f}%（要求 ≥{ts_min*100:.0f}%）",
                           "action": "离场"})
            if status != "TRIGGERED":
                status = "TRIGGERED"
    # ③ 逻辑失效（按类型可计算项）
    fail_rules = {r.get("name", ""): r for r in plan.get("logic_fail_rules", [])}
    if "财务证伪" in fail_rules or "业绩证伪" in fail_rules:
        roe = _latest_roe(code)
        if roe and roe["roe"] < 0:
            alerts.append({"rule": "财务证伪",
                           "detail": f"最新财报（{roe['ann_date']}）ROE {roe['roe']*100:.1f}% 转负",
                           "action": "立即卖出"})
            status = "TRIGGERED"
        metrics["roe"] = round(roe["roe"], 4) if roe else None
    if "动量再转负" in fail_rules and mom20 is not None and mom20 < 0:
        alerts.append({"rule": "动量再转负", "detail": f"20 日动量 {mom20*100:.1f}% <0", "action": "卖出"})
        if status == "NORMAL":
            status = "TRIGGERED"
    if "量能衰竭" in fail_rules and vol_ratio is not None and vol_ratio < 0.7:
        alerts.append({"rule": "量能衰竭", "detail": f"量比 {vol_ratio:.2f} <0.7", "action": "预警减仓"})
        if status == "NORMAL":
            status = "NEAR"
    if "突破点下方" in fail_rules or otype == "breakout":
        pivot = plan.get("pivot_check_pct")
        if pivot and entry_close and last < entry_close * (1 - pivot):
            alerts.append({"rule": "突破点下方", "detail": f"跌破入池价 {pivot*100:.0f}%", "action": "强制卖出"})
            status = "TRIGGERED"
    # 均线预警（NEAR）——★#355 语义修正：
    #   只在「入池日收盘价在 MA20 上方（有均线支撑）」时启用；
    #   入池时本就跌破 MA20（低位买入的价值股）→ 该规则不适用，不预警。
    #   另加 1% 缓冲：跌破幅度 <1% 属噪声（如 4.85→4.83），不预警。
    if status == "NORMAL" and ma20_at_entry and entry_close \
            and entry_close > ma20_at_entry and last < ma20_at_entry * 0.99:
        alerts.append({"rule": "MA20 失守",
                       "detail": f"现价 {last:.2f} 跌破入池日 MA20 {ma20_at_entry:.2f}（入池 {entry_close:.2f} 在均线上方）",
                       "action": "关注"})
        status = "NEAR"

    return {"code": code, "name": e.get("name", code), "otype": otype,
            "entry_date": entry_date, "status": status, "alerts": alerts, "metrics": metrics}


def run() -> Path:
    """监测 pitch_track 入池股 + 模拟仓真实持仓（portfolio）→ 输出触发清单"""
    from risk.type_stop_rules import type_stop_plan
    from datetime import date as _date

    # ① pitch_track 远期池
    pt = _latest(sorted(glob.glob(str(BASE / "logs" / "pitch_track_pool_*.json"))))
    merged = {}
    for e in pt.get("entries", []):
        if e.get("entry_close") and e.get("code"):
            merged[e["code"]] = e

    # ② 模拟仓真实持仓（最新 portfolio_*.json；同 strategy.portfolio._load 口径）
    for pf in sorted(glob.glob(str(BASE / "logs" / "portfolio_*.json")),
                    key=lambda x: Path(x).stat().st_mtime):
        try:
            d = json.loads(Path(pf).read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in d.get("positions", []):
            if p.get("status") not in ("holding", "over_limit"):
                continue
            code = p.get("code")
            if not code or not p.get("entry_price"):
                continue
            ep = float(p["entry_price"])
            try:
                _age = (_date.today() - _date.fromisoformat(str(p.get("entry_date")))).days
            except Exception:
                _age = 0
            merged[code] = {                      # 模拟仓优先（真实持仓，含显式 stop_plan）
                "code": code, "name": p.get("name", code),
                "otype": p.get("otype") or "quality_gap",
                "entry_close": ep, "entry_date": p.get("entry_date", ""),
                "age_days": _age,
                "stop_plan": p.get("stop_plan"),  # portfolio 配合项：保值覆盖在此生效
            }

    results = []
    for e in merged.values():
        plan = type_stop_plan(e.get("otype", "value"), e.get("score") or 70)
        sp = e.get("stop_plan")
        if isinstance(sp, dict):                 # 显式 stop_plan 覆盖类型默认
            for _k in ("stop_loss_pct", "time_stop_weeks", "time_stop_min_gain",
                       "max_drawdown_pct", "trailing_ma", "pivot_check_pct",
                       "atr_stop_mult", "logic_fail_rules"):
                if _k in sp and sp[_k] is not None:
                    plan[_k] = sp[_k]
        results.append(_check_one(e, plan))

    n_trig = sum(1 for r in results if r["status"] == "TRIGGERED")
    n_near = sum(1 for r in results if r["status"] == "NEAR")
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "monitored": len(results),
        "triggered": n_trig,
        "near": n_near,
        "entries": results,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = BASE / "logs" / f"stop_alerts_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    try:
        (BASE / "deck" / f"stop_alerts_{ts}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return p


if __name__ == "__main__":
    p = run()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"止损监测: {p.name} | 监测 {d['monitored']} 只 | 触发 {d['triggered']} | 预警 {d['near']}")
    for r in d["entries"]:
        mark = {"TRIGGERED": "🔴", "NEAR": "🟡", "NORMAL": "🟢", "NO_DATA": "⚪"}.get(r["status"], "?")
        print(f"  {mark} {r['code']} {r['name']} {r['otype']} [{r['status']}] "
              + ("；".join(a['rule'] for a in r['alerts']) or "正常"))
