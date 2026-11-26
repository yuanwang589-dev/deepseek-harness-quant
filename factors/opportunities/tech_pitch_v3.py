# -*- coding: utf-8 -*-
"""factors/opportunities/tech_pitch_v3.py — 科技线 Pitch 池 v3（2026-08-11 用户核心批评重构）

★用户批评（2026-08-11）：
  1. 科技线里一堆价值股（pv_consensus 选出券商/铁建/照明——量价共识≠科技题材）
  2. 科技线 pitch 逻辑不对——应着重「情绪 / 龙虎榜 / 散户讨论度」
  3. 评判标准不对——应着重「① 买入后短线表现 ② 带止损判断」

★v3 重构（总指导）：
  候选源：不再从 pv_consensus 筛，改为「短线强势候选」（bars 近 5 日涨停/连板/高换手）
  评分（满分 100）：
    - 短线表现分 35：涨停反转因子实证（limup_ex_ret_20 ICIR 1.53 全库最强）→ 非一字涨停质量映射
    - 止损安全分 25：ATR20 止损位，现价距止损空间 ≥5% 才安全（带止损判断）
    - 情绪分 20：涨停次数 + 连板高度 + 换手活跃度（散户参与度代理）
    - 龙虎榜分 20：top_list 净买/机构席位（moneyflow_verify；夜间限流降级 0 分）
  输出：logs/tech_pitch_{ts}.json（与 v2 同规格，Deck/门户消费端不变）
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
import glob
import json
import os
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent   # → deepseek-harness-quant
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from factors.opportunities.score import size_tier_of   # ★2026-08-11 市值档位统一口径（券商指数划分）

BARS_DB = Path(r"data/cache/bars.db")
TECH_TOP_N = 6                 # ★2026-08-14 Pitch 改进规格 v2 ③：科技线每日 Top ≤6（原 14——同质化+无实证支撑）
                               #   （跨家族去重 + 竞价反信号过滤 + 昨板今收风控；F4 体检：limup 族 20 日无持续优势）
LIMUP_DAYS = 5                 # 情绪窗口（近 5 日）
CONSEC_DAYS = 10               # 连板计算窗口
ATR_N = 20                     # ATR 窗口

_NAME_MAP = {}                 # code → (name, industry)，build() 时加载
_MV = {}                       # code → 市值（亿元），build() 时加载


def _board_pct(code: str) -> float:
    """涨停阈值：创业板/科创板 20%，其余 10%（ST 5% 由 pct_chg 判定时天然区分）"""
    c6 = code.split(".")[0]
    return 19.5 if c6.startswith(("300", "301", "688")) else 9.5


def load_recent(days: int = CONSEC_DAYS) -> pd.DataFrame:
    """拉 bars 最近 N 个交易日全市场（★#143 修复：真正双库合并——主库+最近 3 增量库，
    原实现注释写"合并"但代码只连主库 → 08-12 数据进增量库后短线池滞后一天）"""
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    try:
        dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT ?", (days + 3,))]
        # ★#143 增量库日期并入（08-12 起增量写 bars_incr_*.db，主库写保护停更）
        try:
            from pathlib import Path as _P
            for _p in sorted(_P(BARS_DB).parent.glob("bars_incr_*.db"))[-3:]:
                try:
                    _c = sqlite3.connect(f"file:{_p}?mode=ro&immutable=1", uri=True, timeout=3)
                    dates += [r[0] for r in _c.execute(
                        "SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT ?", (days + 3,))]
                    _c.close()
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:67 load_recent")
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:69 load_recent")
        dates = sorted(set(dates))[-days:]
        q = "SELECT code,date,open,high,low,close,pct_chg,volume,turn,is_st FROM daily_bar WHERE date IN (%s)" % ",".join("?" * len(dates))
        df = pd.read_sql_query(q, con, params=dates)
        # ★#143 增量库同日数据补充（增量行覆盖主库同 key——keep=last 增量优先）
        try:
            from pathlib import Path as _P
            for _p in sorted(_P(BARS_DB).parent.glob("bars_incr_*.db"))[-3:]:
                try:
                    _c = sqlite3.connect(f"file:{_p}?mode=ro&immutable=1", uri=True, timeout=3)
                    _df2 = pd.read_sql_query(q, _c, params=dates)
                    if len(_df2):
                        df = pd.concat([df, _df2], ignore_index=True).drop_duplicates(
                            subset=["code", "date"], keep="last")
                    _c.close()
                except Exception as _dsh_exc:
                    _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:85 load_recent")
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:87 load_recent")
        # ★2026-08-12 #136 ST 名单补充：主库最新日 is_st 可能丢列（Tushare 增量写 0）→
        #   用 scan.load_st_codes（含异常回溯）名单覆盖最新日 ST 标记，保证涨停板幅度判断正确
        try:
            from factors.opportunities.scan import load_st_codes as _lsc
            _st = _lsc()
            if _st:
                _latest = df["date"].max()
                df.loc[(df["date"] == _latest) & (df["code"].isin(_st)), "is_st"] = 1
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:97 load_recent")
    finally:
        con.close()
    return df


def _emotion_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 code 聚合情绪特征：近5日涨停数 / 连板高度 / 换手均值 / 散户活跃度代理"""
    df = df.copy()
    df["limit_up"] = df.apply(lambda r: 1 if (r["pct_chg"] is not None and r["pct_chg"] >= _board_pct(r["code"]) and not r["is_st"]) else 0, axis=1)
    # 一字板近似：涨停且当日高=低（无振幅）
    df["yizi"] = df.apply(lambda r: 1 if (r["limit_up"] and r["high"] == r["low"] and r["high"] > 0) else 0, axis=1)
    # 连板高度：按 code 分组，倒序日期计算连续涨停天数
    df = df.sort_values(["code", "date"])
    df["consec"] = 0
    for code, g in df.groupby("code", sort=False):
        c = 0
        for i in range(len(g) - 1, -1, -1):
            if g.iloc[i]["limit_up"]:
                c += 1
            else:
                break
        df.loc[g.index, "consec"] = c
    agg = df.groupby("code").agg(
        limup_cnt=("limit_up", "sum"),
        consec_max=("consec", "max"),
        yizi_cnt=("yizi", "sum"),
        turn_mean=("turn", lambda x: float(np.nanmean([v for v in x if v is not None])) if any(v is not None for v in x) else 0.0),
        vol_mean=("volume", "mean"),
    ).reset_index()
    agg["active"] = agg["limup_cnt"] * (1 + agg["turn_mean"] / 5.0)   # 散户讨论度代理：涨停数×换手
    return agg


def _short_term_score(row) -> float:
    """① 买入后短线表现分（0-1）：涨停反转因子实证映射
    limup_ex_ret_20（非一字涨停反转）ICIR 1.53 全库最强 → 低位首板反转 alpha 最纯；
    多板/高位连板追高风险大 → 递减；一字板买不进 → 扣分。"""
    if row["yizi_cnt"] > 0:
        base = 0.40 - 0.15 * min(row["yizi_cnt"], 2)      # 一字买不进
    elif row["limup_cnt"] == 1 and row["consec_max"] <= 1:
        base = 0.85                                        # 低位首板（反转 alpha 最纯）
    elif row["limup_cnt"] <= 2:
        base = 0.75                                        # 2 板内
    else:
        base = 0.60 - 0.05 * (row["limup_cnt"] - 3)        # 多板追高递减
    if row["consec_max"] >= 3:
        base -= 0.20                                       # 高位连板情绪过热
    return float(np.clip(base, 0.05, 0.95))


def _open_premium_trap(code: str, df: pd.DataFrame) -> dict:
    """★2026-08-11 EV-1 深化接入（外包实证）：涨停次日开盘溢价"高开诱多"
    大幅高开 >5% = 出货/诱多（T+1 日内 -1.63%，单调反向）→ 标记诱多风险
    返回 {premium_pct, trap}（最近一次涨停的次日开盘溢价；无次日数据 → trap=False 仅提示）"""
    g = df[df["code"] == code].sort_values("date").reset_index(drop=True)
    if len(g) < 2:
        return {"premium_pct": None, "trap": False}
    # limit_up 列在 _emotion_features 内部算，未合并回 df → 此处自算
    if "limit_up" not in g.columns:
        g["limit_up"] = g.apply(lambda r: 1 if (r["pct_chg"] is not None and r["pct_chg"] >= _board_pct(r["code"]) and not r["is_st"]) else 0, axis=1)
    limup_days = [i for i in range(len(g) - 1) if g["limit_up"].iloc[i] == 1]
    for i in reversed(limup_days):
        nxt = g.iloc[i + 1]
        if nxt["date"] != g["date"].iloc[i]:
            if g["close"].iloc[i] and g["close"].iloc[i] > 0:
                prem = (nxt["open"] - g["close"].iloc[i]) / g["close"].iloc[i]
                return {"premium_pct": round(float(prem), 4), "trap": bool(prem > 0.05)}
    return {"premium_pct": None, "trap": False}


def _stop_safety_score(code: str, df: pd.DataFrame) -> tuple:
    """② 止损安全分（0-1）：ATR20 止损位，现价距止损空间
    低价(<5元)与超波动(ATR%>12%)打折——止损易被扫/流动性风险。返回 (score, stop_price, atr)"""
    g = df[df["code"] == code].sort_values("date")
    if len(g) < 5:
        return 0.3, None, None
    close = g["close"].iloc[-1]
    highs, lows = g["high"].values, g["low"].values
    if len(highs) >= 2:
        trs = [max(h - l, abs(h - close), abs(l - close)) for h, l, close in zip(highs, lows, g["close"].shift(1).fillna(close).values)]
        atr = float(np.mean(trs[-ATR_N:])) if len(trs) >= 2 else close * 0.04
    else:
        atr = close * 0.04
    atr = max(atr, close * 0.02)
    stop = close - 2 * atr
    space = (close - stop) / close
    score = float(np.clip((space - 0.03) / 0.10, 0.05, 0.95))   # 3%→0.05, 13%→0.95
    if close < 5.0:
        score *= 0.75        # 低价仙股风险
    if atr / close > 0.12:
        score *= 0.80        # 超波动，止损易被扫
    return float(np.clip(score, 0.05, 0.95)), round(stop, 2), round(atr, 2)


def _dragon_tiger_score(code: str) -> float:
    """③ 龙虎榜分（0-1）：top_list 净买/机构席位（moneyflow_verify，失败降级 0.5 中性——不拉低不拉高）"""
    try:
        from data import moneyflow_verify as mv
        v = mv.verify_breakout(code, datetime.now().strftime("%Y-%m-%d"))
        if v and v.get("verdict") == "REAL":
            return 0.85
        if v and v.get("verdict") == "FAKE":
            return 0.05
        # UNKNOWN：尝试净买额
        tl = (mv._cache.get("tl") or {}).get("data")
        if tl is not None and len(tl):
            row = tl[tl["ts_code"] == code]
            if len(row) and row.iloc[0].get("net_amount", 0) > 0:
                return 0.65
        return 0.5   # 数据不可用 → 中性（不干扰短线表现+止损主评判）
    except Exception:
        return 0.5


def _name_map():
    """股票名称/行业映射（stock_basic）"""
    try:
        from factors.opportunities.scan import load_basic
        b = load_basic()
        return {c: (r.get("name", ""), r.get("industry", "")) for c, r in b.iterrows()}
    except Exception:
        return {}


def _industry_tag(code: str) -> str:
    """科技行业标签（v2 白名单复用，但仅作标注不拦截——短线情绪与行业无关）"""
    try:
        from factors.opportunities.tech_pitch import _tech_industry, _board_badge
        ind = _NAME_MAP.get(code, ("", ""))[1]
        label = _tech_industry(ind)
        board = _board_badge(code)
        return f"{label or '非白名单'}·{board}" if board else (label or "非白名单")
    except Exception:
        return ""


def build() -> Path:
    global _NAME_MAP, _MV
    _NAME_MAP = _name_map()
    # 市值映射（外包 daily CSV size 是标准化值 → 用 finance/basic 的 total_mv 更可靠；失败降级 0）
    _MV = {}
    try:
        import sqlite3
        con = sqlite3.connect(r"data/cache/finance.db", timeout=3)
        for row in con.execute("SELECT code, total_mv FROM valuation WHERE trade_date=(SELECT MAX(trade_date) FROM valuation)"):
            if row[1]:
                _MV[str(row[0])] = row[1] / 10000.0   # 万元→亿
        con.close()
        if not _MV:
            raise ValueError("valuation 空")
    except Exception:
        try:
            from factors.opportunities.scan import load_valuation
            _MV = {c: (v.get("total_mv") or 0) / 10000.0 for c, v in load_valuation().items()}
        except Exception:
            _MV = {}
    df = load_recent(CONSEC_DAYS)
    feat = _emotion_features(df)
    # 候选：近 5 日有涨停 或 高换手活跃（top 3%）
    active = feat[feat["limup_cnt"] >= 1]
    thr = feat["turn_mean"].quantile(0.97)
    hot = feat[feat["turn_mean"] > thr]
    cands = pd.concat([active, hot]).drop_duplicates(subset="code")
    # ★2026-08-14 Pitch 改进规格 v2 ③：竞价反信号过滤（strength≥6 高开放量过热 → 回避）
    #   T-3 总指导裁决：竞价过热 1 日短效、5/20 日反转——短线追高无肉，直接剔除候选
    try:
        from factors.opportunities.scan import load_auction_signals, auction_date8_for
        _asig = load_auction_signals()
        _d8 = auction_date8_for(str(df["date"].max())[:10])
        if _asig and _d8 and _d8 in _asig:
            _over_codes = {c for c, v in _asig[_d8].items()
                           if isinstance(v, dict) and float(v.get("strength", 0) or 0) >= 6.0}
            _before = len(cands)
            cands = cands[~cands["code"].isin(_over_codes)]
            print(f"  [科技线收敛] 竞价反信号剔除 {_before - len(cands)} 只过热（strength≥6）")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:274 build")

    entries = []
    now_date = df["date"].max()
    # ★#334 短线卡片统一：补机制链/财务操纵/信号族（对齐长线 pitchCard 三问密度）
    _TECH_MECHANISM = ("涨停反转/情绪修复：超跌后资金回流 + 涨停质量验证（非一字、非高位连板）"
                       "→ 短线反弹 alpha（limup_ex_ret_20 ICIR 1.53 全库最强）· 只做反转不追高")
    _beneish_map = {}
    try:
        from factors.opportunities.pitch_v2 import load_beneish
        _beneish_map = load_beneish()
    except Exception:
        _beneish_map = {}
    # ★2026-08-11 风控接入（用户反馈：短线 Pitch 风控未接入）——先批量加载候选风控映射，BLOCK 一票否决
    risk_map = {}
    try:
        from risk.stock_risk import scan_all
        _rr = scan_all()   # 全市场批量（单连接一次取全部）
        for _r in _rr.get("results", []):
            risk_map[_r.get("code")] = _r
        if not risk_map:
            from risk.stock_risk import check_one
            for _, _r2 in cands.iterrows():
                risk_map[_r2["code"]] = check_one(_r2["code"])
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:299 build")
    for _, r in cands.iterrows():
        code = r["code"]
        stop_s, stop_px, atr = _stop_safety_score(code, df)
        short_s = _short_term_score(r)
        dt_s = _dragon_tiger_score(code)
        emo_s = float(np.clip(0.3 + r["limup_cnt"] * 0.15 + min(r["consec_max"], 3) * 0.05 + min(r["active"], 6) * 0.04, 0.1, 0.95))
        # ★2026-08-11 EV-1 深化接入：涨停次日高开诱多（>5% = 出货信号，T+1 日内 -1.63%）→ 扣分 + 标记
        trap = _open_premium_trap(code, df)
        trap_note = ""
        if trap.get("trap"):
            short_s = max(short_s * 0.7, 0.15)     # 诱多确认 → 短线表现分打 7 折
            trap_note = f"·高开诱多(次日溢价{trap['premium_pct']:.0%}>5%)"
        # ★用户要求：评判着重 ① 买入后短线表现 ② 带止损 → 权重 短线表现 40 / 止损 30 / 情绪 20 / 龙虎榜 10
        score = round(40 * short_s + 30 * stop_s + 20 * emo_s + 10 * dt_s, 1)
        # ★2026-08-11 打分拆解（前端展示：各维度分 + 权重）
        score_breakdown = {
            "短线表现": round(short_s, 2), "止损安全": round(stop_s, 2),
            "情绪": round(emo_s, 2), "龙虎榜": round(dt_s, 2),
            "weights": "40/30/20/10", "formula": "短线表现40%+止损安全30%+情绪20%+龙虎榜10%",
        }
        # ★2026-08-11 风控（BLOCK 一票否决剔除；WATCH 标注）
        rc = risk_map.get(code, {})
        rk_level = rc.get("level", "NO_DATA")
        if rk_level == "BLOCK":
            continue
        rk_note = "风控 WATCH（人工复核）" if rk_level == "WATCH" else ""
        rk_flags = [f["id"] for f in rc.get("flags", []) if f.get("id") != "no_data"]
        name = _NAME_MAP.get(code, ("", ""))[0]
        # ★2026-08-11 市值标注（科技线短线情绪天然偏小盘——标注知情，不强配额；08-11 重划：大盘≥1000亿/中盘300-1000亿/小盘<300亿）
        try:
            _mv = float(_MV.get(code, 0) or 0)
        except Exception:
            _mv = 0.0
        _tier = size_tier_of(_mv)
        stop_plan = {
            "trailing_ma": 10,
            "stop_loss_pct": round(0.08, 2),
            "atr_stop": stop_px,
            "short_line": True,
        }
        entries.append({
            "code": code, "name": name or code,
            "otype": "tech_sentiment",
            "otype_name": "短线情绪（涨停反转）",
            # ★2026-08-14 科技线标注规格 v1（因子池交付）：短线·事件通道显性化
            #   channel_tag/horizon_hint 与价值线实证徽章互斥——短线亮短线证据，不套长线胜率
            "channel_tag": "短线·事件通道",
            "horizon_hint": "≤20日持有",
            "tech_badges": [
                {"name": "涨停后3日", "value": "+2.07%", "note": "5.1万样本冒烟实证（F2 短线体检）"},
                {"name": "昨板今收", "value": "+1.95%", "note": "事件研究（F2）"},
                {"name": "20日窗口", "value": "无持续优势", "note": "F4 体检：limup 族 20 日反向/持平（事件≠持仓信号）"},
                {"name": "竞价反信号", "value": "strength≥6 回避", "note": "T-3 总指导裁决（2026-08-10）"},
            ],
            # ★2026-08-14 机制链（第一问"靠什么赚钱"）+ 财务操纵 + 信号族
            "mechanism": _TECH_MECHANISM,
            "beneish": ({"level": _beneish_map[code].get("level"), "m_score": _beneish_map[code].get("m_score")}
                        if _beneish_map.get(code) else None),
            "signal_family": "情绪/动量",
            "score": score,
            "score_breakdown": score_breakdown,   # ★2026-08-11 打分拆解（前端展示各维度分）
            "total_mv_yi": round(_mv, 1) if _mv else None,
            "size_tier": _tier,
            "trigger": f"近{LIMUP_DAYS}日涨停 {int(r['limup_cnt'])} 次 · 连板 {int(r['consec_max'])} · 换手 {r['turn_mean']:.1f}%",
            "evidence": f"情绪分 {emo_s:.2f}（涨停/连板/换手）· 短线表现 {short_s:.2f}（涨停反转 ICIR 1.53 实证）· "
                        f"止损安全 {stop_s:.2f}（ATR{ATR_N} 止损 {stop_px}）· 龙虎榜 {dt_s:.2f}"
                        + (f" · 高开诱多溢价 {trap['premium_pct']:.0%}" if trap.get("premium_pct") is not None else ""),
            "open_premium_trap": trap.get("trap", False),
            # ★2026-08-11 风控接入（BLOCK 已剔除，WATCH 标注）
            "risk_level": rk_level,
            "risk_score": rc.get("score"),
            "risk_flags": rk_flags,
            "risk_note": rk_note,
            "risk_notice": "科技线=情绪与资金短线：必须带止损（ATR 双倍线），跌破即走；高位连板≥3 回避"
                           + ("；涨停次日高开诱多确认（>5% 出货信号，T+1 追入无肉）" if trap.get("trap") else "")
                           + "；实证 C9（外包 08-12）：短线 5 日买拥挤赛道股（高换手/高情绪/放量）全部负超额 -0.3~-0.7pp——只做反转不追高",
            "confidence": "中置信" if score >= 65 else "低置信",
            "board": "创业板" if code.split(".")[0].startswith(("300", "301")) else ("科创板" if code.split(".")[0].startswith("688") else "主板"),
            "tech_label": _industry_tag(code),
            "stop_plan": stop_plan,
            "add_date": now_date,
            "is_new": True,
        })

    # ★2026-08-11 Pitch 决策台 v3：短线（科技突破）子分类（用户指示：分短线/长线双板块 + 每线内子菜单）
    #   ⚡ express   强因子直通 —— 跨家族≥3 三重确认 + 强度排序前 EXPRESS_PER_LINE（只有前几位才能直通）
    #   🤝 consensus 多因子共识达成 —— 家族≥2 未直通
    #   📊 score    加权评分高分 —— 其余按分数补足
    #   数量：express ≤2 + consensus ≤3 + score 补足至 TECH_TOP_N（=14）
    from factors.opportunities.scan import load_strong_hits
    from factors.opportunities.score import (classify_pitch_sub, strong_strength,
                                             EXPRESS_MIN_FAMILY, EXPRESS_PER_LINE, CONSENSUS_PER_LINE)
    sh = {}
    try:
        sh = load_strong_hits() or {}
    except Exception:
        sh = {}
    all_entries = sorted(entries, key=lambda x: -x["score"])

    # 1) ⚡ 强因子直通前几位（按强度排序：家族数↓/rank 靠前↓/icir120↓）
    exp = []
    for e in all_entries:
        sh_ = sh.get(e["code"])
        if not sh_:
            continue
        if len({v["family"] for v in sh_.values() if v.get("family")}) < EXPRESS_MIN_FAMILY:
            continue
        e["pitch_line"] = "short"
        e["pitch_sub"] = "express"
        best = max(sh_.values(), key=lambda v: v["icir120"] or 0)
        e["express_strong"] = {"factor": next(k for k, v in sh_.items() if v == best),
                               "family": best["family"], "icir120": best["icir120"]}
        e["note"] = (e.get("note") or "") + f"·⚡强因子直通（{best['family']} ICIR120={best['icir120']}，三重家族确认）"
        exp.append(e)
    exp.sort(key=lambda x: strong_strength(sh.get(x["code"])), reverse=True)
    exp = exp[:EXPRESS_PER_LINE]
    exp_codes = {e["code"] for e in exp}

    # 2) 🤝 多因子共识达成（走常规门槛）
    con = []
    for e in all_entries:
        if e["code"] in exp_codes:
            continue
        if classify_pitch_sub(sh.get(e["code"]), None, None) != "consensus":
            continue
        e["pitch_line"] = "short"
        e["pitch_sub"] = "consensus"
        nf = len({v["family"] for v in (sh.get(e["code"]) or {}).values() if v.get("family")})
        e["note"] = (e.get("note") or "") + (f"·🤝多因子共识（强因子{nf}家族）" if nf else "·🤝多因子共识")
        con.append(e)
    con.sort(key=lambda x: -x["score"])
    con = con[:CONSENSUS_PER_LINE]
    con_codes = {e["code"] for e in con}

    # 3) 📊 加权评分高分：剩余按分数补足
    sc = [e for e in all_entries if e["code"] not in exp_codes and e["code"] not in con_codes]
    sc.sort(key=lambda x: -x["score"])
    sc = sc[: max(0, TECH_TOP_N - len(exp) - len(con))]
    for e in sc:
        e["pitch_line"] = "short"
        e["pitch_sub"] = "score"
    # ★2026-08-11 百轮#14 短线三级分档（core 核心/alt 备选/temp 临时，与长线一致）
    for _e in entries:
        if _e.get("pitch_sub") == "express":
            _e["tier"] = "core"
        elif _e.get("pitch_sub") == "consensus":
            _e["tier"] = "alt"
        else:
            _e["tier"] = "core" if _e.get("score", 0) >= 70 else ("alt" if _e.get("score", 0) >= 60 else "temp")
    entries = exp + con + sc
    print(f"  [短线 Pitch] ⚡express {len(exp)} + 🤝consensus {len(con)} + 📊score {len(sc)}（总 {len(entries)}）")
    prev = sorted(glob.glob(str(BASE / "logs" / "tech_pitch_*.json")), key=os.path.getmtime)
    prev_codes = set()
    if prev:
        try:
            prev_codes = {e["code"] for e in json.loads(Path(prev[-1]).read_text(encoding="utf-8")).get("entries", [])}
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:457 build")
    for e in entries:
        e["is_new"] = e["code"] not in prev_codes
    new_codes = [e["code"] for e in entries if e["is_new"]]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pool_date": now_date,
        "threshold": 0,
        "n_candidates": len(entries),
        "new_codes": new_codes,
        "tech_filters": {"engine": "v3 短线情绪+龙虎榜+止损", "emotion_window": f"{LIMUP_DAYS}日涨停/连板/换手",
                         "short_term": "limup_ex_ret_20 ICIR 1.53（研究员实证）",
                         "stop": "ATR20 双倍线止损"},
        "entries": entries,
        "note": "科技线 v3：短线情绪（涨停反转）+ 龙虎榜 + 散户活跃度 · 评分=短线表现35%+止损安全25%+情绪20%+龙虎榜20% · "
                "★不再用 pv_consensus（量价共识选出的是价值股，非科技题材）",
    }
    p = BASE / "logs" / f"tech_pitch_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    try:
        (BASE / "deck" / f"tech_pitch_{ts}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/opportunities/tech_pitch_v3.py:481 build")
    return p


if __name__ == "__main__":
    p = build()
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"科技线 v3: {p.name} | {len(d['entries'])} 只 | 新增 {len(d['new_codes'])}")
    for e in d["entries"]:
        print(f"  [{'NEW' if e['is_new'] else '  '}] {e['code']} {e['name']} score={e['score']} | "
              f"{e['trigger']} | 止损 {e['stop_plan'].get('atr_stop')} | {e['tech_label']}")
