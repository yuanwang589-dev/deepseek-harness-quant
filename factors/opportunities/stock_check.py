# -*- coding: utf-8 -*-
"""factors/opportunities/stock_check.py — 个股交叉评级（2026-08-10 用户需求）

★需求：输入个股代码 → 系统主动全维度过一遍 → 输出该股"交叉评级"。

交叉评级 = 多套独立系统交叉验证（谁都不单独说了算）：
  ① 机会引擎（7 类机会池命中/评分/排名）      权重 40
  ② 风控系统（8 红旗 + Beneish 造假 + 假信号） 权重 30
  ③ 技术形态（均线/动量/量比/距52周高）       权重 15
  ④ 估值水平（PB/PE + ROE）                  权重 15
  ⑤ 池状态交叉（Pitch 价值池/科技池/远期池）+ 止损方案

★性能设计（2026-08-10）：不走全市场面板（load_panel 5 分钟太慢）——
  在池股票直接用 opp_pool 数据（秒级）；不在池股票做单股轻量计算（sqlite 单股查询）。
  全市场分位不可得时用绝对估值，并标注"估值=绝对值"。

评级：A ≥80 重点关注 / B 70-79 关注 / C 60-69 中性 / D 50-59 谨慎 / E <50 回避

用法：
  python factors/opportunities/stock_check.py 000650
  Deck 路由 GET /api/stock_check?code=000650
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
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

BARS_DB = r"data/cache/bars.db"
QD_DB = r"data/cache/finance_quality.db"
BASIC_DB = r"data/cache/stock_basic.db"

TYPE_CN = {
    "value": "低估值", "revalue": "价值重估", "quality_gap": "质量折价",
    "pv_consensus": "量价共识", "event": "事件驱动", "breakout": "突破", "reversal": "反转",
}


def _ro_conn(db: str):
    """★immutable 只读连接（2026-08-10：普通 connect 因大文件+锁等待要 20s，只读场景用这个秒开）"""
    return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=3)

_valuation_cache = {"ts": 0, "data": None}


def _load_valuation():
    """当前估值快照（★2026-08-10：文件缓存跨进程——接口慢 85s，快照按日期存，当日复用）"""
    import pandas as pd
    today = datetime.now().strftime("%Y-%m-%d")
    snaps = sorted(glob.glob(str(BASE / "logs" / "valuation_snapshot_*.json")),
                   key=os.path.getmtime)
    if snaps:
        try:
            last = json.loads(Path(snaps[-1]).read_text(encoding="utf-8"))
            if last.get("date") == today and isinstance(last.get("data"), dict):
                return last["data"]
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:65 _load_valuation")
    try:
        from factors.opportunities import scan as S
        v = S.load_valuation() or {}
        if v:
            snap = {"date": today, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "data": v}
            try:
                p = BASE / "logs" / f"valuation_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                p.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            except Exception as _dsh_exc:
                _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:76 _load_valuation")
    except Exception:
        v = {}
    return v


def _latest_pool():
    files = sorted(glob.glob(str(BASE / "logs" / "opp_pool_*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:88 _latest_pool")
    return {}


def _latest_tech_pitch():
    files = sorted(glob.glob(str(BASE / "logs" / "tech_pitch_*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:98 _latest_tech_pitch")
    return {}


def _latest_pitch():
    files = sorted(glob.glob(str(BASE / "logs" / "pitch_v2_*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:108 _latest_pitch")
    return {}


def _latest_pitch_track():
    files = sorted(glob.glob(str(BASE / "logs" / "pitch_track_pool_*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:118 _latest_pitch_track")
    return {}


def _single_tech(code: str):
    """单股轻量技术指标（immutable 只读连接，秒级）→ dict"""
    import pandas as pd
    con = _ro_conn(BARS_DB)
    try:
        df = pd.read_sql_query(
            "SELECT date, close, volume FROM daily_bar WHERE code=? AND adjust='qfq' "
            "AND date >= '2024-01-01' ORDER BY date", con, params=(code,))
    except Exception:
        con.close()
        return {}
    con.close()
    if len(df) < 60:
        return {}
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    last = float(close.iloc[-1])
    ma50 = float(close.tail(50).mean())
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    mom20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else None
    vol_ratio = float(vol.iloc[-1] / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else None
    hi250 = float(close.tail(250).max()) if len(close) >= 60 else float(close.max())
    near_high = last / hi250 - 1
    lo60 = float(close.tail(60).min())
    dd60 = last / lo60 - 1
    if ma50 and ma200:
        if last >= ma50 and ma50 >= ma200:
            state = "🟢 双均线上（趋势健康）"
        elif last >= ma50:
            state = "🟡 仅MA50上（趋势初期）"
        elif last >= ma200:
            state = "🔵 仅MA200上（观察企稳）"
        else:
            state = "⚪ 均线下（弱势）"
    else:
        state = "—（数据不足）"
    return {"state": state, "last": round(last, 2), "ma50": round(ma50, 2),
            "ma200": round(ma200, 2) if ma200 else None,
            "mom20": round(mom20 * 100, 2) if mom20 is not None else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "near_high": round(near_high * 100, 1), "dd60": round(dd60 * 100, 1)}


def _is_tech(code: str, industry: str) -> bool:
    """★双轨判定（2026-08-10 用户需求：科技股不用估值评价，用成长+技术体系）
    科技轨 = 科技行业白名单（与 tech_pitch 一致）或 创业板/科创板
    """
    if code.split(".")[0].startswith(("300", "301", "688")):
        return True
    try:
        from factors.opportunities.tech_pitch import _tech_industry
        return _tech_industry(industry) is not None
    except Exception:
        return False


def _growth_metrics(code: str):
    """成长维度（finance_ts 最新财报）：净利同比 + ROE + 净资产增速"""
    try:
        import sqlite3 as sq
        con = sq.connect(f"file:{BARS_DB.rsplit('/', 1)[0]}/finance_ts.db?mode=ro&immutable=1"
                         if "/" in BARS_DB else
                         "file:data/cache/finance_ts.db?mode=ro&immutable=1",
                         uri=True, timeout=3)
        rows = con.execute(
            "SELECT ann_date, n_income, total_hldr_eqy_exc_min_int FROM financials_ts "
            "WHERE code=? AND ann_date IS NOT NULL AND ann_date != '' "
            "ORDER BY ann_date DESC LIMIT 4", (code,)).fetchall()
        con.close()
        if not rows:
            return None
        # 最新 + 找去年同期（ann_date 月日近似）
        latest = rows[0]
        ann = str(latest[0])[:10]
        yoy = None
        for r in rows[1:]:
            r_ann = str(r[0])[:10]
            if r_ann[:4] == str(int(ann[:4]) - 1) or abs(int(r_ann[5:7]) - int(ann[5:7])) <= 1:
                if r[1] and latest[1] and float(r[1]) != 0:
                    yoy = float(latest[1]) / float(r[1]) - 1
                break
        roe = None
        if latest[1] is not None and latest[2]:
            roe = float(latest[1]) / float(latest[2])
        return {"ann_date": ann, "np_yoy": yoy, "roe": roe}
    except Exception:
        return None


def _build_linkage(code: str) -> dict:
    """★#181 板块联动：聚合该股在 远期池三池/审批历史/因子归因 的状态（跨板块数据互通）"""
    out = {"pool_type": None, "pool_stop_plan": None, "pool_fwd": None,
           "decisions": [], "factors": None, "signal_family": None}
    try:
        from factors.opportunities.pitch_track import load_latest
        _pool = load_latest()
        for _e in _pool.get("entries", []):
            if _e.get("code") == code:
                out["pool_type"] = _e.get("pool_type") or "auto_pitch"
                out["pool_stop_plan"] = _e.get("stop_plan") or {}
                _fwd = _e.get("fwd") or {}
                out["pool_fwd"] = {h: (_fwd.get(h) or {}).get("ret") for h in ("t1", "t5", "t20")}
                break
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:226 _build_linkage")
    try:
        import glob as _g, json as _j, os as _o
        from pathlib import Path as _P
        _dfs = sorted(_g.glob(str(_P(__file__).resolve().parent.parent.parent / "logs" / "deck_decisions_*.json")),
                      key=lambda x: _P(x).stat().st_mtime)
        if _dfs:
            for _r in _j.loads(_P(_dfs[-1]).read_text(encoding="utf-8")):
                if isinstance(_r, dict) and _r.get("code") == code and _r.get("action") == "buy":
                    _pm = _r.get("pitch_meta") or {}
                    out["decisions"].append({"date": _r.get("date"), "env": _r.get("env_level", "")})
                    if _pm.get("factors"):
                        out["factors"] = _pm.get("factors")
                    if _pm.get("signal_family"):
                        out["signal_family"] = _pm.get("signal_family")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:242 _build_linkage")
    return out


def check(code6: str) -> dict:
    """单股交叉评级（轻量路径，秒级）
    ★2026-08-12 #201 修复：归一化 code（"603156.SH" → "603156"）——带后缀入参导致
      拼接 "603156.SH.SZ" 匹配失败 → name 显示 code（HTTP 全息弹窗暴露）"""
    code6 = str(code6 or "").split(".")[0].strip()
    t0 = time.time()

    # 基础信息（load_basic 改用 immutable 直读，避免普通连接 20s 锁等待）
    from factors.opportunities import scan as S
    import pandas as pd
    name, industry = code6, ""
    full_code = code6
    try:
        con = _ro_conn(BASIC_DB)
        basic = pd.read_sql("SELECT code, name, industry FROM stock_basic", con).set_index("code")
        con.close()
        for suf in (".SZ", ".SH", ".BJ"):
            cand = code6 + suf
            if cand in basic.index:
                full_code = cand
                row = basic.loc[cand]
                name = str(row.get("name", code6))
                industry = str(row.get("industry", ""))
                break
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:271 check")
    code = full_code

    # 数据日期（★#143 双库合并探测——主库写保护后增量库含最新日）
    con = _ro_conn(BARS_DB)
    pdate = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    con.close()
    try:
        from data.cache import DailyCache as _DC
        _mx = _DC().latest_trade_date()
        if _mx and _mx > pdate:
            pdate = _mx
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:284 check")

    # ---------- ① 机会引擎交叉（用最新机会池，不重算）----------
    pool = _latest_pool()
    opp = None
    for o in pool.get("opportunities", []):
        if o["code"] == code:
            opp = o
            break
    opp_dim = {"hit": False, "otype": None, "otype_cn": None, "score": None,
               "rank_in_type": None, "rank_global": None, "trigger": None, "note": None}
    opp_score = 0
    if opp:
        ot = opp["otype"]
        sc = opp.get("score", 0)
        opp_dim = {"hit": True, "otype": ot, "otype_cn": TYPE_CN.get(ot, ot),
                   "score": sc, "rank_in_type": opp.get("rank_in_type"),
                   "rank_global": opp.get("rank_global"),
                   "trigger": opp.get("trigger"), "note": opp.get("note")}
        if sc >= 80:
            opp_score = 40
        elif sc >= 70:
            opp_score = 35
        elif sc >= 62:
            opp_score = 30
        elif sc >= 55:
            opp_score = 20
        else:
            opp_score = 15
        if (opp.get("n_types_hit") or 1) >= 2:
            opp_score = min(40, opp_score + 4)
    else:
        opp_score = 6  # 未命中机会池（技术面近似的分在③④给）

    # ---------- ③ 技术形态（单股轻量计算，15 分）----------
    tech = _single_tech(code)
    tech_score = 0
    st = tech.get("state", "")
    if st.startswith("🟢"):
        tech_score += 8
    elif st.startswith("🟡"):
        tech_score += 5
    elif st.startswith("🔵"):
        tech_score += 3
    elif st.startswith("⚪"):
        tech_score -= 5
    if tech.get("mom20") is not None and tech["mom20"] > 0:
        tech_score += 4
    if tech.get("vol_ratio") is not None and tech["vol_ratio"] >= 1.5:
        tech_score += 3
    tech_score = max(-10, min(15, tech_score))
    if not tech:
        tech_score = 0  # 数据不足不惩罚

    # ---------- ④ 评价维度（★双轨：价值轨=估值 15 分；科技轨=成长 15 分，2026-08-10 用户需求）----------
    is_tech = _is_tech(code, industry)
    val_dim = {"pb": None, "pe": None, "roe": None, "np_yoy": None, "mode": "快照", "track": "价值轨" if not is_tech else "科技轨·成长"}
    val_score = 0
    if is_tech:
        # ★科技轨：不用 PB/PE（对成长股无意义）→ 成长维度（净利同比 + ROE）
        g = _growth_metrics(code)
        if g:
            val_dim["np_yoy"] = round(g["np_yoy"] * 100, 1) if g["np_yoy"] is not None else None
            val_dim["roe"] = round(g["roe"] * 100, 1) if g["roe"] is not None else None
            val_dim["ann_date"] = g.get("ann_date", "")
            val_dim["mode"] = "成长（最新财报）"
            yoy = g["np_yoy"]
            if yoy is not None:
                if yoy > 0.5:
                    val_score += 7
                elif yoy > 0.2:
                    val_score += 5
                elif yoy > 0:
                    val_score += 2
                elif yoy < -0.3:
                    val_score -= 5
            if g["roe"] is not None:
                if g["roe"] > 0.15:
                    val_score += 5
                elif g["roe"] > 0.08:
                    val_score += 3
                elif g["roe"] < 0:
                    val_score -= 5
            if g["np_yoy"] is not None and g["roe"] is not None:
                val_score += 3   # 成长+盈利双维度齐全加分
        else:
            val_dim["mode"] = "无财报数据"
    else:
        # 价值轨：PB/PE 估值（现有逻辑）
        val = _load_valuation()
        v = val.get(code) if isinstance(val, dict) else None
        if isinstance(v, dict):
            pb = v.get("pb")
            pe = v.get("pe")
            val_dim["pb"] = round(float(pb), 2) if pb else None
            val_dim["pe"] = round(float(pe), 1) if pe else None
            if pb is not None and float(pb) > 0:
                if float(pb) <= 1.0:
                    val_score += 6
                elif float(pb) <= 2.0:
                    val_score += 3
                elif float(pb) >= 8.0:
                    val_score -= 5
            if pe is not None and float(pe) > 0:
                if float(pe) <= 15:
                    val_score += 5
                elif float(pe) <= 30:
                    val_score += 2
                elif float(pe) >= 80:
                    val_score -= 4
    # ROE（质量库单股直查）
    try:
        con = _ro_conn(QD_DB)
        q = con.execute(
            "SELECT roe_avg FROM quality WHERE code=? ORDER BY period DESC LIMIT 1",
            (code,)).fetchone()
        con.close()
        roe = q[0] if q else None
        if roe is not None:
            val_dim["roe"] = round(float(roe) * 100, 1)
            if float(roe) > 0.08:
                val_score += 4
            elif float(roe) < 0:
                val_score -= 4
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:409 check")
    val_score = max(-10, min(15, val_score))
    if val_dim["pb"] is None and val_dim["pe"] is None:
        val_dim["mode"] = "无估值数据"

    # ---------- ② 风控交叉（30 分）----------
    risk_dim = {"level": None, "score": None, "flags": [], "beneish": None, "fake": []}
    risk_score = 10
    try:
        # ★单股直查 quality（immutable 连接，避开 20s 锁等待）
        from risk.stock_risk import _check_row
        con = _ro_conn(QD_DB)
        q = con.execute(
            "SELECT roe_avg, gp_margin, current_ratio, liability_to_asset, cfo_to_np "
            "FROM quality WHERE code=? ORDER BY period DESC LIMIT 1", (code,)).fetchone()
        con.close()
        if q:
            rk = _check_row(code, None, *q)
            risk_dim["level"] = rk.get("level")
            risk_dim["score"] = rk.get("score")
            risk_dim["flags"] = [fl.get("desc", "") for fl in rk.get("flags", [])[:8]]
            if rk.get("level") == "PASS":
                risk_score += 10
            elif rk.get("level") == "WATCH":
                risk_score += 4
            elif rk.get("level") == "BLOCK":
                risk_score -= 30
        else:
            risk_dim["level"] = "NO_DATA"
            risk_dim["flags"] = ["质量数据未覆盖"]
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:440 check")
    try:
        from factors.opportunities.pitch_v2 import load_beneish
        m = load_beneish().get(code, {})
        if m:
            risk_dim["beneish"] = {"level": m.get("level"), "m_score": m.get("m_score")}
            lv = m.get("level")
            if lv == "LOW":
                risk_score += 10
            elif lv == "WATCH":
                risk_score += 3
            elif lv == "HIGH":
                risk_score -= 20
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:454 check")
    try:
        from risk.fake_signal_flags import compute_flags
        con = _ro_conn(BARS_DB)
        fl = compute_flags([code], pdate, con=con).get(code, {})
        con.close()
        fake = fl.get("flags", []) if isinstance(fl, dict) else []
        risk_dim["fake"] = [f.get("name", str(f)[:30]) for f in fake[:5]]
        for x in fake:
            lvl = x.get("level", x.get("sev", "")) if isinstance(x, dict) else ""
            if lvl in ("BLOCK", "严重"):
                risk_score -= 20
            elif lvl in ("WARN", "警告"):
                risk_score -= 3
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:469 check")
    risk_score = max(-30, min(30, risk_score))

    # ---------- 交叉综合 ----------
    total = opp_score + risk_score + tech_score + val_score
    total = max(0, min(100, round(total)))
    if total >= 80:
        grade, label = "A", "重点关注"
    elif total >= 70:
        grade, label = "B", "关注"
    elif total >= 60:
        grade, label = "C", "中性"
    elif total >= 50:
        grade, label = "D", "谨慎"
    else:
        grade, label = "E", "回避"

    # 结论
    parts = []
    if opp:
        parts.append(f"机会引擎命中「{opp_dim['otype_cn']}」评分 {opp.get('score')}"
                     + (f"（同类 #{opp_dim['rank_in_type']}）" if opp_dim['rank_in_type'] else ""))
    else:
        parts.append("当前未命中任何机会类")
    if risk_dim["beneish"] and risk_dim["beneish"]["level"] == "HIGH":
        parts.append("Beneish 高造假风险")
    elif risk_dim["beneish"] and risk_dim["beneish"]["level"] == "LOW":
        parts.append("Beneish 无造假迹象")
    if risk_dim["level"] == "BLOCK":
        parts.append("⛔ 风控一票否决")
    if risk_dim["fake"]:
        parts.append(f"假信号警示 {len(risk_dim['fake'])} 项")
    if st.startswith("🟢"):
        parts.append("技术形态健康")
    elif st.startswith("⚪"):
        parts.append("技术面弱势")
    if val_score >= 8:
        parts.append("估值有安全垫")
    elif val_score <= -5:
        parts.append("估值偏高")
    conclusion = "；".join(parts) + f"。综合评级：{label}（{total} 分）"

    # ---------- 池状态 + 止损 ----------
    pitch = None
    for p in _latest_pitch().get("pitch", []):
        if p["code"] == code:
            pitch = p
            break
    tech_entries = [e for e in _latest_tech_pitch().get("entries", []) if e["code"] == code]
    track_entries = [e for e in _latest_pitch_track().get("entries", []) if e["code"] == code]

    stop_plan = None
    if opp:
        try:
            from risk.type_stop_rules import type_stop_plan
            stop_plan = type_stop_plan(opp["otype"], opp.get("score"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:526 check")

    # ★2026-08-11 百轮#44：信号联动（信号族/信号分/无效因子——#35 后 opp 自带）
    signal = None
    if opp:
        signal = {
            "signal_family": opp.get("signal_family"),
            "signal_score": opp.get("signal_score"),
            "n_invalid": opp.get("n_invalid", 0),
            "factor_eff_n": len(opp.get("factor_eff", {}) or {}),
        }
    # ★2026-08-12 百轮#73：强因子直通状态（#67-68 落地后个股页补充——跨家族独立证据）
    strong_hit = None
    try:
        from factors.opportunities.scan import load_strong_hits
        _sh = load_strong_hits().get(code)
        if _sh:
            _fams = sorted({v.get("family", "") for v in _sh.values() if v.get("family")})
            strong_hit = {
                "n_family": len(_fams),
                "families": _fams,
                "factors": {f: round(v.get("rank", 0), 3) for f, v in _sh.items()},
            }
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:550 check")
    # ★2026-08-12 百轮#73：FRC 因子风控状态（#65 落地——该股机会触发因子的 eff 降权/禁用）
    frc = None
    try:
        from factors.opportunities.scan import load_risk_multiplier
        _rm = load_risk_multiplier()
        _fe = opp.get("factor_eff", {}) if opp else {}
        if _rm and _fe:
            _effs = []
            for f in _fe.keys():
                eff = _rm.get(f)
                if eff is not None:
                    _effs.append({"factor": f, "eff": eff,
                                  "status": "禁用" if eff == 0 else ("降权" if eff < 1 else "正常")})
            if _effs:
                _n0 = sum(1 for e in _effs if e["eff"] == 0)
                _nlt = sum(1 for e in _effs if 0 < e["eff"] < 1)
                frc = {"n": len(_effs), "n_disabled": _n0, "n_down": _nlt, "factors": _effs[:12]}
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:569 check")
    # ★2026-08-11 百轮#44：远期表现（入池后实际收益 T+1/T+5/最新）
    forward = None
    if track_entries:
        fw = track_entries[0].get("fwd", {}) or {}
        fwd_map = {}
        for h in ("t1", "t5", "t20", "t60", "latest"):
            v = fw.get(h)
            if v and v.get("ret") is not None:
                fwd_map[h] = round(v["ret"], 4)
        forward = {"entry_date": track_entries[0].get("entry_date"),
                   "decided": track_entries[0].get("decided", ""), "rets": fwd_map}
    # ★2026-08-11 百轮#44：持仓状态（是否持仓 + 盈亏 + 止盈止损）
    position = None
    try:
        from strategy.portfolio import _load
        _pf = _load()
        for _p in _pf.get("positions", []):
            if _p.get("code") == code and _p.get("status") in ("holding", "over_limit"):
                position = {
                    "status": _p.get("status"),
                    "entry_date": _p.get("entry_date"),
                    "entry_price": _p.get("entry_price"),
                    "stop": _p.get("stop"),
                    "target": _p.get("target"),
                }
                break
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/stock_check.py:597 check")

    return {
        "ok": True, "code": code, "code6": code.split(".")[0], "name": name,
        "industry": industry, "date": pdate, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "rating": {"grade": grade, "label": label, "score": total},
        "dimensions": {
            "opportunity": opp_dim, "risk": risk_dim,
            "tech": tech, "valuation": val_dim,
        },
        "dim_scores": {"opportunity": opp_score, "risk": risk_score,
                       "tech": tech_score, "valuation": val_score},
        "pool_status": {
            "in_opp_pool": bool(opp), "in_pitch": bool(pitch), "in_tech_pitch": bool(tech_entries),
            "in_forward_track": bool(track_entries),
            "pitch_score": pitch["score"] if pitch else None,
        },
        "signal": signal,       # ★2026-08-11 百轮#44 信号联动（族/分/无效因子）
        "forward": forward,     # ★2026-08-11 百轮#44 远期实际表现（T+1/T+5/最新）
        "position": position,   # ★2026-08-11 百轮#44 持仓状态（是否持有/成本/止损目标）
        "strong_hit": strong_hit,   # ★2026-08-12 百轮#73 强因子直通（跨家族独立证据）
        "frc": frc,                 # ★2026-08-12 百轮#73 FRC 因子风控（eff 降权/禁用）
        "stop_plan": stop_plan,
        # ★2026-08-12 用户需求#181：板块数据联动——远期池三池状态/审批历史/因子归因/止损止盈
        "linkage": _build_linkage(code),
        "conclusion": conclusion,
    }


def main():
    ap = argparse.ArgumentParser(description="个股交叉评级")
    ap.add_argument("code", help="6 位股票代码，如 000650")
    args = ap.parse_args()
    r = check(args.code)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:2200])


if __name__ == "__main__":
    main()
