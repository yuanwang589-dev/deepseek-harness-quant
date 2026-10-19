# -*- coding: utf-8 -*-
"""QuantDeck 启动器：一键启动 量化 Deck（:8787）+ DeepSeek HARNESS（:3080）。

行为：
  - 数据目录：环境变量 LWQUANT_CACHE_DIR > EXE/仓库 同级 data/
  - HARNESS：若存在 harness/ 运行时且系统有 Node.js → 自动启动（DSH_HOME=harness/home）
    · 首次使用：把 harness/home/.credentials.yaml.example 复制为 .credentials.yaml
      并填入 DeepSeek API Key（先接 AI 的 API，AI 会协助你接入其余数据源 API）
    · 没有 Node.js 时跳过 HARNESS（量化系统照常运行）
  - 浏览器：自动打开 量化门户 http://127.0.0.1:8787（HARNESS GUI 为 3080，可手动打开）
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent     # 源码运行：仓库根


def _builtin_python():
    """优先使用发布包内置便携 Python（runtime/python 或 portable-py）"""
    for c in (base_dir() / "runtime" / "python" / "python.exe",
              base_dir() / "portable-py" / "Scripts" / "python.exe"):
        if c.exists():
            return c
    return None


def _builtin_node():
    """优先使用发布包内置便携 Node（runtime/node 或 portable-node）"""
    for c in (base_dir() / "runtime" / "node" / "node.exe",
              base_dir() / "portable-node" / "node.exe"):
        if c.exists():
            return c
    return None


def start_harness(base: Path):
    """启动 DeepSeek HARNESS（harness/ 存在且 Node.js 可用时）。返回进程或 None。"""
    node = _builtin_node() or shutil.which("node")
    harness_root = base / "harness"
    dsh_bin = harness_root / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not node:
        print("[QuantDeck] 未检测到 Node.js —— HARNESS 对话功能跳过（量化系统照常）")
        return None
    if not dsh_bin.exists():
        print("[QuantDeck] harness 运行时缺失 —— 请先运行 harness/install.cmd 安装 HARNESS")
        return None
    cred = harness_root / "home" / ".credentials.yaml"
    if not cred.exists():
        print("[QuantDeck] HARNESS 未配置 API Key：把 harness/home/.credentials.yaml.example "
              "复制为 .credentials.yaml 并填入 DeepSeek API Key（见 docs/HARNESS接入.md）")
    env = dict(os.environ)
    env["DSH_HOME"] = str(harness_root / "home")
    print(f"[QuantDeck] 启动 DeepSeek HARNESS（DSH_HOME={env['DSH_HOME']}）...")
    try:
        proc = subprocess.Popen([str(node), str(dsh_bin), "web"],
                                cwd=str(harness_root), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc
    except Exception as e:
        print(f"[QuantDeck] HARNESS 启动失败: {e}")
        return None


def _open_browsers():
    time.sleep(2.0)
    try:
        webbrowser.open("http://127.0.0.1:8787")
    except Exception:
        pass


def main():
    base = base_dir()
    if not os.environ.get("LWQUANT_CACHE_DIR"):
        os.environ["LWQUANT_CACHE_DIR"] = str(base / "data")
    data_dir = os.environ["LWQUANT_CACHE_DIR"]
    print(f"[QuantDeck] 数据目录: {data_dir}")
    if not (Path(data_dir) / "bars.db").exists():
        print("[QuantDeck] 提示: 未检测到 bars.db —— 请先获取数据（docs/快速开始.md §3），或使用演示数据模式")

    start_harness(base)                 # HARNESS（可选，:3080）
    threading.Thread(target=_open_browsers, daemon=True).start()

    # ★2026-10-15 直接调用 deck_server.main()，不再自己拼 ThreadingHTTPServer。
    #   原实现（from deck.deck_server import Handler; srv = ThreadingHTTPServer(...)）
    #   绕过了 DeckServer 的全部加固：守护线程、未捕获异常回 500（客户端不再挂到超时）、
    #   listen backlog 64、双开自检（Windows SO_REUSEADDR 会被第二个进程绑同一端口，
    #   造成"新进程空跑、旧进程继续服务"的端口劫持）、SIGTERM 优雅退出、后台快照预热。
    #   一键启动是用户的主入口——入口不带加固，加固就只对 `python deck/deck_server.py` 生效。
    from deck.deck_server import main as _deck_main
    _deck_main()


if __name__ == "__main__":
    main()
