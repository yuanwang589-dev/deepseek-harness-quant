# -*- coding: utf-8 -*-
r"""scripts/wecom_push.py — 把 DSHQuant 看板内容推送到企业微信群机器人（方案①：零端口暴露）

设计目标：**不暴露任何端口**，只做「读本机 deck → 外发微信」的单向推送。
  - 读：http://127.0.0.1:8787 的只读聚合端点（/api/live/holdings、/api/daily_report）
  - 写：POST 到企业微信群机器人 webhook（仅出站，无任何入站监听）
  - 本机 API 调用显式绕代理；webhook 默认也绕代理（与 watchdog/launcher 的 clean_env 语义一致），
    确实需要走代理时加 --use-proxy。

webhook 来源优先级：--webhook > 环境变量 WECOM_WEBHOOK > config/wecom.yaml 的 webhook 字段

用法：
  python scripts/wecom_push.py --selftest                 # 只校验配置与连通性，不发消息
  python scripts/wecom_push.py --type all --dry-run       # 预览消息内容，不发
  python scripts/wecom_push.py --type all                 # 持仓（含真实盈亏）+ 日报
  python scripts/wecom_push.py --type holdings            # 只看持仓
  python scripts/wecom_push.py --type daily               # 只看日报
  python scripts/wecom_push.py --type all --format markdown   # 回退到旧版 markdown（带字体颜色）

退出码：0 成功 / 1 发送或读取失败 / 2 未配置 webhook

消息类型（--format，也可写进 config/wecom.yaml 的 format 字段）：
  markdown_v2（默认）—— 支持 1~6 级标题、斜体/加粗、**无序/有序列表（可嵌套）**、多级引用、
                        链接；**不支持字体颜色与 @群成员**。
                        手机端可读性最好：真列表 + 引用块，无需再用 emoji 伪装层级。
                        要求客户端 ≥4.1.36（安卓 ≥4.1.38），低于此版本会退化成纯文本。
  markdown（旧版）—— 支持标题/加粗/链接/行内代码/引用 + 3 种字体颜色
                        （info 绿 / comment 灰 / warning 橙）；**不支持列表、缩进、表格、分割线**。
                        客户端版本极老时用这个兜底。

定时推送（Windows 计划任务，每个交易日 19:05，日报生成后）：
  schtasks /create /tn "DSHQuant-WeCom-Push" /sc weekly /d MON,TUE,WED,THU,FRI /st 19:05 ^
    /tr "\"<venv>\Scripts\pythonw.exe\" -X utf8 \"<repo>\scripts\wecom_push.py\" --type all" /f

企业微信「消息推送」（原「群机器人」，v4.1.41 起改名）：
  电脑端：内部群聊 → 右上角「…」→ 消息推送 → 添加
  手机端：内部群聊 → 右上角三个点 → 消息推送 → 添加
  拿到 Webhook：点开刚创建的「消息推送」→ 复制 Webhook 地址（仅创建人可见）
  前置条件：仅「内部群聊」支持；群若开启「仅群主管理」则只有群主能添加。
限制：每个「消息推送」≤20 条/分钟；消息内容 ≤4096 字节（本脚本自动截断）；
      群开启全员禁言时推送不显示。
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG = BASE / "config" / "wecom.yaml"
PAPER_DB = BASE / "data" / "cache" / "paper.db"   # 模拟账户（真实股数/现金）
DEFAULT_DECK = "http://127.0.0.1:8787"
MAX_BYTES = 4000          # WeCom 上限 4096，留余量
TIMEOUT = 30
SUB_INDENT = "  "         # markdown_v2 用 2 个空格做列表嵌套层级
MSG_TYPES = ("markdown_v2", "markdown")   # 默认 v2：支持真列表 / 多级引用


def _pnl_emoji(ret) -> str:
    """涨跌配色遵循中国习惯：盈利/上涨=红，亏损/下跌=绿。"""
    if ret is None:
        return "⚪"
    return "🔴" if ret >= 0 else "🟢"


def _pct(ret) -> str:
    """收益率 → 带符号百分数；None → —"""
    if ret is None:
        return "—"
    return f"{ret * 100:+.2f}%"


def _money(x) -> str:
    """金额 → ¥1,234.56 / -¥300.70；None → —"""
    if x is None:
        return "—"
    return f"{'-' if x < 0 else ''}¥{abs(x):,.2f}"


def paper_account(name=None):
    """读模拟账户（data/cache/paper.db）的真实持仓股数与现金。只读打开，失败返回 None。

    ★为什么要读它：logs/portfolio.json 只记成本价、**不记股数**，金额盈亏无从算起；
      而 paper.db 的 positions 表有真实 qty/avg_cost，accounts 表有 initial_cash/cash。
      name=None → 取最近创建的一个账户。
    """
    if not PAPER_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{PAPER_DB}?mode=ro&immutable=1", uri=True, timeout=5)
    except Exception:
        return None
    try:
        if name:
            row = con.execute("SELECT name, initial_cash, cash FROM accounts WHERE name=?",
                              (name,)).fetchone()
        else:
            row = con.execute("SELECT name, initial_cash, cash FROM accounts "
                              "ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        acc, init_cash, cash = row
        pos = {r[0]: {"qty": r[1], "avg_cost": r[2]}
               for r in con.execute("SELECT code, qty, avg_cost FROM positions WHERE account=?",
                                    (acc,))}
        return {"name": acc, "initial_cash": init_cash, "cash": cash, "positions": pos}
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass





def _log(msg: str):
    try:
        print(msg, flush=True)
    except Exception:
        pass


def _opener(use_proxy: bool):
    """use_proxy=False → 显式绕开系统代理（本机调用与国内直连都该绕）。"""
    if use_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _log(f"[warn] 读取 {CONFIG} 失败：{e}")
        return {}


def resolve_webhook(args, cfg: dict):
    for src, val in (("--webhook", args.webhook),
                     ("环境变量 WECOM_WEBHOOK", os.environ.get("WECOM_WEBHOOK")),
                     ("config/wecom.yaml", cfg.get("webhook"))):
        if val and str(val).strip():
            return str(val).strip(), src
    return None, None


def deck_get(base: str, path: str, timeout: int = TIMEOUT) -> dict:
    """读本机 deck 只读端点；本机调用一律绕代理。HTTPError 也尝试解析 body。"""
    op = _opener(False)
    url = base.rstrip("/") + path
    try:
        with op.open(urllib.request.Request(url), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            raise RuntimeError(f"{path} 返回 HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{path} 连接失败（deck 未运行？）：{getattr(e, 'reason', e)}")


def fmt_holdings(base: str, paper_name=None) -> str:
    """持仓 + 盈亏。价格取自 /api/live/holdings 的 pnl；股数与现金取自模拟账户 paper.db。

    ★两档口径：
      - 有 paper.db → 报**真实金额盈亏**（账户口径：净值 = 持仓市值 + 现金，对比本金）；
      - 无 paper.db → 退化为只报收益率（portfolio_pnl 的 total_cost/value 是"1 股等权"伪金额，
        报它会误导）。

    排版（手机端优先）：## 级标题分节、> 引用块放账户总账、- 列表放个股（markdown_v2 真列表）。
    """
    d = deck_get(base, "/api/live/holdings", timeout=60)
    if not isinstance(d, dict) or not d.get("ok"):
        raise RuntimeError("持仓数据不可用：" + str(d)[:120])
    pnl = d.get("pnl") or {}
    rows = pnl.get("rows") or []
    pf = d.get("portfolio") or {}
    pos_map = {p.get("code"): p for p in (pf.get("positions") or [])}
    pa = paper_account(paper_name)
    n = len(rows)
    if not rows:
        return "## 💼 持仓\n\n- 😴 当前空仓"
    src = (rows[0].get("price_src") or "").strip()
    src_txt = {"realtime": "实时价", "daily": "收盘价"}.get(src, src or "—")
    lines = [f"## 💼 持仓 {n}/{pf.get('max_positions', '?')} · {src_txt}", ""]

    if pa and pa.get("positions"):
        mv = cost = 0.0
        for r in rows:
            p = pa["positions"].get(r.get("code"))
            last = r.get("last_price")
            if p and last is not None:
                mv += p["qty"] * last
                cost += p["qty"] * p["avg_cost"]
        cash = pa.get("cash") or 0.0
        init = pa.get("initial_cash") or 0.0
        nav = mv + cash
        amt = (nav - init) if init else None
        ret_acc = (nav / init - 1) if init else None
        lines += [f"> 💰 净值 **{_money(nav)}** / 本金 {_money(init)}",
                  f"> {_pnl_emoji(ret_acc)} **{_money(amt)}**（{_pct(ret_acc)}）",
                  f"> 📦 持仓 {_money(mv)}（成本 {_money(cost)}）· 💵 现金 {_money(cash)}",
                  ""]
    else:
        tr = pnl.get("total_ret")
        lines += [f"> 📊 组合 {_pnl_emoji(tr)} **{_pct(tr)}** · {n} 只（无股数，仅收益率）", ""]

    for r in rows:
        code = r.get("code", "")
        name = r.get("name") or code
        ep, last, ret = r.get("entry_price"), r.get("last_price"), r.get("ret")
        p = pos_map.get(code) or {}
        pq = (pa.get("positions") or {}).get(code) if pa else None
        lines.append(f"- **{name}** {code}")
        quote = []
        if pq and ep is not None and last is not None:
            quote.append(f"💵 {pq['qty']}股 {ep}→{last}")
        elif ep is not None and last is not None:
            quote.append(f"💵 {ep} → {last}")
        quote.append(f"{_pnl_emoji(ret)} {_pct(ret)}")
        if pq and last is not None:
            quote.append(_money(pq["qty"] * last - pq["qty"] * pq["avg_cost"]))
        lines.append(SUB_INDENT + "- " + " · ".join(quote))
        plan = []
        if p.get("target") is not None:
            plan.append(f"🎯 目标 {p['target']}")
        if p.get("stop") is not None:
            plan.append(f"🛡 止损 {p['stop']}")
        if plan:
            lines.append(SUB_INDENT + "- " + " · ".join(plan))
    return "\n".join(lines)


def fmt_daily(base: str) -> str:
    d = deck_get(base, "/api/daily_report")
    if not (isinstance(d, dict) and d.get("ok")):
        raise RuntimeError((d or {}).get("error", "日报不可用"))
    return f"## 📰 日报 · {d.get('date', '')}\n\n{d.get('markdown', '')}"


def truncate(s: str, max_bytes: int = MAX_BYTES) -> str:
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    cut = b[:max_bytes]
    while cut and (cut[-1] & 0xC0) == 0x80:      # 不截断在多字节字符中间
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + "\n…（内容过长已截断）"


def build(kind: str, base: str, paper_name=None) -> str:
    parts, errs = [], []
    want = {"all": ["holdings", "daily"], "holdings": ["holdings"], "daily": ["daily"]}[kind]
    for w in want:
        try:
            parts.append({"holdings": lambda: fmt_holdings(base, paper_name),
                          "daily": lambda: fmt_daily(base)}[w]())
        except Exception as e:
            errs.append(f"{w}: {e}")
    if not parts:
        raise RuntimeError("没有可推送的内容 —— " + "; ".join(errs))
    head = f"# 📊 DSHQuant 每日推送\n🕐 {datetime.now():%m-%d %H:%M}"
    body = head + "\n\n" + "\n\n".join(parts)
    if errs:
        body += "\n\n> ⚠️ 部分内容不可用：" + "; ".join(errs)
    return body


def send(webhook: str, content: str, use_proxy: bool, fmt: str = "markdown_v2"):
    """fmt: markdown_v2（默认，支持真列表/多级引用）或 markdown（旧版，支持字体颜色）。"""
    if fmt not in MSG_TYPES:
        raise ValueError(f"不支持的消息类型：{fmt}")
    body = json.dumps({"msgtype": fmt, fmt: {"content": content}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener(use_proxy).open(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "ignore")
    except Exception as e:
        return False, f"发送失败：{type(e).__name__}: {str(e)[:160]}"
    try:
        d = json.loads(raw)
    except Exception:
        return False, f"响应非 JSON：{raw[:120]}"
    if d.get("errcode") == 0:
        return True, "ok"
    return False, f"errcode={d.get('errcode')} errmsg={d.get('errmsg')}"


def selftest(base: str, webhook, src, fmt: str = "markdown_v2") -> int:
    _log("== 自检 ==")
    _log(f"deck 地址   : {base}")
    _log(f"消息类型    : {fmt}"
         + ("（真列表/多级引用；需客户端 ≥4.1.36，安卓 ≥4.1.38，否则退化为纯文本）"
            if fmt == "markdown_v2" else "（旧版：支持字体颜色，不支持列表/缩进）"))
    ok = True
    if webhook:
        host = webhook.split("/")[2] if "//" in webhook else "?"
        _log(f"webhook     : 已配置（来源 {src}，主机 {host}，长度 {len(webhook)}）")
    else:
        _log("webhook     : ✗ 未配置 —— 复制 config/wecom.yaml.example 为 config/wecom.yaml 并填 webhook")
        ok = False
    try:
        d = deck_get(base, "/api/portfolio")
        _log(f"deck 连通   : ✓ 持仓 {len(d.get('positions') or [])} 只")
    except Exception as e:
        _log(f"deck 连通   : ✗ {e}")
        ok = False
    try:
        _d = deck_get(base, "/api/live/holdings", timeout=60)
        _pnl = _d.get("pnl") or {}
        _src = ((_pnl.get("rows") or [{}])[0].get("price_src") or "—")
        _log(f"盈亏数据    : ✓ {_pnl.get('n_holdings', '?')} 只 · "
             f"组合 {_pct(_pnl.get('total_ret'))} · 现价来源 {_src}")
    except Exception as e:
        _log(f"盈亏数据    : ✗ {e}（持仓将退化为不含盈亏）")
    _pa = paper_account()
    if _pa:
        _log(f"模拟账户    : ✓ {_pa['name']} · 本金 {_money(_pa.get('initial_cash'))} · "
             f"现金 {_money(_pa.get('cash'))} · 持仓 {len(_pa.get('positions') or {})} 只")
    else:
        _log("模拟账户    : ✗ paper.db 不可用 → 金额盈亏退化为只报收益率")
    try:
        fmt_daily(base)
        _log("日报        : ✓ 已生成")
    except Exception as e:
        _log(f"日报        : ✗ {e}（不影响持仓推送）")
    return 0 if ok else 2


def main():
    ap = argparse.ArgumentParser(description="DSHQuant → 企业微信群机器人推送（零端口暴露）")
    ap.add_argument("--type", choices=["all", "holdings", "daily"], default="all")
    ap.add_argument("--deck", default=None, help="deck 地址（默认 http://127.0.0.1:8787）")
    ap.add_argument("--webhook", default=None, help="企业微信机器人 webhook（覆盖配置文件）")
    ap.add_argument("--format", choices=list(MSG_TYPES), default=None,
                    help="消息类型：markdown_v2（默认，真列表/多级引用）或 markdown（旧版，支持字体颜色）")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    ap.add_argument("--selftest", action="store_true", help="只校验配置与连通性")
    ap.add_argument("--use-proxy", action="store_true", help="webhook 请求走系统代理")
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = ap.parse_args()

    cfg = load_config()
    base = args.deck or cfg.get("deck") or DEFAULT_DECK
    fmt = args.format or str(cfg.get("format") or "markdown_v2")
    webhook, src = resolve_webhook(args, cfg)

    if args.selftest:
        sys.exit(selftest(base, webhook, src, fmt))

    try:
        content = truncate(build(args.type, base, cfg.get("paper_account")), args.max_bytes)
    except Exception as e:
        _log(f"✗ 组装内容失败：{e}")
        sys.exit(1)

    if args.dry_run:
        _log("== dry-run（未发送）==")
        _log(content)
        _log(f"== 字节数 {len(content.encode('utf-8'))} ==")
        sys.exit(0)

    if not webhook:
        _log("✗ 未配置 webhook。三选一：")
        _log("  1) 复制 config/wecom.yaml.example 为 config/wecom.yaml 并填 webhook")
        _log("  2) 设环境变量 WECOM_WEBHOOK")
        _log("  3) 运行时加 --webhook <url>")
        sys.exit(2)

    ok, detail = send(webhook, content, args.use_proxy, fmt)
    if ok:
        _log(f"✓ 已推送到企业微信（{fmt}，{len(content.encode('utf-8'))} 字节，webhook 来源 {src}）")
        sys.exit(0)
    _log(f"✗ {detail}")
    sys.exit(1)


if __name__ == "__main__":
    main()
