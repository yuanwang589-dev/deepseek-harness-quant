# -*- coding: utf-8 -*-
"""★2026-08-13 #296 知识库 AI 每日自动化（DSHQuant-AIReview 计划任务 20:00）
职责（备料 + 检查履职——主观分析由知识库 AI 完成，主系统自动准备输入/追踪输出）：
  1. 刷新市场知识库视图（market_kb_dump.json——AI 分析主输入）
  2. 生成当日《AI 简报》logs/ai_brief_YYYYMMDD.json（市场摘要 + 候选 top + 任务指令——AI 读它即开工）
  3. 检查当日 AI 是否已提交选股（/api/ai/select 写入 ai_insights.json）：
     已提交 → 确认记录；未提交 → 告警（logs/ai_missed.log + 输出提醒）
用法：python data/ai_daily_review.py [--date YYYY-MM-DD]
"""
# ★2026-08-13 黑框隐藏（计划任务不弹黑框）
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
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception as _dsh_exc:
    _dsh_swallow(_dsh_exc, "data/ai_daily_review.py:16 <module>")

import sys
import json
import os
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
LOGS = BASE / "logs"
REPORT = BASE / "report"


def today():
    return datetime.now().strftime("%Y-%m-%d")


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def refresh_market_kb():
    """刷新市场知识库视图（build_market_kb --dump）"""
    try:
        import subprocess
        r = subprocess.run([sys.executable, "-X", "utf8", str(BASE / "data" / "build_market_kb.py"), "--dump"],
                           capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        ok = "AI 导出" in (r.stdout or "")
        return ok, (r.stdout or r.stderr)[-120:]
    except Exception as e:
        return False, str(e)


def build_brief(date):
    """生成当日 AI 简报（市场摘要 + 候选 top + 因子实测 + 任务指令）"""
    kb = _read(REPORT / "market_kb_dump.json")
    # ★读最新时间戳文件（pitch_v2_*.json / tech_pitch_*.json——固定名不存在或旧）
    _pfs = sorted((LOGS).glob("pitch_v2*.json"), key=lambda x: x.stat().st_mtime)
    pitch = _read(_pfs[-1]) if _pfs else {}
    pitch_es = pitch.get("pitch") or []
    _tfs = sorted((LOGS).glob("tech_pitch*.json"), key=lambda x: x.stat().st_mtime)
    tech = _read(_tfs[-1]) if _tfs else {}
    tech_es = tech.get("pitch") or tech.get("tech") or tech.get("entries") or []

    md = ((kb or {}).get("market_daily_recent") or [{}])[0]
    brief = {
        "date": date,
        "instruction": "请按《知识库 AI 操作手册 v1》分析本简报：①定市场基调（择时/四维）②从候选池主观选 ≤5 只 "
                       "③POST /api/ai/select 提交（带 reason/confidence）④宁缺毋滥，没把握可不选。",
        "market": {
            "timing": f"{md.get('timing_level')} {md.get('timing_score')} 分",
            "dims": {"政策": md.get("dim_policy"), "宏观": md.get("dim_macro"),
                     "情绪": md.get("dim_emotion"), "宽度": md.get("dim_width")},
            "宽度": f"涨 {md.get('n_up')} / 跌 {md.get('n_down')}",
            "成交额_亿": md.get("turnover_亿"),
            "拥挤度分位": md.get("crowd_pctile"),
            "池子": f"机会 {md.get('n_opp')} / Pitch {md.get('n_pitch')} / 短线 {md.get('n_tech')}",
            "择时理由": (md.get("reason") or "")[:400],
        },
        "candidates_long": [{"code": c.get("code"), "name": c.get("name"),
                             "otype": c.get("otype_name") or c.get("otype"),
                             "score": c.get("score"),
                             "factors": list((c.get("factors") or {}).keys())[:5]}
                            for c in sorted(pitch_es, key=lambda x: -(x.get("score") or 0))[:10]],
        "candidates_short": [{"code": c.get("code"), "name": c.get("name"),
                              "confidence": c.get("confidence")}
                             for c in sorted(tech_es, key=lambda x: -(float(x.get("confidence") or 0) if not isinstance(x.get("confidence"), str) else float(x["confidence"]) if x["confidence"].replace('.','',1).isdigit() else 0))[:10]],
        "factor_live_top": (kb or {}).get("factor_perf_top", [])[:10],
        "提交接口": "POST /api/ai/select  {picks:[{code,reason,confidence}], date:'" + date + "'}",
    }
    f = LOGS / f"ai_brief_{date}.json"
    f.write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def check_ai_submission(date):
    """★#297 检查当日 AI 是否已回应（选股 or 明确"今日不选"都算履职——自由裁决权）"""
    recs = _read(LOGS / "ai_insights.json") or []
    todays = [r for r in recs if r.get("date") == date]
    if todays:
        picks = [r for r in todays if r.get("type") != "skip"]
        skips = [r for r in todays if r.get("type") == "skip"]
        if picks:
            return {"submitted": True, "n": len(picks), "skip": False,
                    "codes": [r.get("code") for r in picks]}
        return {"submitted": True, "n": 0, "skip": True,
                "skip_reason": (skips[0].get("reason") or "")[:60], "codes": []}
    # 未回应 → 告警
    miss = LOGS / "ai_missed.log"
    with open(miss, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} AI 未回应（{date}）——未选股也未说明今日不选，请知识库 AI 处理\n")
    return {"submitted": False, "n": 0, "skip": False, "codes": []}


def main():
    date = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else today()
    ok_kb, kb_msg = refresh_market_kb()
    brief = build_brief(date)
    st = check_ai_submission(date)
    if st.get("skip"):
        _st_txt = f"今日不选（{st.get('skip_reason', '')}）——已记录"
    elif st["submitted"]:
        _st_txt = f"已选 {st['n']} 只 " + ",".join(st["codes"][:5])
    else:
        _st_txt = "未回应（已告警）"
    line = (f"AI每日: {date} 视图{'✅' if ok_kb else '❌'} | 简报 {brief.name} | 回应: {_st_txt}")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
