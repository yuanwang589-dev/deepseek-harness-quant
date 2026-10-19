# -*- coding: utf-8 -*-
"""factors/opportunities/pitch_v2.py — Pitch v2 输出升级（外包 AI-2 · 2026-08-09）

★#6 需求（外包需求清单.md #6）：
  对当日 logs/opp_pool_*.json（机会引擎 scan.py --pitch 输出）的 pitch 候选逐只：
  1. 1/2/3 年买入持有 PIT 回测（2020 起每季初信号 → T+1 开盘买入 → 持有 1/2/3 自然年）
     → 胜率 / 平均收益 / 中位收益 / 最大回撤 / 盈亏比 / 样本数 / 沪深300 基准与超额
  2. 风险清单合并：logs/stock_risk_map.json（风控红旗）+ logs/beneish_report.json（M-Score）
  3. 输出 logs/pitch_v2.json（★固定名，Deck deck_server.py /api/pitch_v2 路由读取）
     → deck.html "📈 回测证据"区块（每张 Pitch 卡片 1/2/3 年表 + 风险清单）

口径（PIT 严格，与 backtest_winrate.py 同源）：
  - 信号日 t0 = 每年 1/4/7/10 月该股首个交易日（收盘后信号可得）
  - 买入 = t0 后首个交易日（T+1）开盘价 ★（无未来函数；旧 strategy/pitch_v2.py 用当日收盘，已修正）
  - 持有 1/2/3 自然年（月末 clamp，_add_months）→ 到期后最近交易日收盘卖出
  - ★右删失防御：数据未覆盖完整持有期的买入样本剔除（未到期截断会虚高收益）
  - 收益 = 持有期总收益；max_dd = 持有路径相对前高最大回撤（减法口径）
  - bench = 沪深300（SH.000300 none 复权）同买入/卖出日收益；excess = avg_ret - bench_avg
  - 样本：2020 起每季 1 个 → 1y≈23、2y≈19、3y≈15（数据截至 2026-08）

★重叠处置（2026-08-09）：19:27 另一条线已产出 logs/pitch_v2.json（基于 08-08 旧池 5 只）。
本脚本以「当日最新含候选池」（mtime 最新 + pitch 非空）为准重新生成，字段格式与其对齐
（n/winrate/avg_ret/med_ret/max_dd/pl_ratio/bench_avg/excess_avg），原文件已备份为
logs/pitch_v2_20260809_192738_AIAUTO.json。若需回退，恢复备份即可。

用法：
  python factors/opportunities/pitch_v2.py                 # 用最新 opp_pool_*.json
  python factors/opportunities/pitch_v2.py --pool xxx.json # 指定池文件
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.opportunities.backtest_winrate import _add_months, _holding_stats
from data.io_utils import read_json, read_text

BARS_DB = str(BASE / "data" / "cache" / "bars.db")   # ★2026-10-15 P1-A 绝对路径（原相对依赖 cwd）
LOGS = BASE / "logs"
OUT_FILE = LOGS / f"pitch_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"   # ★动态时间戳文件名（安全层同名二次写被锁，2026-08-09 改）；读取方用 glob 取最新


def _tier_fallback(o: dict) -> str:
    """★2026-08-11 百轮#35 tier 兜底：scan 数据无 tier（旧版）时按 pitch_sub+score 现算"""
    try:
        from factors.opportunities.score import PITCH_GATE
    except Exception:
        PITCH_GATE = {}
    sub = o.get("pitch_sub")
    if sub == "express":
        return "core"
    if sub == "consensus":
        return "alt"
    sc = o.get("score") or 0
    g = PITCH_GATE.get(o.get("otype"), 70)
    return "core" if sc >= g + 10 else ("alt" if sc >= g else "temp")


def _type_stop(otype, score):
    """类型定制止损（2026-08-10 落地；v2 由 8.6 版封装）"""
    """★2026-08-10 类型定制止损方案（risk/type_stop_rules.py）"""
    try:
        from risk.type_stop_rules import type_stop_plan
        return type_stop_plan(otype, score)
    except Exception:
        # ★2026-08-10 B-9.1 验收修复：异常兜底改为"无硬止损"（与 v2.0 实证哲学一致——防守型 7% 有害）
        return {"otype": otype, "desc": "异常兜底：无硬止损（逻辑止损为主）", "stop_loss_pct": None,
                "logic_fail_rules": []}


def _type_mechanism(otype):
    """★2026-08-13 #316 IC Memo 第一问「价值创造杠杆」机制链——从机会注册表透传类型赚钱逻辑
    如 revalue=盈利拐点/戴维斯双击：业绩超预期→估值+盈利双击"""
    try:
        from factors.opportunities.registry import OPPORTUNITY_TYPES
        spec = OPPORTUNITY_TYPES.get(otype, {})
        return spec.get("desc", "")
    except Exception:
        return ""


# 持有年限 → 自然月（复用 backtest_winrate 的 _add_months 月末 clamp）
HORIZONS = {1: 12, 2: 24, 3: 36}
QUARTER_MONTHS = (1, 4, 7, 10)

_bench_cache = None


def get_latest_pool() -> Path:
    """取「生成时间最新 + 含 pitch 候选」的 opp_pool_*.json
    ★两个原因（2026-08-09 实测）：
    1. scan.py 普通扫描（不带 --pitch）也写 opp_pool_*.json 且 pitch=[]，直接取最新文件会拿到空候选；
    2. 周末/节假日 scan 的 date 字段会回退到最近交易日（如 8-09 周日扫描 date=08-07），
       不能用 date 字段排序（会选到昨天的池）；按 mtime 取最新含候选者最贴近"当日审批"。
    """
    best, best_mtime = None, -1.0
    for p in sorted(LOGS.glob("opp_pool_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (d.get("pitch")):
            continue
        mt = p.stat().st_mtime
        if mt > best_mtime:
            best, best_mtime = p, mt
    return best


def load_bench() -> pd.Series:
    """沪深300 收盘（SH.000300 none 复权）→ Series(date->close)，模块级缓存"""
    global _bench_cache
    if _bench_cache is None:
        con = sqlite3.connect(BARS_DB)
        rows = con.execute(
            "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
        con.close()
        _bench_cache = pd.Series({pd.Timestamp(d): float(c) for d, c in rows if c})
    return _bench_cache


_regime_cache = None


def _market_regime(date) -> str:
    """★2026-08-13 #327 IC Memo 第二问「情景赔率」分 Regime 判定（事前，无未来函数）：
    沪深300 200日均线位置 + 斜率 → bull/base/bear。
    bull=价在 MA200 上且均线向上（多头）；bear=价在 MA200 下且均线向下（空头）；base=位置与斜率方向不一致（震荡）。
    验证：2020 牛 / 2022 全年熊 / 2025 牛，与 A 股实际走势一致。"""
    global _regime_cache
    if _regime_cache is None:
        s = load_bench()
        ma200 = s.rolling(200).mean()
        ratio = s / ma200
        slope = ma200.diff(60)

        def _cls(x, y):
            if pd.isna(x) or pd.isna(y):
                return None
            if x > 1.0 and y > 0:
                return "bull"
            if x < 1.0 and y < 0:
                return "bear"
            return "base"
        _regime_cache = pd.Series(
            [_cls(ratio.iloc[i], slope.iloc[i]) for i in range(len(s))],
            index=s.index).dropna()
    try:
        return _regime_cache.asof(pd.Timestamp(date))
    except Exception:
        return None


def quarter_starts(gdates: pd.DatetimeIndex, start_year: int = 2020) -> list:
    """每年 1/4/7/10 月的首个交易日（信号日 t0，按该股自身交易日）"""
    out = []
    seen = set()
    for d in gdates:
        if d.year >= start_year and d.month in QUARTER_MONTHS:
            key = (d.year, d.month)
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def _holdout_one(code: str) -> dict:
    """单只股票 1/2/3 年买入持有 PIT 回测 → {1y:{...}, 2y:{...}, 3y:{...}}
    信号日 t0（季初首交易日）→ T+1 开盘买入 → 持有 1/2/3 年 → 到期最近交易日收盘卖出
    字段对齐 AI-1 版：n/winrate/avg_ret/med_ret/max_dd/pl_ratio/bench_avg/excess_avg
    """
    con = sqlite3.connect(BARS_DB)
    rows = con.execute(
        "SELECT date, open, close FROM daily_bar WHERE code=? AND adjust='qfq' "
        "AND date>='2019-12-01' ORDER BY date", (code,)).fetchall()
    con.close()
    if len(rows) < 300:
        return None
    g = pd.DataFrame(rows, columns=["date", "open", "close"])
    gdates = pd.DatetimeIndex(pd.to_datetime(g["date"]))
    closes = g["close"].astype(float).reset_index(drop=True)
    opens = g["open"].astype(float).reset_index(drop=True)
    bench = load_bench()

    out = {}
    for yrs, months in HORIZONS.items():
        rets, dds, bench_rets, regimes = [], [], [], []
        for t0 in quarter_starts(gdates):
            ixs = np.where(gdates > t0)[0]          # T+1 开盘（信号日收盘后才知道信号）
            if len(ixs) == 0:
                continue
            buy_i = int(ixs[0])
            buy_price = float(opens.iloc[buy_i])
            if not np.isfinite(buy_price) or buy_price <= 0:
                continue
            # ★右删失防御：数据须覆盖完整持有期（到期日之后有交易日）才计为样本
            buy_date = gdates[buy_i]
            target = _add_months(buy_date, months)
            sell_mask = gdates[buy_i + 1:] > target
            if not sell_mask.any():
                continue
            sell_i = buy_i + 1 + int(np.argmax(np.asarray(sell_mask)))
            r = _holding_stats(closes, buy_i, buy_price, gdates, months)
            if r:
                rets.append(r[0])
                dds.append(r[1])
                regimes.append(_market_regime(buy_date))   # ★#327 买入时点市场状态（牛/基/熊）
                # 基准：同买入/卖出日沪深300（none 复权）
                b0 = bench.get(buy_date)
                b1 = bench.get(gdates[sell_i])
                if b0 is not None and b1 is not None and b0 > 0:
                    bench_rets.append(b1 / b0 - 1)
        if not rets:
            out[f"{yrs}y"] = {"n": 0, "winrate": None, "avg_ret": None, "med_ret": None,
                              "max_dd": None, "pl_ratio": None, "bench_avg": None,
                              "excess_avg": None,
                              "regime": {"bull": {"n": 0}, "base": {"n": 0}, "bear": {"n": 0}}}
        else:
            rets_a = np.array(rets)
            wins = rets_a[rets_a > 0]
            losses = rets_a[rets_a <= 0]
            bench_avg = float(np.mean(bench_rets)) if bench_rets else None
            # ★#327 分 Regime（牛/基/熊）情景赔率——IC Memo 第二问：各市场状态下赚多少/胜率多少
            regime_stat = {}
            for rg in ("bull", "base", "bear"):
                idx = [i for i, g in enumerate(regimes) if g == rg]
                if not idx:
                    regime_stat[rg] = {"n": 0}
                    continue
                rr = rets_a[idx]
                regime_stat[rg] = {
                    "n": int(len(rr)),
                    "winrate": round(float((rr > 0).mean()), 4),
                    "avg_ret": round(float(rr.mean()), 4),
                    "med_ret": round(float(np.median(rr)), 4),
                    "max_dd": round(float(min(dds[i] for i in idx)), 4),
                }
            out[f"{yrs}y"] = {
                "n": int(len(rets_a)),
                "winrate": round(float((rets_a > 0).mean()), 4),
                "avg_ret": round(float(rets_a.mean()), 4),
                "med_ret": round(float(np.median(rets_a)), 4),
                "max_dd": round(float(min(dds)), 4),
                "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 4)
                             if len(wins) and len(losses) and losses.mean() != 0 else None,
                "bench_avg": round(bench_avg, 4) if bench_avg is not None else None,
                "excess_avg": round(float(rets_a.mean()) - bench_avg, 4)
                              if bench_avg is not None else None,
                "regime": regime_stat,
            }
    return out


def load_risk_map() -> dict:
    """logs/stock_risk_map*.json → {code: {level, score, flags[]}}
    ★#365 修复：风控脚本写 stock_risk_map_v2.json（#42）+ 时间戳版，
      但这里原读固定名 stock_risk_map.json（08-09 旧 v1 残留）→ 风控红旗滞后 4 天。
      改 glob 取 mtime 最新（v2/时间戳版都会被正确取到）"""
    import glob as _g
    fs = sorted(_g.glob(str(LOGS / "stock_risk_map*.json")),
                key=lambda x: Path(x).stat().st_mtime)
    if not fs:
        return {}
    try:
        d = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
        return {r["code"]: r for r in d.get("results", [])}
    except Exception:
        return {}


def load_beneish() -> dict:
    """Beneish M-Score → {code: {level, m_score, note, mode}}
    ★2026-08-10 升级：优先 logs/beneish_report_full.json（完整 8 指标，5501 只，
      2026-08-09 生成）；缺失时回退降级版 beneish_report.json（兼容旧数据）
    """
    for p in (LOGS / "beneish_report_full.json", LOGS / "beneish_report.json"):
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return {r["code"]: r for r in d.get("results", [])}
        except Exception:
            continue
    return {}


def build(pool_path: Path = None) -> tuple:
    """主流程 → (result dict, 输出文件 Path)"""
    pool_path = pool_path or get_latest_pool()
    if pool_path is None:
        return {"error": "logs 下无含 pitch 候选的 opp_pool_*.json（先跑 python factors/opportunities/scan.py --pitch）"}, None
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pitch = pool.get("pitch") or []
    if not pitch:
        return {"error": f"{pool_path.name} 中无 pitch 候选（scan.py --pitch 四重过滤后为空）",
                "date": pool.get("date")}, None

    risk_map = load_risk_map()
    beneish = load_beneish()
    # ★2026-08-14 Pitch 改进规格 v2 ⑤：实证徽章表（因子池 pitch_priority_badges 数据包，glob 最新）
    _badge_map = {}
    try:
        import glob as _gl, os as _os
        _bc = sorted(_gl.glob(r"data/factorpool/output/pitch_priority_badges_*.json"),
                     key=_os.path.getmtime)
        if _bc:
            _bd = read_json(_bc[-1])
            _badge_map = {b.get("otype"): b for b in (_bd.get("badges") or [])}
    except Exception:
        pass

    out_pitch = []
    for o in pitch:
        code = o["code"]
        hist = _holdout_one(code)
        if hist is None:
            print(f"  [跳过] {code} {o.get('name','')}：日线不足 300 行", flush=True)
            continue
        # 风险清单合并（stock_risk_map 红旗 + Beneish M-Score）
        rk = risk_map.get(code, {})
        bn = beneish.get(code, {})
        flags_detail = [{"id": f.get("id"), "desc": f.get("desc"),
                         "value": f.get("value"), "weight": f.get("weight")}
                        for f in rk.get("flags", []) if f.get("id") != "no_data"]
        # ★risk_flags 输出字符串数组（对齐 deck.html evidenceBlock 的渲染方式：
        #   对方前端按字符串 badge 渲染，对象会显示 [object Object]）；
        #   完整明细（desc/value/weight）放 risk_flags_detail 备用
        risk_flags = [f["id"] for f in flags_detail]
        m = None
        if bn and bn.get("m_score") is not None:
            m = {"level": bn.get("level"), "m_score": round(float(bn["m_score"]), 4),
                 "note": bn.get("note", "")}
        out_pitch.append({
            "code": code, "name": o.get("name", code), "industry": o.get("industry", ""),
            "pitch_date": pool.get("date"),   # ★#337 pitch 时间（数据日）——这只股票是哪天被 pitch 出来的
            "otype": o.get("otype"), "otype_name": o.get("otype_name"),
            # ★2026-08-13 #316 IC Memo 第一问「价值创造杠杆」机制链（registry.desc 透传）
            "mechanism": _type_mechanism(o.get("otype")),
            "score": o.get("score"), "note": o.get("note"),
            # ★2026-08-11 打分拆解透传（用户反馈：打分系统逻辑不明）——机会分四维 + 类型权重
            "gains": o.get("gains"), "prob": o.get("prob"), "risk": o.get("risk"),
            "score_breakdown": o.get("score_breakdown"),
            "upside_est": o.get("upside_est"), "winrate_est": o.get("winrate_est"),
            "rank_in_type": o.get("rank_in_type"), "rank_global": o.get("rank_global"),
            "n_types_hit": o.get("n_types_hit"), "also_types": o.get("also_types", []),
            "factors": o.get("factors", {}),
            "evidence": o.get("evidence", ""),
            "horizons": hist,
            # ★2026-08-11 强因子直通标记透传（⚡express_strong：family/icir120——Deck 审批页展示特殊权限）
            "express_strong": o.get("express_strong"),
            # ★2026-08-13 外包5因子终版派单 §7.2：中小盘域 amihud 排序加分标记（决策台展示非流动性溢价）
            "amihud_flag": o.get("amihud_flag"),
            # ★2026-08-11 市值档位透传（大小盘分开 Pitch：大盘≥1000亿/中盘300-1000亿/小盘<300亿，券商指数口径）
            "total_mv_yi": o.get("total_mv_yi"),
            "size_tier": o.get("size_tier"),
            # ★2026-08-11 Pitch 决策台 v3 分类透传（短线short/长线long × ⚡express/🤝consensus/📊score）
            "pitch_line": o.get("pitch_line", "long"),
            "pitch_sub": o.get("pitch_sub", "score"),
            # ★2026-08-11 三级分档（core 核心/alt 备选/temp 临时，百轮#10）
            #   ★2026-08-11 百轮#35 兜底：scan 数据未含 tier（旧版）时按 pitch_sub+score 现算，免重跑 scan
            "tier": o.get("tier") or _tier_fallback(o),
            # ★2026-08-14 Pitch 改进规格 v2 ⑤：实证徽章透传（17 年 6 月胜率——quality_gap 🏆70.4%/+14.6% …）
            #   优先用 scan 挂载的 pitch_badge；opp_pool 旧数据无 → 查徽章表兜底（免重跑 scan）
            "pitch_badge": o.get("pitch_badge") or (_badge_map.get(o.get("otype")) or {}).get("badge", ""),
            "pitch_badge_tier": o.get("pitch_badge_tier") or (_badge_map.get(o.get("otype")) or {}).get("tier", ""),
            "t1_prefill": o.get("t1_prefill"),
            # ★2026-08-14 顶级买点标签透传（scan 打标：择时高概率 + 高稀有度 + 强买入，极其严格）
            "top_buy": o.get("top_buy"),
            "top_buy_note": o.get("top_buy_note", ""),
            # ★2026-08-11 信号族联动透传（机会池×因子池：信号族/信号分/无效因子数）
            "signal_family": o.get("signal_family"),
            "signal_score": o.get("signal_score"),
            "n_invalid": o.get("n_invalid", 0),
            "risk_level": rk.get("level", o.get("risk_level")),
            "risk_score": rk.get("score", o.get("risk_score")),
            "risk_flags": risk_flags,
            "risk_flags_detail": flags_detail,
            "beneish": m,
            # ★2026-08-10 类型定制止损方案（用户需求：每只 Pitch 独有止损条件）
            "stop_plan": _type_stop(o.get("otype"), o.get("score")),
        })

    result = {
        "date": pool.get("date", datetime.now().strftime("%Y-%m-%d")),
        "pool_date": pool.get("date"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_pool": pool_path.name,
        "n": len(out_pitch),
        "pitch": out_pitch,
        "meta": {
            "method": "2020 起每季初信号（1/4/7/10 月首交易日）→ T+1 开盘买入 → 持有 1/2/3 自然年 → 到期最近交易日收盘卖出",
            "note": "标的级持有特征回测（描述性统计，非信号级回测）；纯买入持有口径，不含择时修正",
            "avg_ret": "持有期总收益算术均值；med_ret 中位；max_dd 持有路径相对前高最大回撤（减法）",
            "bench": "沪深300（SH.000300 none 复权）同买入/卖出日；excess_avg = avg_ret - bench_avg",
            "data_limits": [
                "行情 qfq 2019+；样本 2020-2026 每季 1 个（1y≈23 / 2y≈19 / 3y≈15）",
                "★右删失：数据未覆盖完整持有期的买入样本已剔除",
                "未处理一字涨停无法买入/停牌；极端收益 |ret|>=2 已剔除（_holding_stats 内置）",
                "Beneish 为降级模式（GMI/SGI/LVGI/TATA 近似，DSRI/AQI/DEPI/SGAI 数据不足置中性）",
                "risk_level 以 logs/stock_risk_map.json 为准（若当日未重扫则可能滞后于池内标记）",
            ],
        },
    }
    # ★F5-3 一字板披露（外包 F5 对接：一字涨停当日无法买入 → 标注 T+1 再评估）
    try:
        from factors.opportunities.shortterm_hook import one_word_disclosure
        n_w = one_word_disclosure(out_pitch)
        if n_w:
            print(f"  [短线因子] 一字板披露 {n_w} 只（当日不可买，T+1 再评估）")
    except Exception:
        pass
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result, OUT_FILE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=str, default=None, help="指定 opp_pool_*.json 文件（默认取最新）")
    args = ap.parse_args()
    pool_path = Path(args.pool) if args.pool else None
    r, f = build(pool_path)
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    print(f"=== Pitch v2 生成完成 {r['date']}（源 {r['source_pool']}，{r['n']} 只）===")
    for o in r["pitch"]:
        h1 = o["horizons"].get("1y", {})
        print(f"  {o['code']} {o['name']:8s} [{o['otype_name']}] score={o['score']} "
              f"风控={o['risk_level']} | 1y: 胜率{h1.get('winrate','-')} 均{h1.get('avg_ret','-')} "
              f"回撤{h1.get('max_dd','-')} 超额{h1.get('excess_avg','-')} n={h1.get('n',0)}")
    print(f"\n已存 {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
