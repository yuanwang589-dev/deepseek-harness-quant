# -*- coding: utf-8 -*-
"""scripts/watchdog.py — DSHQuant 24小时守护进程（★2026-10-15 #桌面常驻运行）

用途：让量化看板（deck_server :8787）7×24 在线——进程退出/僵死/漏启时自动拉起，
      配合 Windows 计划任务（登录时自启）实现无人值守常驻。

设计（与 manual_update_worker 同一套铁律）：
  1. 单实例锁：O_EXCL pid 文件，旧 pid 存活则保守退出（防止计划任务重复拉起多个守护）
  2. 清理代理环境变量：沙箱/办公网 HTTP_PROXY 会拖死 akshare 新浪/Tushare 直连（直连实测均通）
  3. 解释器优先级：仓库同级 .venv → runtime/python → portable-py → 当前解释器
  4. 健康检查：每 interval 秒 GET /api/live/opp，非 200 视为不健康；
     子进程存活但不健康 → kill 重启；子进程已退出 → 重启；指数退避 5→60s 封顶
  5. 日志：logs/watchdog.log（追加）+ logs/watchdog_server.out（服务 stdout/stderr）

用法：
  python scripts/watchdog.py                 # 守护模式（常驻，计划任务用这个）
  python scripts/watchdog.py --once          # 单次巡检：健康则退出，不健康则拉起一个后台服务后退出
  python scripts/watchdog.py --port 8787 --interval 30

计划任务（登录自启，无窗口）：
  schtasks /create /tn "DSHQuant-24h-Keeper" /sc onlogon /rl limited /f ^
    /tr "\\"<venv>\\Scripts\\pythonw.exe\\" -X utf8 \\"<repo>\\scripts\\watchdog.py\\""
删除：
  schtasks /delete /tn "DSHQuant-24h-Keeper" /f
"""
import argparse
import http.client
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
PIDFILE = LOGS / "watchdog.pid"
SERVER = BASE / "deck" / "deck_server.py"
PROXY_KEYS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"]
HEALTH_PATH = "/healthz"          # ★2026-10-18：存活探针（零 I/O）。原用 /api/live/opp 是业务接口
READY_PATH = "/readyz"            # 就绪探针（查依赖+产物），只观测，**不触发重启**
FAIL_THRESHOLD = 3                # 连续失败多少次才判死（k8s failureThreshold 默认 3）


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOGS / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def clean_env() -> dict:
    """子进程环境：剥代理 + UTF-8 + 数据目录 + 仓库根入 PYTHONPATH（与 启动.cmd 语义一致）

    ★2026-10-18 修复（例行停摆根因之二）：
    原来没设 PYTHONPATH。`python deck/deck_server.py` 的 sys.path[0] 是 deck/，
    而 live_api.py 需要 `from data.io_utils import ...` → ModuleNotFoundError
    → 所有 /api/live/* 返回 500 → watchdog 健康检查（/api/live/opp）永远失败
    → 每 30s 杀一次、重启一次，看板实际不可用（10-15 当天 watchdog.log 里
      15:00/15:02/15:05/15:26/17:40 的连续"守护启动"就是这个循环）。
    """
    env = dict(os.environ)
    for k in PROXY_KEYS:
        env.pop(k, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LWQUANT_CACHE_DIR", str(BASE / "data" / "cache"))
    _pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(BASE) + (os.pathsep + _pp if _pp else "")
    return env


def pick_python() -> str:
    for c in (BASE.parent / ".venv" / "Scripts" / "python.exe",
              BASE / "runtime" / "python" / "python.exe",
              BASE / "portable-py" / "Scripts" / "python.exe"):
        if c.exists():
            return str(c)
    return sys.executable


def health(port: int, timeout: int = 8) -> bool:
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", HEALTH_PATH)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status == 200
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    """pid 是否仍是**本守护**（watchdog.py）进程。

    ★2026-10-18 修复（例行停摆根因之一）：
    原实现 `os.kill(pid, 0)` 在 Windows 上不是"探测"，而是
    `TerminateProcess(pid, 0)` —— 传 0 等于**无条件杀掉该 pid**。
    后果有二：
      ① pid 被系统复用（关机重启后极易发生）时会误杀一个无辜进程；
      ② 杀完返回成功 → 误判"守护还活着"→ 本实例直接退出 → 看板再也起不来。
    现改为：psutil 读命令行确认确为 watchdog.py（只查询、零副作用）；
    无 psutil 时退化为 OpenProcess + GetExitCodeProcess，同样只查询。
    """
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        cmd = " ".join(p.cmdline()).lower()
        return "watchdog.py" in cmd
    except Exception:
        pass
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)          # 仅 POSIX：0 号信号是真正的存在性探测
        return True
    except Exception:
        return False


_LOCK_HANDLE = None


def single_instance() -> bool:
    """内核级独占锁单实例；返回 False 表示已有存活守护，本进程应退出。

    ★2026-10-18 重写（例行停摆根因之三）：
    原实现是 pid 文件 + O_EXCL，两个致命缺陷：
      ① pid 文件一旦被删（人工清理、崩溃残留清理脚本、我这次排查时的 rm）→
         两个实例都认为自己是唯一 → **双守护**；
      ② pid 被系统复用 → _pid_alive 误判（即使已修成查命令行，也仍有窗口）。
    实测后果：2 个 watchdog 各自 spawn 一个 deck_server；因为
    DeckServer.allow_reuse_address=True（Windows 等价 SO_REUSEADDR）**两个都能绑 8787**，
    连接只落到其中一个 → 改代码不生效、同一个响应里混着新旧两版字段，极难排查。

    改为 msvcrt.locking / fcntl.flock 独占锁：锁由内核随进程退出自动释放，
    不依赖 pid 是否正确、不依赖进程是否"看起来还活着"。
    ⚠ 约定：logs/watchdog.lock **永不手动删除**（锁在 inode 上，删了文件等于作废锁）；
       pid 文件仅作可读信息，删了也无害。
    """
    global _LOCK_HANDLE
    lock_path = LOGS / "watchdog.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
    except PermissionError:
        # ★2026-10-18 关键细节：Windows 的字节范围锁是**强制锁**，另一个进程持锁后，
        #   本进程连 open(...,"a+") 都会 EACCES。因此"打开就被拒"= 已有实例持锁，
        #   必须判为"应退出"，绝不能 fail-open（fail-open 会直接放行出第二个守护）。
        log("已有存活守护进程（锁文件被占用 → Permission denied），本实例退出")
        return False
    except Exception as e:
        log(f"单实例锁文件打开失败（fail-open，允许启动）: {e}")
        return True
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = Path(PIDFILE).read_text(encoding="utf-8").strip() if PIDFILE.exists() else "?"
        except Exception:
            holder = "?"
        log(f"已有存活守护进程（锁被占用，holder pid≈{holder}），本实例退出")
        try:
            fh.close()
        except Exception:
            pass
        return False
    except Exception as e:
        log(f"单实例加锁异常（fail-open，允许启动）: {e}")
        _LOCK_HANDLE = fh
        return True

    _LOCK_HANDLE = fh                      # 故意不关闭：持有到进程退出
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    return True


def spawn(port: int, env: dict, py: str) -> subprocess.Popen:
    out = open(LOGS / "watchdog_server.out", "a", encoding="utf-8")
    return subprocess.Popen(
        [py, "-X", "utf8", str(SERVER), "--port", str(port)],
        cwd=str(BASE), env=env, stdout=out, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


def readiness(port: int, timeout: int = 8):
    """读就绪态摘要（仅用于日志观测）。失败返回 None，不影响重启判定。"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", READY_PATH)
        r = c.getresponse()
        body = r.read()
        c.close()
        import json as _j
        d = _j.loads(body.decode("utf-8", "replace"))
        w = d.get("warnings") or []
        e = d.get("errors") or []
        return f"status={d.get('status')} errors={len(e)} warnings={len(w)}" + \
               (f" 首个:{(e or w)[0].get('name')}/{(e or w)[0].get('issue') or (e or w)[0].get('detail')}"
                if (e or w) else "")
    except Exception:
        return None


def wait_healthy(child: subprocess.Popen, port: int, budget: int = 90) -> bool:
    """最多等 budget 秒：子进程存活且健康检查通过 → True"""
    t0 = time.time()
    while time.time() - t0 < budget:
        if child.poll() is not None:
            log(f"deck_server 提前退出 code={child.returncode}")
            return False
        if health(port):
            return True
        time.sleep(2)
    return False


def serve_forever(port: int, interval: int):
    env, py = clean_env(), pick_python()
    backoff, restarts, child = 5, 0, None
    fails = 0
    log(f"守护启动 pid={os.getpid()} port={port} interval={interval}s py={py}")
    try:
        while True:
            if health(port):
                if fails:
                    log(f"存活探针恢复（此前连续失败 {fails} 次）")
                fails = 0
                time.sleep(interval)
                continue
            fails += 1
            # ★2026-10-18 借 k8s failureThreshold 语义：**不要一次探不通就杀**。
            #   Kubernetes 官方警告：错误实现的存活探针会在高负载下重启容器，引发级联失败。
            #   本机实测正是如此——funnel 慢查询占满线程 → 探针超时 → 立刻 kill → 全站 30~60s 不可用。
            #   现改为连续 FAIL_THRESHOLD 次失败才动手；中间只记录 not-ready 观察结果。
            if fails < FAIL_THRESHOLD:
                log(f"存活探针失败 {fails}/{FAIL_THRESHOLD}（未达阈值，仅观察不重启）")
                _r = readiness(port)
                if _r:
                    log(f"  readiness: {_r}")
                time.sleep(interval)
                continue
            log(f"存活探针连续失败 {fails} 次（≥阈值 {FAIL_THRESHOLD}）→ 判死")
            # 健康失败：清理可能僵死的旧子进程（端口占用型僵死）
            if child is not None and child.poll() is None:
                log("健康检查失败但旧子进程仍存活（疑似僵死）→ 终止后重启")
                try:
                    child.kill()
                except Exception:
                    pass
                time.sleep(1)
            log(f"拉起 deck_server (port={port}) ...")
            try:
                child = spawn(port, env, py)
            except Exception as e:
                log(f"启动失败: {str(e)[:120]}；{backoff}s 后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            restarts += 1
            log(f"deck_server pid={child.pid}（第 {restarts} 次启动），等待健康（≤90s）...")
            if wait_healthy(child, port):
                log("健康检查通过 ✓ 服务在线")
                backoff = 5
                fails = 0
            else:
                if child.poll() is None:
                    log("90s 未健康 → 终止并重启")
                    try:
                        child.kill()
                    except Exception:
                        pass
                log(f"{backoff}s 后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，守护退出（deck_server 子进程保留运行）")
    finally:
        try:
            Path(PIDFILE).unlink()
        except Exception:
            pass


def once(port: int) -> int:
    """单次巡检：健康→0；不健康→拉起后台服务并等待健康，成功 0 / 失败 1"""
    if health(port):
        log("--once: 服务在线，无需操作")
        return 0
    env, py = clean_env(), pick_python()
    log(f"--once: 服务不在线，拉起 deck_server (port={port}) ...")
    child = spawn(port, env, py)
    if wait_healthy(child, port):
        log("--once: 拉起成功，服务在线（子进程已脱离本进程）")
        return 0
    log("--once: 拉起失败")
    return 1


def main():
    ap = argparse.ArgumentParser(description="DSHQuant 24小时守护")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--interval", type=int, default=30, help="健康检查间隔秒（默认 30）")
    ap.add_argument("--once", action="store_true", help="单次巡检模式（供计划任务定时调用）")
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    if args.once:
        sys.exit(once(args.port))
    if not single_instance():
        sys.exit(0)
    serve_forever(args.port, args.interval)


if __name__ == "__main__":
    main()
