# -*- coding: utf-8 -*-
"""dsh_log.py — 静默异常统一记录（2026-11-26）

全项目 337 处 `except Exception: pass` 改为「记录日志后继续降级」：
行为完全不变，但每处失败都有据可查。

特性：
- 专用 logger `dsh.swallowed`（setLevel(DEBUG)、propagate=False），不污染 root logger / 控制台
- 输出 `logs/swallowed_exceptions.log`（可用环境变量 DSH_SWALLOW_LOG 覆盖）
- 限流：同一位置前 3 次全记，之后每 100 次记一条（防 live_api 等热路径日志爆炸）
- 日志基础设施自身失败时静默（最后防线，不纳入清理范围）

用法（由批量脚本自动注入到各文件）：
    try:
        from dsh_log import swallow as _dsh_swallow
    except ImportError:
        ...  # 向上找 dsh_log.py
    ...
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "deck/live_api.py:232 live_factors")
"""
import logging
import os
import threading
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_LOG = logging.getLogger("dsh.swallowed")
_LOG.setLevel(logging.DEBUG)
_LOG.propagate = False

_COUNTS = {}
_LOCK = threading.Lock()
_HANDLER = None


def _ensure_handler():
    """惰性创建 FileHandler（避免 import 时就写日志目录）。"""
    global _HANDLER
    if _HANDLER is not None:
        return
    p = Path(os.environ.get("DSH_SWALLOW_LOG",
                            str(_BASE / "logs" / "swallowed_exceptions.log")))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(p, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _LOG.addHandler(h)
        _HANDLER = h
    except Exception:
        pass   # 最后防线：日志基础设施失败绝不影响业务


def swallow(exc, where):
    """记录被吞掉的异常（按 where 限流）。

    exc: 异常对象；where: "相对路径:行号 函数名" 定位串。
    """
    key = str(where)
    with _LOCK:
        n = _COUNTS.get(key, 0) + 1
        _COUNTS[key] = n
    # 前 3 次全记；之后每 100 次记一条
    if n > 3 and n % 100 != 0:
        return
    try:
        _ensure_handler()
        _LOG.debug("%s x%d | %s: %s", key, n, type(exc).__name__, exc)
    except Exception:
        pass   # 同上，最后防线
