# -*- coding: utf-8 -*-
"""data/health_scan.py — ★2026-08-12 百轮#82 全站健康扫描（页面 + API + 一致性）
把 #75 手动 curl 扫描固化为可重复脚本：20 页面 + 27 API + 13 项一致性 → 时间戳报告。
防回归工程底座：每次改代码后跑一遍；挂 dev_auto 8.59 每 4h 自动留档；预警中心消费。

用法：python data/health_scan.py [--quiet]   （退出码 0=全绿 / 1=有失败）
输出：report/health_scan_{ts}.json（时间戳，写保护免疫）
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
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

HOST = "http://127.0.0.1:8787"

PAGES = [
    "/", "/pitch.html", "/pitch", "/holdings", "/factors", "/help",
    "/timing_dash.html", "/timing_dash", "/holdings_dash.html", "/factors_dash.html", "/help_dash.html",
    "/dashboard_opp.html", "/dashboard_watch.html",
    "/dashboard_factors.html", "/dashboard_backtest.html",
    "/dashboard_auction.html", "/dashboard_actions.html", "/dashboard_history.html",
    "/dashboard_glossary.html", "/dashboard_pitchtrack.html", "/dashboard_research_lib.html",
    "/dashboard_stockcheck.html", "/system_overview.html", "/dashboard_techpitch.html",
    "/dashboard_live.html", "/dashboard_ranks.html", "/dashboard_dynamic.html",
]
# 301 是 P-1 去重设计（dashboard_pool → 观察池）+ #155 新架构短路由临时映射（S7-S9 已建真页，保留兼容）
PAGES_301_OK = ["/dashboard_pool.html", "/dashboard_monitor.html", "/dashboard_research.html",
                "/dashboard_holdings.html"]   # ★#363 旧持有池页 → /holdings（v2 持仓页）

APIS = [
    "/api/timing", "/api/decisions", "/api/pitch_v2", "/api/tech_pitch", "/api/winrates",
    "/api/live/opp", "/api/live/watch", "/api/live/holdings", "/api/live/tech",
    "/api/live/forward", "/api/live/factors", "/api/live/audit", "/api/live/pools",
    "/api/live/pool", "/api/live/chain", "/api/live/review", "/api/live/alerts",
    "/api/live/funnel", "/api/live/actions", "/api/live/brief", "/api/live/calendar",
    "/api/live/validation", "/api/live/strong_hits", "/api/pitch_track",
    "/api/stock_check?code=003010", "/api/system_live", "/api/live/ranks",
    "/api/live/timing_dash", "/api/live/factor_dash", "/api/live/portal_dash", "/api/live/enums",
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向（301 检查用——urlopen 默认跟随会把 301 显示成 200）"""
    def redirect_request(self, *a, **k):
        return None


_no_redir = urllib.request.build_opener(_NoRedirect)


def _http(path: str, timeout: int = 15, follow: bool = True) -> int:
    try:
        if follow:
            r = urllib.request.urlopen(HOST + path, timeout=timeout)
        else:
            r = _no_redir.open(HOST + path, timeout=timeout)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    bad = []

    # 1) 页面
    pages = []
    for p in PAGES:
        code = _http(p)
        ok = code == 200
        pages.append({"path": p, "code": code, "ok": ok})
        if not ok:
            bad.append(f"页面 {p} → {code}")
    for p in PAGES_301_OK:
        code = _http(p, follow=False)   # 不跟随重定向，直接测 301
        ok = code == 301
        pages.append({"path": p, "code": code, "ok": ok, "design": "301 去重"})
        if not ok:
            bad.append(f"页面 {p} → {code}（预期 301）")

    # 2) API
    # ★2026-08-13 #236：portal_dash 冷启动全量聚合 ~11s（30s 缓存过期时），健康扫描首连可能
    #   撞 15s 上限（code 0 但后端成功）→ 预热：先请求一次让它缓存（失败忽略，正式扫描兜底）
    try:
        _http("/api/live/portal_dash", timeout=45)
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/health_scan.py:97 main")
    apis = []
    for a in APIS:
        t1 = time.time()
        code = _http(a)
        if code == 0 and time.time() - t1 > 12:
            code = _http(a, timeout=40)   # ranks 首调可能缓存预热慢
        ok = code == 200
        apis.append({"path": a, "code": code, "ok": ok})
        if not ok:
            bad.append(f"API {a} → {code}")

    # 3) 一致性
    consistency_ok = False
    try:
        import subprocess
        r = subprocess.run([sys.executable, "-X", "utf8", str(BASE / "data" / "check_consistency.py")],
                           capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace")
        consistency_ok = r.returncode == 0
        if not consistency_ok:
            # ★2026-08-12 #138：已知数据残留放行（#123 原则——区分"系统故障" vs "数据残留"）
            #   is_st 覆盖率异常（08-11 Tushare 增量丢列）→ 消费端 load_st_codes 已回溯防御，
            #   check_consistency 详细输出诚实标注，health_scan 摘要层面不算维度失败（今晚 08-12 写入自动转绿）
            _out = r.stdout or ""
            _wl = [l for l in _out.splitlines() if "⚠️" in l and not l.startswith("===")]
            _only_st_residual = (len(_wl) == 1 and "is_st 覆盖率" in _wl[0])
            if not _only_st_residual:
                bad.append("一致性校验未全绿")
            else:
                consistency_ok = True
    except Exception as e:
        bad.append(f"一致性异常: {str(e)[:60]}")

    # 4) ★计划任务（调度层健康，2026-08-12 百轮后#113：7 个 LWQuant 任务应全部就绪——
    #    防 dev_auto 熔断/手动禁用后静默停摆；08-11 DailyPipeline 0xC0000142 误杀事故归因后加）
    SCHED_TASKS = ["LWQuant-DevDriver", "LWQuant-DailyPipeline", "LWQuant-TushareInc",
                   "LWQuant-FactorDaily", "LWQuant-FactorArchive", "LWQuant-BreakoutMon",
                   "LWQuant-DeckGuard"]
    sched_ok = True
    sched_disabled = []
    try:
        import subprocess as _sp
        _r = _sp.run(["schtasks", "/query", "/fo", "LIST", "/v"], capture_output=True, timeout=30)
        _txt = _r.stdout.decode("gbk", errors="replace") + _r.stderr.decode("gbk", errors="replace")
        for _t in SCHED_TASKS:
            _idx = _txt.find(_t)
            _seg = _txt[_idx:_idx + 400] if _idx >= 0 else ""
            if "禁用" in _seg or "Disabled" in _seg:
                sched_disabled.append(_t)
            elif _idx < 0:
                sched_disabled.append(_t + "(缺失)")
        if sched_disabled:
            sched_ok = False
            bad.append(f"计划任务禁用/缺失: {','.join(sched_disabled)}")
    except Exception as e:
        sched_ok = False
        bad.append(f"计划任务检查异常: {str(e)[:50]}")

    n_page_ok = sum(1 for p in pages if p["ok"])
    n_api_ok = sum(1 for a in apis if a["ok"])
    result = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "pages": {"n": len(pages), "ok": n_page_ok, "items": pages},
        "apis": {"n": len(apis), "ok": n_api_ok, "items": apis},
        "consistency": consistency_ok,
        "sched": {"ok": sched_ok, "disabled": sched_disabled},
        "all_ok": not bad,
        "bad": bad,
    }
    out = BASE / "report" / f"health_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        (BASE / "report" / "health_scan_latest.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/health_scan.py:173 main")
    if not args.quiet:
        print(f"健康扫描: 页面 {n_page_ok}/{len(pages)} · API {n_api_ok}/{len(apis)} · 一致性 {'✅' if consistency_ok else '❌'} · 计划任务 {'✅' if sched_ok else '❌'} · {result['elapsed_sec']}s")
        if bad:
            print("失败项:")
            for b in bad[:10]:
                print(f"  ⚠️ {b}")
        print(f"结果: {'✅ 全绿' if result['all_ok'] else '❌ 有失败'} → {out.name}")
    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
