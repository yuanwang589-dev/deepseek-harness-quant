# -*- coding: utf-8 -*-
"""factors/opportunities/tech_pitch.py — 科技突破 Pitch 池 v2（2026-08-10 用户指示修正）

★背景：Deck 上全是价值股——因为 breakout/pv_consensus（强技术信号）评分低
  （市场容量小：breakout 全市场仅 0.3%），被统一 pitch 门槛过滤掉，科技股永远进不了 Deck。

★v2 修正（2026-08-10 用户反馈"科技池里一堆银行和药业，期望半导体/新科技/创业板"）：
  1. ★科技行业白名单过滤：只收 C39 电子（半导体/消费电子/通信）/ I65 软件 / C38 电气（新能源电池）
     / C40 仪器仪表 / C35 专用设备（半导体设备）/ C37 航空航天军工
  2. ★排除非科技行业：J66 银行、C27 医药、F 批发零售、K70 房地产、C15 酒饮料等
  3. ★创业板（300/301）+ 科创板（688）优先：同分排序靠前 + 徽章标记
  4. ★字段补全到 Pitch 同规格：horizons 1/2/3 年回测 + Beneish + 风控 + 类型止损方案
     （供"科技/价值两池并列"的 Pitch 卡页面消费）

输出：logs/tech_pitch_{ts}.json + deck/ 双写；Deck 路由 /api/tech_pitch + /pitch.html（合并页）
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
import glob
import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent   # factors/opportunities/ → deepseek-harness-quant
sys_path_ok = True
import sys
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TECH_TYPES = ("breakout", "pv_consensus")
TECH_GLOBAL_THRESHOLD = 62.0   # 独立门槛（技术类容量小）
TECH_TOP_N = 8                 # 科技池候选上限

# ★科技行业白名单（证监会行业分类代码，2026-08-10 用户指示：半导体/新科技）
TECH_INDUSTRIES = {
    "C39": "电子（半导体/消费电子/通信）",
    "I65": "软件与信息技术",
    "C38": "电气机械（新能源/电池/光伏）",
    "C40": "仪器仪表",
    "C35": "专用设备（半导体设备等）",
    "C37": "航空航天/军工",
}
# 明确排除（用户点名：银行/药业）
NON_TECH_KEYWORDS = ("J66", "C27", "J67", "F5", "K70", "C15", "A0", "H6")

RISK_NOTICE = {
    "breakout": "突破类 17 年胜率仅 45.5%（A股反转市不追高）：仅在强趋势市有效，建议仓位减半 + 突破回踩确认",
    "pv_consensus": "量价共识为多因子共识信号（ICIR≥0.47 实证），胜率 53.3%：建议观察 3-5 日量能是否延续",
}


def _tech_industry(industry: str) -> str:
    """行业代码 → 科技标签（None=非科技）"""
    if not industry:
        return None
    for kw in NON_TECH_KEYWORDS:
        if industry.startswith(kw):
            return None
    for code, label in TECH_INDUSTRIES.items():
        if industry.startswith(code):
            return label
    return None


def _board_badge(code: str) -> str:
    """板块徽章：创业板/科创板/其他"""
    c6 = code.split(".")[0]
    if c6.startswith(("300", "301")):
        return "创业板"
    if c6.startswith("688"):
        return "科创板"
    return ""


def load_latest() -> dict:
    files = sorted(glob.glob(str(BASE / "logs" / "tech_pitch_*.json")))
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch.py:82 load_latest")
    return {"ts": "", "entries": [], "new_codes": []}


def _holdout_one(code: str) -> dict:
    """1/2/3 年 PIT 回测（复用 pitch_v2 逻辑，供卡片回测证据区）"""
    try:
        from factors.opportunities.pitch_v2 import _holdout_one as _ho
        return _ho(code)
    except Exception:
        return None


def _enrich(c: dict) -> dict:
    """补全 Pitch 同规格字段：horizons + beneish + 风控 + 止损方案"""
    code = c["code"]
    out = {
        "code": code, "name": c.get("name", code),
        "industry": c.get("industry", ""),
        "otype": c["otype"], "otype_name": c.get("otype_name", c["otype"]),
        "score": c["score"], "trigger": c.get("trigger", ""),
        "evidence": c.get("evidence", ""),
        "risk_notice": RISK_NOTICE.get(c["otype"], ""),
        "confidence": "中置信" if c["score"] >= 70 else "低置信",
        "board": _board_badge(code),
    }
    # 回测证据（1/2/3 年）
    try:
        h = _holdout_one(code)
        if h:
            out["horizons"] = h
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch.py:114 _enrich")
    # Beneish
    try:
        from factors.opportunities.pitch_v2 import load_beneish
        m = load_beneish().get(code)
        if m:
            out["beneish"] = {"level": m.get("level"), "m_score": m.get("m_score")}
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch.py:122 _enrich")
    # 风控
    try:
        from factors.opportunities.pitch_v2 import load_risk_map
        rk = load_risk_map().get(code, {})
        if rk:
            out["risk_level"] = rk.get("level", c.get("risk_level"))
            out["risk_flags"] = rk.get("flags", c.get("risk_flags", []))
        else:
            out["risk_level"] = c.get("risk_level")
            out["risk_flags"] = c.get("risk_flags", [])
    except Exception:
        out["risk_level"] = c.get("risk_level")
        out["risk_flags"] = c.get("risk_flags", [])
    # 类型定制止损
    try:
        from risk.type_stop_rules import type_stop_plan
        sp = type_stop_plan(c["otype"], c["score"])
        out["stop_plan"] = sp
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch.py:142 _enrich")
    return out


def build(pool_file=None) -> Path:
    """从最新机会池提取技术类（科技行业白名单）→ 科技 Pitch 池"""
    if pool_file is None:
        files = sorted(glob.glob(str(BASE / "logs" / "opp_pool_*.json")),
                       key=os.path.getmtime)
        pool_file = files[-1] if files else None
    if not pool_file:
        raise FileNotFoundError("无机会池文件")
    d = json.loads(Path(pool_file).read_text(encoding="utf-8"))
    ops = d.get("opportunities", [])
    # ★v2：技术类 + 科技行业白名单过滤 + 板块优先
    tech_ops = [o for o in ops if o["otype"] in TECH_TYPES]
    cands_all = []
    for o in tech_ops:
        label = _tech_industry(o.get("industry", ""))
        if label is None:
            continue
        o["_tech_label"] = label
        o["_board_pri"] = 0 if _board_badge(o["code"]) else 1   # 创业/科创优先
        cands_all.append(o)
    # 分数 ≥ 独立门槛 → 排序（板块优先 → 分数）
    cands = sorted(
        [o for o in cands_all if o["score"] >= TECH_GLOBAL_THRESHOLD],
        key=lambda x: (x["_board_pri"], -x["score"]))[:TECH_TOP_N]
    # 无候选时：科技行业 Top3 兜底（低置信）
    if not cands:
        cands = sorted(cands_all, key=lambda x: (x["_board_pri"], -x["score"]))[:3]
    # NEW 监控
    prev = load_latest()
    prev_codes = {e["code"] for e in prev.get("entries", [])}
    new_codes = [c["code"] for c in cands if c["code"] not in prev_codes]

    entries = []
    for c in cands:
        e = _enrich(c)
        e["is_new"] = e["code"] in new_codes
        e["add_date"] = d.get("date", "")
        e["tech_label"] = c.get("_tech_label", "")
        entries.append(e)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pool_date": d.get("date", ""),
        "threshold": TECH_GLOBAL_THRESHOLD,
        "n_candidates": len(entries),
        "new_codes": new_codes,
        "tech_filters": {
            "industries": TECH_INDUSTRIES,
            "exclude": list(NON_TECH_KEYWORDS),
            "board_priority": "创业板(300)/科创板(688) 优先",
        },
        "entries": entries,
        "note": "科技突破 Pitch 池 v2：breakout+pv_consensus · 科技行业白名单（半导体/软件/新能源/军工等）"
                "· 排除银行/药业 · 创业板科创板优先；突破 17 年胜率 45.5% 需警惕追高；new=本次新增",
    }
    p = BASE / "logs" / f"tech_pitch_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    try:
        (BASE / "deck" / f"tech_pitch_{ts}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch.py:208 build")
    return p


if __name__ == "__main__":
    p = build()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"科技突破 Pitch 池 v2: {p.name} | {len(d['entries'])} 只 | 新增 {len(d['new_codes'])} 只")
    for e in d["entries"]:
        print(f"  [{'NEW' if e['is_new'] else '  '}] {e['code']} {e['name']} "
              f"{e['otype_name']} score={e['score']} | {e['tech_label']} {e['board']}")
