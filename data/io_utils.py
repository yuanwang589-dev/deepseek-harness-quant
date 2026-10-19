# -*- coding: utf-8 -*-
"""data/io_utils.py — 集中式 IO / sqlite 连接管理（★2026-10-15 资源泄漏收口）

为什么要专门建这个模块
----------------------
全库原有 19 处这样的写法（本轮扫描结果）：

    d = json.load(open(f, encoding="utf-8"))
    _d = json.loads(open(fs[-1], encoding="utf-8").read())
    html = open(f, encoding="utf-8").read()

文件句柄不关闭，全靠 GC 兜底。在**7×24 常驻、每秒多请求**的 deck 服务里，
句柄泄漏会累积到 Windows 默认限制，届时表现为：

  - 「莫名打不开文件」
  - sqlite 报 `database is locked`
  - 端口/句柄耗尽后服务假死

而**没有任何报错会指向真正的泄漏点**——本次只是跑了一遍测试，就同时看到 4 个
ResourceWarning。同类问题：`sqlite3.connect()` 写在 try 内、`close()` 也在 try 内，
一旦 `execute()` 抛异常连接就永久泄漏（deck/live_api.py 原有 2 处正是如此）。

本模块把这些模式统一收口，纯标准库实现，不引入第三方依赖。

用法
----
    from data.io_utils import read_json, read_text, write_json_atomic, sqlite_ro

    d = read_json(path)                       # 语义等同 json.load(open(...))，但句柄必关
    with sqlite_ro(db_path) as con:           # 退出必关，异常也关
        rows = con.execute(sql).fetchall()
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]

_ENC_VIOLATIONS = str.maketrans({"?": "%3f", "#": "%23"})


def read_text(path: PathLike, encoding: str = "utf-8") -> str:
    """读文本，句柄必关。语义与 open(path, encoding=...).read() 完全一致（缺文件照样抛）。"""
    with open(path, "r", encoding=encoding) as f:
        return f.read()


def read_json(path: PathLike, encoding: str = "utf-8") -> Any:
    """读 JSON，句柄必关。语义与 json.load(open(...)) 完全一致（缺文件/坏 JSON 照样抛）。"""
    with open(path, "r", encoding=encoding) as f:
        return json.load(f)


def read_json_safe(path: PathLike, default: Any = None, encoding: str = "utf-8") -> Any:
    """容错版读 JSON：文件不存在 / JSON 坏 / 权限不足 → 返回 default，不抛异常。

    适用于「这些文件缺失是常态」（流水线未跑、产物被清理）的展示类路径，
    避免一页因单个缺文件整体 500。
    """
    try:
        with open(path, "r", encoding=encoding) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
        return default


def write_json_atomic(path: PathLike, obj: Any, encoding: str = "utf-8",
                      ensure_ascii: bool = False, indent: int = 1) -> Path:
    """原子写 JSON：先写临时文件 fsync 再 os.replace。

    解决「半写文件被并发读者读到残 JSON」——deck 服务多线程同时读 portfolio /
    signals 的时序问题；原作者靠写两份（固定名 + 时间戳）绕开，这里从根上闭合。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding=encoding) as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p


def write_bytes_atomic(path: PathLike, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p


@contextmanager
def sqlite_ro(path: PathLike, timeout: float = 5.0, immutable: bool = True):
    """只读 sqlite 连接上下文管理器：退出必关，异常也关。

    immutable=True 走 `mode=ro&immutable=1`，比普通只读快一个数量级（跳 SQLite 锁表），
    前提是库文件在打开期间不会被写。若库可能被并发写入（如增量库），传 immutable=False，
    否则会读到缓存旧快照。
    """
    uri = "file:" + str(path).replace("\\", "/").translate(_ENC_VIOLATIONS)
    uri += "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def sqlite_rw(path: PathLike, timeout: float = 10.0):
    """读写 sqlite 连接上下文管理器：退出必关，异常也回滚。

    ★2026-10-18 T3.1：新建连接统一 WAL + busy_timeout——
    读不阻塞写、写不阻塞读（原 rollback journal 下看板读取会被批量写入阻塞）。
    journal_mode 持久化在库文件头，此处设置对旧库是幂等的。
    """
    con = sqlite3.connect(str(path), timeout=timeout)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
