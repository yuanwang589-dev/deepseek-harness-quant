# -*- coding: utf-8 -*-
"""data/policy_hook.py — 政策面择时钩子（过渡方案 · 2026-08-11）

★宏观政策传导研究要求：policy_score 接入择时（防守触发器）。
  ⚠️ 用户指示（08-11）：政策面统一走因子池通道——本模块为过渡方案，待外包因子池
  完成政策因子代码（EPU locked 重验证 + 政策线因子进 factor_manifest）→ 主系统
  manifest 消费端自动显示 + 本钩子改读 manifest 政策因子（届时退役本方案）。
  实现：调 factors/policy/policy_score.py 的 compute_score（外包代码不动）→ 写缓存
  output/policy_score_latest.json（{month, score, bucket}，月度一次）→ regime_cash 读它降档。
  防守区（score<-0.5）→ 额外降 1 档（full→half，cash 至少 0.5）；中性/进攻不干预。
  当前状态：2026-06 score +0.21 中性区 → 0 干预（无害过渡）。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

OUT = BASE / "output" / "policy_score_latest.json"
_cache = {"ts": 0.0, "data": None}
_fail = {"ts": 0.0}          # ★2026-10-18 失败也缓存，避免每次调用都去打开缺失的库
_TTL = 12 * 3600  # 12h（月度数据，防高频重算 macro.db）


def _compute():
    try:
        from factors.policy import policy_score as ps
        panel = ps.load_panel()
        p = ps.compute_score(panel)
        latest = p.dropna(subset=["epu"]).tail(1)
        if latest.empty:
            return None
        r = latest.iloc[-1]
        return {"month": r["month"], "score": round(float(r["score"]), 3),
                "bucket": r["bucket"], "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        # ★2026-10-18：原截断 60 字符会把"缺哪个库"这个关键信息切掉，改成保留完整原因
        print(f"  [policy_hook] 政策面不可算 → 择时退化为中性 50 | {type(e).__name__}: {str(e)[:160]}")
        return None


def load() -> dict:
    """读政策评分（缓存 12h；无缓存/失败 → {} 不阻断）"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _TTL:
        return _cache["data"]
    if _fail["ts"] and now - _fail["ts"] < _TTL:
        return {}          # 12h 内已知不可算，直接返回空（不重复开库、不刷屏）
    if OUT.exists() and now - OUT.stat().st_mtime < _TTL:
        try:
            d = json.loads(OUT.read_text(encoding="utf-8"))
            _cache.update({"ts": now, "data": d})
            return d
        except Exception:
            pass
    d = _compute()
    if d:
        try:
            OUT.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        _cache.update({"ts": now, "data": d})
        _fail["ts"] = 0.0
    else:
        # 失败同样进缓存窗口：12h 内不再重试（缺库是结构性缺失，重试无意义且刷屏）
        _fail["ts"] = now
    return d or {}


def regime_penalty() -> float:
    """政策防守降仓：防守区 → 额外现金 0.5（full→half 级）；否则 0"""
    d = load()
    if d.get("bucket") == "防守区":
        return 0.5
    return 0.0


if __name__ == "__main__":
    d = load()
    print(f"政策评分: {d.get('month', '?')} score={d.get('score', '?')} bucket={d.get('bucket', '?')} → 降仓 {regime_penalty()}")
