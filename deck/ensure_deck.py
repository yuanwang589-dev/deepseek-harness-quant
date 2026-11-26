# -*- coding: utf-8 -*-
"""deck/ensure_deck.py — Deck 守护（2026-08-10 总指导）

检查 8787 端口是否有 Deck 在监听且响应正常；异常则重启。
被 dev_auto 每轮调用（保证桌面门户永不失效）。
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
import socket

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception as _dsh_exc:
    _dsh_swallow(_dsh_exc, "deck/ensure_deck.py:15 <module>")

import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8787


def _port_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=3):
            return True
    except OSError:
        return False


def _http_ok() -> bool:
    try:
        import urllib.request
        r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5)
        return r.status == 200
    except Exception:
        return False


def ensure() -> bool:
    # ★2026-08-10 检测日志（记录每次守护结论）
    _log = BASE / "logs" / "deck_ensure.log"
    # ★2026-08-14 日志轮转：>1MB 滚动保留 3 份（守护日志长期 append 无界增长，防磁盘膨胀）
    try:
        if _log.exists() and _log.stat().st_size > 1024 * 1024:
            for _i in (2, 1, 0):
                _src = _log if _i == 0 else _log.with_suffix(f".{_i}.log")
                _dst = _log.with_suffix(f".{_i + 1}.log")
                if _src.exists():
                    _dst.write_bytes(_src.read_bytes())
            _log.write_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 日志轮转（>1MB）\n", encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "deck/ensure_deck.py:57 ensure")
    def _trace(msg):
        try:
            with open(_log, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "deck/ensure_deck.py:63 _trace")
    _port = _port_listening()
    _http = _http_ok()
    if _port and _http:
        return True  # 健康
    _trace(f"异常检测: 端口监听={_port} HTTP={_http} → 触发自愈")
    # ★2026-08-15 双开免疫：杀【全部】deck_server 进程（按命令行匹配，非仅端口占用者——
    #   Windows SO_REUSEADDR 允许两进程同绑 8787，端口法只杀一个会留冗余实例累积；
    #   且杀共享 socket 组中的单个会导致监听中断——必须全杀后单实例重启）
    try:
        import subprocess as sp
        _cmdline = ""
        try:
            _cmdline = sp.run(["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
                              capture_output=True, text=True, errors="replace", timeout=15).stdout or ""
        except Exception:
            _cmdline = ""
        if "deck_server.py" not in _cmdline:
            try:
                _cmdline = sp.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\").CommandLine"],
                    capture_output=True, text=True, errors="replace", timeout=15).stdout or ""
            except Exception:
                _cmdline = ""
        for line in _cmdline.splitlines():
            if "deck_server.py" in line:
                _pid = line.split()[-1].strip()
                if _pid.isdigit():
                    sp.run(["taskkill", "/PID", _pid, "/F"], capture_output=True)
        time.sleep(2)
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "deck/ensure_deck.py:95 ensure")
    # 启动（★2026-08-10 加独立日志：不再 DEVNULL，崩溃原因可查）
    try:
        log_f = open(BASE / "logs" / "deck_ensure.log", "a", encoding="utf-8")
        p = subprocess.Popen(
            [PY, "-u", str(BASE / "deck" / "deck_server.py")],
            cwd=str(BASE),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            stdout=log_f, stderr=log_f)
        time.sleep(4)
        ok = _port_listening() and _http_ok()
        if not ok:
            p.terminate()
            log_f.write(f"[{time.strftime('%H:%M:%S')}] 启动失败（4s 内未就绪）\n")
            log_f.close()
            return False
        log_f.write(f"[{time.strftime('%H:%M:%S')}] 启动成功 PID={p.pid}\n")
        log_f.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    ok = ensure()
    print(f"[{time.strftime('%H:%M:%S')}] Deck 守护: {'健康' if ok else '启动失败'}")
    sys.exit(0 if ok else 1)
