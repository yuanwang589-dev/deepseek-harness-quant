# -*- coding: utf-8 -*-
"""deck/deck_server.py — Deck 审批界面 + 看板 本地服务器（外包 AI · 2026-08-08）

★用途：浏览器无法直接读写本地 JSON（file:// 下 fetch 被 CORS 拦截），
      用 Python 标准库起一个零依赖小服务器，提供页面 + 读写 API。

用法：
  cd DSHQuant
  NO_PROXY=* ./.venv/Scripts/python.exe deck/deck_server.py [--port 8787]
  浏览器打开 http://127.0.0.1:8787/deck.html      （Deck 审批界面）
               http://127.0.0.1:8787/dashboard.html （看板总览）

API：
  GET  /api/pool        → logs/opportunity_pool.json   机会池 + Pitch 候选
  GET  /api/risk        → logs/stock_risk_map.json     风控地图（BLOCK 名单）
  GET  /api/beneish     → logs/beneish_report.json     Beneish 造假嫌疑
  GET  /api/winrates    → logs/opportunity_winrates.json 机会胜率回测
  GET  /api/decisions   → logs/deck_decisions.json     历史审批记录
  POST /api/decide      → 追加审批记录 {date, code, action, ts, note, target, stop}
  GET  /api/regime      → output/dynamic_regime.json   择时档位
  GET  /api/signal      → output/daily_signal.json     每日信号
  GET  /api/audit       → report/data_audit_report.json 数据健康度
  GET  /api/sim         → logs/sim_tracks.json         模拟盘双轨
"""
import json
import re

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception:
    pass

import os
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# ★2026-10-19 安全修复：路径逃逸防护（路径穿越 H-1/M-4）
#   背景：/dl/ 与 /v2/ 静态路由用字符串拼接 + 黑名单过滤构造路径，
#   `../config/params.yaml` 可读出 output/ 之外的任意文件（含明文 token）。
#   修法：先 resolve() 消解 `..`/符号链接，再用 is_relative_to() 做**包含性**判定，
#   并叠加扩展名白名单（原先 Content-Type 由用户输入的 .endswith 决定）。
#   注意：str.replace("..","") 是黑名单——`//etc/passwd` 形式仍会逃逸（已在 Windows 实测）。
def _safe_join(root: Path, rel: str, allowed_suffixes=None):
    """把用户可控的相对路径安全地拼到 root 下；越界或扩展名不符返回 None。

    root  : 允许的基准目录（会被 resolve）
    rel   : 用户传入的相对路径（可能含 ../、//、绝对路径）
    allowed_suffixes : 允许的扩展名白名单（含点，如 ".xlsx"）；None 表示不限制
    """
    try:
        root_r = root.resolve()
        fp = (root_r / rel).resolve()
        if not fp.is_relative_to(root_r):      # 核心：必须落在 root 之内
            return None
        # 目录本身或符号链接逃逸都要挡住
        if allowed_suffixes is not None and fp.suffix.lower() not in allowed_suffixes:
            return None
        return fp
    except Exception:
        return None
# ★2026-10-18 审查修复：仓库根自举到 sys.path。
#   `python deck/deck_server.py` 时 sys.path[0] 是 deck/，而 live_api.py 需要
#   `from data.io_utils import ...` → ModuleNotFoundError → 36 个 /api/live/* 全 500、
#   启动预热恒跳过（"No module named 'data'"）。此前只有 scripts/watchdog.py 用
#   PYTHONPATH 兜住，任何人按本文件 docstring / 仓库地图直接启动都起不来。
#   这里自举，去掉对调用方环境的隐式依赖（幂等，不改变 import 优先级语义）。
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
LOGS = BASE / "logs"
OUTPUT = BASE / "output"
REPORT = BASE / "report"
DECISIONS = LOGS / "deck_decisions.json"


def _log_slow(msg: str):
    """慢请求日志：stdout（watchdog_server.out）+ logs/deck_slow.log（带 2MB 截断）。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        p = LOGS / "deck_slow.log"
        if p.exists() and p.stat().st_size > 2 * 1024 * 1024:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sector_parquet_hint(e) -> dict:
    """板块研究台缺 output/daily_close.parquet 时的降级响应。★2026-10-18 审查修复。

    该 parquet 只由 data/subj_quant_stats._load_daily_close() 懒生成，sector_research._daily()
    直接 pd.read_parquet 无兜底 → 缺失时 FileNotFoundError → 500 + 把本机绝对路径
    原样回给浏览器（信息泄露，且用户无从下手）。改为 503 + 可执行提示。
    刻意不在请求路径里自动生成：8.6M 行 pivot 会占满请求线程（同 live_funnel 的教训）。
    """
    return {"ok": False,
            "error": "板块研究台依赖的日线透视缓存未生成（output/daily_close.parquet）",
            "hint": "python -c \"from data.subj_quant_stats import _load_daily_close as f; f()\" 生成一次即可",
            "detail": str(e)[:120]}

# ★2026-08-15 并发写锁：ThreadingHTTPServer 多线程下 /api/decide 的
#   读-改-写（读最新 deck_decisions → append → 写新时间戳文件）存在竞争，
#   并发审批会丢记录（实测 10 并发仅存 4 条）→ 串行化整个决策读写区
DECIDE_LOCK = threading.Lock()

# ★2026-10-18 进程级单实例锁句柄（持有到进程退出，见 _acquire_server_lock）
_SRV_LOCK_HANDLE = None


def _acquire_server_lock(port: int) -> bool:
    """独占端口锁：保证同一端口只有一个 deck 实例能进入「启动」阶段。

    ★2026-10-18 为什么在 _port_already_served() 之外还要这一层：
      探测式防双开存在**启动竞态**——实例 A 还在 import（几十秒，deck_server 依赖很重），
      尚未 bind；此时实例 B 探测得到 ConnectionRefusedError → 判定"端口空闲" → 也去 bind。
      而 DeckServer.allow_reuse_address = True（Windows 上等价 SO_REUSEADDR）允许两个
      进程绑同一端口，于是两个都在"监听"，连接只落到其中一个 →
        改代码重启后旧实例继续服务、新实例空跑；请求行为随机不一致。
      本轮实测踩中：同一个 /api/system_live 响应里混着新旧两版字段（scheduled 走旧
      schtasks 解析、next_schedule 走新通路），排查成本极高。
      文件锁把「启动」这段串行化，竞态窗口直接关掉。

    返回 False 表示已有实例持有锁 → 调用方应退出。
    锁机制本身异常时返回 True（fail-open，保服务可用），但会打印原因。
    """
    global _SRV_LOCK_HANDLE
    lock_path = LOGS / f"deck_server_{port}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
    except PermissionError:
        # Windows 字节范围锁是强制锁：另一实例持锁后连 open("a+") 都 EACCES
        # → 这就是"已有实例在跑"的确定性信号，必须拒绝启动（不能 fail-open）
        return False
    except Exception as e:
        print(f"  [单实例锁] 打开锁文件失败（fail-open）: {e}", flush=True)
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
            fh.close()
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"  [单实例锁] 加锁异常（fail-open）: {e}", flush=True)
        _SRV_LOCK_HANDLE = fh
        return True
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except Exception:
        pass
    _SRV_LOCK_HANDLE = fh          # 故意不关闭：持有到进程退出
    return True


def _json_safe(o):
    """★2026-08-13 #326：全局清洗 NaN/Infinity → None（序列化兜底）。
    factor_corr 矩阵含 NaN，Python json.dumps 默认输出非法字面量 `NaN`，
    前端浏览器 JSON.parse 抛 SyntaxError → 整个接口加载失败。
    所有 /api/live 响应统一走这里清洗，杜绝非法 JSON。"""
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_safe(v) for v in o]
    return o


def _latest_page(*patterns: str, fallback: str = "") -> Path:
    """★#269 页面路由统一取数：多前缀 glob 合并取 mtime 最新（生成器输出名与路由名可能脱节——
    S10-S11 短路由改造遗留：/factors 路由读 factors_dash_* 但生成器输出 dashboard_factors_* →
    用户看到旧版）。任一 pattern 匹配即取最新；无匹配回退固定名 fallback。"""
    import glob as _glob
    fs = []
    for pat in patterns:
        fs += [Path(p) for p in _glob.glob(str(BASE / "deck" / pat))]
    if fs:
        return max(fs, key=lambda x: x.stat().st_mtime)
    return BASE / "deck" / fallback if fallback else Path("")


def _latest_decisions() -> Path:
    """★2026-08-11 写保护免疫：deck_decisions 改时间戳文件名（同文件多次写被锁），
    读最新 deck_decisions_*.json；无则返回固定名（不存在时前端得 []）"""
    import glob as _g
    fs = sorted([Path(p) for p in _g.glob(str(LOGS / "deck_decisions_*.json"))],
                key=lambda x: x.stat().st_mtime)
    return fs[-1] if fs else DECISIONS

PORT = 8787


def _latest_pool_path() -> Path:
    """最新含 pitch 候选的机会池（外包 #6 补：scan.py 现输出时间戳文件名，
    且普通扫描 pitch=[]；固定名 opportunity_pool.json 可能落后 1-2 天 →
    Deck 卡片须与 pitch_v2 证据同源。无候选池时回退固定名文件。）"""
    best, best_mt = None, -1.0
    for f in sorted(LOGS.glob("opp_pool_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("pitch"):
            continue
        mt = f.stat().st_mtime
        if mt > best_mt:
            best, best_mt = f, mt
    return best if best is not None else LOGS / "opportunity_pool.json"


# API 路由 → 文件（相对路径；不存在的返回 {"error": "..."}）
ROUTES = {
    "/api/pool": LOGS / "opportunity_pool.json",
    "/api/risk": LOGS / "stock_risk_map_v2.json",   # ★2026-08-10 审计修复：主程序 08-09 起输出切 v2（5202 只全市场），v1(1767 只)已过时
    "/api/beneish": LOGS / "beneish_report.json",
    "/api/winrates": LOGS / "opportunity_winrates.json",
    "/api/decisions": DECISIONS,
    "/api/regime": OUTPUT / "dynamic_regime.json",    "/api/signal": OUTPUT / "daily_signal.json",
    "/api/audit": REPORT / "data_audit_report.json",    "/api/sim": LOGS / "sim_tracks.json",
    "/api/pitch_v2": LOGS / "pitch_v2.json",
    "/api/pool_hist": LOGS / "opportunity_pool_hist.json",
    "/api/pitch_track": LOGS / "pitch_track_pool.json",   # ★2026-08-10 历史 Pitch 远期收益池
    "/api/timing": OUTPUT / "timing_system.json",          # ★2026-08-11 新择时系统（适合买入/谨慎/不适合）
    "/api/take_profit": LOGS / "take_profit_signals.json", # ★2026-08-11 止盈引擎（持仓止盈状态）
    "/api/etf_map": OUTPUT / "etf_map.json",               # ★2026-08-15 ETF 映射（配置类：策略暴露→ETF 相关性+pitch+权重）
}

# ★2026-08-09 安全层同名文件二次写被锁 → 产出方全部改时间戳文件名；
#   静态 ROUTES 找不到时，回退 glob 取最新同前缀文件（如 pitch_v2_20260809_234012.json）
ROUTE_GLOB = {
    "/api/pitch_v2": "pitch_v2_*.json",
    "/api/risk": "stock_risk_map*.json",
    "/api/beneish": "beneish_report*.json",
    "/api/winrates": "opp_winrates_*.json",
    "/api/sim": "sim_tracks*.json",
    "/api/pitch_track": "pitch_track_pool_*.json",   # ★2026-08-10 历史 Pitch 远期收益池
    "/api/timing": "timing_system_*.json",           # ★2026-08-11 新择时系统（时间戳版 glob）
    "/api/take_profit": "take_profit_signals_*.json",# ★2026-08-11 止盈引擎（时间戳版 glob）
    "/api/signal": "daily_signal_*.json",            # ★2026-08-10 时间戳版 glob（固定名被锁）
    "/api/audit": "data_audit_report_*.json",        # ★2026-08-10 时间戳版 glob
    "/api/decisions": "deck_decisions_*.json",       # ★2026-08-11 审批记录时间戳版（写保护免疫）
    "/api/regime": "dynamic_regime*.json",           # ★#144 月度择时档（原固定名，防被锁残留/产出改名）
}


def _resolve_route(path: str) -> Path:
    """★2026-08-09 v2 逻辑：有 ROUTE_GLOB 的路径**优先 glob 最新**（产出方全部改时间戳文件名，
    固定名可能是旧内容/被锁残留）；无 glob 规则时用 ROUTES 静态路径"""
    f = ROUTES.get(path)
    if f is None:
        return None
    pat = ROUTE_GLOB.get(path)
    if pat:
        # ★排除测试/归档文件（test/done/calib 等），取真实产出最新
        # ★2026-08-12 百轮后#124：改 mtime 排序（文件名 ASCII 排序会被 _v2 后缀干扰）
        matches = [m for m in sorted(LOGS.glob(pat), key=lambda x: x.stat().st_mtime)
                   if not any(t in m.name for t in ("test", "done", "calib", "_old"))]
        if not matches and str(f).startswith(str(OUTPUT)):
            # ★2026-08-10 产出在 output/ 的路径（daily_signal 等）也搜 output 目录
            matches = [m for m in sorted(OUTPUT.glob(pat), key=lambda x: x.stat().st_mtime)
                       if not any(t in m.name for t in ("test", "done", "calib", "_old"))]
        if matches:
            return matches[-1]
    # ★2026-10-18 删除死代码：原此处调用未定义的 _latest_opp_pool()（ruff F821）。
    #   /api/pool 实际由 do_GET 的 _latest_pool_path() 分支处理（见 handler），
    #   本分支永不可达；一旦哪天可达就是 NameError→500。删除后行为不变。
    if f.exists():
        return f
    return f



# ─────────────────────────────────────────────────────────────────────
# ★2026-10-15 可靠性加固：服务器层
# 原缺陷（ThreadingHTTPServer 裸用）：
#   A. daemon_threads 默认 False —— 任一请求线程卡住（慢端点 / 客户端半开连接 /
#      前端启动瞬间 6~8 个并发请求抢同一个大库），进程就无法干净退出：Ctrl+C 无效、
#      端口持续被占，而 watchdog 拉起的新实例会撞 "Address already in use" 反复失败
#      → 表现为「服务莫名起不来 / 重启无效」。
#   B. 无 handle_error —— 工作线程未捕获异常时，默认实现只往 stderr 打一遍 traceback，
#      **不给客户端发任何响应**，浏览器/前端一直挂到自身超时 → 表现为「连接假死」。
#   C. request_queue_size 默认 5 —— listen backlog 太小，前端集中刷新时直接拒连。
#
# 修法：自定义 DeckServer（守护线程 + 雪崩时回 500 + 大 backlog + 错误落盘可观测）。
# ─────────────────────────────────────────────────────────────────────
class DeckServer(ThreadingHTTPServer):
    daemon_threads = True          # A：工作线程改守护，卡住也不阻塞进程退出
    allow_reuse_address = True     # A：关服后立刻重启不再撞 WSAEADDRINUSE
    request_queue_size = 64        # C：listen backlog 5 → 64

    # 近 60s 未捕获异常时间戳（客户端主动断连不计入；供 /api/live/server_errors 观测）
    _err_window = []

    # ★2026-10-15 未捕获异常日志路径。做成类属性（可被实例/子类覆盖）是为了让测试
    #   把合成异常重定向到临时文件——否则每跑一轮测试都往真实观测面写噪音，
    #   运维看 logs/deck_server_errors.log 时分不清哪些是真异常。
    err_log = LOGS / "deck_server_errors.log"

    # 客户端主动断开的行为/码：刷新、关标签页、无头工具超时。非服务缺陷，静默跳过。
    _CLIENT_ABORT_CODES = {10053, 10054, 10058, 10060, 10051, 10004}

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if getattr(exc, "code", None) in self._CLIENT_ABORT_CODES or isinstance(
                exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        now = time.time()
        self._err_window[:] = [t for t in self._err_window if now - t < 60.0]
        self._err_window.append(now)
        summary = f"{type(exc).__name__}: {exc}"
        try:
            _lf = Path(self.err_log)
            _lf.parent.mkdir(parents=True, exist_ok=True)
            with open(_lf, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {client_address or '?'} "
                        f"{summary}\n")
        except Exception:
            pass
        sys.stderr.write(f"[deck] 工作线程未捕获异常 → {summary}\n")
        # B：尽力回 500，绝不让客户端悬挂等超时
        try:
            request.sendall(b"HTTP/1.0 500 Internal Server Error\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: 21\r\n"
                            b"Cache-Control: no-store\r\n\r\n"
                            b'{"error":"internal"}')
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    # ★2026-10-15：单请求 socket 超时。防客户端半开连接长期占用工作线程
    # （配合 DeckServer.daemon_threads，双保险）。
    timeout = 120
    # ★2026-08-10 个股检测缓存（类变量：跨请求共享；每请求实例会重置实例属性）
    _sc_cache = {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[deck] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            # ★2026-08-15 客户端中止容错：浏览器刷新/关页/无头工具超时等会 10053 中止连接，
            #   写响应抛异常——静默吞掉（单请求失败不影响服务；此前会在日志刷整页 traceback）
            pass

    def _send_json(self, obj, code: int = 200):
        try:
            obj = _json_safe(obj)
        except Exception:
            pass
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _redirect(self, location: str):
        """★U1-4 301 合并：旧页路由 → 新页（浏览器自动跳转，老链接/书签仍可用）"""
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _v2_page(self, name: str, missing_txt: str):
        """★V8 路由切换（#292）：读 ui_v2/pages/{name}（API 驱动静态页——改文件即生效，无时间戳机制）"""
        f = BASE / "ui_v2" / "pages" / name
        return self._send(200, f.read_bytes() if f.exists() else missing_txt.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ★2026-10-18 借 GitHub 惯例：请求计时 + 慢请求日志（Starlette/Grafana 式中间件的最小等价物）
    #   stdlib http.server 没有中间件，但 do_GET 就是唯一入口，包一层即可。
    #   价值：本轮 P0（funnel 72s 拖死全站）是"没有慢请求可观测性"才拖了三天没被发现——
    #   上线后只要某个接口变慢，日志里立刻出现 SLOW，不用再等用户报"看板打不开"。
    SLOW_S = float(os.environ.get("DECK_SLOW_S", "1.0"))
    VERY_SLOW_S = 10.0

    def _require_auth(self):
        """DECK_API_TOKEN 未设置 → 不启用鉴权（保持本地现状）。
        设置后，所有非探针请求须带 `Authorization: Bearer <token>`，否则回 401。
        返回 True 表示『需要鉴权且未通过，已回 401』。"""
        tok = os.environ.get("DECK_API_TOKEN")
        if not tok:
            return False
        if self.headers.get("Authorization", "") == "Bearer " + tok:
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": "unauthorized"}).encode("utf-8"))
        return True

    def do_GET(self):
        t0 = time.time()
        try:
            return self._do_GET_impl()
        finally:
            dt = time.time() - t0
            if dt >= self.VERY_SLOW_S:
                _log_slow(f"VERYSLOW {dt:6.1f}s  GET {self.path}")
            elif dt >= self.SLOW_S:
                _log_slow(f"SLOW     {dt:6.1f}s  GET {self.path}")

    def _do_GET_impl(self):
        path = self.path.split("?")[0]
        # ★2026-10-19 H-5 修复：启用 DECK_API_TOKEN 时，对所有非探针 GET 要求 Bearer 鉴权
        if path not in ("/healthz", "/readyz") and self._require_auth():
            return
        # ★2026-10-18 探针（借 Kubernetes liveness/readiness 语义，见 deck/health.py 顶部说明）
        #   /healthz 零 I/O，只回进程内状态 —— watchdog 用它判"该不该重启"。
        #   /readyz  查依赖与产物新鲜度 —— 只用于"能不能服务/数据新不新"，**绝不触发重启**。
        if path == "/healthz":
            try:
                from deck.health import liveness
                return self._send_json(liveness())
            except Exception as e:
                return self._send_json({"status": "ok", "degraded_probe": str(e)[:80]})
        if path == "/readyz":
            try:
                from deck.health import readiness
                r = readiness()
                return self._send_json(r, 200 if r["status"] != "failed" else 503)
            except Exception as e:
                return self._send_json({"status": "failed", "ok": False,
                                        "errors": [{"name": "readyz", "detail": str(e)[:120]}]}, 503)
        if path == "/api/live/server_errors":
            # ★2026-10-15 服务端未捕获异常观测面（近 60s 计数 + 日志尾部）。
            #   客户端主动断连不计入；count>0 说明有真异常，需看 logs/deck_server_errors.log。
            _ew = getattr(self.server, "_err_window", []) or []
            _tail = []
            _lf = Path(getattr(self.server, "err_log", LOGS / "deck_server_errors.log"))
            if _lf.exists():
                try:
                    _tail = _lf.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-15:]
                except Exception:
                    _tail = []
            return self._send_json({"ok": True, "recent_60s": len(_ew),
                                    "log": "logs/deck_server_errors.log",
                                    "recent_lines": _tail})
        if path == "/api/portfolio":     # ★T-2：持仓（strategy/portfolio.py）
            try:
                sys.path.insert(0, str(BASE))
                from strategy.portfolio import _load
                return self._send_json(_load())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/pool":          # ★外包 #6：读最新含候选池（与 /api/pitch_v2 同源）
            f = _latest_pool_path()
            if not f.exists():
                return self._send_json({"error": f"机会池不存在: {f.name}", "hint": "先运行 python factors/opportunities/scan.py --pitch"}, 404)
            try:
                return self._send_json(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/":   # ★V8 路由切换（#292）：门户 → ui_v2/pages/portal.html（API 驱动静态页，旧 portal_* 模板退役）
            return self._v2_page("portal.html", "portal missing")
        if path == "/deck.html":   # ★U1-4 301 合并：旧审批台 → /pitch.html（价值+科技并列新审批页）
            return self._redirect("/pitch.html")
        if path == "/index.html":   # ★U1-4 孤儿页 → 门户
            return self._redirect("/")
        if path == "/pitch.html" or path == "/pitch":   # ★V8 路由切换（#292）：决策页 → ui_v2/pages/pitch.html（6+1 Tab + 审批操作）
            return self._v2_page("pitch.html", "pitch missing")
        if path == "/holdings" or path == "/holdings_dash.html":   # ★V8 路由切换（#292）：持仓页 → ui_v2/pages/holdings.html（KPI+分组+卖出）
            return self._v2_page("holdings.html", "holdings missing")
        if path == "/factors" or path == "/factors_dash.html":   # ★V8 路由切换（#292）：因子池页 → ui_v2/pages/factors.html（拥挤度+健康+推荐质量）
            return self._v2_page("factors.html", "factors missing")
        if path == "/help" or path == "/help_dash.html":   # ★V8 路由切换（#292）：说明页 → ui_v2/pages/help.html（Pitch 原理+数据流通+术语）
            return self._v2_page("help.html", "help missing")
        if path == "/report" or path == "/report.html":   # ★#298 日报页（v2：读 /api/daily_report 展示自动生成的日报）
            return self._v2_page("report.html", "report missing")
        if path == "/data" or path == "/data.html":   # ★#310 数据资产页（v2：读 /api/live/data_assets 展示 unified.db 可读性）
            return self._v2_page("data.html", "data missing")
        if path == "/backtest" or path == "/backtest.html":   # ★2026-08-14 回测档案页（读 /api/live/backtest_archive 列出可视化+存档）
            return self._v2_page("backtest.html", "backtest missing")
        if path == "/subj_quant" or path == "/subj_quant.html":   # ★2026-08-14 主观量化融合系统（相对独立页面）
            return self._v2_page("subj_quant.html", "subj_quant missing")
        if path == "/control" or path == "/control.html":   # ★2026-08-15 DeepSeek HARNESS 控制台（harness 状态/目标/轮次）
            return self._v2_page("control.html", "control missing")
        if path == "/etf" or path == "/etf.html":   # ★2026-08-15 ETF 映射页（配置类：策略暴露→ETF 相关性/pitch/小资金配置）
            return self._v2_page("etf.html", "etf missing")
        if path == "/pitchtrack" or path == "/pitchtrack.html":   # ★2026-08-15 主观多池远期（5 池对比 + 牛散决策者分组）
            return self._v2_page("pitchtrack.html", "pitchtrack missing")
        if path == "/api/harness":   # ★2026-08-16 连接状态：实时探测 :3080 + 合并可选快照（不再因快照缺失返 404）
            # ★修复（#v1.0.9 复审）：旧实现在 output/harness_state.json 缺失时返回 404，而该文件全项目无生产者
            #   （注释称"agent 维护"，DSH_HOME 无工作区/会话时 agent 从未运行）→ /control「连接状态」卡片
            #   长期显示红字「读取失败：Error: HTTP 404」。现改为：
            #   ① 实时探测 :3080（/quantapi/sessions，桥接插件提供）判定在线 + 桥接是否加载；
            #   ② 快照存在则合并其 agent/goal/rounds/links（agent 维护的自主推进信息）；
            #   ③ 无论如何返回 200，缺失字段交给前端 R._f() 兜底为 '—'。
            _probe = {}
            _online = False           # :3080 是否在监听
            _bridge = False           # 桥接插件是否已加载（决定对话/会话管理能否用）
            import urllib.request as _ur
            # ★2026-10-15 必须显式绕开系统代理：有 http_proxy 时对 127.0.0.1 的探测
            #   会被代理应答 → HARNESS 离线也被误报成在线，「连接状态」卡片长期假绿。
            _hopener = _ur.build_opener(_ur.ProxyHandler({}))
            try:
                _pr = _hopener.open(_ur.Request("http://127.0.0.1:3080/quantapi/sessions"), timeout=3)
                _probe = json.loads(_pr.read().decode("utf-8")) or {}
                _online = True
                _bridge = isinstance(_probe.get("sessions"), list)
            except Exception:
                _online = False
            if not _online:   # 桥接不可用时退回探测首页：仍能判定"HARNESS 在跑但桥接缺失"
                try:
                    _hopener.open(_ur.Request("http://127.0.0.1:3080/"), timeout=3).read(1)
                    _online = True
                except Exception:
                    _online = False
            # 活跃根会话（current=True）= 前端「主系统」会话；headless 建的历史会话不算活跃
            _root_ids = [str(s.get("id")) for s in (_probe.get("sessions") or [])
                         if isinstance(s, dict) and s.get("id") and s.get("current")]
            # 版本号：:3080 首页 __DSH_BOOT__ 里的 rev（探测失败不影响在线判定）
            _rev = None
            if _online:
                try:
                    import urllib.request as _ur
                    _page = _ur.urlopen(_ur.Request("http://127.0.0.1:3080/"), timeout=3).read().decode("utf-8", "replace")
                    _m = re.search(r'"rev"\s*:\s*"([0-9a-fA-F]+)"', _page)
                    _rev = _m.group(1) if _m else None
                except Exception:
                    _rev = None
            # 框架版本：读 harness/package.json（文件驱动，不写死版本号）
            _fw = "DeepSeek HARNESS"
            try:
                _pj = json.loads((BASE / "harness" / "package.json").read_text(encoding="utf-8"))
                _dv = (_pj.get("dependencies") or {}).get("@deepseek-ai/dsh")
                if _dv:
                    _fw = "DeepSeek HARNESS (dsh " + str(_dv) + ")"
            except Exception:
                pass
            _snap = {}
            try:
                _hs = BASE / "output" / "harness_state.json"
                if _hs.exists():
                    _snap = json.loads(_hs.read_text(encoding="utf-8")) or {}
            except Exception:
                _snap = {}
            _sh = _snap.get("harness") or {}
            _out = dict(_snap)
            _out["harness"] = {
                "online": _online,
                "name": _sh.get("name") or "DeepSeek HARNESS",
                "rev": _sh.get("rev") or _rev,
                "updated_at": _sh.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "endpoint": "http://127.0.0.1:3080",
                "bridge": _bridge,
                "root_ids": _root_ids,
            }
            _ag = dict(_snap.get("agent") or {})
            _ag.setdefault("framework", _fw)
            _ag.setdefault("session_id", _root_ids[0] if _root_ids else None)
            _out["agent"] = _ag
            _out.setdefault("goal", {})
            _out.setdefault("rounds", [])
            _out.setdefault("links", {})
            if not _online:
                _out["hint"] = "HARNESS :3080 未响应 —— 双击 启动DSHQuant.cmd 启动"
            elif not _bridge:
                _out["hint"] = "HARNESS 在运行，但量化桥接插件未加载 —— 检查 harness/home/profiles/web/cordis.patch.yml"
            elif not _snap:
                _out["hint"] = "HARNESS 在线；output/harness_state.json 尚未生成（由 :3080 的 agent 维护，缺省不影响连接）"
            return self._send_json(_out)
        if path == "/api/build_mode":   # ★2026-08-16 构建模式（dev=开发版徽章 / release=公测发布版无徽章；config/ui_build.json 驱动）
            try:
                _bm = BASE / "config" / "ui_build.json"
                if _bm.exists():
                    _cfg = json.loads(_bm.read_text(encoding="utf-8"))
                    return self._send_json({
                        "mode": _cfg.get("mode", "release"),
                        "label": _cfg.get("label", "DEV 开发版"),
                        "hint": _cfg.get("hint", "内部测试构建，非公测发布版"),
                    })
                return self._send_json({"mode": "release"})
            except Exception as e:
                return self._send_json({"mode": "release", "error": str(e)})
        if path == "/api/assets/list":   # ★2026-08-16 资产导入源列表（skills + agent presets，来自 assets/）
            try:
                _sk = BASE / "assets" / "skills"
                _pr = BASE / "assets" / "presets"

                def _desc(md_path):
                    try:
                        for ln in md_path.read_text(encoding="utf-8", errors="replace").splitlines():
                            if ln.startswith("description:"):
                                return ln.split(":", 1)[1].strip().strip('"').strip("'")[:120]
                    except Exception:
                        pass
                    return ""

                skills = []
                if _sk.exists():
                    for d in sorted(_sk.iterdir()):
                        if d.is_dir():
                            md = d / "SKILL.md"
                            skills.append({"name": d.name, "desc": _desc(md) if md.exists() else ""})
                presets = []
                if _pr.exists():
                    for d in sorted(_pr.iterdir()):
                        if d.is_dir():
                            pm = d / "preset.yml"
                            desc = ""
                            if pm.exists():
                                try:
                                    for ln in pm.read_text(encoding="utf-8", errors="replace").splitlines():
                                        if ln.startswith("name:"):
                                            desc = ln.split(":", 1)[1].strip()[:120]
                                            break
                                except Exception:
                                    pass
                            presets.append({"name": d.name, "desc": desc})
                return self._send_json({"ok": True, "skills": skills, "presets": presets})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path.startswith("/api/proxy/"):   # ★2026-08-16 反向代理 GET → :3080 HARNESS（前端同源访问，零跨源）
            import urllib.request as _ur
            import urllib.error as _ue
            _tgt = "http://127.0.0.1:3080/" + path[len("/api/proxy/"):]
            if "?" in self.path:
                _tgt += "?" + self.path.split("?", 1)[1]
            try:
                _r = _ur.urlopen(_ur.Request(_tgt), timeout=40)
                _data = _r.read()
                try:
                    return self._send_json(json.loads(_data.decode("utf-8")))
                except Exception:
                    self.send_response(_r.status)
                    self.send_header("Content-Type", _r.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    self.wfile.write(_data)
                    return
            except _ue.HTTPError as e:
                _d = e.read()
                try:
                    return self._send_json(json.loads(_d.decode("utf-8")), e.code)
                except Exception:
                    return self._send_json({"ok": False, "error": "proxy upstream " + str(e.code)}, e.code)
            except Exception as e:
                return self._send_json({"ok": False, "error": "proxy: " + str(e)}, 502)
        if path == "/api/subj_quant":   # ★2026-08-14 主观量化融合系统状态（标签+否决+胜率徽章）
            try:
                sys.path.insert(0, str(BASE))
                from data.subj_quant import get_state
                return self._send_json(get_state())
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/sector_research/sectors":   # ★2026-08-14 板块研究台：83 大类列表
            try:
                sys.path.insert(0, str(BASE))
                from data.sector_research import list_sectors
                return self._send_json({"ok": True, "sectors": list_sectors()})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/sector_research/analyze":   # ★2026-08-14 板块研究台：板块择时 + 板块内强因子
            try:
                import urllib.parse
                _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                code = (_q.get("code") or [""])[0]
                if not code:
                    return self._send_json({"ok": False, "error": "需要 code 参数"}, 400)
                sys.path.insert(0, str(BASE))
                from data.sector_research import (sector_timing, sector_strong_factors,
                                                  sector_pitch, sector_retail, sector_crowd)
                timing = sector_timing(code)
                factors = sector_strong_factors(code)
                pitch = sector_pitch(code, strong_factors=factors)
                retail = sector_retail(code)
                crowd = sector_crowd(code)
                return self._send_json({"ok": True, "code": code,
                                        "timing": timing, "factors": factors,
                                        "pitch": pitch, "retail": retail, "crowd": crowd})
            except FileNotFoundError as e:
                return self._send_json(_sector_parquet_hint(e), 503)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/sector_research/stock_search":   # ★2026-08-14 板块研究台：股票搜索
            try:
                import urllib.parse
                _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                kw = (_q.get("q") or [""])[0]
                sys.path.insert(0, str(BASE))
                from data.sector_research import stock_search
                return self._send_json({"ok": True, "stocks": stock_search(kw)})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/sector_research/stock":   # ★2026-08-14 板块研究台：个股诊断（命中因子+命中率）
            try:
                import urllib.parse
                _q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                code = (_q.get("code") or [""])[0]
                if not code:
                    return self._send_json({"ok": False, "error": "需要 code 参数"}, 400)
                sys.path.insert(0, str(BASE))
                from data.sector_research import stock_factors
                return self._send_json(stock_factors(code))
            except FileNotFoundError as e:
                return self._send_json(_sector_parquet_hint(e), 503)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/live/backtest_archive":   # ★2026-08-14 回测档案（latest 当前 + history 历史）
            try:
                sys.path.insert(0, str(BASE))
                from backtest.bt_report import list_archives
                return self._send_json({"ok": True, **list_archives()})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/live/backtest_strategies":   # ★2026-08-16 回测策略目录（前端菜单动态生成：筛选/选择）
            try:
                sys.path.insert(0, str(BASE))
                from backtest.bt_runner import list_strategies
                return self._send_json({"ok": True, "strategies": list_strategies()})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/live/backtest_run":   # ★2026-08-14 动态回测（因子页「回测」Tab 按参数即时跑）
            try:
                import urllib.parse
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                def _g(k, d):
                    v = (q.get(k) or [d])[0]
                    return v
                sys.path.insert(0, str(BASE))
                from backtest.bt_runner import run_backtest
                r = run_backtest(strategy=_g("strategy", "tech3"),
                                 topn=int(_g("topn", "5")),
                                 stocks=int(_g("stocks", "300")),
                                 start=_g("start", "2021-01-01"),
                                 end=_g("end", "2025-12-31"))
                return self._send_json({"ok": True, **r})
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/dashboard.html":   # ★U1-4 301 合并：旧静态系统看板 → /dashboard_live.html（实时系统状态面板）
            return self._redirect("/dashboard_live.html")
        if path == "/dashboard_opp.html":   # ★2026-08-10 机会池看板（UI A4 核心，总指导落地；glob 取最新时间戳版）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_opp_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_opp.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_opp.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_pool.html":   # ★2026-08-11 P-1 去重：合并页废弃 → 301 原页（观察池）
            return self._redirect("/dashboard_watch.html")
        if path == "/dashboard_monitor.html":   # ★2026-08-11 P-1 去重：合并页废弃 → 301 原页（系统实时）
            return self._redirect("/dashboard_live.html")
        if path == "/dashboard_research.html":   # ★2026-08-11 P-1 去重：合并页废弃 → 301 原页（回测证据）
            return self._redirect("/dashboard_backtest.html")
        if path == "/dashboard_factors.html":   # ★2026-08-10 因子监控页（UI B4 核心，总指导落地）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_factors_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_factors.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_factors.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_watch.html":   # ★2026-08-10 观察池页（UI B1 核心，总指导落地）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_watch_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_watch.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_watch.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_research_lib.html":   # ★2026-08-11 P-6 研究成果库（AI-1，避开 P-1 301 的 research 名）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_research_lib_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_research_lib.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_research_lib.html missing", "text/html; charset=utf-8")
        if path == "/system_overview.html":   # ★2026-08-11 百轮#50 系统全景（50 轮里程碑）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "system_overview_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "system_overview.html"
            return self._send(200, p.read_bytes() if p.exists() else b"system_overview.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_dynamic.html":   # ★2026-08-11 P-3 动态因子面板（总指导，问题 6/8）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_dynamic_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_dynamic.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_dynamic.html missing", "text/html; charset=utf-8")
        if path == "/timing_dash" or path == "/timing_dash.html":   # ★2026-08-12 #155 择时面板深度打磨（深色侧边栏 + 浅色内容 + KPI 卡 + sparkline）；#218 补 /timing_dash 裸路径（与 /holdings /factors /help 对齐）
            # ★#269：timing_dash 无独立生成器（S 系列遗留固定名），保持固定名兜底
            p = _latest_page("timing_dash_*.html", fallback="timing_dash.html")
            return self._send(200, p.read_bytes() if p.exists() else b"timing_dash.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_holdings.html":   # ★#363 旧持有池页 → 301 重定向到新 v2 持仓页（#292 已切 v2，旧 URL 直接跳新页）
            return self._redirect("/holdings")
        if path == "/dashboard_pitchtrack.html":   # ★2026-08-10 历史 Pitch 远期收益池页
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_pitchtrack_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_pitchtrack.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_pitchtrack.html missing", "text/html; charset=utf-8")
        # ★U2 新增三页（外包 AI-1 2026-08-10）：竞价信号/操作历史/术语表
        if path in ("/dashboard_auction.html", "/dashboard_history.html", "/dashboard_glossary.html"):
            import glob as _glob
            name = path.strip("/").removesuffix(".html")
            files = sorted(_glob.glob(str(BASE / "deck" / f"{name}_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / f"{name}.html"
            return self._send(200, p.read_bytes() if p.exists() else b"page missing", "text/html; charset=utf-8")
        if path == "/api/system_live":   # ★2026-08-10 实时系统状态（动态聚合，dashboard_live.html 每 5s 轮询）
            try:
                sys.path.insert(0, str(BASE / "deck"))
                from system_live import collect
                return self._send_json(collect())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/manual_update":   # ★2026-08-10 手动全域更新（POST；自动程序运行中自动拒绝）
            try:
                sys.path.insert(0, str(BASE))
                from data.manual_update import start
                return self._send_json(start())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/update_status":   # ★2026-08-10 手动更新状态（busy/上次结果/日志）
            try:
                sys.path.insert(0, str(BASE))
                from data.manual_update import status
                return self._send_json(status())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/live_patch.js":   # ★F5 页面 API 化统一实时层（总指导 2026-08-10）
            f = BASE / "deck" / "live_patch.js"
            return self._send(200, f.read_bytes() if f.exists() else b"live_patch.js missing", "application/javascript; charset=utf-8")
        if path == "/ui_common.css":   # ★U1-5 统一导航条样式（总指导 2026-08-10）
            f = BASE / "deck" / "ui_common.css"
            return self._send(200, f.read_bytes() if f.exists() else b"ui_common.css missing", "text/css; charset=utf-8")
        if path == "/anim_common.css":   # ★#156 StyleKit 动画公共库（用户素材站，全站一致性动画）
            f = BASE / "deck" / "anim_common.css"
            return self._send(200, f.read_bytes() if f.exists() else b"anim_common.css missing", "text/css; charset=utf-8")
        if path == "/nav_common.js":   # ★U1-5 统一导航条渲染
            f = BASE / "deck" / "nav_common.js"
            return self._send(200, f.read_bytes() if f.exists() else b"nav_common.js missing", "application/javascript; charset=utf-8")
        if path == "/v2" or path == "/v2/":   # ★#366 旧骨架页 → 301 门户（index.html 是"V2 建设中"历史残留，主入口已切 /）
            return self._redirect("/")
        if path.startswith("/v2/"):   # ★2026-08-13 UI v2 静态路由（ui_v2/ 目录——API 驱动静态页，无生成器机制）
            _rel = path[len("/v2/"):]
            # ★2026-10-19 安全修复：原 `.replace("..","")` 是黑名单，`/v2/../../etc/passwd`
            #   过滤后成 `//etc/passwd`，在 Windows 解析为 UNC 根 → 逃逸 ui_v2（已实测）。
            #   改为 _safe_join 包含性判定；静态资源白名单含 css/js/html/svg/png/ico/json。
            _sv = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                   ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                   ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".gif": "image/gif", ".ico": "image/x-icon",
                   ".json": "application/json; charset=utf-8", ".map": "application/json; charset=utf-8",
                   ".woff": "font/woff", ".woff2": "font/woff2"}
            f = _safe_join(BASE / "ui_v2", _rel, allowed_suffixes=tuple(_sv.keys()))
            if f is not None and f.is_file():
                return self._send(200, f.read_bytes(), _sv.get(f.suffix.lower(), "text/html; charset=utf-8"))
            return self._send(404, b"ui_v2 404", "text/plain; charset=utf-8")
        if path == "/stock_link.js":   # ★2026-08-12 用户需求#181：全站个股代码联动组件（点击弹全息）
            f = BASE / "deck" / "stock_link.js"
            return self._send(200, f.read_bytes() if f.exists() else b"stock_link.js missing", "application/javascript; charset=utf-8")
        if path == "/live_ticker.js":   # ★#166 动态行情条 + 信息轮播（用户"像金融系统文字一直在动"）
            f = BASE / "deck" / "live_ticker.js"
            return self._send(200, f.read_bytes() if f.exists() else b"live_ticker.js missing", "application/javascript; charset=utf-8")
        if path == "/chain_status.js":   # ★2026-08-11 百轮#16 决策链状态组件
            f = BASE / "deck" / "chain_status.js"
            return self._send(200, f.read_bytes() if f.exists() else b"chain_status.js missing", "application/javascript; charset=utf-8")
        if path == "/factor_perf.js":   # ★2026-08-13 #275 因子推荐质量渲染（独立 JS 避免 f-string 转义）
            f = BASE / "deck" / "factor_perf.js"
            return self._send(200, f.read_bytes() if f.exists() else b"factor_perf.js missing", "application/javascript; charset=utf-8")
        if path == "/api/live/forward" or path == "/api/live/factors" \
                or path == "/api/live/audit" or path == "/api/live/pools" \
                or path == "/api/live/ranks" or path == "/api/live/pool" \
                or path == "/api/live/opp" or path == "/api/live/watch" \
                or path == "/api/live/holdings" or path == "/api/live/tech" \
                or path == "/api/live/chain" or path == "/api/live/review" \
                or path == "/api/live/alerts" or path == "/api/live/funnel" \
                or path == "/api/live/actions" or path == "/api/live/brief" \
                or path == "/api/live/calendar" or path == "/api/live/validation" \
                or path == "/api/live/strong_hits" \
                or path == "/api/live/timing_dash" \
                or path == "/api/live/factor_dash" \
                or path == "/api/live/portal_dash" \
                or path == "/api/live/factor_perf" \
                or path == "/api/live/enums" \
                or path == "/api/live/data_assets" \
                or path == "/api/live/factor_ranking" \
                or path == "/api/live/db_view" \
                or path == "/api/live/factor_ui_pack" \
                or path == "/api/live/rotation_calendar" \
                or path == "/api/live/auction" \
                or path == "/api/live/realtime" \
                or path == "/api/live/endpoints" \
                or path == "/api/live/turnlow_top" \
                or path == "/api/live/wufu_rotation" \
                or path == "/api/live/chain_refresh":   # ★#310 数据资产 + #313 因子排名 + #319 数据库面板 + #323 因子池UI数据包 + #329 轮动日历 + #339 决策链实时更新 + #424 竞价因子 + #2026-08-14 盘中实时行情 + API接口状态 + #2026-08-15 turn_low 防守主力 + #2026-08-16 五福轮动摆件
            try:
                sys.path.insert(0, str(BASE / "deck"))
                import live_api
                _fn = path.split("/")[-1]
                if _fn == "ranks":
                    from data.rank_live import compute
                    return self._send_json(compute())
                fn = {"forward": live_api.live_forward, "factors": live_api.live_factors,
                      "audit": live_api.live_audit, "pools": live_api.live_pools,
                      "opp": live_api.live_opp, "watch": live_api.live_watch,
                      "holdings": live_api.live_holdings, "tech": live_api.live_tech,
                      "pool": live_api.live_pool, "chain": live_api.live_chain,
                      "review": live_api.live_review, "alerts": live_api.live_alerts,
                      "funnel": live_api.live_funnel, "actions": live_api.live_actions,
                      "brief": live_api.live_brief, "calendar": live_api.live_calendar,
                      "validation": live_api.live_validation,
                      "strong_hits": live_api.live_strong_hits,
                      "timing_dash": live_api.live_timing_dash,
                      "factor_dash": live_api.live_factor_dash,
                      "portal_dash": live_api.live_portal_dash,
                      "factor_perf": live_api.live_factor_perf,
                      "enums": live_api.live_enums,
                      "data_assets": live_api.live_data_assets,
                      "factor_ranking": live_api.live_factor_ranking,
                      "db_view": live_api.live_db_view,
                      "factor_ui_pack": live_api.live_factor_ui_pack,
                      "rotation_calendar": live_api.live_rotation_calendar,
                      "auction": live_api.live_auction,
                      "realtime": live_api.live_realtime,   # ★2026-08-14 盘中实时行情
                      "endpoints": live_api.live_endpoints,   # ★2026-08-14 API接口状态探测
                      "turnlow_top": live_api.live_turnlow_top,   # ★2026-08-15 turn_low 防守主力参考
                      "wufu_rotation": live_api.live_wufu_rotation,   # ★2026-08-16 五福轮动门户摆件
                      "chain_refresh": live_api.live_chain_refresh}[_fn]
                return self._send_json(fn())
            except Exception as e:
                # ★2026-08-14 调试：live API 500 打印完整堆栈（排查 float-datetime）
                try:
                    import traceback as _tb
                    _tb.print_exc()
                except Exception:
                    pass
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/export_data":   # ★2026-08-12 用户需求#179：数据导出中心（Excel + 海报）
            try:
                import subprocess as _sp_e, glob as _gl_e
                _r = _sp_e.run([sys.executable, "-X", "utf8",
                                str(BASE / "report" / "export_center.py")],
                               capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
                _xf = sorted([Path(x) for x in _gl_e.glob(str(BASE / "output" / "data_export_*.xlsx"))],
                             key=lambda p: p.stat().st_mtime)
                _pf = sorted([Path(x) for x in _gl_e.glob(str(BASE / "output" / "data_poster_*.html"))],
                             key=lambda p: p.stat().st_mtime)
                return self._send_json({
                    "ok": _r.returncode == 0,
                    "excel": _xf[-1].name if _xf else None,
                    "poster": _pf[-1].name if _pf else None,
                    "log": (_r.stdout or "").strip().splitlines()[-1] if _r.stdout else "",
                })
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path.startswith("/dl/"):   # ★2026-08-12 #179：导出文件下载（/dl/data_export_xxx.xlsx）
            _name = path.split("/", 2)[2]
            # ★2026-10-19 安全修复：原本 `BASE/"output"/_name` 直接拼接，`../` 可逃逸到
            #   output/ 之外（实测可读 config/params.yaml 明文 token）。改为 _safe_join 包含性校验。
            _fp = _safe_join(BASE / "output", _name,
                             allowed_suffixes=(".xlsx", ".html", ".csv", ".json"))
            if _fp is not None and _fp.is_file():
                _ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"                     if _fp.suffix.lower() == ".xlsx" else "text/html; charset=utf-8"
                return self._send(200, _fp.read_bytes(), _ct)
            return self._send(404, b"file not found")
        if path == "/api/stock_check":   # ★2026-08-10 个股交叉评级（GET ?code=000650）
            try:
                import urllib.parse
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                code = (q.get("code") or [""])[0].strip()
                if not code:
                    return self._send_json({"ok": False, "error": "需要 ?code=股票代码（如 000650）"}, 400)
                # ★2026-08-10 性能：同 code 60s 缓存（重复查询秒回；数据源更新后自动过期）
                _now_sc = time.time()
                _c = self._sc_cache.get(code)
                if _c and _now_sc - _c[0] < 60:
                    return self._send_json(_c[1])
                sys.path.insert(0, str(BASE))
                from factors.opportunities.stock_check import check
                _r = check(code)
                self._sc_cache[code] = (_now_sc, _r)
                return self._send_json(_r)
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/dashboard_stockcheck.html":   # ★2026-08-10 个股检测页
            p = BASE / "deck" / "stockcheck.html"
            return self._send(200, p.read_bytes() if p.exists() else b"stockcheck.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_actions.html":   # ★2026-08-10 待处理面板（止损触发/待审批 Pitch/新突破）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_actions_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_actions.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_actions.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_backtest.html":   # ★2026-08-10 B3 回测页（17 年验证 + 模拟盘 + 竞价反信号）
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_backtest_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_backtest.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_backtest.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_ranks.html":   # ★2026-08-10 涨跌幅榜 + 引擎/风控对照
            p = BASE / "deck" / "ranks.html"
            return self._send(200, p.read_bytes() if p.exists() else b"ranks.html missing", "text/html; charset=utf-8")
        if path == "/dashboard_live.html":   # ★2026-08-10 实时系统状态面板
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_live_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_live.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_live.html missing", "text/html; charset=utf-8")
        if path == "/api/tech_pitch":   # ★2026-08-10 科技突破 Pitch 池
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "logs" / "tech_pitch_*.json")), key=lambda x: Path(x).stat().st_mtime)
            if not files:
                return self._send_json({"error": "科技池未生成（运行 tech_pitch.py）"}, 404)
            return self._send_json(json.loads(Path(files[-1]).read_text(encoding="utf-8")))
        if path == "/dashboard_techpitch.html":   # ★2026-08-10 科技突破 Pitch 池页
            import glob as _glob
            files = sorted(_glob.glob(str(BASE / "deck" / "dashboard_techpitch_*.html")), key=lambda x: Path(x).stat().st_mtime)
            p = Path(files[-1]) if files else BASE / "deck" / "dashboard_techpitch.html"
            return self._send(200, p.read_bytes() if p.exists() else b"dashboard_techpitch.html missing", "text/html; charset=utf-8")
        if path in ROUTES:
            f = _resolve_route(path)
            if f is None or not f.exists():
                # ★2026-08-11 审批链路修复：/api/decisions 无文件时返回 []（前端审批页依赖空数组初始化）
                if path == "/api/decisions":
                    return self._send_json([])
                return self._send_json({"error": f"文件不存在: {f.name if f else path}", "hint": "先运行对应产出脚本"}, 404)
            try:
                return self._send_json(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/daily_report":   # ★#298 日报（GET：最新 report/daily_report_*.md 转 JSON——v2 日报页）
            try:
                import glob as _gr
                _fs = sorted(_gr.glob(str(BASE / "report" / "daily_report_*.md")), key=lambda x: Path(x).stat().st_mtime)
                if not _fs:
                    return self._send_json({"ok": False, "error": "日报未生成（等 19:00 自动化）"}, 404)
                _md = Path(_fs[-1]).read_text(encoding="utf-8")
                _date = re.search(r"daily_report_(\d{4}-\d{2}-\d{2})\.md", _fs[-1]).group(1) if re.search(r"daily_report_(\d{4}-\d{2}-\d{2})\.md", _fs[-1]) else ""
                return self._send_json({"ok": True, "date": _date, "markdown": _md,
                                        "path": _fs[-1]})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/market_kb":   # ★#294 市场知识库 AI 视图（GET：report/market_kb_dump.json——知识库 AI 每日分析输入）
            try:
                _f = BASE / "report" / "market_kb_dump.json"
                if not _f.exists():
                    return self._send_json({"error": "market_kb_dump.json 未生成（跑 data/build_market_kb.py 或等 18:30 链）"}, 404)
                return self._send(200, _f.read_bytes(), "application/json; charset=utf-8")
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/ai/insights":   # ★#294 AI 复盘读取（GET：知识库 AI 读历史结论 + 远期表现）
            try:
                sys.path.insert(0, str(BASE))
                from factors.opportunities.pitch_track import load_latest
                pool = load_latest()
                insights = []
                _f = BASE / "logs" / "ai_insights.json"
                if _f.exists():
                    insights = json.loads(_f.read_text(encoding="utf-8"))
                ai_entries = [e for e in pool["entries"] if e.get("pool_type") == "ai_select"]
                return self._send_json({"ok": True, "n_insights": len(insights),
                                        "insights": insights[-100:],
                                        "ai_pool": ai_entries,
                                        "n_ai_pool": len(ai_entries)})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/pitch_track_pools":   # ★2026-08-15 主观多池远期：5 池分组汇总（A/B/C/D + 牛散按决策者）
            try:
                sys.path.insert(0, str(BASE))
                from factors.opportunities.pitch_track import summary_by_pool
                return self._send_json({"ok": True, **summary_by_pool()})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        return self._send_json({"error": f"未知路由 {path}"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        # ★2026-10-19 H-5 修复：启用 DECK_API_TOKEN 时，所有 POST 要求 Bearer 鉴权（含 /api/proxy 与 /api/import）
        if self._require_auth():
            return
        if path.startswith("/api/proxy/"):   # ★2026-08-16 反向代理 POST → :3080（删除/批量/清空/发送全走这里）
            import urllib.request as _ur
            import urllib.error as _ue
            _tgt = "http://127.0.0.1:3080/" + path[len("/api/proxy/"):]
            try:
                _body = self._read_json()
                _req = _ur.Request(_tgt, data=json.dumps(_body).encode("utf-8"),
                                   headers={"Content-Type": "application/json"})
                _r = _ur.urlopen(_req, timeout=60)
                _data = _r.read()
                try:
                    return self._send_json(json.loads(_data.decode("utf-8")))
                except Exception:
                    self.send_response(_r.status)
                    self.send_header("Content-Type", _r.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    self.wfile.write(_data)
                    return
            except _ue.HTTPError as e:
                _d = e.read()
                try:
                    return self._send_json(json.loads(_d.decode("utf-8")), e.code)
                except Exception:
                    return self._send_json({"ok": False, "error": "proxy upstream " + str(e.code)}, e.code)
            except Exception as e:
                return self._send_json({"ok": False, "error": "proxy: " + str(e)}, 502)
        if path == "/api/import/install":   # ★2026-08-16 导入 skill / agent 预设（assets → 运行库）
            try:
                rec = self._read_json()
                typ = rec.get("type")
                name = rec.get("name") or ""
                import re as _re, shutil as _sh
                if typ not in ("skill", "preset") or not _re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                    return self._send_json({"ok": False, "error": "type 需为 skill/preset，name 需为小写字母数字连字符"}, 400)
                src = BASE / "assets" / ("skills" if typ == "skill" else "presets") / name
                if not src.is_dir():
                    return self._send_json({"ok": False, "error": "assets 中不存在 " + name}, 404)
                # 安装目标：DSH_HOME 环境变量 > 随包 harness/home（仅本地开发用，勿硬编码本机路径）
                _home = Path(os.environ.get("DSH_HOME", str(BASE / "harness" / "home")))
                dst_root = _home / "skills" if typ == "skill" else _home / ".agent-presets"
                dst_root.mkdir(parents=True, exist_ok=True)
                dst = dst_root / name
                if dst.exists():
                    _sh.rmtree(dst)
                _sh.copytree(src, dst)
                return self._send_json({"ok": True, "type": typ, "name": name, "target": str(dst)})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/subj_quant/tag":   # ★2026-08-14 主观量化：录入标签 {code,name,tag,confidence,note}
            try:
                rec = self._read_json()
                from data.subj_quant import add_tag
                if not rec.get("code") or not rec.get("tag"):
                    return self._send_json({"ok": False, "error": "需要 code 和 tag"}, 400)
                add_tag(rec.get("code"), rec.get("name", ""), rec.get("tag"),
                        rec.get("confidence", 0.5), rec.get("note", ""))
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/subj_quant/veto":   # ★2026-08-14 主观量化：录入否决 {code,name,reason}
            try:
                rec = self._read_json()
                from data.subj_quant import add_veto
                if not rec.get("code"):
                    return self._send_json({"ok": False, "error": "需要 code"}, 400)
                add_veto(rec.get("code"), rec.get("name", ""), rec.get("reason", ""))
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/subj_quant/veto/remove":   # ★2026-08-14 主观量化：撤销否决
            try:
                rec = self._read_json()
                from data.subj_quant import remove_veto
                if not rec.get("code"):
                    return self._send_json({"ok": False, "error": "需要 code"}, 400)
                remove_veto(rec.get("code"))
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
        if path == "/api/manual_update":   # ★2026-08-10 手动全域更新（POST 触发；自动程序运行中自动拒绝）
            try:
                sys.path.insert(0, str(BASE))
                from data.manual_update import start
                return self._send_json(start())
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
        if path == "/api/decide":
            try:
                rec = self._read_json()
            except Exception:
                return self._send_json({"error": "JSON 解析失败"}, 400)
            rec.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
            rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
            # ★2026-08-11 撤回：action=undo → 从审批历史删除该 code 全部记录（buy/drop 均可撤回）
            if rec.get("action") == "undo":
                if not rec.get("code"):
                    return self._send_json({"error": "需要 code"}, 400)
                with DECIDE_LOCK:   # ★2026-08-15 并发写锁（读-改-写串行化）
                    hist = []
                    _dcur = _latest_decisions()
                    if _dcur.exists():
                        try:
                            hist = json.loads(_dcur.read_text(encoding="utf-8"))
                            if not isinstance(hist, list):
                                hist = []
                        except Exception:
                            hist = []
                    before = len(hist)
                    hist = [r for r in hist if r.get("code") != rec["code"]]
                    try:
                        _dnew = LOGS / f"deck_decisions_{time.strftime('%Y%m%d_%H%M%S')}.json"
                        _dnew.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception as _e:
                        return self._send_json({"ok": False, "error": f"撤回写入失败: {str(_e)[:60]}"}, 500)
                # ★2026-08-11 撤回联动：异步清空远期池该 code 条目的 decided 标记 + 回退未成交持仓（与决策记录一致）
                try:
                    import threading as _th
                    def _bg_undo_all():
                        try:
                            sys.path.insert(0, str(BASE))
                            # ① 远期池 decided 清空
                            from factors.opportunities.pitch_track import load_latest as _pl, _write as _pw
                            _pool = _pl()
                            _changed = False
                            for _e in _pool.get("entries", []):
                                if _e.get("code") == rec["code"] and _e.get("decided"):
                                    _e["decided"] = ""
                                    _changed = True
                            if _changed:
                                _pw(_pool)
                            # ② 持仓回退：该 code 未成交（entry_price=None）的 holding/over_limit 移除
                            from strategy.portfolio import _load as _pl2, _save as _ps2
                            _d = _pl2()
                            _removed = 0
                            _keep = []
                            for _p in _d.get("positions", []):
                                if (_p.get("code") == rec["code"]
                                        and _p.get("entry_price") is None
                                        and _p.get("status") in ("holding", "over_limit")):
                                    _removed += 1
                                else:
                                    _keep.append(_p)
                            if _removed:
                                _d["positions"] = _keep
                                _ps2(_d)
                        except Exception:
                            pass
                    _th.Thread(target=_bg_undo_all, daemon=True).start()
                except Exception:
                    pass
                return self._send_json({"ok": True, "undo": rec["code"],
                                        "removed": before - len(hist), "total": len(hist)})
            if not rec.get("code") or rec.get("action") not in ("buy", "drop"):
                return self._send_json({"error": "需要 code + action(buy/drop/undo)"}, 400)
            rec = {k: rec.get(k) for k in ("date", "code", "action", "ts", "note", "target", "stop")}
            # ★★L0 硬门控（2026-08-15 低频主观量化改造 · 设计书 P2-2）：
            #   防御期（择时 score<40）下 revalue/tech_sentiment 买入硬拒绝——实盘归因（因子池留言）：
            #   这两类下跌日放大亏损（T+1 归因 -1.65%~-2.18%），UI 二次确认可绕过 → 后端固化为硬规则
            try:
                import glob as _tg0
                import os as _os0
                _tf0 = sorted(_tg0.glob(str(OUTPUT / "timing_system_*.json")), key=_os0.path.getmtime)
                _tm_score = None
                if _tf0:
                    _td0 = json.loads(Path(_tf0[-1]).read_text(encoding="utf-8"))
                    _tm_score = _td0.get("score")
                if rec.get("action") == "buy" and _tm_score is not None and _tm_score < 40:
                    _gcode = str(rec["code"]).upper()
                    _otype = ""
                    _pf0 = sorted(_tg0.glob(str(LOGS / "pitch_v2*.json")), key=_os0.path.getmtime)
                    if _pf0:
                        _pd0 = json.loads(Path(_pf0[-1]).read_text(encoding="utf-8"))
                        for _p0 in (_pd0.get("pitch") or []):
                            if str(_p0.get("code", "")).upper() == _gcode:
                                _otype = _p0.get("otype") or ""
                                break
                    if not _otype:
                        _tf1 = sorted(_tg0.glob(str(LOGS / "tech_pitch*.json")), key=_os0.path.getmtime)
                        if _tf1:
                            _td1 = json.loads(Path(_tf1[-1]).read_text(encoding="utf-8"))
                            for _e1 in (_td1.get("entries") or []):
                                if str(_e1.get("code", "")).upper() == _gcode:
                                    _otype = _e1.get("otype") or ""
                                    break
                    if _otype in ("revalue", "tech_sentiment"):
                        return self._send_json({
                            "ok": False,
                            "error": (f"L0 门控拦截：防御期（择时 score={_tm_score:.0f}<40）下 "
                                      f"{_otype} 禁止买入——实盘归因下跌日放大亏损（-1.65%~-2.18%），硬规则不放开"),
                            "gate": {"defensive": True, "score": _tm_score, "otype": _otype}}, 403)
            except Exception:
                pass
            # ★2026-08-11 百轮#36：决策留痕——记录审批时的市场环境档位（择时系统结论）
            #   复盘时能回答"这笔买入是在什么环境下做的"（适合买入/谨慎/不适合 + 分数）
            try:
                import glob as _tg
                import os as _os
                _tf = sorted(_tg.glob(str(OUTPUT / "timing_system_*.json")), key=_os.path.getmtime)
                if _tf:
                    _td = json.loads(Path(_tf[-1]).read_text(encoding="utf-8"))
                    rec["env_level"] = _td.get("level", "")
                    rec["env_score"] = _td.get("score")
            except Exception:
                pass
            # ★2026-08-12 用户需求#177：Pitch 全量标记——buy 时从最新 pitch_v2 候选自动提取
            #   因子归因（哪个因子/多因子命中）/止损条件/风控/预期，存数据库供"实战 vs 回测"归因
            #   （未来复盘：因子实战最强 → 对比 ICIR120/ICIR变化 → 给因子打分）
            try:
                import glob as _pg
                import os as _os2   # ★#177 修复：deck_server 顶部无 os，需局部导入
                _pf = sorted(_pg.glob(str(LOGS / "pitch_v2*.json")), key=_os2.path.getmtime)
                _meta = None
                if _pf:
                    _pd = json.loads(Path(_pf[-1]).read_text(encoding="utf-8"))
                    for _p in (_pd.get("pitch") or []):
                        if _p.get("code") == rec["code"]:
                            _meta = {k: _p.get(k) for k in (
                                "otype", "otype_name", "score", "factors", "signal_family",
                                "stop_plan", "risk_level", "risk_score", "risk_flags",
                                "beneish", "winrate_est", "upside_est",
                                "rank_in_type", "rank_global", "n_types_hit", "also_types",
                                "evidence", "horizons", "express_strong", "size_tier",
                                "pitch_line", "pitch_sub", "tier",
                            ) if _p.get(k) is not None}
                            break
                if _meta:
                    rec["pitch_meta"] = _meta
            except Exception:
                pass
            # append 模式（★写保护免疫：读最新时间戳文件，写新时间戳文件）
            # ★2026-08-15 并发写锁：读-改-写串行化（实测并发 10 请求仅存 4 条 → 修复）
            with DECIDE_LOCK:
                hist = []
                _dcur = _latest_decisions()
                if _dcur.exists():
                    try:
                        hist = json.loads(_dcur.read_text(encoding="utf-8"))
                        if not isinstance(hist, list):
                            hist = []
                    except Exception:
                        hist = []
                hist.append(rec)
                try:
                    _dnew = LOGS / f"deck_decisions_{time.strftime('%Y%m%d_%H%M%S')}.json"
                    _dnew.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception as _e:
                    return self._send_json({"ok": False, "error": f"写入失败: {str(_e)[:60]}"}, 500)
            # ★T-2：buy 审批 → 自动同步持仓（持股≤5 纪律）
            # ★2026-08-11 异步化：联动（持仓同步+远期池）放后台线程——审批秒回，
            #   避免 bars.db 读锁等 20s 拖垮审批响应（实测 30s+ 超时）
            sync = {"added": [], "over_limit": []}
            if rec.get("action") == "buy":
                try:
                    import threading
                    def _bg_sync(code=rec.get("code")):
                        # ★2026-08-11 去静默：联动异常写入日志（按日时间戳名，固定名被写保护锁丢日志）
                        _log = LOGS / f"decide_sync_{time.strftime('%Y%m%d')}.log"
                        def _wlog(msg):
                            try:
                                with open(_log, "a", encoding="utf-8") as f:
                                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
                            except Exception:
                                pass
                        _wlog(f"decide={code} start")
                        # 1) 持仓同步（持股≤5：buy → holding）
                        try:
                            sys.path.insert(0, str(BASE))
                            from strategy.portfolio import sync_from_decisions
                            sync_from_decisions()
                        except Exception as e:
                            _wlog(f"  ✕ sync_from_decisions: {str(e)[:200]}")
                        # 2) 远期池联动（长线 append_pitch + 科技线追加）——独立 try，失败不阻断
                        try:
                            from factors.opportunities.pitch_track import append_pitch, _write as _pt_write, load_latest
                            p2 = _resolve_route("/api/pitch_v2")
                            if p2 and p2.exists():
                                append_pitch(p2)
                            # ★2026-08-11 修复：glob 返回 str，x.stat() 崩溃（Path 才可 stat）→ 科技线入池一直静默失败
                            tp_files = sorted([Path(p) for p in LOGS.glob("tech_pitch_*.json")],
                                              key=lambda x: x.stat().st_mtime)
                            if tp_files:
                                _pool = load_latest()
                                _d = json.loads(tp_files[-1].read_text(encoding="utf-8"))
                                _existing = {e["code"] for e in _pool["entries"]}
                                _added = 0
                                for _t in _d.get("entries", []):
                                    if _t["code"] in _existing:
                                        continue
                                    _pool["entries"].append({
                                        "code": _t["code"], "name": _t.get("name", _t["code"]),
                                        "otype": _t.get("otype", "tech_sentiment"),
                                        "score": _t.get("score"),
                                        "risk_level": _t.get("risk_level", ""),
                                        "beneish": "",
                                        "stop_plan": _t.get("stop_plan") or {},
                                        "decided": "",   # ★2026-08-11 审批状态标记
                                        "entry_date": _d.get("pool_date") or _t.get("add_date", ""),
                                        "entry_close": None,
                                        "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
                                        "age_days": 0,
                                    })
                                    _existing.add(_t["code"])
                                    _added += 1
                                if _added:
                                    _pt_write(_pool)
                        except Exception as e:
                            _wlog(f"  ✕ 远期池联动: {str(e)[:200]}")
                        # 3) 审批状态标记（独立 try，不受上面影响）：远期池该 code 条目标 decided=buy + 升级 human_select
                        #    ★#346 四池独立：人工买入 → 该 code 从 auto_pitch/machine_top01 流动到 human_select（人工池 = 持仓）
                        try:
                            from factors.opportunities.pitch_track import load_latest as _pl, _write as _pw
                            _pool2 = _pl()
                            _chg = False
                            for _e in _pool2.get("entries", []):
                                if _e.get("code") == code:
                                    if _e.get("decided") != "buy":
                                        _e["decided"] = "buy"
                                        _chg = True
                                    if _e.get("pool_type") != "human_select":
                                        _e["pool_type"] = "human_select"
                                        _chg = True
                            if _chg:
                                _pw(_pool2)
                                _wlog(f"  ✓ 远期池 decided=buy + human_select: {code}")
                        except Exception as e:
                            _wlog(f"  ✕ decided 标记: {str(e)[:120]}")
                    threading.Thread(target=_bg_sync, daemon=True).start()
                    sync = {"async": True, "note": "持仓+远期池联动已在后台执行"}
                except Exception as e:
                    sync = {"error": str(e)[:80]}
            return self._send_json({"ok": True, "record": rec, "total": len(hist),
                                    "portfolio_sync": sync})
        if path == "/api/portfolio/sell":    # ★T-2：卖出 → 状态机 exit + history + 人工池降级
            try:
                body = self._read_json()
                sys.path.insert(0, str(BASE))
                from strategy.portfolio import sell
                r = sell(body.get("code"), body.get("price"), body.get("reason") or "manual")
                # ★#346 四池独立：卖出 → 该 code 从 human_select 池降级（人工池 = 当前持仓，卖出即流出）
                if r.get("ok"):
                    try:
                        from factors.opportunities.pitch_track import load_latest as _pl, _write as _pw
                        _pool = _pl()
                        _chg = False
                        for _e in _pool.get("entries", []):
                            if _e.get("code") == body.get("code") and _e.get("pool_type") == "human_select":
                                _e["pool_type"] = None
                                _e["decided"] = "sell"
                                _chg = True
                        if _chg:
                            _pw(_pool)
                    except Exception:
                        pass
                return self._send_json(r, 200 if r.get("ok") else 404)
            except Exception as e:
                return self._send_json({"error": str(e)}, 400)
        if path == "/api/ai/select":   # ★#294 AI 主观选股入池（知识库 AI 调用——每天跑库后从 pitch 主观选股）
            try:
                body = self._read_json()
                picks = body.get("picks") or ([body] if body.get("code") else [])
                date = body.get("date") or ""
                skip_reason = body.get("skip_reason") or ""
                # ★#297 自由裁决权：picks 空但带 skip_reason = 今日不选（算已回应，不告警）
                if not picks and not skip_reason:
                    return self._send_json({"error": "需要 picks / code，或 skip_reason（今日不选+理由）"}, 400)
                sys.path.insert(0, str(BASE))
                from factors.opportunities.pitch_track import append_ai_select
                pool = append_ai_select(picks, date, skip_reason)
                added = sum(1 for e in pool["entries"] if e.get("pool_type") == "ai_select")
                return self._send_json({"ok": True, "added": added, "ai_select_n": added,
                                        "skip": bool(skip_reason and not picks),
                                        "note": "AI 主观选股已入远期池（fwd 自动验证复盘）；今日不选已记录"})
            except Exception as e:
                return self._send_json({"error": str(e)}, 400)
        if path == "/api/ai/insights":   # ★#294 AI 复盘读取（知识库 AI 读历史结论 + 远期表现）
            try:
                sys.path.insert(0, str(BASE))
                from factors.opportunities.pitch_track import load_latest
                pool = load_latest()
                # AI 结论历史（logs/ai_insights.json）
                insights = []
                _f = BASE / "logs" / "ai_insights.json"
                if _f.exists():
                    insights = json.loads(_f.read_text(encoding="utf-8"))
                # 远期池 ai_select 条目（含 fwd 表现——复盘数据）
                ai_entries = [e for e in pool["entries"] if e.get("pool_type") == "ai_select"]
                return self._send_json({"ok": True, "n_insights": len(insights),
                                        "insights": insights[-100:],
                                        "ai_pool": ai_entries,
                                        "n_ai_pool": len(ai_entries)})
            except Exception as e:
                return self._send_json({"error": str(e)}, 400)
        if path == "/api/niu/record":   # ★2026-08-15 牛散决策入远期池（独立模块：决策者/决策时间/标的/动作）
            try:
                body = self._read_json()
                persona = str(body.get("persona") or "").strip()
                text = str(body.get("text") or "")
                snapshot_date = str(body.get("snapshot_date") or "")
                if not persona or not text:
                    return self._send_json({"error": "需要 persona + text（牛散回复文本）"}, 400)
                # 提取回复末尾的结构化 JSON {"niu_decisions":[...]}
                import re as _re
                _decisions = []
                _i = text.find('"niu_decisions"')
                if _i >= 0:
                    _start = text.rfind("{", 0, _i)
                    if _start >= 0:
                        _depth = 0
                        for _j in range(_start, len(text)):
                            if text[_j] == "{":
                                _depth += 1
                            elif text[_j] == "}":
                                _depth -= 1
                                if _depth == 0:
                                    try:
                                        _obj = json.loads(text[_start:_j + 1])
                                        _decisions = _obj.get("niu_decisions") or []
                                    except Exception:
                                        _decisions = []
                                    break
                # code 规范化（6 位 → 补交易所后缀）
                def _norm(c):
                    c = str(c or "").strip().upper()
                    if "." in c:
                        return c
                    if len(c) == 6 and c.isdigit():
                        return c + (".SH" if c[0] in "69" else ".SZ")
                    return c
                picks = [{"code": _norm(d.get("code") or d.get("ticker")), "action": d.get("action", "watch"),
                          "priority": d.get("priority", ""),
                          "reason_short": d.get("reason_short", "")}
                         for d in _decisions if d.get("code")]
                sys.path.insert(0, str(BASE))
                from factors.opportunities.pitch_track import append_niu_select, PERSONA_NAMES
                # 决策日 = 最新交易日（bars）
                _date = ""
                try:
                    from data.cache import DailyCache as _DCN
                    _date = _DCN().latest_trade_date()
                except Exception:
                    _date = ""
                pool = append_niu_select(persona, picks, _date, snapshot_date)
                _niu_n = sum(1 for e in pool["entries"]
                             if e.get("pool_type") == "niu_select" and e.get("persona") == persona)
                return self._send_json({
                    "ok": True, "persona": persona,
                    "persona_name": PERSONA_NAMES.get(persona, persona),
                    "recorded": len(picks), "picks": picks, "date": _date,
                    "niu_pool_n": _niu_n,
                    "note": "牛散决策已入远期池（pool_type=niu_select，fwd 自动验证；决策者分组见 /api/pitch_track_pools）"})
            except Exception as e:
                return self._send_json({"error": str(e)}, 400)
        return self._send_json({"error": f"未知路由 {path}"}, 404)


def _port_already_served(port: int, path: str = "/", timeout: float = 3.0,
                         attempts: int = 3) -> bool:
    """端口上是否已有另一个存活的 HTTP 实例在应答（双开自检用）。

    ★关键语义（2026-10-15 实测踩坑后修正）：必须区分「拒连」与「超时」。
      - 拒连 = 端口真的空 → 放行（这是 watchdog 故障转移的路径，不能堵）；
      - 超时 = 端口上有活实例但正忙（deck 重端点会把 GIL 打满，探测超过 timeout 不应答）
              → 视为被占用：重试若干次，仍不应答则拒绝启动。
    原实现把"没拿到 200"一律当成空端口：实测被重请求占满的 deck 探测 3s 超时 →
    误判空闲 → 第二个实例双开成功——这正是本轮线上双实例端口劫持的成因
    （34776 活着在服务，15220 的探测超时后被放行绑上同一个端口）。

    另外两点：
      - 探测必须显式绕开系统代理（ProxyHandler({})）：有 http_proxy 时对 127.0.0.1 的
        探测会被代理应答，把空闲端口误判成"被占用"→ 服务永远起不来。
      - 故意不用 bind 试探：bind 试探分不清"已死实例的 TIME_WAIT 残留"和"活实例"，
        而 allow_reuse_address 已让我们能绑上有残留的端口。
    """
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(max(1, attempts)):
        try:
            with opener.open(urllib.request.Request(f"http://127.0.0.1:{port}{path}"),
                             timeout=timeout) as r:
                r.read(1)
                return True         # 有东西在应答（哪怕 404）就算被占用
        except urllib.error.HTTPError:
            return True             # HTTP 错误码也是"有实例在应答"
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ConnectionRefusedError):
                return False        # 端口真的没有活实例 → 允许接管
            # 超时 / 连接被重置 / 其他网络错 → 疑似有活实例但忙，重试
        except Exception:
            pass
        if attempt < max(1, attempts) - 1:
            time.sleep(1.5)
    return True    # 始终不应答：保守视为被占用（不双开，也不给累死的服务器雪上加霜）


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    # ★2026-10-15 双开自检。Windows 上 SO_REUSEADDR（allow_reuse_address=True）允许
    #   第二个进程绑同一端口，结果是两个 deck 同时"监听"而连接只落在一个实例上：
    #   改代码重启后旧实例继续服务、新实例空跑。本轮实测踩中——请求全落在加固前的
    #   旧进程上，/api/live/server_errors 持续 404，新代码形同没部署，且极难排查。
    #   判定语义：端口有另一个存活实例在应答 HTTP → 拒绝启动并说清该杀谁；
    #   没有 → 正常绑定（旧实例已死，端口可安全接管，这也是 watchdog 故障转移的路径）。
    # ★2026-10-18 先抢进程级独占锁（消除启动竞态），再做端口探测（处理"锁已释放但
    #   旧实例仍在应答"的残留情形）。两者都过才允许 bind。
    if not _acquire_server_lock(port):
        print(f"✗ 端口 {port} 已有另一个 deck 实例持有单实例锁，本次拒绝启动。", flush=True)
        print("  双开会造成「新进程空跑、旧进程继续服务」：改代码不生效、请求行为随机。", flush=True)
        print(f"  处理：结束旧进程后重试；查占用：netstat -ano | findstr :{port}", flush=True)
        sys.exit(3)
    if _port_already_served(port):
        print(f"✗ 端口 {port} 已被另一个存活的 deck 实例占用，本次拒绝启动（防双开劫持）。",
              flush=True)
        print("  双开会造成「新进程空跑、旧进程继续服务」：改代码不生效、请求行为不一致。",
              flush=True)
        print(f"  处理：结束占用 {port} 的旧进程后重试；查占用：netstat -ano | findstr :{port}",
              flush=True)
        sys.exit(2)
    # ★2026-10-15 用加固版服务器（见 DeckServer 注释：守护线程/异常回500/大backlog）
    srv = DeckServer(("127.0.0.1", port), Handler)
    print(f"★ Deck/看板 服务器已启动：http://127.0.0.1:{port}/deck.html")
    print(f"  看板：http://127.0.0.1:{port}/dashboard.html  （Ctrl+C 退出）")
    # ★2026-10-15 启动即触发后台全市场快照预热（~22s/70 页，守护线程，不阻塞服务）。
    #   修复：原先首个 holdings 请求同步拉全市场 → 阻塞 22s 且 GIL 饿死全站（连接被拒）。
    try:
        sys.path.insert(0, str(BASE / "deck"))
        import live_api as _la
        _la.live_realtime()   # 缓存未就绪 → 仅启动后台刷新并立即返回
        print("  [预热] 实时快照后台刷新已触发（约 25s 后就绪；此前持仓现价走轻量按需报价）", flush=True)
    except Exception as _e:
        print(f"  [预热] 跳过（不影响服务）: {_e}", flush=True)

    # ★2026-10-15 优雅退出：Ctrl+C / 任务管理器结束 / 计划任务 stop 都会走到这里。
    #   原实现只捕 KeyboardInterrupt；SIGTERM 下 serve_forever 不返回，
    #   且非守护线程会把进程挂住 → 端口不释放，重启即失败。
    _stop = threading.Event()

    def _graceful(signum, _frame):
        print(f"\n收到信号 {signum}，正在优雅退出…", flush=True)
        _stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for _sig in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, _sig), _graceful)
        except (ValueError, AttributeError, OSError):
            pass   # 非主线程/平台不支持时忽略，不影响服务本身

    try:
        # poll_interval=0.5：shutdown() 最多等 0.5s 即可返回，退出不再拖沓
        while not _stop.is_set():
            srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        print("端口已释放")


if __name__ == "__main__":
    main()
