# -*- coding: utf-8 -*-
"""本地自动开发驱动（dev_auto）— 用户指定的"脚本自动给自己发指令实现连续写"

背景：WorkBuddy automation 的定时触发在当前环境不可靠（用户实测需手动点），
且无公开 CLI 可从外部唤醒会话。本脚本用 Windows 计划任务调度，实现：

  1) 确定性工作全自动：数据更新/质量检查/进度快照（不需要 AI 的部分脚本自己跑）
  2) "给自己发指令"：把"下一步需要 AI 处理的任务"以标准格式写入 待办队列.md
     —— WorkBuddy 会话（automation 触发 或 用户打开会话）读取队列即续写，
        形成"脚本驱动 → 会话续写 → 脚本再驱动"的连续闭环
  3) 夜间自动运行：计划任务注册为每天 22:00 起每 4 小时（22/02/06/10/14/18）
  4) ★熔断机制（fail-safe）：出现停止条件时自动断开，防止死循环/失控
     - 手动熔断：python dev_auto.py --stop（或创建 STOP.md）
     - 自动熔断：连续 3 轮进度无变化 / 单轮异常 ≥5 次 / 每轮超时 60 分钟
     - 熔断后自动禁用计划任务（LWQuant-DevDriver /Disable），彻底断开
     - 恢复：python dev_auto.py --reset
  5) ★学习笔记监控：用户会持续往项目根目录《课程笔记_*》文件夹放学习笔记
     - 每轮自动扫描，检测"新增/改动"的笔记文件（指纹：大小+修改时间）
     - 发现新笔记 → 自动追加到待办队列.md（【笔记吸收】任务）
     - WorkBuddy 会话读到后：读笔记 → 评估对 deepseek-harness-quant 的价值 → 落地
     - 首次运行只建立基线（不排队），只对基线之后的更新排队

用法：
  python dev_auto.py --sched     计划任务模式：熔断检查+更新+快照+笔记监控+生成 AI 待办
  python dev_auto.py --notes     仅笔记监控（扫描学习文件夹，新笔记入待办队列）
  python dev_auto.py --update    仅确定性数据更新（M1 管道就绪后启用）
  python dev_auto.py --bridge    桥接模式（会话调用）：输出待办队列给 AI 执行
  python dev_auto.py --status    查看进度/队列/熔断状态
  python dev_auto.py --stop      熔断：停止循环（禁用计划任务 + 写 STOP.md）
  python dev_auto.py --reset     恢复：清除熔断（启用计划任务）
"""
import argparse

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception:
    pass

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from data.io_utils import read_json, read_text

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent                       # 项目根目录（桌面DeepSeek HARNESS Quant）
PROGRESS = BASE / "进度.md"
QUEUE = BASE / "待办队列.md"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ---- 学习笔记监控配置 ----
NOTES_PATTERNS = ["学习笔记", "课程笔记_*"]   # 项目根目录下的学习文件夹（新结构：学习笔记/，含原文/子目录）
NOTES_FP_FILE = LOG_DIR / "notes_fingerprint.json"   # 笔记指纹（检测新增/改动）
NOTES_LOG = LOG_DIR / "notes_absorb.log"             # 笔记吸收记录
NOTES_EXT = (".md", ".py", ".txt")

# ---- 熔断配置 ----
STOP_FILE = BASE / "STOP.md"                 # 手动熔断标记文件
STATE_FILE = LOG_DIR / "breaker_state.json"  # 熔断状态（自动熔断记录）
# ★2026-10-18 修虚假绿灯：原名 "LWQuant-DevDriver" 是历史前缀，实际注册为 "DSHQuant-DevDriver"，
#   两套名字互不相认 → 查询必然失败 → 旧代码 `except: return True` 让界面显示「🟢 启用中」，
#   而该任务从未注册。现改为「后缀解析」（data/task_registry.py），前缀无关，换名也不会再失联。
TASK_SUFFIX = "DevDriver"
MAX_STALE_ROUNDS = 3                          # 连续 N 轮进度无变化 → 自动熔断
MAX_ERRORS_PER_RUN = 5                        # 单轮异常 ≥N 次 → 熔断
RUN_TIMEOUT_MIN = 60                          # 每轮超时（分钟）


def _task_name() -> str | None:
    """解析真实注册的任务名（DSHQuant-DevDriver / LWQuant-DevDriver 都能命中）。"""
    try:
        from data.task_registry import resolve
        return resolve(TASK_SUFFIX)
    except Exception:
        return None


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_DIR / "dev_auto.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------- ★熔断机制（fail-safe） ----------------

def _read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _task_enabled() -> bool | None:
    """计划任务是否「已注册且未禁用」。

    ★2026-10-18 修虚假绿灯：返回三态，不再用 True 兜底。
      True  = 已注册且未禁用
      False = 已注册但被禁用
      None  = 任务不存在 / 查询失败 → **无法判定**
    旧实现 `except: return True` 把「查不到」当成「启用中」，导致界面显示 🟢
    而任务实际从未注册（本机 10-18 前的真实状况）。
    """
    try:
        from data.task_registry import is_healthy
        return is_healthy(TASK_SUFFIX)
    except Exception as e:
        log(f"计划任务状态查询失败（不假定为启用）: {type(e).__name__}: {e}")
        return None


def _set_task(enabled: bool) -> bool:
    """启用/禁用计划任务（任务名走后缀解析，前缀无关）"""
    flag = "/Enable" if enabled else "/Disable"
    name = _task_name()
    if not name:
        log(f"计划任务 {flag} 失败：未找到 *-{TASK_SUFFIX} 任务（未注册？）")
        return False
    try:
        r = subprocess.run(["schtasks", "/Change", "/TN", name, flag],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception as e:
        log(f"计划任务 {flag} 失败: {e}")
        return False


def breaker_active() -> bool:
    """是否处于熔断状态：STOP.md 存在 或 状态文件标记熔断"""
    if STOP_FILE.exists():
        return True
    st = _read_state()
    return bool(st.get("breaker"))


def _set_breaker(reason: str, auto: bool):
    """触发熔断：写 STOP.md + 状态 + 禁用计划任务（彻底断开）"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STOP_FILE.write_text(
        f"# 熔断标记（STOP）\n\n- 触发时间：{ts}\n- 原因：{reason}\n"
        f"- 来源：{'自动' if auto else '手动'}\n\n> 删除本文件或运行 `python dev_auto.py --reset` 恢复。\n",
        encoding="utf-8")
    _write_state({"breaker": True, "reason": reason, "auto": auto, "at": ts})
    _st = _task_enabled()
    if _st is True:
        ok = _set_task(False)
        log(f"★熔断触发[{reason}]：计划任务{'已禁用' if ok else '禁用失败，请手动处理'}")
    elif _st is False:
        log(f"★熔断触发[{reason}]：计划任务已处于禁用状态")
    else:
        # 任务不存在或查不了：熔断标记照写（本进程不再自驱），但要诚实说明调度层没断开
        log(f"★熔断触发[{reason}]：⚠ 计划任务状态未知（未注册/查询失败）→ 调度层未能断开，请手动核查")
    log(f"熔断原因已写入 STOP.md（恢复方式: python dev_auto.py --reset）")


def clear_breaker():
    """恢复：清除熔断 + 启用计划任务"""
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    _set_task(True)
    log("★熔断已清除，计划任务已启用，循环恢复")


def _progress_fingerprint() -> str:
    if not PROGRESS.exists():
        return ""
    return hashlib.md5(PROGRESS.read_text(encoding="utf-8").encode()).hexdigest()


def _check_no_progress():
    """自动熔断-1：连续 MAX_STALE_ROUNDS 轮进度无变化（死循环检测）"""
    st = _read_state()
    fp = _progress_fingerprint()
    last_fp = st.get("last_progress_fp", "")
    stale = st.get("stale_rounds", 0)
    if fp and fp == last_fp:
        stale += 1
    else:
        stale = 0
    st["last_progress_fp"] = fp
    st["stale_rounds"] = stale
    _write_state(st)
    if stale >= MAX_STALE_ROUNDS:
        _set_breaker(f"连续 {stale} 轮进度无变化（疑似死循环）", auto=True)
        return True
    if stale >= 1:
        log(f"[预警] 进度连续 {stale} 轮无变化（{MAX_STALE_ROUNDS} 轮触发熔断）")
    return False


def _check_errors(errors: int):
    """自动熔断-2：单轮异常次数超限"""
    if errors >= MAX_ERRORS_PER_RUN:
        _set_breaker(f"单轮异常 {errors} 次（阈值 {MAX_ERRORS_PER_RUN}）", auto=True)
        return True
    return False


# ---------------- 进度与待办队列 ----------------

def read_tail(path: Path, n: int = 15) -> str:
    if not path.exists():
        return "(文件不存在)"
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[-n:])


def push_todo(tasks: list):
    """把 AI 待办任务写入队列（★合并模式：保留已有未完成项，去重追加，不覆盖会话维护的任务）
    ★2026-08-13 #230 修复：原只保留 `- [ ]` 行——`> ✅` 推进记录与 `- [x]` 完成项在每次链合并时
      被清空（02:11 已发生：25 条推进记录丢失）→ 保留全部非待办行（`> ` 注释/`- [x]`/说明行）"""
    existing = []
    keep_lines = []
    if QUEUE.exists():
        for line in QUEUE.read_text(encoding="utf-8").splitlines():
            if line.startswith("- [ ]"):
                existing.append(line[6:].strip())
            elif line.strip() and not line.startswith("# AI 待办队列"):
                keep_lines.append(line)   # ★保留 `> ✅` 推进记录 / `- [x]` 完成项 / 说明行
    merged, seen = [], set()
    for t in existing + tasks:
        if t not in seen:
            merged.append(t)
            seen.add(t)
    with open(QUEUE, "w", encoding="utf-8") as f:
        f.write(f"# AI 待办队列（dev_auto 合并更新 {datetime.now():%Y-%m-%d %H:%M}）\n\n"
                f"> WorkBuddy 会话读取本队列即续写开发。完成一项勾选一项。\n\n")
        if keep_lines:
            f.write("\n".join(keep_lines) + "\n\n")
        f.write("\n".join(f"- [ ] {t}" for t in merged) + "\n")
    log(f"已合并待办队列: 保留 {len(existing)} 项 + 新增 {len(merged) - len(existing)} 项 → 待办队列.md"
        f"（保留注释 {len(keep_lines)} 行）")


def append_todo(task: str) -> bool:
    """追加单个待办任务（不覆盖已有队列，避免丢会话手工更新）"""
    if QUEUE.exists():
        content = QUEUE.read_text(encoding="utf-8")
        if task in content:
            return False  # 已存在，不重复排队
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(f"- [ ] {task}\n")
    else:
        with open(QUEUE, "w", encoding="utf-8") as f:
            f.write(f"# AI 待办队列（dev_auto 自动生成 {datetime.now():%Y-%m-%d %H:%M}）\n\n"
                    f"> WorkBuddy 会话读取本队列即续写开发。完成一项勾选一项。\n\n"
                    f"- [ ] {task}\n")
    log(f"已追加待办: {task[:60]}...")
    return True


# ---------------- 学习笔记监控（用户持续放笔记 → 自动入队吸收） ----------------

def _notes_fingerprint() -> dict:
    """计算学习文件夹全部文件的指纹 {相对路径: "大小_修改时间"}"""
    cur = {}
    for pat in NOTES_PATTERNS:
        for d in sorted(PROJECT.glob(pat)):
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in NOTES_EXT:
                    try:
                        st = f.stat()
                        cur[str(f.relative_to(PROJECT))] = f"{st.st_size}_{int(st.st_mtime)}"
                    except Exception:
                        pass
    return cur


def scan_notes() -> list:
    """扫描学习文件夹，返回新增/改动的笔记相对路径列表。

    首次运行（无指纹文件）只建立基线，不返回任何文件——
    之后每次对比基线，检测新增或修改的笔记，交给会话吸收。
    """
    cur = _notes_fingerprint()
    prev = {}
    if NOTES_FP_FILE.exists():
        try:
            prev = json.loads(NOTES_FP_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    if not prev:  # 首次：只建基线
        NOTES_FP_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"笔记监控: 首次建立基线（{len(cur)} 个文件），后续检测更新")
        return []

    new_files = [rel for rel, fp in cur.items() if prev.get(rel) != fp]
    NOTES_FP_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

    removed = [rel for rel in prev if rel not in cur]
    if removed:
        log(f"笔记监控: 移除 {len(removed)} 个文件（{removed[:3]}...）")
    return new_files


def run_notes_check():
    """笔记监控主流程：扫描 → 新笔记入待办队列 + 记日志"""
    log("== 笔记监控 ==")
    try:
        new = scan_notes()
        if not new:
            log("笔记监控: 无新增/改动笔记")
            return
        desc = "；".join(new[:5]) + ("；..." if len(new) > 5 else "")
        task = (f"【笔记吸收】发现 {len(new)} 个学习笔记更新（{desc}）→ "
                f"读项目根目录《课程笔记_*》文件夹对应笔记，评估对 deepseek-harness-quant 的价值："
                f"有用的落地（更新 params.yaml/代码/主文档/待办），无用的忽略并说明理由")
        ok = append_todo(task)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(NOTES_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {'入队' if ok else '已存在'} {len(new)} 个: {desc}\n")
        log(f"笔记监控: 发现 {len(new)} 个更新 → {'已入待办队列' if ok else '队列中已存在'}")
    except Exception as e:
        log(f"笔记监控异常: {e}")


# ---------------- M3 自动触发（M2 完成后自动跑因子验证） ----------------

M2_LOCK = BASE / "data" / "logs" / "bulk_load.lock"     # 锁文件存在 = 下载实例在跑
BULK_LOG = BASE / "logs" / "bulk_load.log"
M3_SCRIPT = BASE / "validation" / "m3_validate.py"
M3_REPORT = BASE / "output" / "因子有效性报告.md"
M3_MARKER = BASE / "logs" / "m3_done.marker"             # 防重复触发标记


def _m2_finished() -> bool:
    """M2 是否已完成：日志【最后一行】为"批量下载完成" 且 无下载实例在跑。

    注意：不能用"全文包含"判断——中途的小样本测试（--limit 20）也会写完成标记，
    会误判全量已完成。只有日志末尾是完成标记才算本轮全量收尾。
    ★#400 锁文件残留修复：锁文件存在 ≠ 实例在跑——锁里是 PID，PID 已死=陈旧残留
    （bulk_loader 的 acquire 语义就是"PID 死=锁失效"，此处不能只看文件存在）。
    """
    if M2_LOCK.exists():
        try:
            pid_str = M2_LOCK.read_text(encoding="utf-8").strip()
            if pid_str.isdigit() and _pid_alive(int(pid_str)):
                return False                             # 锁里 PID 真存活 → 实例在跑
            # PID 已死/非数字 → 陈旧残留锁，视为无实例在跑，继续判断日志
        except Exception:
            pass
    if not BULK_LOG.exists():
        return False
    lines = [l for l in BULK_LOG.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    return bool(lines and "== 批量下载完成 ==" in lines[-1])


def _pid_alive(pid: int) -> bool:
    """检查 PID 对应进程是否存活（Windows tasklist 查询，与 bulk_loader._pid_alive 一致）"""
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
            capture_output=True, timeout=15)
        out = r.stdout.decode("gbk", errors="ignore")
        return str(pid) in out
    except Exception:
        return False  # 查询失败时保守认为不存活（允许继续）


def run_m3_trigger():
    """M2 完成后自动触发 M3 因子验证（幂等：有标记则跳过）"""
    log("== M3 触发检查 ==")
    if not _m2_finished():
        log("M3 触发: M2 未完成（无完成标记或下载实例在跑），本轮不触发")
        return
    if M3_MARKER.exists():
        log(f"M3 触发: 已完成过（标记 {M3_MARKER.name} 存在），跳过")
        return
    log("★M2 已完成 → 自动触发 M3 因子验证...")
    import subprocess
    try:
        # 参数构造：报告未生成时用快速模式（抽样 200 只验证逻辑），已生成则全量
        cmd = [sys.executable, str(M3_SCRIPT)]
        if not M3_REPORT.exists():
            cmd.append("--quick")
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                           timeout=1800, encoding="utf-8", errors="replace")
        out = (r.stdout or "")[-2000:]
        log(f"M3 触发结果: exit={r.returncode}\n{out}")
        if r.returncode == 0:
            M3_MARKER.write_text(
                f"M3 因子验证完成 {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"报告: {M3_REPORT}\n", encoding="utf-8")
            log("★M3 因子验证完成，报告已生成 → output/因子有效性报告.md")
        else:
            log(f"M3 运行失败 exit={r.returncode}（下次自动重试，不写标记）")
    except Exception as e:
        log(f"M3 触发异常: {e}")


# ---------------- 确定性更新（M1 管道就绪后启用） ----------------

def run_update():
    """确定性数据更新：数据审计（风控前置）+ 因子池巡检（挖→抓→测）"""
    log("== run_update ==")
    # 0) ★Deck 守护（2026-08-10：桌面门户自愈，挂了自动拉起）
    try:
        import subprocess as _sp
        _rd = _sp.run([sys.executable, "-X", "utf8", str(BASE / "deck" / "ensure_deck.py")],
                      capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        log(f"Deck 守护: {(_rd.stdout or '').strip()}")
    except Exception as _e:
        log(f"Deck 守护失败: {_e}")
    # 1) 数据审计（风控前置闸门）
    try:
        from risk.data_audit import DataAuditor, _load_config
        auditor = DataAuditor(_load_config())
        r = auditor.run(quick=True)
        # ★F2 断链修复（2026-08-10）：run 后必须 save_report，否则 report/data_audit_report.json 永远旧
        try:
            auditor.save_report(r)
        except Exception as _se:
            log(f"审计报告保存失败: {_se}")
        log(f"数据审计(quick): 健康度 {r['health']}/100 "
            f"(PASS {r['n_pass']}/WARN {r['n_warn']}/FAIL {r['n_fail']}) "
            f"[{r['elapsed_sec']}s]")
        if r["fails"]:
            log(f"★数据审计 FAIL: {r['fails']} → 数据不可信，修复清单见待办队列（F-1/F-2）")
        else:
            log("数据审计通过 → 数据可信，可进入策略/回测")
    except Exception as e:
        log(f"数据审计失败: {e}")
    # 2) 因子池巡检（抓数据 → 评估候选/巡检 active → 报告）
    try:
        from factors.pool.lifecycle import fetch_policy_data, evaluate_pool, write_report
        from factors.pool.registry import FactorRegistry
        n = fetch_policy_data()
        log(f"因子池: 政策数据刷新 {n} 条")
        reg = FactorRegistry()
        results = evaluate_pool(reg)
        actives = [x for x in reg.list_factors(status="active")]
        log(f"因子池评估 {len(results)} 个，活跃因子 {len(actives)} 个: {[f['name'] for f in actives]}")
        write_report(reg)
    except Exception as e:
        log(f"因子池巡检失败: {e}")
    # 3) 每日信号（v3 口径）+ 看板（数据驱动版）
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "report" / "daily_signal.py")],
            capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        out = (r.stdout or "")[-800:]
        log(f"每日信号(v3): exit={r.returncode} {out.strip()[:200]}")
        r2 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "report" / "dashboard.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        log(f"看板更新: exit={r2.returncode} {((r2.stdout or '')[-200:]).strip()}")
    except Exception as e:
        log(f"每日信号/看板失败: {e}")
    # 4) Tushare 精确版历史市值增量（--one：每轮尝试拉 1 个月，72 轮 ≈ 12 天补齐 hist_mv_ts，
    #    与后台连续版互不冲突；反推版 hist_mv 已用于 PIT 验收）
    try:
        import subprocess
        r3 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "fetcher_hist_mv.py"), "--one"],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        out3 = (r3.stdout or "")[-300:].strip()
        log(f"Tushare 市值增量: {out3.splitlines()[-1] if out3 else '无输出'}")
    except Exception as e:
        log(f"Tushare 市值增量失败: {e}")
    # 5) 模拟盘信号跟踪（S1：每日 v3 信号 → 模拟净值累积，幂等）
    try:
        import subprocess
        r4 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "strategy" / "paper_tracker.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        out4 = (r4.stdout or "")[-200:].strip()
        log(f"模拟盘跟踪: {out4.splitlines()[-1] if out4 else '无输出'}")
    except Exception as e:
        log(f"模拟盘跟踪失败: {e}")
    # 5.1) ★#417 模拟盘月度双轨回测（sim_tracks——config 轨 vs pitch 轨；paper_tracker 是每日信号，
    #   sim_tracks 是月度回测，之前漏入调度导致 sim_tracks.json 停 08-09/08-10）。每周一跑一次即可（月度粒度）
    try:
        if datetime.now().weekday() == 0:
            import subprocess as _sp2
            _r5b = _sp2.run(
                [sys.executable, "-X", "utf8", str(BASE / "strategy" / "sim_tracks.py")],
                capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
            _o5b = (_r5b.stdout or "")[-120:].strip()
            log(f"模拟盘双轨: {_o5b.splitlines()[-1] if _o5b else '无输出'}")
    except Exception as e:
        log(f"模拟盘双轨失败: {e}")
    # 6) 指数日线增量（★Regime 择时数据源：沪深300 每日收盘更新 → 择时信号"日频自动更新"）
    try:
        import subprocess
        r5 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "fetch_index_daily.py")],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        out5 = (r5.stdout or "")[-300:].strip()
        log(f"指数增量: {out5.splitlines()[-1] if out5 else '无输出'}")
    except Exception as e:
        log(f"指数增量失败: {e}")
    # 7) 优中选优排名 + ★三层池（观察池 Top100 → 技术确认候选 → 决策池，低频纪律：大观察池小决策池）
    try:
        import subprocess
        r6 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "strategy" / "ranking_v2.py"), "--n", "100"],
            capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        out6 = (r6.stdout or "")[-300:].strip()
        log(f"精选排名: {out6.splitlines()[0] if out6 else '无输出'}")
        # 三层池（读 daily_signal 的现金比例做防守档判断）
        import json as _json
        sig = {}
        sp = BASE / "output" / "daily_signal.json"
        if sp.exists():
            sig = _json.loads(sp.read_text(encoding="utf-8"))
        r6b = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "strategy" / "pool_layers.py"),
             "--n", "100", "--capital", str(sig.get("capital", 200000)),
             "--regime-cash", str(sig.get("regime_cash_ratio", 0.3))],
            capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        out6b = (r6b.stdout or "")[-300:].strip()
        log(f"三层池: {out6b.splitlines()[0] if out6b else '无输出'}")
    except Exception as e:
        log(f"精选排名/三层池失败: {e}")
    # 7.5) ★底座池 + Pitch（极低频：底座池每日刷新便宜；pitch 仅每月或 30 天以上未生成时重跑，
    #     防守档（现金≥50%）自动暂停；pitch 只推荐，用户审批后才买入）
    try:
        import subprocess
        r6c = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "strategy" / "base_pool.py")],
            capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        out6c = (r6c.stdout or "")[-200:].strip()
        log(f"底座池: {out6c.splitlines()[1] if len(out6c.splitlines()) > 1 else out6c}")
        # pitch：30 天以上未生成才重跑（pitch 频率极低）
        import json as _json2
        pp = BASE / "output" / "pitch_report.json"
        stale = True
        if pp.exists():
            try:
                gen = _json2.loads(pp.read_text(encoding="utf-8")).get("generated_at", "")
                if gen and gen[:10] >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"):
                    stale = False
            except Exception:
                pass
        if stale:
            r6d = subprocess.run(
                [sys.executable, "-X", "utf8", str(BASE / "strategy" / "pitch.py"), "--n", "5"],
                capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
            out6d = (r6d.stdout or "")[-200:].strip()
            log(f"Pitch: {out6d.splitlines()[0] if out6d else '无输出'}")
        else:
            log("Pitch: 30 天内已生成，跳过（极低频）")
    except Exception as e:
        log(f"底座池/Pitch 失败: {e}")
    # 8) 动态择时序列刷新（★2026-08-14 切换：均线金叉6/12 趋势档，替代原"满仓主义大波段"回撤档）
    #    traffic_light.py 同时生成 dynamic_regime.json（仓位档）+ traffic_light.json（红绿灯 UI）
    try:
        import subprocess
        r7 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "traffic_light.py")],
            capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        out7 = (r7.stdout or "")[-300:].strip()
        log(f"动态择时(均线金叉6/12): {out7.splitlines()[0] if out7 else '无输出'}")
    except Exception as e:
        log(f"动态择时失败: {e}")
    # 8.1) ★新择时系统（适合买入判断）——决策链"新择时"节点
    #   ★2026-10-19 修复：此前仅数据管道(daily_pipeline / manual_update_worker)调用，
    #   dev_auto 每日调度遗漏 → 新择时长期停在上一完整交易日（本次卡在 2026-10-16）。
    #   现纳入每日确定性更新，与观察池/今日信号同频刷新（date 取 bars.db 合并最新日）。
    try:
        import subprocess as _sp_t
        r_t = _sp_t.run(
            [sys.executable, "-X", "utf8", str(BASE / "factors" / "policy" / "timing_system.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        out_t = (r_t.stdout or "")[-200:].strip()
        log(f"新择时系统(适合买入): {out_t.splitlines()[0] if out_t else '无输出'}")
    except Exception as e:
        log(f"新择时系统失败: {e}")
    # 8.5) ★机会发现引擎（v3.0 重构：多类型机会扫描 → 大池子统一评分 → Pitch 三重过滤）
    try:
        import subprocess
        r8 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"],
            capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        out8 = (r8.stdout or "")[-400:].strip()
        log(f"机会扫描: {out8.splitlines()[0] if out8 else '无输出'}")
        import json as _json3
        # 读最新 opp_pool_*.json（时间戳文件名策略）
        import glob as _glob
        op_files = sorted(_glob.glob(str(BASE / "logs" / "opp_pool_*.json")))
        if op_files:
            data = _json3.loads(read_text(op_files[-1]))
            pitch = data.get("pitch", [])
            brief = "; ".join("{} ({} score={})".format(p["code"], p["otype"], p["score"]) for p in pitch)
            log(f"机会大池子 {data.get('n', 0)} 条 | Pitch 候选 {len(pitch)} 只: {brief}")
    except Exception as e:
        log(f"机会扫描失败: {e}")
    # 8.55) ★机会池看板（2026-08-10 总指导：UI A4 核心，机会扫描后自动刷新 dashboard_opp.html）
    try:
        import subprocess
        r8b = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "report" / "dashboard_opp.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        out8b = (r8b.stdout or "").strip()
        log(f"机会池看板: {out8b.splitlines()[-1] if out8b else '无输出'}")
    except Exception as e:
        log(f"机会池看板失败: {e}")
    # 8.57) ★科技突破 Pitch 池 + 突破监控（2026-08-10 用户需求 2：技术信号独立池 + NEW 监控提示）
    try:
        import subprocess as _sp2
        _rt = _sp2.run([sys.executable, "-X", "utf8",
                        str(BASE / "factors" / "opportunities" / "tech_pitch_v3.py")],
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        _ot = (_rt.stdout or "").strip().splitlines()
        log(" | ".join(_ot[:2]) if _ot else "科技池: 无输出")
    except Exception as e:
        log(f"科技池失败: {e}")
    # 8.58) ★突破实时监控（每轮检测新突破 → 提示）
    try:
        import subprocess as _sp3
        _rb = _sp3.run([sys.executable, "-X", "utf8",
                        str(BASE / "factors" / "opportunities" / "breakout_monitor.py")],
                       capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        _ob = (_rb.stdout or "").strip().splitlines()
        log(" | ".join(_ob[:2]) if _ob else "突破监控: 无输出")
    except Exception as e:
        log(f"突破监控失败: {e}")
    # 8.59) ★假信号 flag 打标（2026-08-10 固化研究员《假信号识别大全》FS-1~FS-12）
    try:
        import subprocess as _sp4
        _rf = _sp4.run([sys.executable, "-X", "utf8",
                        str(BASE / "risk" / "fake_signal_flags.py")],
                       capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        _of = (_rf.stdout or "").strip().splitlines()
        log(" | ".join(_of[:2]) if _of else "假信号: 无输出")
    except Exception as e:
        log(f"假信号 flag 失败: {e}")
    # 8.595) ★止损自动监测（2026-08-10 用户需求：自动检测止损条件是否达成 → 待处理面板）
    try:
        import subprocess as _sp5
        _rs = _sp5.run([sys.executable, "-X", "utf8",
                        str(BASE / "risk" / "stop_monitor.py")],
                       capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        _os = (_rs.stdout or "").strip().splitlines()
        log(" | ".join(_os[:2]) if _os else "止损监测: 无输出")
    except Exception as e:
        log(f"止损监测失败: {e}")
    # 8.597) ★涨跌幅榜 + 引擎/风控对照（2026-08-10 用户需求：涨幅榜/跌幅榜实时 + 命中率/拦截率）
    try:
        import subprocess as _sp6
        _rr = _sp6.run([sys.executable, "-X", "utf8",
                        str(BASE / "data" / "rank_live.py")],
                       capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        _or = (_rr.stdout or "").strip().splitlines()
        log(" | ".join(_or[:3]) if _or else "涨跌幅榜: 无输出")
    except Exception as e:
        log(f"涨跌幅榜失败: {e}")
    # 8.60) ★Pitch 历史回放回测（2026-08-10 用户需求：PIT 回放远期收益；每天重跑更新远期）
    try:
        import subprocess as _sp5
        _rq = _sp5.run([sys.executable, "-X", "utf8",
                        str(BASE / "factors" / "opportunities" / "pitch_replay.py")],
                       capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
        _oq = (_rq.stdout or "").strip().splitlines()
        log(" | ".join(_oq[-2:]) if _oq else "回放: 无输出")
    except Exception as e:
        log(f"Pitch 回放失败: {e}")
    # 8.555) ★F3 因子档案自动生成（★2026-08-10 13:10 总指导统一：改用外包 F3 交付版 gen_factor_archive.py
    #        —— mom_20_120 修正定义（mom20-mom120）+ verdict 映射；build_factor_archive.py 公式
    #        （shift20/shift120）口径不同，停止调用避免双版本混淆；17:40 计划任务 LWQuant-FactorArchive
    #        另跑 C 包产物链（拥挤度/EP/基本面/裁决），本步只刷档案本体）
    try:
        import subprocess as _sp7
        _ra = _sp7.run([sys.executable, "-X", "utf8",
                        str(BASE / "report" / "gen_factor_archive.py")],
                       capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        _oa = (_ra.stdout or "").strip().splitlines()
        log(" | ".join(_oa[-3:]) if _oa else "因子档案: 无输出")
    except Exception as e:
        log(f"因子档案失败: {e}")
    # 8.57) ★因子风险评估 + 强因子清单（2026-08-11 用户指示：强因子直通 + 统计误差审计）
    #       读外包 E7 health → 家族归并 → 独立强因子 → output/factor_risk_*.json（scan 直通分支消费）
    try:
        import subprocess
        _rr = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "factors" / "risk" / "factor_risk.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        _or = (_rr.stdout or "").strip().splitlines()
        log(" | ".join(_or[-2:]) if _or else f"factor_risk: 无输出（{_rr.stderr[-80:]}）")
    except Exception as e:
        log(f"factor_risk 失败: {e}")
    # 8.56) ★因子池报告增强 + 因子监控 + 观察池 + 持有池 + 远期池 + 科技池 + 实时面板 + 待处理 + 回测页 + 门户
    #       ★U1-2 频率控制（2026-08-10）：页面已 API 化（live_patch.js 拉 /api/live/*），
    #       模板只在每日首轮生成（同日跳过）——版本文件不再每 4h 累积（写保护下删除不可行）
    _ui_date_file = BASE / "logs" / f"ui_generated_{datetime.now().strftime('%Y%m%d')}.flag"
    if not _ui_date_file.exists():
        # ★因子池回测全景解析器（外包 60 因子全历史回测 → dashboard_factors 区块 5 + /api/live/factors.backtest）
        try:
            import subprocess
            _rb = subprocess.run(
                [sys.executable, "-X", "utf8", str(BASE / "report" / "factor_pool_backtest.py")],
                capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
            log(f"factor_pool_backtest: {( _rb.stdout or '').strip().splitlines()[-1] if (_rb.stdout or '').strip() else '无输出'}")
        except Exception as _e:
            log(f"factor_pool_backtest 失败: {_e}")
        for _s in ("enhance_factor_report", "dashboard_factors", "dashboard_watch",
                   "dashboard_holdings", "dashboard_pitchtrack", "dashboard_techpitch",
                   "dashboard_actions", "dashboard_live", "dashboard_backtest", "dashboard_u2",
                   "dashboard_pool", "dashboard_monitor", "dashboard_research",
                   "dashboard_dynamic", "dashboard_portal"):   # ★2026-08-13 #233：移除 dashboard_research_lib 冗余条目（lib 页由 dashboard_research.py 生成，无独立 py，原条目每轮留"无输出"失败日志）
            try:
                import subprocess
                _r = subprocess.run(
                    [sys.executable, "-X", "utf8", str(BASE / "report" / f"{_s}.py")],
                    capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
                log(f"{_s}: {( _r.stdout or '').strip().splitlines()[-1] if (_r.stdout or '').strip() else '无输出'}")
            except Exception as _e:
                log(f"{_s} 失败: {_e}")
        try:
            _ui_date_file.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            log(f"页面模板已生成（今日首轮，标记 {_ui_date_file.name}）")
        except Exception:
            pass
    else:
        log("页面模板今日已生成（API 实时刷新，跳过重复生成）")
    # 8.57) ★版本清理（每日首轮生成后尝试；写保护下删除失败忽略）
    try:
        import subprocess as _sp_c
        _rc = _sp_c.run([sys.executable, "-X", "utf8", str(BASE / "data" / "cleanup_versions.py")],
                        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _oc = (_rc.stdout or "").strip().splitlines()
        log(" | ".join(_oc[-2:]) if _oc else "清理: 无输出")
    except Exception as e:
        log(f"清理失败: {e}")
    # 8.58) ★系统一键巡检（2026-08-10：全链时效 + 计划任务 + Deck + 数据库，每 4h 自动留档）
    try:
        import subprocess as _sp_h
        _rh = _sp_h.run([sys.executable, "-X", "utf8", str(BASE / "data" / "health_check.py"), "--json"],
                        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _oh = (_rh.stdout or "").strip().splitlines()
        log(" | ".join(_oh[:3] + _oh[-2:]) if _oh else "巡检: 无输出")
    except Exception as e:
        log(f"巡检失败: {e}")
    # 8.59) ★全站健康扫描（2026-08-12 百轮#82：20 页面 + 27 API + 13 项一致性 → 时间戳报告，
    #       防回归工程底座；失败时 log 标注（预警中心 5min 轮询自动上报））
    try:
        import subprocess as _sp_hs
        _rhs = _sp_hs.run([sys.executable, "-X", "utf8", str(BASE / "data" / "health_scan.py"), "--quiet"],
                          capture_output=True, text=True, timeout=360, encoding="utf-8", errors="replace")
        _ohs = (_rhs.stdout or "").strip()
        log(f"健康扫描: {_ohs.splitlines()[0] if _ohs else '无输出'}" +
            (f"｜{_ohs.splitlines()[-1]}" if _ohs and len(_ohs.splitlines()) > 1 else ""))
    except Exception as e:
        log(f"健康扫描失败: {e}")
    # 8.595) ★实盘裁决快照（2026-08-12 百轮后#114：降权/观察/维持每日落盘 → 演进可追溯；
    #         同日期覆盖安全（一天多轮只留最新）；08-14 T+5 复核后自动记录恢复/确认状态）
    try:
        import subprocess as _sp_v
        _rv = _sp_v.run([sys.executable, "-X", "utf8", str(BASE / "data" / "verdict_snapshot.py"),
                         "--days", "3"], capture_output=True, text=True, timeout=120,
                        encoding="utf-8", errors="replace")
        _ov = [l for l in (_rv.stdout or "").splitlines() if "当前:" in l or "快照仓:" in l]
        log("裁决快照: " + " ｜ ".join(_ov[:4]) if _ov else "裁决快照: 无输出")
    except Exception as e:
        log(f"裁决快照失败: {e}")
    # 8.597) ★管道健康探针（2026-08-12 十轮#175：4 关键产物 mtime 阈值 → 告警落盘 → 预警中心显示，
    #         防管道静默中断（18:30 exit=143 案例）——每 4h 自动探测）
    try:
        import subprocess as _sp_ph
        _rph = _sp_ph.run([sys.executable, "-X", "utf8", str(BASE / "data" / "pipeline_health.py")],
                          capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _oph = [l for l in (_rph.stdout or "").splitlines() if "✅" in l or "❌" in l or "⚠️" in l]
        log("管道健康: " + " ｜ ".join(_oph[:6]) if _oph else "管道健康: 无输出")
    except Exception as e:
        log(f"管道健康探针失败: {e}")
    # 8.6) ★Pitch v2 Deck（机会候选 → 1/2/3 年回测 + 风控 → Deck 审批清单）
    # ★2026-08-09 裁决：统一调用外包 AI-2 版 factors/opportunities/pitch_v2.py（T+1 开盘+右删失+基准口径，优于旧 strategy/pitch_v2.py）
    try:
        import subprocess
        r9 = subprocess.run(
            [sys.executable, "-X", "utf8", str(BASE / "factors" / "opportunities" / "pitch_v2.py")],
            capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        out9 = (r9.stdout or "")[-500:].strip()
        log(f"Pitch Deck: {out9.splitlines()[0] if out9 else '无输出'}")
    except Exception as e:
        log(f"Pitch Deck 失败: {e}")
    # 8.65) ★历史 Pitch 远期收益池（2026-08-10 用户需求：入池 + 每日更新远期走势）
    #      凡进入 Pitch 的股票自动入池，追踪 T+1/5/20/60 实际收益（验证 Pitch 质量）
    try:
        import subprocess as _sp, glob as _gl
        _pf = sorted(_gl.glob(str(BASE / "logs" / "pitch_v2_*.json")))
        if _pf:
            _r = _sp.run(
                [sys.executable, "-X", "utf8", str(BASE / "factors" / "opportunities" / "pitch_track.py"),
                 "--append", _pf[-1], "--update"],
                capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
            _out = (_r.stdout or "").strip().splitlines()
            log(" | ".join(_out[-3:]) if _out else "Pitch 远期池: 无输出")
        else:
            log("Pitch 远期池: 无 pitch_v2 文件")
    except Exception as e:
        log(f"Pitch 远期池失败: {e}")
    # 8.65) ★机器强因子池（2026-08-12 用户需求#180：ext_hits 共识 top0.1% 自动入池，纯机器客观）
    try:
        import subprocess as _sp_m
        _rm = _sp_m.run(
            [sys.executable, "-X", "utf8", str(BASE / "factors" / "opportunities" / "pitch_track.py"),
             "--machine", "5", "--update"],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        _om = [l for l in (_rm.stdout or "").splitlines() if "机器池" in l]
        log("机器池: " + " ｜ ".join(_om[:2]) if _om else "机器池: 无输出")
    except Exception as e:
        log(f"机器池失败: {e}")
    # 8.66) ★2026-08-12 用户需求#180：T+5 批次复核（每日自动跑，到期批次自动出报告——08-14 首批）
    try:
        import subprocess as _sp_m   # 独立导入（不依赖 8.65 的 try 绑定）
        _rr = _sp_m.run(
            [sys.executable, "-X", "utf8", str(BASE / "risk" / "pitch_review.py")],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _or = [l for l in (_rr.stdout or "").splitlines() if "已存" in l or "待核" in l or "T+5" in l]
        log("T+5复核: " + " ｜ ".join(_or[:3]) if _or else f"T+5复核: 已生成（rc={_rr.returncode}）")
    except Exception as e:
        log(f"T+5复核失败: {e}")
    # 8.67) ★2026-08-13 用户需求#272：因子归因业绩库重建（远期池 fwd 更新后同步因子×业绩关系）
    try:
        import subprocess as _sp_fp
        _fr = _sp_fp.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "build_factor_pitch_db.py")],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _fl = [l for l in (_fr.stdout or "").splitlines() if "建库完成" in l or "因子" in l]
        log("因子业绩库: " + (_fl[-1] if _fl else f"rc={_fr.returncode}"))
    except Exception as e:
        log(f"因子业绩库失败: {e}")
    # 8.68) ★2026-08-13 用户需求#273：unified.db 整合库重建（资产清单 + 数据链路 + 因子业绩统一查询）
    try:
        import subprocess as _sp_ud
        _ur = _sp_ud.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "build_unified_db.py")],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _ul = [l for l in (_ur.stdout or "").splitlines() if "unified.db" in l or "asset_inventory" in l]
        log("整合库: " + (_ul[0] if _ul else f"rc={_ur.returncode}"))
    except Exception as e:
        log(f"整合库失败: {e}")
    # 8.685) ★2026-08-13 #322 因子全量回测入库（每个因子 top20% 多头 → T+1，存 unified.db factor_backtest）
    try:
        import subprocess as _sp_fb
        _br = _sp_fb.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "build_factor_backtest_db.py")],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        _bl = [l for l in (_br.stdout or "").splitlines() if "已入库" in l or "daily_scores" in l]
        log("因子回测库: " + (_bl[-1] if _bl else f"rc={_br.returncode}"))
    except Exception as e:
        log(f"因子回测库失败: {e}")
    # 8.69) ★2026-08-13 择时历史归档（跨日 score_series 积累——"评分走势"数据源；每天追加，动起来）
    try:
        import subprocess as _sp_th
        _tr = _sp_th.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "timing_history.py")],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        _tl = [l for l in (_tr.stdout or "").splitlines() if "score 序列" in l or "归档" in l]
        log("择时历史: " + (_tl[0] if _tl else f"rc={_tr.returncode}"))
    except Exception as e:
        log(f"择时历史失败: {e}")
    # 8.70) ★2026-08-13 市场知识库（#293：unified.db 扩展 market_daily/style/health/timing_series + AI 导出 market_kb_dump.json——供知识库 AI 主观分析）
    try:
        import subprocess as _sp_mk
        _mr = _sp_mk.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "build_market_kb.py"), "--dump"],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        _ml = [l for l in (_mr.stdout or "").splitlines() if "市场知识库" in l or "AI 导出" in l]
        log("市场知识库: " + (_ml[0] if _ml else f"rc={_mr.returncode}"))
    except Exception as e:
        log(f"市场知识库失败: {e}")
    # 8.71) ★2026-08-14 #431 时间戳版本文件归档（logs/output 多写侧每轮写时间戳累积几百个 → 保留最新 N 个移垃圾桶）
    try:
        import subprocess as _sp_la
        _lr = _sp_la.run(
            [sys.executable, "-X", "utf8", str(BASE / "data" / "logs_archive.py")],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        _ll = [l for l in (_lr.stdout or "").splitlines() if "合计" in l]
        log("时间戳归档: " + (_ll[0] if _ll else f"rc={_lr.returncode}"))
    except Exception as e:
        log(f"时间戳归档失败: {e}")
    # 8.7) ★数据健康巡检（2026-08-09 加入：重复/最新日期/格式残留自检）
    try:
        from data.daily_pipeline import health_check
        health_check()
    except Exception as e:
        log(f"健康巡检失败: {e}")
    log("数据更新: 本轮完成（审计 + 因子池 + 信号 + 看板 + Tushare 增量 + 模拟盘 + 指数增量 + 精选排名 + 动态择时 + 机会扫描 + Pitch Deck + 健康巡检）")


def run_sched():
    """计划任务模式：熔断检查 → 更新 → 快照 → 生成 AI 待办"""
    log("== dev_auto --sched 启动 ==")
    start_ts = time.time()

    # ★熔断检查 0：已熔断则本轮直接跳过
    if breaker_active():
        st = _read_state()
        log(f"★熔断中，本轮跳过（原因: {st.get('reason', 'STOP.md 存在')}，"
            f"恢复: python dev_auto.py --reset）")
        return

    errors = 0

    # ★熔断检查 1：连续无进展
    if _check_no_progress():
        return

    # 确定性更新
    try:
        run_update()
    except Exception as e:
        errors += 1
        log(f"run_update 异常: {e}")

    # ★熔断检查 2：异常次数
    if _check_errors(errors):
        return

    # 生成 AI 待办
    progress_text = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else ""
    todos = []
    if "fetcher_tushare" not in progress_text or "下一步：写 `data/fetcher_tushare.py`" in progress_text:
        todos.append("M1 收尾：写 data/fetcher_tushare.py（复权因子/市值/股票列表，免费积分可用项），跑通后做 M1 验收 demo（单只三源一致性 + 财报校验挂接）")
    elif "M2 全市场数据入库" in progress_text and "下一步：写 `data/bulk_loader.py`" in progress_text:
        todos.append("M2 全市场入库：写 data/bulk_loader.py（批量下载器：多源主备 + 断点续传 + 进度日志），先小样本 50 只验证速率，再全量 5538 只（夜间分片跑）")
    if not todos:
        todos.append("读取 deepseek-harness-quant/进度.md 判断当前里程碑，执行下一个开发任务（写代码→验证→更新进度）")

    push_todo(todos)

    # ★学习笔记监控（不覆盖队列，追加新笔记吸收任务）
    run_notes_check()

    # ★M3 自动触发：M2 完成后自动跑因子验证（幂等，不重复）
    run_m3_trigger()

    # ★熔断检查 3：超时
    if time.time() - start_ts > RUN_TIMEOUT_MIN * 60:
        _set_breaker(f"本轮运行超时（>{RUN_TIMEOUT_MIN} 分钟）", auto=True)
        return

    log("== 快照 ==")
    log(read_tail(PROGRESS, 10).replace("\n", " | "))
    log("== dev_auto --sched 完成 ==")


def run_bridge():
    """桥接模式：输出待办队列给会话执行"""
    print(">>> AI 待办队列（供 WorkBuddy 会话执行）<<<")
    print(read_tail(QUEUE, 20) if QUEUE.exists() else "(队列为空)")
    print("\n>>> 进度尾部 <<<")
    print(read_tail(PROGRESS, 10))


def run_status():
    print(f"上次运行日志尾部：\n{read_tail(LOG_DIR / 'dev_auto.log', 8)}")
    print(f"\n待办队列：\n{read_tail(QUEUE, 8) if QUEUE.exists() else '(空)'}")
    st = _read_state()
    print(f"\n熔断状态: {'🔴 已熔断' if breaker_active() else '🟢 正常'}"
          f"（原因: {st.get('reason', '无')}）")
    # ★2026-10-18 三态显示：查不到就说查不到，不再一律 🟢
    _te, _tn = _task_enabled(), _task_name()
    if _te is True:
        _disp = f"🟢 启用中（{_tn}）"
    elif _te is False:
        _disp = f"🔴 已禁用（{_tn}）"
    else:
        _disp = (f"⚪ 未注册（未找到 *-{TASK_SUFFIX} 任务 → 例行运行不会自动触发）"
                 if _tn is None else f"🟡 状态未知（{_tn}）")
    print(f"计划任务: {_disp}")


def main():
    ap = argparse.ArgumentParser(description="DeepSeek HARNESS Quant · 本地自动开发驱动")
    ap.add_argument("--sched", action="store_true", help="计划任务模式（熔断检查+更新+快照+笔记监控+生成AI待办）")
    ap.add_argument("--notes", action="store_true", help="仅笔记监控（扫描学习文件夹，新笔记入待办队列）")
    ap.add_argument("--update", action="store_true", help="仅确定性数据更新")
    ap.add_argument("--bridge", action="store_true", help="桥接模式：输出待办队列")
    ap.add_argument("--status", action="store_true", help="状态查看（含熔断状态）")
    ap.add_argument("--stop", action="store_true", help="熔断：停止循环（禁用计划任务+写 STOP.md）")
    ap.add_argument("--reset", action="store_true", help="恢复：清除熔断（启用计划任务）")
    ap.add_argument("--ui", action="store_true", help="★UI 框架手动重建：删除今日 flag 后重跑全部页面模板生成器（框架改动立即生效，不等次日首轮）")
    args = ap.parse_args()

    if args.sched:
        run_sched()
    elif args.notes:
        run_notes_check()
    elif args.update:
        run_update()
    elif args.ui:
        _ui_flag = BASE / "logs" / f"ui_generated_{datetime.now().strftime('%Y%m%d')}.flag"
        try:
            if _ui_flag.exists():
                _ui_flag.unlink()
                log(f"--ui: 已删除 {_ui_flag.name}，触发模板重建")
            else:
                log("--ui: 今日 flag 不存在，直接重建")
        except Exception as _e:
            log(f"--ui: 删 flag 失败（写保护）: {_e}")
        run_update()
    elif args.bridge:
        run_bridge()
    elif args.stop:
        _set_breaker("用户手动熔断", auto=False)
    elif args.reset:
        clear_breaker()
    elif args.status:
        run_status()
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
