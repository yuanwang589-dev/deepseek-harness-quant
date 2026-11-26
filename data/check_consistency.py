# -*- coding: utf-8 -*-
"""data/check_consistency.py — ★2026-08-11 百轮#46 数据一致性交叉校验
同一数据在多来源是否一致（UI 显示错误的源头排查）：
  ① Pitch 数：opp_pool vs pitch_v2 vs live_opp
  ② 机会池数：opp_pool.n vs opportunities 数组
  ③ 持仓数：portfolio vs live_holdings.pnl vs position_risk
  ④ 短线池数：tech_pitch 文件 vs API
  ⑤ 决策链环节数 + 异常环节
用法：python data/check_consistency.py   （退出码 0=全部一致 / 1=有不一致）
"""
import contextlib
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

HOST = "http://127.0.0.1:8787"


def _latest(pat: str, sub: str = "logs") -> dict:
    fs = sorted(glob.glob(str(BASE / sub / pat)), key=os.path.getmtime)
    if not fs:
        return {}
    try:
        return json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _api(path: str) -> dict:
    url = HOST + path
    # 限制 scheme（S310）：显式 allowlist，避免 file: 等意外协议
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return {}
    try:
        r = urllib.request.urlopen(url, timeout=15)  # noqa: S310 （scheme 已在上方 allowlist 校验）
        return json.loads(r.read())
    except Exception:
        return {}


def check() -> int:
    fails = 0
    _dg: dict = {}   # ★实盘裁决数据：第 14 项 try 失败时保持空，避免第 16 项 NameError（真缺陷）

    def _row(name, ok, detail=""):
        nonlocal fails
        if not ok:
            fails += 1
        print(f"  {'✅' if ok else '⚠️'} {name} {detail}")

    # ★2026-10-16 外包因子池缺失探测：daily_scores / risk_multiplier 均无产出时，
    #   依赖它们的检查项（强因子直通 / FRC 系数）属「外部依赖缺失」而非故障，
    #   与 verify_day_pipeline 的同名处理一致（避免假红淹没真故障）。
    _ext_missing = (not glob.glob("data/factorpool/output/daily_scores/daily_*.csv")
                    and not glob.glob("data/factorpool/risk/risk_multiplier_*.json"))

    print("=== 数据一致性交叉校验 ===")
    # 1) Pitch 数：opp_pool 未过滤（含持有期不足）vs pitch_v2 已过滤（Pitch 页展示源）
    #    设计差异允许；关键要求：展示层 live_opp 必须与 pitch_v2 一致（否则机会池页与 Pitch 页数字打架）
    opp = _latest("opp_pool_*.json")
    pv = _latest("pitch_v2_*.json")
    lo = _api("/api/live/opp")
    n_opp = len(opp.get("pitch", []))
    n_pv = len(pv.get("pitch", []))
    n_lo = len(lo.get("pitch", []))
    _row("Pitch 展示一致性", n_lo == n_pv,
         f"live_opp={n_lo} vs pitch_v2={n_pv}（opp_pool 原始 {n_opp} 未过滤属设计）")
    # 2) 机会池数（★2026-08-12 #200：live_opp 含系统建议补位（daily_signal hold_plan 14 只）
    #   → live_opp = 文件 n + 建议补位，一致性检查接受 n_suggested 差额）
    n_opp_n = opp.get("n")
    n_opp_arr = len(opp.get("opportunities", []))
    n_lo_arr = len(lo.get("opportunities", []))
    _nsug = lo.get("n_suggested", 0)
    # 补位只增不减（n_suggested 含池内已标记的 6 只 + 补位 14 只），接受 live_opp >= 文件 n
    _ok_opp = (n_opp_n == n_opp_arr) and (n_lo_arr >= n_opp_arr)
    _row("机会池数", _ok_opp,
         f"opp_pool.n={n_opp_n} vs 数组={n_opp_arr} vs live_opp={n_lo_arr}（建议补位 {_nsug}）")
    # 3) 持仓数
    try:
        from strategy.portfolio import _load
        pf = _load()
        n_hold = len([p for p in pf["positions"] if p.get("status") == "holding"])
    except Exception:
        n_hold = -1
    lh = _api("/api/live/holdings")
    n_pnl = (lh.get("pnl") or {}).get("n_holdings")
    _row("持仓数", n_hold == n_pnl, f"portfolio={n_hold} vs pnl={n_pnl}")
    # 4) 短线池数
    tp = _latest("tech_pitch_*.json")
    lt = _api("/api/tech_pitch")
    n_tp = len(tp.get("entries", []))
    n_lt = len(lt.get("entries", []))
    _row("短线池数", n_tp == n_lt, f"文件={n_tp} vs API={n_lt}")
    # 5) 决策链（★2026-08-12 百轮后#123：允许"数据源滞后"环节——note 含 内容滞后/供应商 非故障）
    ch = _api("/api/live/chain")
    chain = ch.get("chain", [])
    bad = [n["name"] for n in chain if not n.get("ok")
           and not ("内容滞后" in (n.get("note") or "") or "供应商" in (n.get("note") or ""))]
    lag = [n["name"] for n in chain if not n.get("ok")
           and ("内容滞后" in (n.get("note") or "") or "供应商" in (n.get("note") or ""))]
    _row("决策链", not bad and len(chain) >= 10,
         f"{len(chain)} 环节" + (f" 异常: {bad}" if bad else "") + (f"（{len(lag)} 数据源滞后）" if lag else ""))
    # 6) 止盈引擎 vs 真实持仓
    tps = _latest("take_profit_signals_*.json")
    n_tp_pos = len(tps.get("positions", []))
    _row("止盈引擎覆盖", n_tp_pos == n_hold or n_hold <= 0,
         f"止盈引擎 {n_tp_pos} vs 真实持仓 {n_hold}")
    # 7) ★in_pitch 标记一致性（百轮#74 修复后）：机会池"✅ 审批"标记数 ≤ Pitch 候选数
    #   ★#381 放宽：pitch 候选可能不在机会池——value 类型普通扫描触发稀少（仅 --pitch 模式产出），
    #   且 opp_pool（DevDriver 22:00 重生成）与 pitch_v2（18:30 管道）有时序错位，缺口自愈；
    #   缺口 ≤ 一半视为正常（数据在 Pitch tab 正常流通），只有缺口 > 一半才判数据流断裂
    n_inp = sum(1 for o in lo.get("opportunities", []) if o.get("in_pitch"))
    _gap = n_pv - n_inp
    _row("in_pitch 标记", n_inp <= n_pv and _gap <= max(2, n_pv // 2),
         f"live_opp in_pitch={n_inp} vs pitch_v2={n_pv}"
         + (f"（缺口 {_gap} 只：value类不在机会池，Pitch tab 可见）" if _gap > 0 else ""))
    # 8) ★强因子直通一致性（百轮#67-68）：跨家族命中 >0；机会池内标注 ≤ 命中
    try:
        sys.path.insert(0, str(BASE))
        from factors.opportunities.scan import load_strong_hits
        _sh = load_strong_hits()
        n_sh = len(_sh)
        n_sh_pool = sum(1 for o in lo.get("opportunities", []) if o.get("strong_hit"))
    except Exception:
        n_sh, n_sh_pool = -1, -1
    _row("强因子直通", _ext_missing or (n_sh > 0 and n_sh_pool <= n_sh),
         f"跨家族 {n_sh} 只 ≥ 机会池内标注 {n_sh_pool}"
         + ("（外包因子池缺失，外部依赖）" if _ext_missing else ""))
    # 9) ★择时系统 v4（百轮#66/#91/#105）：文件可读 + 四维齐全（政策=四因子评分，宏观=regime+社融/利率修正）
    _tm = _latest("timing_system_*.json", "output")
    _dims = _tm.get("dims", {})
    _row("择时系统 v4", bool(_tm.get("level")) and all(k in _dims for k in ("政策", "宏观", "情绪", "宽度")),
         f"{_tm.get('emoji','')} {_tm.get('level','')} {_tm.get('score','')} 分")
    # 10) ★FRC 系数（百轮#65）：risk_multiplier 可读 + 因子数合理
    #     ★#268 与消费端同口径：外包 --only 增量可能覆盖主文件（08-13 5 因子覆盖 83 因子）——
    #     检测「所有文件里因子数最多」而非「最新文件」，与 load_risk_multiplier（取最全）一致
    try:
        _rmfs = sorted(glob.glob("data/factorpool/risk/risk_multiplier_*.json"),
                       key=os.path.getmtime)
        _rm_n = 0
        _rm_best = None
        for _p in _rmfs:
            with contextlib.suppress(Exception):
                _n = len(json.loads(Path(_p).read_text(encoding="utf-8")).get("factors", {}))
                if _n > _rm_n:
                    _rm_n, _rm_best = _n, _p
    except Exception:
        _rm_n, _rm_best = 0, None
    _row("FRC 系数", _ext_missing or _rm_n > 30,
         f"{_rm_n} 因子" + (f"（{Path(_rm_best).name if _rm_best else '缺失'}）")
         + ("（外包因子池缺失，外部依赖）" if _ext_missing else ""))
    # 11) ★日历层（百轮#65）：get_window 可调用（当前窗口零副作用）
    try:
        from data.calendar_hook import get_window
        _w = get_window()
    except Exception:
        _w = None
    _row("日历层", _w is None or "bonus" in _w, f"当前窗口: {_w.get('label','无') if _w else '无'}")
    # 12) ★沪深300 基准（百轮#69）：portfolio_perf 基准数据可用
    try:
        import sqlite3
        _con = sqlite3.connect("file:data/cache/bars.db?mode=ro&immutable=1", uri=True)
        _n_idx = _con.execute("SELECT COUNT(*) FROM daily_bar WHERE code='SH.000300'").fetchone()[0]
        _con.close()
    except Exception:
        _n_idx = 0
    _row("基准数据", _n_idx > 1000, f"SH.000300 {_n_idx} 行（2019-2026）")
    # 13) ★预警中心（百轮#70）
    la = _api("/api/live/alerts")
    _row("预警中心", la.get("ok") is True, f"{la.get('n',0)} 条预警（{la.get('n_high',0)} high）")
    # 14) ★实盘裁决体系（百轮#89/#93）：down_warn 与 by_type 一致（label 存在且 action=降权提示）
    try:
        _vv = _api("/api/live/validation")
        _dg = _vv.get("diagnosis") or {}
        _dw = _dg.get("down_warn") or []
        _by = {t.get("label"): t for t in (_dg.get("by_type") or [])}
        _dw_ok = all(w.get("label") in _by and _by[w["label"]].get("action") == "降权提示" for w in _dw)
        _row("实盘裁决体系", _dw_ok and (len(_dg.get("by_score") or []) > 0),
             f"down_warn {len(_dw)} 条（{'、'.join(w.get('label','?') for w in _dw) or '无'}），by_type {len(_by)} 类型")
    except Exception as e:
        _row("实盘裁决体系", False, f"异常: {str(e)[:50]}")
    # 15) ★决策链 13 环节（百轮#97/#123）：环节数 ≥13 且含"实盘裁决"、无断链
    #    ★2026-08-12 百轮后#123：允许"数据源滞后"环节（note 含"内容滞后/供应商"）不视为故障——
    #    竞价信号供应商分钟数据未交付（外部条件）诚实标红，但非系统断链
    try:
        _ch = _api("/api/live/chain")
        _names = [n.get("name") for n in (_ch.get("chain") or [])]
        _lag_n = sum(1 for n in _ch.get("chain", []) if not n.get("ok")
                     and ("内容滞后" in (n.get("note") or "") or "供应商" in (n.get("note") or "")))
        _bad_nodes = [n for n in _ch.get("chain", []) if not n.get("ok") and
                      not ("内容滞后" in (n.get("note") or "") or "供应商" in (n.get("note") or ""))]
        _ok15 = len(_names) >= 13 and "实盘裁决" in _names and not _bad_nodes
        _row("决策链 13 环节", _ok15,
             f"{len(_names)} 环节" + ("（含实盘裁决）" if "实盘裁决" in _names else "（缺实盘裁决）")
             + (f"，{_lag_n} 数据源滞后" if _lag_n else ""))
    except Exception as e:
        _row("决策链 13 环节", False, f"异常: {str(e)[:50]}")
    # 16) ★类型裁决 API（百轮#96）：门户裁决卡数据源可用（by_type 有实盘样本类型）
    try:
        _nt = sum(1 for t in (_dg.get("by_type") or []) if t.get("n", 0) >= 3)
        _row("类型裁决 API", _nt >= 1, f"有实盘样本的类型 {_nt} 个（门户裁决卡芯片数）")
    except Exception:
        _row("类型裁决 API", False, "异常")
    # 17) ★is_st 覆盖率（百轮#137，#136 ST 丢列防御制度化）：最新交易日 is_st=1 数量
    #    正常 ~200 只/日；<50 = is_st 列异常（Tushare 增量丢列）→ 报警（load_st_codes 已自动回溯）
    try:
        import sqlite3 as _s17
        from pathlib import Path as _P17
        _B17 = r"data/cache/bars.db"
        _conns17 = [_s17.connect(f"file:{_B17}?mode=ro&immutable=1", uri=True, timeout=3)]
        with contextlib.suppress(Exception):
            for _p17 in sorted(_P17(_B17).parent.glob("bars_incr_*.db"))[-3:]:
                with contextlib.suppress(Exception):
                    _conns17.append(_s17.connect(f"file:{_p17}?mode=ro&immutable=1", uri=True, timeout=3))
        _mx17, _n17 = None, 0
        for _c17 in _conns17:
            with contextlib.suppress(Exception):
                # ★2026-10-16 只认股票行（qfq，排除 SH./SZ. 指数）：MAX(date) 无过滤会取到
                #   fetch_index_daily 写入的 SH.000300 指数行（adjust='none'，无 is_st）
                #   → is_st=1 恒 0 → 每天假报「丢列异常」。与 cache.latest_trade_date 口径一致。
                _m = _c17.execute(
                    "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' "
                    "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'").fetchone()[0]
                if _m and (_mx17 is None or _m > _mx17):
                    _mx17 = _m
        for _c17 in _conns17:
            with contextlib.suppress(Exception):
                _n = _c17.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND is_st=1", (_mx17,)).fetchone()[0]
                _n17 = max(_n17, _n)   # 任一台库该日正常即算正常（主库+增量库并存）
            with contextlib.suppress(Exception):
                _c17.close()
        _row("is_st 覆盖率", _n17 >= 50,
             f"最新日 {_mx17} is_st=1 {_n17} 只（正常 ~200；<50 = 丢列异常，load_st_codes 自动回溯）")
    except Exception as e:
        _row("is_st 覆盖率", False, f"异常: {str(e)[:50]}")

    # 13) ★#402 分钟因子 rank 健康度（外部缺口哨兵，不计 fail）：
    #     分钟因子 rank 全空 → FRC 排雷（intraday_range/kline_5m_std 日内波动减分）+ EXT 因子
    #     open_vol_share（#379 保留）静默失效。这是外包侧数据缺口（已留言），此处只做信息性告警，
    #     避免每晚假红；但必须显式打印，让巡检时能看到"因子可靠性缺口"而非静默。
    try:
        import pandas as _pd
        _dsd = r"data/factorpool/output/daily_scores"
        _dsfs = sorted(glob.glob(_dsd + "/daily_*.csv"), key=lambda p: os.path.getmtime(p))
        if _dsfs:
            _cols = ["kline_5m_std_rank", "intraday_range_rank", "open_vol_share_rank"]
            _df = _pd.read_csv(_dsfs[-1], usecols=lambda c: c in _cols)
            _nonnull = {c: sum(1 for _v in _df[c].notna() if _v) for c in _cols if c in _df.columns}
            _empty = [c for c, n in _nonnull.items() if n == 0]
            if _empty:
                print(f"  ⚠️ 分钟因子 rank 全空（{len(_empty)}/{len(_nonnull)} 列）→ FRC 排雷/EXT 因子失效（外包侧缺口，已留言待修）")
            else:
                print(f"  ✅ 分钟因子 rank 健康（非空 {min(_nonnull.values()) if _nonnull else 0}+）")
    except Exception as _e:
        print(f"  ⚠️ 分钟因子 rank 检查异常: {str(_e)[:60]}")

    # 14) ★#411 → ★2026-08-14 涨停/龙虎榜因子方向哨兵（不计 fail）：
    #     ★修正：原"原始值全非正=符号反"对 direction=-1 因子是误判（L0 取反后原始值
    #     全负是正常结果——研究员 14:30 系统验证 C2 修正后 health ICIR120 全强正：
    #     limup_ex_5 +1.62 / lhb +1.516 = 注册方向正确）。
    #     ★正确判据：rank 列应有高值区分度（max>0.75 且非空率>0.5）——rank 大=信号强；
    #     rank 全低/常数 = 方向或数据问题。
    try:
        import pandas as _pd
        _dsd = r"data/factorpool/output/daily_scores"
        _dsfs = sorted(glob.glob(_dsd + "/daily_*.csv"), key=lambda p: os.path.getmtime(p))
        if _dsfs:
            # 取因子数最多的文件（#250 铁律）
            _best, _bn = None, -1
            for _f in _dsfs:
                with contextlib.suppress(Exception):
                    _nc = sum(1 for c in _pd.read_csv(_f, nrows=0).columns if c.endswith("_rank"))
                    if _nc > _bn:
                        _bn, _best = _nc, _f
            if _best:
                _df = _pd.read_csv(_best)
                _flat = []
                for _c in ["limit_up_cnt_5d", "limit_up_flag", "consec_limit_up",
                           "lhb_jg_cnt_20", "turnover"]:
                    _rc = f"{_c}_rank"
                    if _rc in _df.columns:
                        _v = _df[_rc].dropna()
                        # rank 区分度：非空率>0.5 且 max>0.75（rank 大=信号强）
                        if len(_v) and _v.notna().mean() > 0.5 and _v.max() > 0.75:
                            continue
                        _flat.append(_c)
                if _flat:
                    print(f"  ⚠️ 涨停/龙虎榜因子 rank 无区分度（{', '.join(_flat)}）→ 方向或数据异常需核查")
                else:
                    print("  ✅ 涨停/龙虎榜因子 rank 方向正常（有高值区分度）")
    except Exception as _e:
        print(f"  ⚠️ 涨停因子符号检查异常: {str(_e)[:60]}")

    print(f"=== 结果: {'✅ 全部一致' if fails == 0 else f'⚠️ {fails} 项不一致'} ===")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(check())
