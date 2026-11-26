# -*- coding: utf-8 -*-
"""data/factor_archive_chain.py — F3 因子档案 + C 包产物统一刷新链（总指导 · 2026-08-10）

★任务包 F-F3 接入：把外包交付的 5 个可重跑脚本串成一条每日链，由计划任务
  DSHQuant-FactorArchive（17:40，避开 17:35 外包因子池评分）调用。

链步骤（顺序固定，互不依赖，单步失败不阻塞后续）：
  1. report/gen_factor_archive.py      → output/因子档案.json（~2-3min，97 个月×5 因子）
  2. report/factor_crowding.py         → report/factor_crowding.json（~1min）
  3. report/ep_factor_icir.py          → report/ep_icir_full.json（~1min）
  4. report/fundamental_factors.py     → report/fundamental_factor_report.json（~1min）
  5. report/apply_factor_verdict.py    → logs/技术因子反向决策报告.md（秒级）

用法：
  python data/factor_archive_chain.py            # 全链
  python data/factor_archive_chain.py --skip-gen # 跳过因子档案（C 包产物单独刷）
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

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception as _dsh_exc:
    _dsh_swallow(_dsh_exc, "data/factor_archive_chain.py:26 <module>")

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = BASE / "logs" / "factor_archive_chain.log"

STEPS = [
    ("因子档案生成器(F3)", [str(BASE / "report" / "gen_factor_archive.py")]),
    ("因子拥挤度(C3)",      [str(BASE / "report" / "factor_crowding.py")]),
    ("EP因子ICIR(C4)",      [str(BASE / "report" / "ep_factor_icir.py")]),
    ("基本面因子(C4)",      [str(BASE / "report" / "fundamental_factors.py")]),
    ("技术因子裁决(C2)",    [str(BASE / "report" / "apply_factor_verdict.py")]),
]


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/factor_archive_chain.py:55 log")


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-gen", action="store_true", help="跳过因子档案生成（仅 C 包产物）")
    args = ap.parse_args()

    log(f"===== F3 因子档案链启动（python={PY}）=====")
    ok, fail = 0, 0
    for name, cmd in STEPS:
        if args.skip_gen and "gen_factor_archive" in cmd[0]:
            log(f"  ⏭ 跳过 {name}")
            continue
        log(f"  ▶ {name}：{cmd[0]}")
        t0 = datetime.now()
        try:
            # ★2026-08-10 修复：子进程强制 UTF-8（计划任务环境 GBK 编码，外包脚本打印 emoji ⚠️ 会崩）
            r = subprocess.run([PY, "-X", "utf8", *cmd], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1500,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            dt = (datetime.now() - t0).total_seconds()
            if r.returncode == 0:
                tail = (r.stdout or "").strip().splitlines()
                log(f"    ✅ {name} 完成（{dt:.0f}s）" + (f"｜{tail[-1][:120]}" if tail else ""))
                ok += 1
            else:
                err = (r.stderr or "").strip().splitlines()
                log(f"    ❌ {name} 退出码 {r.returncode}（{dt:.0f}s）｜{err[-1][:200] if err else '无错误输出'}")
                fail += 1
        except subprocess.TimeoutExpired:
            log(f"    ❌ {name} 超时（>25min），终止")
            fail += 1
        except Exception:
            log(f"    ❌ {name} 异常：{traceback.format_exc()[-300:]}")
            fail += 1

    # 验证因子档案当日时间戳（★glob 最新时间戳版——固定名已被写保护锁，读它会显示旧时间误导）
    import glob as _gl
    _afs = sorted(_gl.glob(str(BASE / "output" / "因子档案_2*.json")), key=os.path.getmtime)
    try:
        import json
        fa = _afs[-1] if _afs else (BASE / "output" / "因子档案.json")
        meta = json.loads(Path(fa).read_text(encoding="utf-8")).get("_meta", {})
        log(f"  🕐 因子档案 generated_at = {meta.get('generated_at')}（{Path(fa).name}）")
    except Exception:
        log("  ⚠ 因子档案读取失败")

    log(f"===== 完成：成功 {ok} / 失败 {fail} =====")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
