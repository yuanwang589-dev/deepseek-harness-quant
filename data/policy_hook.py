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
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

OUT = BASE / "output" / "policy_score_latest.json"
_cache = {"ts": 0.0, "data": None}
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
        print(f"  [policy_hook] 计算失败: {str(e)[:60]}")
        return None


def load() -> dict:
    """读政策评分（缓存 12h；无缓存/失败 → {} 不阻断）"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _TTL:
        return _cache["data"]
    if OUT.exists() and now - OUT.stat().st_mtime < _TTL:
        try:
            d = json.loads(OUT.read_text(encoding="utf-8"))
            _cache.update({"ts": now, "data": d})
            return d
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/policy_hook.py:54 load")
    d = _compute()
    if d:
        try:
            OUT.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/policy_hook.py:60 load")
        _cache.update({"ts": now, "data": d})
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
