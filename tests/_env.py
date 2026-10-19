# -*- coding: utf-8 -*-
"""tests/_env.py — 测试公共环境

设计约束：
  1. 只用标准库（项目 venv 未装 pytest/coverage），保证 `python -m unittest` 直接可跑。
  2. 所有查询走 `mode=ro&immutable=1` 只读打开，测试绝不写业务库。
  3. 大库（bars.db 1.8GB）只做走索引的轻量查询，避免拖慢测试。
"""
import ast
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # 仓库根 DSHQuant/
CACHE = BASE / "data" / "cache"
LOGS = BASE / "logs"
OUTPUT = BASE / "output"

# 让 `import backtest.bt_runner` 之类的仓库内导入可用（unittest -t . 已加，双保险）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def db_path(name: str) -> Path:
    return CACHE / name


def ro(dbname: str, sql: str, params=()):
    """只读查询。库文件不存在时返回 None（由用例自己决定是否算失败）。"""
    p = db_path(dbname)
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def bars_max_date():
    """bars.db 最新交易日（qfq）。无数据返回 None。"""
    r = ro("bars.db", "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'")
    return r[0][0] if r and r[0] else None


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def latest(pattern: str, folder: Path = None):
    """取 folder 下匹配 pattern 的最新文件（按 mtime），无则 None。"""
    folder = folder or LOGS
    fs = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return fs[-1] if fs else None


def code_only(path) -> str:
    """源码的"纯代码形态"：剥掉注释与文档字符串，按 ast.unparse 规范化空白。

    为什么需要它：注释里描述历史缺陷时必然要提老常量名（如 ST_LIMIT_PCT）、老的
    硬编码公式（如 cost * 2 * 60），直接对全文做字面搜索会误报；assertNotIn 的
    默认 failure message 还会把整份源码 dump 出来。这里用 ast 重建，只留可执行代码；
    unparse 的空格是规范化的（"cost * 2 * 60" 会以这种形态出现），可以做子串断言。
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)
