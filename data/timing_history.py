# -*- coding: utf-8 -*-
"""data/timing_history.py — 择时历史归档器（2026-08-12 十轮第6轮 #172）

★背景：timing_system_*.json 是单日多次评估快照（文件数少，score_hist 只有当日 5 样本），
  择时面板 spark 需要跨日长序列才能看到"择时走势"。本工具按日聚合 → 跨日历史。

逻辑：
  1. 扫描 output/timing_system_*.json（时间戳 glob，mtime 排序）
  2. 按 date 分组，每组取最新一次评估的 score/level
  3. 追加到 logs/timing_history_{ts}.json（时间戳，写保护免疫）+ 固定名尝试
  4. 输出跨日序列 score_series/level_series（供 API/面板 spark）

用法：
  python data/timing_history.py            # 归档 + 输出序列
  python data/timing_history.py --print    # 打印序列（不写）
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
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "output"
LOG_DIR = BASE / "logs"


def build_series() -> dict:
    """扫描 timing_system 文件 → 跨日序列（date → score/level）"""
    files = sorted(glob.glob(str(OUT_DIR / "timing_system_*.json")),
                   key=os.path.getmtime)
    by_day = {}
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        dt = d.get("date") or datetime.fromtimestamp(Path(f).stat().st_mtime).strftime("%Y-%m-%d")
        # 同一天保留最新（mtime 排序后自然覆盖）
        by_day[dt] = {
            "score": round(float(d.get("score", 0)), 1),
            "level": d.get("level", ""),
            "ts": d.get("ts", ""),
            "file": Path(f).name,
        }
    days = sorted(by_day)
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_days": len(days),
        "score_series": [by_day[d]["score"] for d in days],
        "level_series": [by_day[d]["level"] for d in days],
        "days": days,
        "detail": by_day,
    }


def main():
    ap = argparse.ArgumentParser(description="择时历史归档")
    ap.add_argument("--print", action="store_true", help="仅打印不写")
    ap.add_argument("--no-dedupe", action="store_true", help="不按天去重（保留全部评估）")
    args = ap.parse_args()
    data = build_series()
    if args.print:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = LOG_DIR / f"timing_history_{ts}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        (LOG_DIR / "timing_history.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/timing_history.py:77 main")
    print("✅ 择时历史已归档: {}（{} 个交易日）".format(p.name, data["n_days"]))
    print("   score 序列:", data["score_series"])


if __name__ == "__main__":
    main()
