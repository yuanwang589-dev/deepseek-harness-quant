# -*- coding: utf-8 -*-
"""data/health_check.py — 系统一键巡检（总指导 · 2026-08-10）

把 F6 端到端验收的"数据流时效矩阵"做成可随时重跑的巡检：
  1. 全链产出时效（每链最新文件 mtime vs 期望时效）
  2. 计划任务状态（DSHQuant-* 是否存在、下次运行）
  3. Deck 存活（8787 端口 + 关键路由）
  4. 磁盘/数据库健康（bars 最新交易日、增量库数量）

用法：python data/health_check.py [--json]
输出：控制台报告 + logs/health_check_{ts}.json（时间戳，写保护免疫）
"""
import argparse
import glob
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
BARS_DB = r"data\cache\bars.db"

# (名称, glob 模式, 目录, 期望最大时效(小时), 说明)
CHAINS = [
    ("审计", "data_audit_report_*.json", "report", 12, "dev_auto 每 4h"),
    ("因子档案", "因子档案_2*.json", "output", 24, "17:40 链 + dev_auto 4h"),
    ("拥挤度", "factor_crowding_*.json", "report", 24, "17:40 链"),
    ("EP-ICIR", "ep_icir_full_*.json", "report", 24, "17:40 链"),
    ("基本面因子", "fundamental_factor_report_*.json", "report", 24, "17:40 链"),
    ("技术裁决", "factor_pool_report_verdict_*.json", "report", 24, "17:40 链"),
    ("机会池", "opp_pool_*.json", "logs", 26, "18:30 全链/4h"),
    ("Pitch", "pitch_v2_*.json", "logs", 26, "18:30 全链"),
    ("科技池", "tech_pitch_*.json", "logs", 26, "dev_auto 4h"),
    ("远期池", "pitch_track_pool_*.json", "logs", 28, "dev_auto 4h"),
    ("止损告警", "stop_alerts_*.json", "logs", 26, "dev_auto 4h"),
    ("今日信号", "daily_signal_*.json", "output", 26, "dev_auto 4h"),
    ("涨跌幅榜", "rank_live_*.json", "logs", 26, "dev_auto 4h"),
    ("策略标注", "策略标注卡片*.json", "output", 720, "D 包静态（非每日刷新，30 天阈值）"),
    ("持仓", "portfolio_*.json", "logs", 72, "用户操作驱动（缺失=未买入，正常）"),
]


def latest(pattern, sub):
    fs = sorted(glob.glob(str(BASE / sub / pattern)), key=os.path.getmtime)
    return fs[-1] if fs else None


def check_chains() -> list:
    rows = []
    for name, pat, sub, max_h, src in CHAINS:
        f = latest(pat, sub)
        if not f:
            # ★持仓为可选链：缺失=未开始买入（deck_buys 空），不判故障
            if name == "持仓":
                rows.append({"name": name, "ok": True, "issue": "未开始买入（正常）",
                             "mtime": None, "hours": None, "src": src})
            else:
                rows.append({"name": name, "ok": False, "issue": "文件缺失", "mtime": None, "hours": None, "src": src})
            continue
        age_h = (time.time() - os.path.getmtime(f)) / 3600
        ok = age_h <= max_h
        rows.append({"name": name, "ok": ok, "issue": "" if ok else f"陈旧 {age_h:.0f}h（>期望 {max_h}h）",
                     "mtime": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%m-%d %H:%M"),
                     "hours": round(age_h, 1), "src": src})
    return rows


def check_tasks() -> list:
    """计划任务就绪度。

    ★2026-10-18 二次修复（任务书 D1 的 ⚠️ 收口）：
      上一版把 schtasks 异常降级为"未验证"——不崩了，但**永远查不出结果**：
      本机安全策略把 schtasks.exe 列入程序黑名单，6 项恒为"未验证"，这个检查形同虚设。
      同时它还硬编码了 DSHQuant-FactorDaily / FactorArchive 两个**根本没有脚本**的任务
      （因子池 scheduler.py 不随包发布），schtasks 一旦可用就会恒报"缺失"。

      现改为走 data/task_registry.py（PowerShell Get-ScheduledTask，不受黑名单影响）：
        · 任务名按后缀解析，前缀无关，不存在的不列入规格
        · 查不到 → 明确区分"未注册"与"查询失败"，不再混淆
    """
    try:
        from data.task_registry import snapshot as _snap
        s = _snap()
    except Exception as e:
        return [{"name": "(全部)", "ok": False, "unverified": True,
                 "issue": f"task_registry 不可用（{type(e).__name__}）"}]
    if not s.get("queryable"):
        return [{"name": "(全部)", "ok": False, "unverified": True,
                 "issue": s.get("note") or "计划任务查询失败"}]
    rows = []
    for suffix, info in s["found"].items():
        st = info["state"]
        if st == "Missing":
            rows.append({"name": f"*-{suffix}", "ok": False,
                         "issue": "未注册（例行运行不会自动触发）"})
        elif st == "Disabled":
            rows.append({"name": info["name"], "ok": False, "issue": "已被禁用"})
        else:
            rows.append({"name": info["name"], "ok": True, "issue": ""})
    return rows


def check_deck() -> dict:
    try:
        with socket.create_connection(("127.0.0.1", 8787), timeout=3):
            port = True
    except OSError:
        port = False
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:8787/", timeout=5)
        http = r.status == 200
    except Exception:
        http = False
    return {"ok": port and http, "port": port, "http": http}


def check_db() -> dict:
    out = {}
    try:
        con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        out["bars_latest"] = con.execute(
            "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
        out["n_inc_db"] = len(glob.glob(r"data\cache\bars_incr_*.db"))
        con.close()
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON（含保存文件）")
    args = ap.parse_args()
    now = datetime.now()
    chains = check_chains()
    tasks = check_tasks()
    deck = check_deck()
    db = check_db()
    n_ok = sum(1 for c in chains if c["ok"])
    n_task = sum(1 for t in tasks if t["ok"])
    n_unv = sum(1 for t in tasks if t.get("unverified"))
    lines = [f"===== 系统一键巡检 {now:%Y-%m-%d %H:%M} =====",
             f"数据链: {n_ok}/{len(chains)} 时效正常"]
    for c in chains:
        flag = "✅" if c["ok"] else "❌"
        lines.append(f"  {flag} {c['name']}: {c.get('mtime') or '—'} {c.get('issue') or ''}")
    lines.append(f"计划任务: {n_task}/{len(tasks)} 就绪"
                 + (f"（{n_unv} 项无法查询，未验证）" if n_unv else ""))
    for t in tasks:
        lines.append(f"  {'✅' if t['ok'] else '❌'} {t['name']} {t.get('issue')}")
    lines.append(f"Deck(8787): {'✅' if deck['ok'] else '❌'} 端口={deck['port']} HTTP={deck['http']}")
    lines.append(f"数据库: bars 最新 {db.get('bars_latest')} | 增量库 {db.get('n_inc_db')} 个")
    text = "\n".join(lines)
    print(text)
    if args.json:
        out = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "chains": chains, "tasks": tasks,
               "deck": deck, "db": db, "summary": f"{n_ok}/{len(chains)} 链 + {n_task}/{len(tasks)} 任务"}
        p = BASE / "logs" / f"health_check_{now.strftime('%Y%m%d_%H%M%S')}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已存 {p.name}")


if __name__ == "__main__":
    main()
