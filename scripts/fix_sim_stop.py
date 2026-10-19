# -*- coding: utf-8 -*-
"""scripts/fix_sim_stop.py — 一次性修正当前 1w 模拟仓：补真实 otype + 显式止损位

- otype 改为真实类型 quality_gap（从机会池反查确认 3 只均为质量修复）
- 写 stop_plan 保值覆盖：-10% 硬止损 + -15% 回撤线 + 26 周时间止损
- 写 stop(价)/target(价)/take_profit 字典，供观察池直接显示
- 同时修正 logs/portfolio.json 与最新 logs/portfolio_*.json（两个读取方都用时间戳文件）

幂等：仅改 holding 状态且有 entry_price 的持仓。
"""
import glob
import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TS = 0.10   # 硬止损 -10%
DD = 0.15   # 最大回撤 -15%
TP = 0.70   # 止盈 +70%
PULL = 0.10
TW = 26


def patch(d: dict) -> dict:
    for p in d.get("positions", []):
        if p.get("status") != "holding":
            continue
        ep = p.get("entry_price")
        if not ep or ep <= 0:
            continue
        p["otype"] = "quality_gap"
        p["stop_plan"] = {
            "stop_loss_pct": TS, "time_stop_weeks": TW,
            "max_drawdown_pct": DD, "trailing_ma": None,
            "desc": "保值覆盖：质量修复股原生无硬止损（实证硬止损打掉防守股），"
                    "按用户诉求加 -10% 硬止损 + -15% 回撤线，保留 26 周时间止损",
        }
        p["stop"] = round(ep * (1 - TS), 2)
        p["target"] = round(ep * (1 + TP), 2)
        p["take_profit"] = {"target_pct": TP, "pullback_pct": PULL, "time_weeks": TW}
    return d


def main():
    # 固定名
    fp = BASE / "logs" / "portfolio.json"
    if fp.exists():
        d = json.loads(fp.read_text(encoding="utf-8"))
        d = patch(d)
        d["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        d["source"] = (d.get("source") or "") + " | 已补 otype=quality_gap + 止损位(-10%/回撤-15%)"
        fp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"patched 固定名 {fp.name}")

    # 最新时间戳文件（_load / 检查引擎都读它）
    fs = sorted(glob.glob(str(BASE / "logs" / "portfolio_*.json")),
                key=lambda x: Path(x).stat().st_mtime)
    if fs:
        latest = fs[-1]
        d2 = json.loads(Path(latest).read_text(encoding="utf-8"))
        d2 = patch(d2)
        Path(latest).write_text(json.dumps(d2, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"patched 最新时间戳 {Path(latest).name}")
    else:
        print("无 portfolio_*.json，跳过时间戳文件")


if __name__ == "__main__":
    main()
