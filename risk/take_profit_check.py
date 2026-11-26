# -*- coding: utf-8 -*-
"""risk/take_profit_check.py — 实盘持仓自动止盈检查引擎（2026-08-11 百轮#4 实施）

★用户指示（20:48）："止盈系统条件没建立没融入观察池"——止损体系已有（position_stop_check/type_stop_rules），
  止盈只有逻辑规则（估值兑现/事件兑现）嵌入止损，缺独立执行器 + 观察池展示。

★规则（类型定制，与止损矩阵对齐 + 知识库《Pitch台深度指导建议》情景赔率）：
  ① 目标价止盈：收益 ≥ target_pct × 预期空间（兑现 70%）
  ② 移动止盈：持仓区间高点回撤 ≥ pullback_pct 且当前仍盈利 → 落袋
  ③ 时间止盈：持有 ≥ time_weeks 且收益 < 5%（时间换空间失败）
  ④ 逻辑止盈（建议标记）：type_stop_rules 的估值兑现/事件兑现（数据源接入后自动生效）

★边界（值守铁律，同 position_stop_check）：
  - 只读 portfolio.json / deck_decisions / pitch_v2 / bars.db，不修改他人文件
  - 输出 logs/take_profit_signals_{ts}.json（时间戳，写保护免疫）+ 控制台摘要
  - 融入观察池：dashboard_holdings 读最新 take_profit 显示"止盈状态"（目标价/移动回撤位/触发）

用法：
  python risk/take_profit_check.py            # 实盘检查
  python risk/take_profit_check.py --demo     # 3 只模拟持仓自测
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
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
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

# ★止盈矩阵（类型定制：目标兑现%/高点回撤%/时间周）
TAKE_PROFIT_RULES = {
    "value":          {"target_pct": 0.70, "pullback_pct": 0.10, "time_weeks": 13, "note": "厚安全垫长持，70%兑现"},
    "quality_gap":    {"target_pct": 0.70, "pullback_pct": 0.10, "time_weeks": 13, "note": "质量修复，70%兑现"},
    "revalue":        {"target_pct": 0.60, "pullback_pct": 0.08, "time_weeks": 13, "note": "重估兑现 60%"},
    "event":          {"target_pct": 0.60, "pullback_pct": 0.08, "time_weeks": 8,  "note": "事件驱动落袋快"},
    "pv_consensus":   {"target_pct": 0.50, "pullback_pct": 0.08, "time_weeks": 12, "note": "量价共识 50%兑现"},
    "breakout":       {"target_pct": 0.40, "pullback_pct": 0.06, "time_weeks": 8,  "note": "突破环境敏感，落袋快"},
    "reversal":       {"target_pct": 0.35, "pullback_pct": 0.06, "time_weeks": 6,  "note": "反弹兑现 35%"},
    "tech_sentiment": {"target_pct": 0.20, "pullback_pct": 0.06, "time_weeks": 4,  "note": "短线见光死，20%即走"},
}
DEFAULT_TP = {"target_pct": 0.50, "pullback_pct": 0.08, "time_weeks": 10, "note": "默认止盈"}


def _latest_bars(code: str, n: int = 120) -> pd.DataFrame:
    """qfq 日线最近 n 根：date/open/high/low/close（bars.db immutable 只读秒开）"""
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    try:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close FROM daily_bar "
            "WHERE code=? AND adjust='qfq' ORDER BY date DESC LIMIT ?", con, params=(code, n))
    finally:
        con.close()
    return df.sort_values("date").reset_index(drop=True)


def _lookup_otype(code: str) -> dict:
    """从 deck_decisions / pitch_v2 反查 (otype, upside_est)；查不到返回 {}
    ★#369 修复：glob 条件 bug——原 `if "*" in pat` 永远 False（pat 是固定名），fs 恒空，
      实际只读固定名旧残留；且 key=p.stat() 在 str 上报错。改正确 glob 取最新"""
    import glob as _g
    for base, is_pitch in ((DECISIONS, False), (PITCH_V2, True)):
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
                    return {"otype": it.get("otype"),
                            "upside_est": it.get("upside_est"),
                            "score": it.get("score"), "src": p.name}
    return {}


_stock_name_cache = None


def _stock_name(code: str) -> str:
    """★2026-08-13 #321：name 兜底从全市场名表补（持仓 name 为空时显示 code 的问题）"""
    global _stock_name_cache
    if _stock_name_cache is None:
        try:
            _bc = sqlite3.connect("file:data/cache/stock_basic.db?mode=ro&immutable=1", uri=True, timeout=3)
            _stock_name_cache = dict(_bc.execute("SELECT code, name FROM stock_basic").fetchall())
            _bc.close()
        except Exception:
            _stock_name_cache = {}
    return _stock_name_cache.get(code, code)


def tp_rule_for(otype: str) -> dict:
    """类型 → 止盈规则（config risk.take_profit_short_types 驱动）。
    长持类型（value/quality_gap/…）保留类型表目标；短线类型用 risk.profit_take_low。
    展示层兜底也必须调它，不能自己写死 20%。"""
    rule = dict(TAKE_PROFIT_RULES.get(otype or "", DEFAULT_TP))
    try:
        from risk.risk_config import risk_defaults
        _d = risk_defaults()
        _short = _d.get("take_profit_short_types") or ["tech_sentiment"]
        _tp_low = _d.get("take_profit_low")
        if _tp_low is not None and (otype or "") in _short:
            rule["target_pct"] = _tp_low
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/take_profit_check.py:121 tp_rule_for")
    return rule


def check_position(pos: dict) -> dict:
    """单持仓止盈检查 → 信号 dict"""
    code = pos.get("code")
    otype = pos.get("otype")
    info = _lookup_otype(code) if not otype else {}
    otype = otype or info.get("otype")
    upside_est = pos.get("target") or info.get("upside_est")
    rule = tp_rule_for(otype)
    bars = _latest_bars(code)
    nm = pos.get("name") or ""
    if not nm or nm == code:
        nm = _stock_name(code)   # ★#321 name 兜底（全市场名表）
    out = {"code": code, "name": nm, "otype": otype or "unknown",
           "entry_date": pos.get("entry_date"), "status": "holding", "signals": [], "tp": rule,
           # ★2026-11-20 前端直接读顶层字段（原只写 tp 子对象，持仓卡 target_pct/pullback_pct 恒 undefined）
           "target_pct": rule.get("target_pct"), "pullback_pct": rule.get("pullback_pct"),
           "time_weeks": rule.get("time_weeks"),
           # ★2026-11-20 目标止盈由类型规则推导（个股无专属目标）→ 标「规则推导」；
           #   若将来按审批写入个股专属目标，需同时让 target_pct 取该值，再把这里改成 bool(pos.get("target"))
           "estimated": True,
           "note": rule.get("note")}
    if len(bars) < 5:
        out["signals"].append({"type": "info", "msg": "K线不足，无法评估"})
        return out
    entry_price = pos.get("entry_price")
    # ★2026-08-13 修复：入场基准 = entry_date 之后的 bars（移动止盈只跟踪入场后高点）
    #   原 bug：entry_price 兜底用 120 根里最早 open、max_high 用 120 根历史最高 → 刚买入就误触发移动止盈
    entry_date_str = str(pos.get("entry_date") or "")
    bars_after = bars[bars["date"] >= entry_date_str] if entry_date_str else bars
    if not entry_price or entry_price <= 0:
        entry_price = float(bars_after.iloc[0]["open"]) if len(bars_after) else float(bars.iloc[-1]["close"])
    close = float(bars.iloc[-1]["close"])
    ret = close / entry_price - 1 if entry_price else 0.0
    max_high = float(bars_after["high"].max()) if len(bars_after) else entry_price
    pullback = (max_high - close) / max_high if max_high else 0.0
    hold_days = max(0, (datetime.strptime(str(bars.iloc[-1]["date"]), "%Y-%m-%d") -
                 datetime.strptime(str(pos.get("entry_date") or bars.iloc[0]["date"]), "%Y-%m-%d")).days)
    weeks = hold_days / 7.0
    out.update({
        "entry_price": round(entry_price, 3), "close": round(close, 3),
        "ret": round(ret, 4), "max_high": round(max_high, 3),
        "pullback": round(pullback, 4), "hold_days": hold_days, "weeks": round(weeks, 1),
        "target_price": round(entry_price * (1 + rule["target_pct"]), 3),
        "tp_note": rule["note"],
    })
    # ① 目标价止盈（兑现 70% 预期空间）
    if ret >= rule["target_pct"]:
        out["signals"].append({"type": "target", "msg": f"🎯 目标价止盈：已赚 {ret:.0%} ≥ {rule['target_pct']:.0%}，建议落袋"})
    # ② 移动止盈（从高点回撤 + 仍盈利）
    elif pullback >= rule["pullback_pct"] and ret > 0:
        out["signals"].append({"type": "pullback", "msg": f"🔔 移动止盈：从高点回撤 {pullback:.0%} ≥ {rule['pullback_pct']:.0%}，仍盈 {ret:.0%}，建议卖出"})
    # ③ 时间止盈（超期未兑现）
    elif weeks >= rule["time_weeks"] and ret < 0.05:
        out["signals"].append({"type": "time", "msg": f"⏰ 时间止盈：持有 {weeks:.0f} 周 ≥ {rule['time_weeks']} 周且仅 {ret:.0%}，建议退出换股"})
    # 正常持有 → 展示止盈位
    else:
        tp_bit = round(entry_price * (1 + rule["target_pct"]), 2)
        out["signals"].append({"type": "hold", "msg": f"正常持有：止盈位 {tp_bit}（+{rule['target_pct']:.0%}），移动回撤 {rule['pullback_pct']:.0%}，已持 {weeks:.0f} 周"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="3 只模拟持仓自测")
    args = ap.parse_args()
    if args.demo:
        positions = [
            {"code": "600221.SH", "name": "海航控股", "otype": "revalue", "entry_date": "2026-07-15", "entry_price": None},
            {"code": "002818.SZ", "name": "富森美", "otype": "revalue", "entry_date": "2026-07-01", "entry_price": 12.5},
            {"code": "688111.SH", "name": "金山办公", "otype": "value", "entry_date": "2026-05-10", "entry_price": None},
        ]
    else:
        # ★2026-08-11 写保护免疫：portfolio 已是时间戳文件（portfolio.py _save），固定名可能被锁/空 → glob 取最新
        import glob as _g
        fs = sorted([Path(p) for p in _g.glob(str(OUT_DIR / "portfolio_*.json"))],
                    key=lambda p: p.stat().st_mtime)
        pfile = fs[-1] if fs else PORTFOLIO
        if not pfile.exists():
            print("portfolio 文件不存在（未买入）→ 无持仓可查")
            return 0
        d = json.loads(pfile.read_text(encoding="utf-8"))
        positions = [p for p in d.get("positions", []) if p.get("status") in ("holding", "over_limit")]
        if not positions:
            print(f"无 holding/over_limit 持仓（{pfile.name}，总 {len(d.get('positions', []))} 条）")
    signals = [check_position(p) for p in positions]
    alerts = [s for s in signals if any(x["type"] in ("target", "pullback", "time") for x in s["signals"])]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "n_positions": len(signals),
           "n_alerts": len(alerts), "positions": signals, "alerts": alerts}
    p = OUT_DIR / f"take_profit_signals_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        (OUT_DIR / "take_profit_signals.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/take_profit_check.py:219 main")
    print(f"止盈检查：{len(signals)} 持仓 · {len(alerts)} 触发 | {p.name}")
    for s in signals:
        for sig in s["signals"]:
            print(f"  {s['code']} {s['name'][:6]}（{s['otype']} {s['ret']:+.0%}）{sig['msg']}")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
