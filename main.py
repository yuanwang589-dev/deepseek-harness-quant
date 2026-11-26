# -*- coding: utf-8 -*-
"""DeepSeek HARNESS Quant · 主入口（CLI）

用法：
    python main.py update      数据更新（盘后增量管道）
    python main.py validate    因子评估/验证
    python main.py screen      机会扫描 + Pitch
    python main.py backtest    回测（bt_runner）
    python main.py report      今日信号聚合（日报数据）

说明：本文件只是 CLI 分发器，实际实现在对应模块；参数透传见各模块 --help。
图形界面请用 `python launcher.py`（Deck :8787 / HARNESS :3080）。
"""
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "params.yaml"

CMDS = {
    "update": ["data/daily_pipeline.py"],
    "validate": ["validation/factor_evaluator.py"],
    "screen": ["factors/opportunities/scan.py", "--pitch"],
    "backtest": ["backtest/bt_runner.py"],
    "report": ["report/daily_signal.py"],
}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd not in CMDS:
        print(__doc__)
        return 0
    if not CONFIG_PATH.exists():
        print("缺少 config/params.yaml：请先执行 "
              "`copy config\\params.yaml.example config\\params.yaml` 并填写数据源配置。")
        return 1
    target = BASE_DIR / CMDS[cmd][0]
    if not target.exists():
        print(f"入口不存在：{target}")
        return 1
    print(f"[DSHQuant] {cmd} → {target.relative_to(BASE_DIR)}")
    # ★2026-11-11：透传用户附加参数（原固定 CMDS 参数 → `main.py backtest --strategy tech3` 被忽略）
    return subprocess.run([sys.executable, "-X", "utf8", str(target),
                           *CMDS[cmd][1:], *sys.argv[2:]],
                          cwd=str(BASE_DIR)).returncode


if __name__ == "__main__":
    sys.exit(main())