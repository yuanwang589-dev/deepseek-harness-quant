# -*- coding: utf-8 -*-
"""data/task_registry.py — 计划任务名唯一事实源（2026-10-18 · 修虚假绿灯）

【为什么需要这个模块】
项目历史上计划任务前缀为 `LWQuant-*`（health_scan.py 硬编码 7 个、deck/system_live.py
硬编码 5 个），2026-10-18 重装调度层时实际注册为 `DSHQuant-*`。两套名字互不相认：

  · dev_auto._task_enabled() 用 schtasks 查 "LWQuant-DevDriver" → 查不到 → 抛异常
    → `except: return True` → 界面显示「计划任务: 🟢 启用中」，实际该任务根本不存在。
  · health_scan 把 7 个 LWQuant 任务全判「缺失」→ 调度维度永远红灯。
  · deck/system_live 页面同样显示未注册。

结果：调度层「看起来在跑」，实际从未注册 → 例行运行停摆而无人察觉。这是典型的
**虚假绿灯**（比红灯更危险：红灯会让人去修，绿灯让人以为没事）。

【本模块的约定】
1. 不硬编码前缀。按「-后缀」解析真实任务名，前缀从 DSHQuant → LWQuant → 任意 依次尝试。
2. 查询失败必须返回 None / "Unknown"，**绝不**用 True 兜底。
3. 调用方拿到 None 必须诚实显示「未注册」或「未知」，不得显示绿色。

用法：
    from data.task_registry import resolve, state, snapshot
    resolve("DevDriver")        # → "DSHQuant-DevDriver" 或 None
    state("DevDriver")          # → "Ready" / "Running" / "Disabled" / "Missing" / "Unknown"
    snapshot()                  # → {"ok": bool, "found": {...}, "missing": [...]}
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

PREFERRED_PREFIX = "DSHQuant"
LEGACY_PREFIX = "LWQuant"

# 调度层规格：后缀 → (人类可读说明, 期望频率)
# 注意：这里只登记「本仓库有对应脚本、应当注册」的任务。
# FactorDaily / FactorArchive 对应的 data/factorpool/core/scheduler.py 在本构建中不存在
# （因子池是外包组件，未随包发布），故不登记——避免健康检查对着不存在的规格报红灯。
SPEC = {
    "DeckGuard":     ("24h 守护 deck 服务（每 30min 巡检）", "30min"),
    "DailyPipeline": ("每日数据全链（工作日 18:30）",         "daily"),
    "DevDriver":     ("开发自驱巡检 + 熔断（22:00 起每 4h）",  "4h"),
    "AfterCloseScan": ("盘后机会扫描（工作日 17:35）",        "daily"),
    "TushareInc":    ("日线增量拉取（工作日 17:30）",         "daily"),
    "BreakoutMon":   ("突破监控（工作日 18:50）",             "daily"),
}

_CACHE: dict | None = None

# PowerShell ConvertTo-Json 会把 TaskState 枚举序列化成整数（3=Ready, 4=Running…），
# 必须映射回名字，否则状态判定会把 "3" 当成未知状态。
_STATE_NUM = {"0": "Unknown", "1": "Disabled", "2": "Queued",
              "3": "Ready", "4": "Running"}


def _norm_state(raw) -> str:
    s = str(raw if raw is not None else "Unknown").strip()
    return _STATE_NUM.get(s, s or "Unknown")


def _query_powershell() -> dict[str, str] | None:
    """用 PowerShell 拿 {TaskName: State}。最可靠：能区分 Ready/Running/Disabled 与不存在。"""
    ps = ("Get-ScheduledTask | Select-Object TaskName,State | "
          "ConvertTo-Json -Compress")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        txt = r.stdout.decode("utf-8", errors="replace").strip()
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        out = {}
        for d in data:
            n = d.get("TaskName")
            if n:
                out[n] = _norm_state(d.get("State"))
        return out or None
    except Exception:
        return None


def _query_schtasks() -> dict[str, str] | None:
    """schtasks 兜底。中文/英文输出都解析，拿不到状态就给 Unknown（不猜）。"""
    try:
        r = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/nh"],
                           capture_output=True, timeout=25)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        txt = r.stdout.decode("gbk", errors="replace")
        out = {}
        for line in txt.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) < 3:
                continue
            name = parts[0].strip('"').lstrip("\\")
            if not name:
                continue
            st = parts[2].strip('"') if len(parts) > 2 else ""
            out[name] = _norm_state(st)
        return out or None
    except Exception:
        return None


def query_tasks(refresh: bool = False) -> dict[str, str] | None:
    """返回 {TaskName: State}；两次查询都失败时返回 None（诚实失败，不伪装）。"""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    tasks = _query_powershell() or _query_schtasks()
    if tasks is not None:
        _CACHE = tasks
    return tasks


def resolve(suffix: str, tasks: dict[str, str] | None = None) -> str | None:
    """按后缀解析真实任务名。前缀无关，故 DSHQuant/LWQuant/将来换名都能命中。"""
    tasks = tasks if tasks is not None else query_tasks()
    if not tasks:
        return None
    bare = suffix.lstrip("-")
    for pref in (PREFERRED_PREFIX, LEGACY_PREFIX):
        name = f"{pref}-{bare}"
        if name in tasks:
            return name
    if bare in tasks:
        return bare
    for n in tasks:
        if n == bare or n.endswith("-" + bare):
            return n
    return None


def state(suffix: str, tasks: dict[str, str] | None = None) -> str:
    """返回 'Ready'/'Running'/'Disabled'/'Missing'/'Unknown'。Unknown = 查不了，别当健康。"""
    tasks = tasks if tasks is not None else query_tasks()
    if tasks is None:
        return "Unknown"
    name = resolve(suffix, tasks)
    if name is None:
        return "Missing"
    return tasks.get(name, "Unknown")


def is_healthy(suffix: str, tasks: dict[str, str] | None = None) -> bool | None:
    """True=已注册且未禁用；False=缺失或禁用；None=无法判定（查询失败）。"""
    st = state(suffix, tasks)
    if st == "Unknown":
        return None
    return st not in ("Disabled", "Missing")


def snapshot(tasks: dict[str, str] | None = None) -> dict:
    """给健康检查/状态页用的一次性汇总。"""
    tasks = tasks if tasks is not None else query_tasks()
    if tasks is None:
        return {"ok": False, "queryable": False,
                "found": {}, "missing": list(SPEC), "disabled": [],
                "note": "计划任务查询失败（schtasks/PowerShell 均不可用），状态未知"}
    found, missing, disabled = {}, [], []
    for suffix, (desc, freq) in SPEC.items():
        name = resolve(suffix, tasks)
        if name is None:
            missing.append(suffix)
            found[suffix] = {"name": None, "state": "Missing", "desc": desc, "freq": freq}
        else:
            st = tasks.get(name, "Unknown")
            found[suffix] = {"name": name, "state": st, "desc": desc, "freq": freq}
            if st == "Disabled":
                disabled.append(name)
    return {"ok": not missing and not disabled, "queryable": True,
            "found": found, "missing": missing, "disabled": disabled}


_NEXT_CACHE: tuple[float, dict[str, str] | None] | None = None


def query_next_runs(refresh: bool = False, ttl: float = 120.0) -> dict[str, str] | None:
    """返回 {TaskName: 下次运行时间 'YYYY-MM-DD HH:MM:SS'}；查不到返回 None。

    ★2026-10-18 为什么不用 schtasks：本机安全策略把 schtasks.exe 列入程序黑名单，
      deck 页面原来用它取"下次运行时间"→ 全部 ERR [WinError 5]，页面看起来像调度层坏了。
      PowerShell 的 Get-ScheduledTaskInfo 不在黑名单，改走它，并且一次调用拿全部任务
      （避免 6 次进程启动把 API 拖慢）。
    """
    global _NEXT_CACHE
    if _NEXT_CACHE is not None and not refresh and (time.time() - _NEXT_CACHE[0]) < ttl:
        return _NEXT_CACHE[1]

    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-ScheduledTask | ForEach-Object {"
        "  $n=$_.TaskName;"
        "  $i=Get-ScheduledTaskInfo -TaskName $n -TaskPath $_.TaskPath;"
        "  $t=''; if($i.NextRunTime){$t=$i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')};"
        "  Write-Output ($n + '|' + $t)"
        "}"
    )
    out: dict[str, str] | None = None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30)
        if r.returncode == 0:
            txt = r.stdout.decode("utf-8", errors="replace")
            parsed = {}
            for line in txt.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                name, _, nxt = line.partition("|")
                parsed[name.strip()] = nxt.strip()
            out = parsed or None
    except Exception:
        out = None

    _NEXT_CACHE = (time.time(), out)
    return out


def next_run(suffix: str, runs: dict[str, str] | None = None) -> str | None:
    """某任务的下次运行时间字符串；任务不存在或时间为空 → None。"""
    runs = runs if runs is not None else query_next_runs()
    if not runs:
        return None
    name = resolve(suffix)
    if not name:
        return None
    return runs.get(name) or None


def main() -> int:
    snap = snapshot()
    if not snap["queryable"]:
        print("❌ " + snap["note"])
        return 2
    print(f"计划任务规格 {len(SPEC)} 项：就绪 {len(SPEC) - len(snap['missing']) - len(snap['disabled'])}"
          f" / 缺失 {len(snap['missing'])} / 禁用 {len(snap['disabled'])}")
    for suffix, info in snap["found"].items():
        mark = {"Ready": "🟢", "Running": "🟢", "Disabled": "🔴", "Missing": "⚪"}.get(info["state"], "🟡")
        name = info["name"] or "(未注册)"
        print(f"  {mark} {suffix:14s} {info['state']:9s} {name:26s} {info['desc']}")
    return 0 if snap["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
