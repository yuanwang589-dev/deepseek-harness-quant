# -*- coding: utf-8 -*-
"""data/factorpool/build_pool.py — 因子池清单落盘（★2026-09-02 接 builtin_purify 到 daily_pipeline）

背景：factors/pool/builtin_purify.py 已实现 build_manifest() / build_health()（外包缺失兜底），
  但只在 deck.live_api.live_factor_dash 渲染时即时算 → 落盘后供前端快照 + 离线分析。

输出：
  - data/factorpool/output/factor_manifest_{YYYYMMDD_HHMMSS}.json
  - data/factorpool/output/health/health_{YYYYMMDD_HHMMSS}.csv
  - 固定名 manifest.json / health.csv（软链 → 最新一份，供前端 GET /api/factorpool/* 用）

调度：daily_pipeline.py 步骤 2.7 之后（市场快照重建之后）
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))
from factors.pool.builtin_purify import build_manifest, build_health  # noqa: E402

OUT = BASE / "data" / "factorpool" / "output"
(OUT / "health").mkdir(parents=True, exist_ok=True)


def main() -> int:
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    m = build_manifest()
    h = build_health()
    # 1) manifest（时间戳版本 + 固定名）
    mp = OUT / f"factor_manifest_{ts}.json"
    mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    # 2) health
    hp = OUT / "health" / f"health_{ts}.csv"
    if h.get("rows"):
        with hp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(h["rows"][0].keys()))
            w.writeheader()
            w.writerows(h["rows"])
        # 固定名拷贝
        (OUT / "health" / "health.csv").write_text(hp.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[factor_pool] manifest={mp.name} n={m.get('n', 0)} | health={hp.name} n={h.get('n', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
