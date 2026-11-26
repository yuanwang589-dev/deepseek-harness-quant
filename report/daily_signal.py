# -*- coding: utf-8 -*-
"""report/daily_signal.py — 今日信号聚合（★2026-09-02 P3 实现，7 源合一）

背景：deck/live_api.py:live_holdings 8-25 加了 daily_signal fallback（红绿灯拼凑），
  仅 regime_label/level/cash_ratio 三个字段；缺 composite_level / dims / 来源审计。

输入（7 源，全部走 output/ 已有产物，零网络）：
  1. timing_system.json  — 4 维择时（政策/宏观/情绪/宽度 + score）
  2. traffic_light.json   — 均线金叉/死叉 + hs300
  3. dynamic_regime.json  — 月度 regime 仓位档
  4. market_snapshot_ext_*.json — 风格/宽度/拥挤度
  5. global_rotation.json — 跨资产轮动
  6. opp_pool_*.json      — 机会池（n=2234 量级）
  7. pitch_v2_*.json + tech_pitch_*.json — Pitch 长短线

输出：
  - output/daily_signal.json （固定名，前端读这个）
  - output/daily_signal_{YYYYMMDD_HHMMSS}.json （归档）

集成：deck/live_api.py:live_holdings:498 fallback 改读 daily_signal.json（仍保留红绿灯兜底）
调度：daily_pipeline.py 步骤 2.7 之后（市场快照重建之后）
"""
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def _latest(pattern: str):
    """pattern 相对 BASE 的 glob；返回 Path 或 None"""
    files = sorted(glob.glob(str(BASE / pattern)), key=lambda p: Path(p).stat().st_mtime)
    return Path(files[-1]) if files else None


def _safe_read(p):
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _composite_level(timing_score: float, traffic_state: str) -> str:
    """综合等级：timing 0-100 + traffic 3 档 → 4 档等级"""
    if timing_score >= 60 and traffic_state == "greedy":
        return "买入"
    if timing_score >= 50 and traffic_state in ("greedy", "wait"):
        return "谨慎买入"
    if timing_score >= 40:
        return "观望"
    return "减仓"


def main() -> int:
    now = datetime.now()
    ym = now.strftime("%Y-%m")

    # 7 源聚合
    timing = _safe_read(_latest("output/timing_system*.json")) or {}
    traffic = _safe_read(BASE / "output" / "traffic_light.json") or {}
    regime = _safe_read(BASE / "output" / "dynamic_regime.json") or {}
    snap_p = _latest("data/factorpool/output/market_snapshot_ext_*.json")
    rotation = _safe_read(BASE / "output" / "global_rotation.json") or {}
    # ★2026-11-10 契约修复：机会池/Pitch 产物在 logs/（原写 output/ → 4 源恒空）
    opp_pool = _safe_read(_latest("logs/opp_pool_*.json")) or {}
    pitch_v2 = _safe_read(_latest("logs/pitch_v2_*.json")) or {}
    tech_pitch = _safe_read(_latest("logs/tech_pitch_*.json")) or {}

    # ★2026-11-10 字段修复：timing_system 顶层即 score/level（原读 timing["timing"] → 恒 50）
    timing_score = float(timing.get("score", 50) or 50)
    traffic_state = traffic.get("state", "wait")
    traffic_label = traffic.get("label", "观望")
    regime_cur = regime.get(ym, {}) or {}
    regime_pos = float(regime_cur.get("pos", 0.5))
    regime_cash = round(1 - regime_pos, 2)

    snap_age_h = round((now.timestamp() - snap_p.stat().st_mtime) / 3600, 1) if snap_p else 999.0

    opp_n = opp_pool.get("n")
    if opp_n is None:
        opp_n = len(opp_pool.get("opportunities") or [])
    pitch_v2_picks = pitch_v2.get("pitch", []) or []   # ★字段名是 pitch（原 picks）
    tech_picks = tech_pitch.get("entries", []) or []  # ★字段名是 entries（原 picks）

    out = {
        "ok": True,
        "date": now.strftime("%Y-%m-%d"),
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "report/daily_signal.py",
        "composite_level": _composite_level(timing_score, traffic_state),
        "regime_label": traffic_label,
        "regime_state": traffic_state,
        "regime_cash_ratio": regime_cash,
        "regime_level": regime_cur.get("level", "中性"),
        "dims": {
            "timing": {
                "score": timing_score,
                "level": timing.get("level", "中性"),
            },
            "traffic_light": {
                "state": traffic_state,
                "label": traffic_label,
                "asof": traffic.get("asof"),
            },
            "regime": {
                "label": regime_cur.get("label", "中性"),
                "pos": regime_pos,
                "cash_ratio": regime_cash,
            },
            "snapshot_health": {
                "age_h": snap_age_h,
                "fresh": snap_age_h < 24,
            },
            "rotation": {
                "date": rotation.get("date"),
                "a_share_weak": rotation.get("a_share_weak"),
            },
            "opportunities": {
                "n": opp_n,
                "date": opp_pool.get("date"),
            },
            "pitch": {
                "v2_n": len(pitch_v2_picks),
                "tech_n": len(tech_picks),
            },
        },
        "sources_used": [
            "timing_system", "traffic_light", "dynamic_regime",
            "market_snapshot_ext", "global_rotation", "opp_pool",
            "pitch_v2", "tech_pitch",
        ],
        "note": f"聚合 7 源（timing={timing_score:.1f} + traffic={traffic_state}）",
    }

    # 写
    out_dir = BASE / "output"
    out_dir.mkdir(exist_ok=True)
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    (out_dir / "daily_signal.json").write_text(payload, encoding="utf-8")
    ts = now.strftime("%Y%m%d_%H%M%S")
    (out_dir / f"daily_signal_{ts}.json").write_text(payload, encoding="utf-8")
    print(f"[daily_signal] composite={out['composite_level']} timing={timing_score:.1f} traffic={traffic_state} | {ts}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
