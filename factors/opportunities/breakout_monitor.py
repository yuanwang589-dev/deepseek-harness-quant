# -*- coding: utf-8 -*-
"""factors/opportunities/breakout_monitor.py — 突破实时监控（2026-08-10 用户需求 2b）

★需求：一旦有新股票实时符合突破条件就立刻提示。
★方案：独立监控脚本（计划任务每 30 分钟跑一次，与 DeckGuard 并行）——
  1. 用 bars.db 最新交易日数据检测 breakout 触发条件（距52周高<5% + 量比≥1.5 + 双均线向上）
  2. 对比"已提示集合"（logs/breakout_alerts.json）→ 新突破股 → 追加提示
  3. 输出：logs/breakout_alerts_{ts}.json（供 /dashboard_techpitch.html NEW 提示 + 门户横幅）
★诚实说明：数据源为 bars.db 日线（盘后数据，08-10 盘中无当日行情）；
  主服务器恢复后可接实时竞价/分钟流 → 升级为盘中真·实时检测。
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
import json

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception as _dsh_exc:
    _dsh_swallow(_dsh_exc, "factors/opportunities/breakout_monitor.py:20 <module>")

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # factors/opportunities/ → deepseek-harness-quant
sys.path.insert(0, str(BASE))

BARS_DB = r"data/cache/bars.db"


def check_breakouts(date: str = None) -> list:
    """检测最新交易日的突破候选（复用 scan 的触发条件）"""
    from factors.opportunities import scan as S
    px, vx = S.load_panel(end=date, days=320)
    if px is None or px.empty:
        return []
    basic = S.load_basic()
    fin = S.load_fundamentals(date)
    st = S.load_st_codes()
    f = S.compute_factors(px, vx, fin, basic, st)
    # breakout 触发：距52周高<5% + 量比≥1.5 + MA50/MA200 向上
    cond = ((f["near_high_250"] > -0.05) & (f["vol_ratio"] >= 1.5) &
            (f["ma50_up"] == 1) & (f["ma200_up"] == 1) & (f["non_st"] == 1))
    hits = f[cond]
    # 基本面加分（ROE>0 才提示）
    hits = hits[hits["roe"] > 0]
    out = []
    for code, row in hits.iterrows():
        out.append({
            "code": code, "name": row.get("name", code),
            "industry": row.get("industry", ""),
            "near_high_250": round(float(row["near_high_250"]), 4),
            "vol_ratio": round(float(row["vol_ratio"]), 2),
            "close": round(float(row["close"]), 2),
            "score_hint": round(60 + float(row["near_high_250"]) * 100 + float(row["vol_ratio"]) * 5, 1),
        })
    out.sort(key=lambda x: -x["score_hint"])
    return out


def _load_alerts() -> dict:
    import glob
    files = sorted(glob.glob(str(BASE / "logs" / "breakout_alerts_*.json")))
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/breakout_monitor.py:71 _load_alerts")
    return {"ts": "", "date": "", "codes": [], "history": []}


def run() -> Path:
    from factors.opportunities import scan as S
    # ★2026-08-10 双库合并探测：主库 + 增量库取最新（直连主库会停在 08-07）
    try:
        from data.cache import DailyCache
        last_date = DailyCache().latest_trade_date()
    except Exception:
        import sqlite3 as sq
        con = sq.connect(BARS_DB)
        last_date = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        con.close()
    if not last_date:
        return BASE / "logs" / "breakout_alerts_empty.json"

    hits = check_breakouts(last_date)
    prev = _load_alerts()
    prev_codes = set(prev.get("codes", []))
    new_hits = [h for h in hits if h["code"] not in prev_codes]
    all_codes = sorted({h["code"] for h in hits} | prev_codes)

    # 历史累积（每条：code/首次提示日/最近确认日）
    hist_map = {h["code"]: h for h in prev.get("history", [])}
    # ★假突破识别深化（2026-08-10 研究员指派落地）：资金流/龙虎榜/筹码三维验证
    #   接口夜间可能限流 → 降级 UNKNOWN（不阻断）；白天自动链有数据自动生效
    try:
        from data.moneyflow_verify import verify_breakout
        for h in hits:
            v = verify_breakout(h["code"], last_date)
            h["verify"] = {"verdict": v["verdict"], "support": v["support"], "against": v["against"]}
        n_real = sum(1 for h in hits if h.get("verify", {}).get("verdict") == "REAL")
        n_fake = sum(1 for h in hits if h.get("verify", {}).get("verdict") == "FAKE")
        print(f"  [资金验证] 突破候选 {len(hits)} 只：真确认 {n_real} / 假警示 {n_fake} / 其余 UNKNOWN")
    except Exception as e:
        print(f"  [资金验证] 跳过（模块不可用: {str(e)[:50]}）")
    for h in hits:
        if h["code"] in hist_map:
            hist_map[h["code"]]["last_seen"] = last_date
        else:
            hist_map[h["code"]] = {**h, "first_seen": last_date, "last_seen": last_date}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": last_date,
        "n_current": len(hits),
        "n_new": len(new_hits),
        "codes": all_codes,
        "new_hits": new_hits,
        "history": sorted(hist_map.values(), key=lambda x: x.get("first_seen", ""), reverse=True),
        "note": "突破监控：每 30 分钟检测最新交易日突破条件；new=新突破（触发提示）；数据源 bars.db 日线（盘后）",
    }
    p = BASE / "logs" / f"breakout_alerts_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    # 固定名（供读取方，时间戳版为主）
    try:
        (BASE / "logs" / "breakout_alerts.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/breakout_monitor.py:133 run")
    return p


if __name__ == "__main__":
    t0 = time.time()
    p = run()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"突破监控: {d['date']} | 当前 {d['n_current']} 只 | 新增 {d['n_new']} 只 | {time.time()-t0:.1f}s")
    for h in d.get("new_hits", [])[:5]:
        print(f"  🔔 NEW {h['code']} {h['name']} 距高{abs(h['near_high_250'])*100:.1f}% 量比{h['vol_ratio']}")
