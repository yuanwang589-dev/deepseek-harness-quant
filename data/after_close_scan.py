# -*- coding: utf-8 -*-
"""data/after_close_scan.py — 盘后机会扫描（2026-08-14 用户需求：pitch 池收盘后及时更新）

★背景：pitch 池原只在 18:30 DailyPipeline 里生成，但 TushareInc 17:30 就拉完当日日线、
  实际 Tushare 盘后数据 ~16:45 即可用 → 收盘 15:00 到 pitch 更新 18:30 有 ~3.5h 空窗。
  本脚本 17:35 跑：因子池评分补跑 → 机会扫描 → Pitch v2 → 科技线 v3，
  让 pitch 池在收盘后 ~2.5h（而非 18:30）更新到当日。

★安全：各步骤幂等（scheduler latest<=done 自动跳过；scan/pitch 覆盖式重写同文件），
  与 18:30 DailyPipeline 可共存（后者补跑会跳过已完成的评分）。
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
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = BASE / "logs" / "after_close_scan.log"


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/after_close_scan.py:28 log")


def run_step(name, cmd, timeout=1800):
    log(f"→ {name}")
    try:
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        tail = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        log(f"  {name} exit={r.returncode} {tail[:120]}")
        if r.returncode != 0 and r.stderr:
            log(f"  stderr: {(r.stderr or '').strip()[:200]}")
    except subprocess.TimeoutExpired:
        log(f"  ⚠ {name} 超时（{timeout}s）")
    except Exception as e:
        log(f"  ⚠ {name} 异常: {str(e)[:120]}")


def main():
    # ★2026-08-14 非交易日跳过：周末（周六/周日）无新数据，跑 scan/因子池评分纯浪费算力，
    #   pitch 保持上一交易日数据即可；周一自动恢复正常。
    import datetime as _dt
    _d = _dt.date.today()
    if _d.weekday() >= 5:
        log(f"非交易日（{_d} 周末）→ 跳过盘后扫描（pitch 保持上一交易日数据）")
        return
    log("=== 盘后机会扫描启动 ===")
    # 1) 因子池评分补跑（当日评分，供 scan ext_signal 消费；幂等）
    _sched = Path(r"data/factorpool/core/scheduler.py")
    if _sched.exists():
        run_step("因子池评分补跑", [PY, "-X", "utf8", str(_sched), "daily"], timeout=2700)
    else:
        log("  ⚠ 因子池 scheduler 不存在 → 跳过（scan 将用旧评分）")
    # 2) 机会扫描 + Pitch + 科技线（核心：pitch 池更新到当日）
    run_step("机会扫描 --pitch", [PY, str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"])
    run_step("Pitch v2 Deck", [PY, str(BASE / "factors" / "opportunities" / "pitch_v2.py")])
    run_step("科技线 Pitch v3", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "tech_pitch_v3.py")])
    # 2.5) ★2026-08-14 跨资产轮动防守信号（a_share_weak + global_rotation，因子池 P0 落地）
    run_step("跨资产轮动信号", [PY, "-X", "utf8", str(BASE / "factors" / "policy" / "global_rotation.py")])
    log("=== 盘后机会扫描完成 ===")


if __name__ == "__main__":
    main()
