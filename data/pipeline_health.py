# -*- coding: utf-8 -*-
"""data/pipeline_health.py — 管道健康探针（2026-08-12 十轮第9轮 #175）

★背景：2026-08-12 18:30 每日信号 exit=143 中断 2 小时无人知（alerts 无管道告警），
  用户手动排查才发现。本探针把"管道最新成功时间"落盘 → alerts API 消费显示，
  超过阈值（工作日 26h / 周末 50h）→ high 告警"管道中断需人工干预"。

产出：logs/pipeline_health_{ts}.json + 固定名（写保护免疫）→ live_alerts 读最新。
用法：
  python data/pipeline_health.py            # 探测并落盘
  python data/pipeline_health.py --alerts   # 直接输出告警列表（供 API 调试）
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
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "output"
LOG_DIR = BASE / "logs"

# 关键产出物：名称 → (目录, glob 模式)（找最新 mtime）
# ★2026-08-12 十轮#175：pitch_v2 在 logs/，opp_pool 已并入 scan 产出（用 output/scan_*.json 替代）
PRODUCTS = {
    "daily_signal": (OUT_DIR, "daily_signal_*.json"),   # 每日信号（18:30 管道核心产物）
    "pool_layers": (OUT_DIR, "pool_layers_*.json"),     # 机会池（scan 三层池产物）
    "pitch_v2": (LOG_DIR, "pitch_v2_*.json"),           # Pitch 候选（pitch_v2 产物）
    "timing": (OUT_DIR, "timing_system_*.json"),        # 择时
}


def probe() -> dict:
    """探测各产物最新 mtime → 状态 + 告警"""
    now = datetime.now()
    results = {}
    alerts = []
    for name, (_dir, pat) in PRODUCTS.items():
        fs = sorted(glob.glob(str(_dir / pat)), key=os.path.getmtime)
        if not fs:
            results[name] = {"ok": False, "last": None, "age_h": None, "file": None}
            alerts.append({"level": "high", "cat": "管道", "name": name,
                           "msg": f"{name} 从未产出（无任何历史文件）"})
            continue
        f = Path(fs[-1])
        mt = datetime.fromtimestamp(f.stat().st_mtime)
        age_h = (now - mt).total_seconds() / 3600
        # 阈值：工作日 26h（每日一次 18:30），周末 50h
        is_weekend = now.weekday() >= 5
        thresh = 50 if is_weekend else 26
        ok = age_h <= thresh
        results[name] = {"ok": ok, "last": mt.strftime("%Y-%m-%d %H:%M"),
                         "age_h": round(age_h, 1), "file": f.name}
        if not ok:
            alerts.append({"level": "high", "cat": "管道", "name": name,
                           "msg": f"{name} 中断 {age_h:.0f}h（最新 {mt.strftime('%m-%d %H:%M')}，阈值 {thresh}h）→ 需人工检查管道"})
    return {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "results": results, "alerts": alerts}


def main():
    ap = argparse.ArgumentParser(description="管道健康探针")
    ap.add_argument("--alerts", action="store_true", help="仅输出告警（调试）")
    args = ap.parse_args()
    data = probe()
    if args.alerts:
        print(json.dumps(data["alerts"], ensure_ascii=False, indent=1))
        return
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = LOG_DIR / f"pipeline_health_{ts}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        (LOG_DIR / "pipeline_health.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/pipeline_health.py:79 main")
    print("✅ 管道健康已落盘: {}".format(p.name))
    for n, r in data["results"].items():
        print("  {}: {} | age {}h".format(n, "✅" if r["ok"] else "❌", r["age_h"]))
    for a in data["alerts"]:
        print("  ⚠️ {}".format(a["msg"]))


if __name__ == "__main__":
    main()
