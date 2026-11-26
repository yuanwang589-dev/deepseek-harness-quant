# -*- coding: utf-8 -*-
"""risk/position_monitor.py — S6 持仓风险实时监控（短板补齐收尾）

对 v3 当前持仓做组合风险体检（数据全部来自本地 daily_bar，无网络）：
  个股维度：近 20 日波动率 / 距 MA50 回撤 / ATR(20) 止损距离 / 60 日最大回撤
  组合维度：单票集中度（等权 1/n）/ Top5 集中度 / 持仓间平均相关性（伪分散检测）/ 组合波动率近似

输出：report/position_risk.json（看板第 5 页消费）+ 控制台摘要

用法：
  python risk/position_monitor.py                        # 默认取 v3 最新信号持仓
  python risk/position_monitor.py --codes 600519.SH,000001.SZ
  python risk/position_monitor.py --limit 50             # 持仓太多时限制样本
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
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache

OUT_DIR = BASE / "report"


def load_v3_holdings(date: str, limit=None) -> list:
    """取 v3 最新信号持仓清单（output/daily_signal_*.json glob 取最新）
    ★#370 修复：原读固定名 daily_signal.json（08-10 旧残留、内容 08-07 滞后 6 天）"""
    import glob as _g
    fs = sorted([Path(x) for x in _g.glob(str(BASE / "output" / "daily_signal_*.json"))],
                key=lambda x: x.stat().st_mtime)
    if fs:
        d = json.loads(fs[-1].read_text(encoding="utf-8"))
        codes = d.get("codes", [])
    else:
        sig = BASE / "output" / "daily_signal.json"
        if sig.exists():
            d = json.loads(sig.read_text(encoding="utf-8"))
            codes = d.get("codes", [])
        else:
            from strategy.equal_weight_timing import portfolio
            codes = portfolio(date)["codes"]
    return codes[:limit] if limit else codes


def compute(codes: list, date: str) -> dict:
    cache = DailyCache()
    closes = {}
    for code in codes:
        df = cache.get_daily(code, start=None, end=date, adjust="qfq")
        if df is None or df.empty or len(df) < 40:
            continue
        closes[code] = df.set_index("date").sort_index()["close"].astype(float)
    if not closes:
        return {"error": "无有效持仓数据", "codes": codes[:5], "n_codes": len(codes)}

    px = pd.DataFrame(closes)
    ret = px.pct_change()
    last = px.iloc[-1]

    # 个股指标
    rows = []
    for c in px.columns:
        s = px[c].dropna()
        if len(s) < 20:
            continue
        vol20 = ret[c].tail(20).std() * np.sqrt(252)
        ma50 = s.tail(50).mean() if len(s) >= 50 else np.nan
        dd_ma50 = (s.iloc[-1] / ma50 - 1) if ma50 == ma50 else np.nan
        atr = ret[c].tail(20).std() * s.iloc[-1]          # 简化 ATR ≈ 20 日波动×价
        dd60 = (s.tail(60) / s.tail(60).cummax() - 1).min() if len(s) >= 30 else np.nan
        rows.append({"code": c, "close": round(float(s.iloc[-1]), 2),
                     "vol20": round(float(vol20), 3), "dd_ma50": round(float(dd_ma50), 4),
                     "atr_dist": round(float(atr), 2), "dd60": round(float(dd60), 4)})
    per = pd.DataFrame(rows).set_index("code")

    # 组合维度
    n = len(per)
    w = 1.0 / n
    top5 = per["close"].nlargest(5)
    corr = ret[per.index].corr()
    avg_corr = float(corr.values[np.triu_indices(n, k=1)].mean()) if n > 1 else 0.0
    # 组合波动率近似（等权，对角项忽略协方差 → sqrt(w²·Σσ²)）
    port_vol = float(np.sqrt((per["vol20"] ** 2).mean() / n)) if n else np.nan
    # ★2026-08-11 百轮#11 行业集中度（M4 纪律：单行业≤20%；industry 从持仓/机会池反查）
    ind_map = _industry_map(codes)
    ind_cnt = {}
    for _c in codes:
        _i = ind_map.get(_c, "未知")
        ind_cnt[_i] = ind_cnt.get(_i, 0) + 1
    top_ind = max(ind_cnt.items(), key=lambda x: x[1]) if ind_cnt else ("未知", 0)
    ind_pct = round(top_ind[1] / n, 4) if n else 0.0

    return {
        "date": date,
        "n_holdings": n,
        "per_stock": per.reset_index().to_dict("records"),
        "concentration_single": round(w, 4),
        "concentration_top5": round(float(top5.sum() / last[per.index].sum()), 4),
        # ★2026-08-11 单行业集中度（M4：≤20%）
        "concentration_industry": ind_pct,
        "top_industry": top_ind[0],
        "avg_pairwise_corr": round(avg_corr, 3),
        "est_port_vol": round(port_vol, 3),
        "flags": {
            "high_corr": bool(avg_corr > 0.6),        # >0.6 伪分散警告
            "high_concentration": bool(n < 10),        # <10 只集中度警告
            "deep_drawdown": int((per["dd60"] < -0.25).sum()),  # 深回撤个股数
            "industry_high": bool(ind_pct > 0.20),     # ★单行业>20% 违反 M4 纪律
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _industry_map(codes: list) -> dict:
    """code → industry（从机会池/科技池反查；找不到返回 未知）"""
    import glob as _g
    out = {}
    for pat in ("opp_pool_*.json", "tech_pitch_*.json"):
        for p in sorted([Path(x) for x in _g.glob(str(BASE / "logs" / pat))],
                        key=lambda x: x.stat().st_mtime)[-3:]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = d.get("opportunities") or d.get("entries") or d.get("pitch") or []
            for it in items:
                if it.get("code") in codes and it.get("industry"):
                    out[it["code"]] = str(it["industry"]).split("（")[0][:12]
    return out


def main():
    ap = argparse.ArgumentParser(description="S6 持仓风险监控")
    ap.add_argument("--codes", default=None, help="逗号分隔持仓（默认取真实 portfolio 持仓，兜底 v3 信号清单）")
    ap.add_argument("--date", default=None, help="基准日（默认最新交易日）")
    ap.add_argument("--limit", type=int, default=200, help="持仓数量上限")
    ap.add_argument("--candidate", action="store_true",
                    help="★2026-08-12 #168 空仓时也监控候选池（默认空仓输出空风控，防误报单行业超限）")
    args = ap.parse_args()

    cache = DailyCache()
    date = args.date or cache.latest_trade_date() or datetime.now().strftime("%Y-%m-%d")
    if args.codes:
        codes = args.codes.split(",")
    else:
        # ★2026-08-11 百轮#11：优先真实持仓（portfolio holding，写保护免疫 glob 最新）；无则 v3 信号清单
        import glob as _g
        pfs = sorted([Path(p) for p in _g.glob(str(BASE / "logs" / "portfolio_*.json"))],
                     key=lambda p: p.stat().st_mtime)
        codes = []
        if pfs:
            try:
                _pd = json.loads(pfs[-1].read_text(encoding="utf-8"))
                codes = [p.get("code") for p in _pd.get("positions", [])
                         if p.get("status") in ("holding", "over_limit") and p.get("code")]
            except Exception:
                codes = []
        if not codes:
            if not args.candidate:
                # ★2026-08-12 #168 空仓正常态：输出空风控（防候选池误报单行业超限/深回撤）
                result = {"ok": True, "date": date, "n_holdings": 0, "positions": [],
                          "flags": {}, "note": "空仓（无真实持仓）——风控空态",
                          "concentration_top5": 0, "avg_corr": None, "vol_approx": None}
                OUT_DIR.mkdir(exist_ok=True)
                import time as _t
                p_ts = OUT_DIR / f"position_risk_{_t.strftime('%Y%m%d_%H%M%S')}.json"
                try:
                    p_ts.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "risk/position_monitor.py:176 main")
                try:
                    (OUT_DIR / "position_risk.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "risk/position_monitor.py:181 main")
                print(f"（空仓 → 输出空风控 {p_ts.name}）")
                return
            codes = load_v3_holdings(date, limit=args.limit)
            print(f"（无真实持仓 → 用 v3 信号清单 {len(codes)} 只，--candidate 模式）")
    print(f"监控持仓 {len(codes)} 只 @ {date}（取前 {min(len(codes), args.limit)} 只）")

    result = compute(codes[:args.limit], date)
    OUT_DIR.mkdir(exist_ok=True)
    # ★2026-08-11 写保护免疫：时间戳文件名主写 + 固定名失败不阻断
    import time as _t
    p_ts = OUT_DIR / f"position_risk_{_t.strftime('%Y%m%d_%H%M%S')}.json"
    try:
        p_ts.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/position_monitor.py:196 main")
    try:
        (OUT_DIR / "position_risk.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/position_monitor.py:200 main")

    if "error" in result:
        print("ERROR:", result["error"])
        return 1
    print(f"持仓数: {result['n_holdings']} | 单票集中度 {result['concentration_single']:.2%}"
          f" | Top5 集中度 {result['concentration_top5']:.1%}"
          f" | 单行业 {result['top_industry']} {result['concentration_industry']:.1%}"
          f"（{'超 20% 上限' if result['flags']['industry_high'] else 'OK ≤20%'}）")
    print(f"平均两两相关: {result['avg_pairwise_corr']:.3f}"
          f"（{'伪分散风险' if result['flags']['high_corr'] else 'OK 分散良好'}）")
    print(f"组合波动率(等权近似): {result['est_port_vol']:.1%} | 深回撤个股: {result['flags']['deep_drawdown']} 只")
    if result["flags"]["high_concentration"]:
        print("⚠️ 持仓 <10 只，集中度过高")
    print(f"报告: {p_ts.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
