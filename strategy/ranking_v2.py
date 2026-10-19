# -*- coding: utf-8 -*-
"""strategy/ranking_v2.py — 多因子排名引擎（优中选优 · 2026-08-07）

定位：v3 等权+Regime 的**精选参考层**——在通过硬过滤的池子里，用
★实证有效方向 的因子打分排名，取 Top N（用户要求"优中选优最好几只+排名"）。

因子（全部有全量实证支撑，CS-01~38）：
  1. lowvol 低波（RankIC +0.099 全市场最稳，CS-04）      → 60 日波动率升序（低=高分）
  2. reversal 反转（动量全区间反向 CS-01）                → 120 日动量升序（超跌=高分）
  3. quality 质量（ROE 同比沪深300 IC 4%，大盘池才有效） → ROE 降序
  4. growth 成长（SUE/加速度 IC +0.008，弱正但有效方向） → 净利润同比降序
合成：4 因子等权（分位数平均 → 0-100 分）。

★诚实限定：M7 实证"选股引擎 Top10 集中度负贡献"（0.86→0.36），本引擎是
  精选参考层而非主策略替代；用验证过的因子方向 + 分散（Top N≥15）控制风险。

用法：
  python strategy/ranking_v2.py                 # 生成 output/ranking_top.json（Top 30）
  python strategy/ranking_v2.py --n 10          # Top 10
"""
import argparse
import json
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

# ★2026-10-15 P1-A 修复：原硬编码 D:/劳威派量化数据/ 旧机器路径——部署机不存在 →
#   "unable to open database file" → 三层池静默失败（旧 worker 丢弃返回值假成功 ✓）。
#   收敛到 data.cache CACHE_DIR 单出口；只读 URI 一律走 db_uri（中文路径需 percent-encode，
#   裸 f"file:{path}" 拼法非法）。
try:
    from data.cache import CACHE_DIR
    BARS_DB = CACHE_DIR / "bars.db"
    FIN_DB = CACHE_DIR / "finance.db"
    BASIC_DB = CACHE_DIR / "stock_basic.db"
    MV_CSV = CACHE_DIR / "circ_mv_map_full.csv"
except Exception:
    BARS_DB = Path(r"D:/劳威派量化数据/cache/bars.db")
    FIN_DB = Path(r"D:/劳威派量化数据/cache/finance.db")
    BASIC_DB = Path(r"D:/劳威派量化数据/cache/stock_basic.db")
    MV_CSV = Path(r"D:/劳威派量化数据/cache/circ_mv_map_full.csv")


def load_close_panel(days=300, end=None) -> pd.DataFrame:
    """近 N 个交易日全市场 close+volume 面板（date × code，qfq）
    ★2026-08-07 扩展：days 默认 130→300（决策池技术确认需要 MA200/52 周高点）；
    返回 (close_df, volume_df) 双面板"""
    from data.cache import db_uri
    con = sqlite3.connect(db_uri(BARS_DB), uri=True, timeout=3)
    # ★#143 双库合并：主库写保护后增量库含最新日（08-12 起排行不虚标旧日）
    if end:
        last = con.execute(
            "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' AND date<=?",
            (end,)).fetchone()[0]
    else:
        last = con.execute(
            "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
        try:
            from pathlib import Path as _P
            for _p in sorted(_P(BARS_DB).parent.glob("bars_incr_*.db"))[-3:]:
                try:
                    _c = sqlite3.connect(db_uri(_p), uri=True, timeout=3)
                    _m = _c.execute(
                        "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
                    _c.close()
                    if _m and _m > last:
                        last = _m
                except Exception:
                    pass
        except Exception:
            pass
    df = pd.read_sql(
        "SELECT date, code, close, volume, high, low FROM daily_bar "
        "WHERE adjust='qfq' AND date<=? AND close>0", con, params=(last,))
    # ★#143 增量库数据补充（增量行覆盖主库同 key）
    try:
        from pathlib import Path as _P
        for _p in sorted(_P(BARS_DB).parent.glob("bars_incr_*.db"))[-3:]:
            try:
                _c = sqlite3.connect(db_uri(_p), uri=True, timeout=3)
                _df2 = pd.read_sql(
                    "SELECT date, code, close, volume, high, low FROM daily_bar "
                    "WHERE adjust='qfq' AND date<=? AND close>0", _c, params=(last,))
                if len(_df2):
                    df = pd.concat([df, _df2], ignore_index=True).drop_duplicates(
                        subset=["date", "code"], keep="last")
                _c.close()
            except Exception:
                pass
    except Exception:
        pass
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close")
    v = df.pivot_table(index="date", columns="code", values="volume")
    h = df.pivot_table(index="date", columns="code", values="high")
    l = df.pivot_table(index="date", columns="code", values="low")
    p = p.sort_index().tail(days)
    v = v.reindex(p.index).sort_index()
    h = h.reindex(p.index).sort_index()
    l = l.reindex(p.index).sort_index()
    return p, v, h, l


def load_fundamentals(end=None) -> pd.DataFrame:
    """最新一期财报基本面（roe / sq_net_yoy，PIT：period <= end）"""
    con = sqlite3.connect(str(FIN_DB))
    sql = """SELECT f.code, f.period, f.roe, f.sq_net_yoy, f.revenue, f.net_profit
             FROM finance_report f
             JOIN (SELECT code, MAX(period) p FROM finance_report
                   WHERE period <= ? GROUP BY code) m ON f.code=m.code AND f.period=m.p
             WHERE f.period >= '2018-01-01'"""
    df = pd.read_sql(sql, con, params=(end or "2099-12-31",))
    con.close()
    df = df.drop_duplicates("code")
    df = df.rename(columns={"sq_net_yoy": "nyoy"})
    return df.set_index("code")


def load_basic() -> pd.DataFrame:
    con = sqlite3.connect(str(BASIC_DB))
    df = pd.read_sql("SELECT * FROM stock_basic", con)
    con.close()
    return df.set_index("code")


def rank(date: str = None, n: int = 30) -> dict:
    # ★U1-3 日期口径修复（2026-08-10）：默认 date 用 bars 最近交易日（原用今天日期 →
    #   周末/数据未更新时虚标，pool_layers 观察池显示 08-10 实际数据只有 08-07）
    # ★第四十波：改双库合并探测（主库 08-07 + 增量库 08-10）
    if not date:
        try:
            from data.cache import DailyCache as _DC2
            _last = _DC2().latest_trade_date()
        except Exception:
            _last = None
        if not _last:
            try:
                import sqlite3 as _s
                _c = _s.connect(str(BARS_DB))
                _last = _c.execute(
                    "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
                _c.close()
            except Exception:
                _last = None
        date = _last or datetime.now().strftime("%Y-%m-%d")
    px, vx, hx, lx = load_close_panel(end=date)
    if px.empty:
        return {"error": "close 面板为空", "date": date}

    # ---- 因子计算 ----
    close = px.astype(float)
    ret = close.pct_change()
    vol60 = ret.rolling(60, min_periods=40).std() * np.sqrt(252)
    mom120 = close / close.shift(120) - 1   # 120 日动量
    # ★2026-08-16 B-14 落地：量价前四强进排名引擎（max_ret20/std20/amp20/skew20，全负向）
    max_ret20 = ret.rolling(20, min_periods=10).max()
    std20 = ret.rolling(20, min_periods=10).std()
    skew20 = ret.rolling(20, min_periods=10).skew()
    amp20 = (hx / lx - 1).rolling(20, min_periods=10).mean()
    # ★技术状态（决策池严格确认用）：MA50/MA200/52周高低点/量比
    ma50 = close.rolling(50, min_periods=40).mean()
    ma200 = close.rolling(200, min_periods=120).mean()
    high52 = close.rolling(250, min_periods=150).max()
    low52 = close.rolling(250, min_periods=150).min()
    vol20 = vx.rolling(20, min_periods=10).mean()
    vol60d = vx.rolling(60, min_periods=30).mean()
    f = pd.DataFrame({
        "vol60": vol60.iloc[-1],
        "mom120": mom120.iloc[-1],
        "max_ret20": max_ret20.iloc[-1],
        "std20": std20.iloc[-1],
        "skew20": skew20.iloc[-1],
        "amp20": amp20.iloc[-1],
        "close": close.iloc[-1],
        "ma50": ma50.iloc[-1],
        "ma200": ma200.iloc[-1],
        "high52": high52.iloc[-1],
        "low52": low52.iloc[-1],
        "vol_ratio": (vol20 / vol60d).iloc[-1],
    })

    # 基本面（最新一期；finance code 为 6 位，用 code6 对齐）
    fin = load_fundamentals(date)
    f["code6"] = f.index.str[:6]
    f = f.merge(fin[["roe", "nyoy", "revenue", "net_profit"]],
                left_on="code6", right_index=True, how="left")
    f = f.drop(columns=["code6"])

    # 市值（快照，信息展示用）
    mv = {}
    if MV_CSV.exists():
        try:
            m = pd.read_csv(MV_CSV, encoding="utf-8-sig")
            mv = {str(r.ts_code).upper(): float(r.circ_mv) / 10000 for r in m.itertuples()}
        except Exception:
            pass
    f["mv_yi"] = pd.Series(mv).reindex(f.index)

    # ---- 数据清洗（优中选优前提：盈利 + 无脏数据）----
    f["nyoy"] = pd.to_numeric(f["nyoy"], errors="coerce").clip(-300, 300)  # 异常同比截断
    f["roe"] = pd.to_numeric(f["roe"], errors="coerce")

    # ST 过滤（最新交易日 is_st=1 剔除）
    con = sqlite3.connect(str(BARS_DB))
    last = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
    st_codes = {r[0] for r in con.execute(
        "SELECT code FROM daily_bar WHERE date=? AND is_st=1", (last,)).fetchall()}
    con.close()
    f = f[~f.index.isin(st_codes)]

    # ---- 打分（加权分位：优中选优偏质量/成长，兼顾低波/超跌）----
    # ★2026-08-14 审计修复：lowvol/reversal 原 ascending=True 方向反（rank(pct, ascending=True)
    #   最小值得最小分位→低波/超跌最深反而最低分，三层池推荐"高波动+高动量"股）。
    #   改 ascending=False：波动最低/超跌最深得最高分（低波+反转实证方向）。
    score = pd.DataFrame(index=f.index)
    score["lowvol"] = f["vol60"].rank(pct=True, ascending=False)      # 波动低→高分
    score["reversal"] = f["mom120"].rank(pct=True, ascending=False)   # 超跌→高分（动量反向）
    score["quality"] = f["roe"].rank(pct=True, ascending=False)       # ROE 高→高分
    score["growth"] = f["nyoy"].rank(pct=True, ascending=False)       # 净利同比高→高分
    # ★2026-08-16 B-14：量价前四强（全负向：低彩票/低波/低振幅/低偏度 → 高分）
    score["max_ret20"] = f["max_ret20"].rank(pct=True, ascending=False)
    score["std20"] = f["std20"].rank(pct=True, ascending=False)
    score["amp20"] = f["amp20"].rank(pct=True, ascending=False)
    score["skew20"] = f["skew20"].rank(pct=True, ascending=False)
    # ★2026-08-16 权重调整：质量/成长为主（0.25×2），量价四强合计 0.20 增强
    W = {"quality": 0.25, "growth": 0.25, "lowvol": 0.15, "reversal": 0.15,
         "max_ret20": 0.05, "std20": 0.05, "amp20": 0.05, "skew20": 0.05}
    total = sum(score[k] * w for k, w in W.items())
    f["score"] = total * 100

    # 硬筛选：优中选优 = 高质量 + 正成长（ROE≥5% 且 净利同比>0）→ 再按四因子排名
    f_valid = f[(f["roe"] >= 0.05) & (f["nyoy"] > 0)].sort_values("score", ascending=False)
    # 行业上限（★2026-08-07 观察池需求：3→5，保证观察池规模；决策池阶段再收紧分散度）
    basic = load_basic()
    f_valid["industry"] = basic["industry"].reindex(f_valid.index).fillna("")
    picked, ind_count = [], {}
    for code in f_valid.index:
        ind = f_valid.loc[code, "industry"]
        if ind_count.get(ind, 0) < 5:
            picked.append(code)
            ind_count[ind] = ind_count.get(ind, 0) + 1
        if len(picked) >= n:
            break
    top = f_valid.loc[picked]

    # ---- 组装输出 ----
    out = []
    for i, (code, r) in enumerate(top.iterrows(), 1):
        b = basic.loc[code].to_dict() if code in basic.index else {}
        close_v = float(r["close"]) if pd.notna(r.get("close")) else None
        ma50_v = float(r["ma50"]) if pd.notna(r.get("ma50")) else None
        ma200_v = float(r["ma200"]) if pd.notna(r.get("ma200")) else None
        h52 = float(r["high52"]) if pd.notna(r.get("high52")) else None
        l52 = float(r["low52"]) if pd.notna(r.get("low52")) else None
        dist_high = (close_v / h52 - 1) * 100 if close_v and h52 else None   # 距52周高点%（负=回撤）
        dist_low = (close_v / l52 - 1) * 100 if close_v and l52 else None
        out.append({
            "rank": i,
            "code": code,
            "name": b.get("name", ""),
            "industry": b.get("industry", ""),
            "price": round(close_v, 2) if close_v else None,
            "mv_yi": round(float(r["mv_yi"]), 1) if pd.notna(r.get("mv_yi")) else None,
            "score": round(float(r["score"]), 1),
            "tech": {
                "ma50": round(ma50_v, 2) if ma50_v else None,
                "ma200": round(ma200_v, 2) if ma200_v else None,
                "above_ma50": bool(close_v > ma50_v) if close_v and ma50_v else None,
                "above_ma200": bool(close_v > ma200_v) if close_v and ma200_v else None,
                "dist_high52_pct": round(dist_high, 1) if dist_high is not None else None,
                "dist_low52_pct": round(dist_low, 1) if dist_low is not None else None,
                "vol_ratio_20_60": round(float(r["vol_ratio"]), 2) if pd.notna(r.get("vol_ratio")) else None,
            },
            "factors": {
                "lowvol_rank": round(float(score.loc[code, "lowvol"]), 3),
                "reversal_rank": round(float(score.loc[code, "reversal"]), 3),
                "quality_rank": round(float(score.loc[code, "quality"]), 3),
                "growth_rank": round(float(score.loc[code, "growth"]), 3),
                "vol60": round(float(r["vol60"]) * 100, 1),
                "mom120": round(float(r["mom120"]) * 100, 1),
                "roe": round(float(r["roe"]) * 100, 1) if pd.notna(r.get("roe")) else None,
                "nyoy": round(float(r["nyoy"]), 1) if pd.notna(r.get("nyoy")) else None,
            },
            "reason": build_reason(r, score.loc[code], b),
        })
    return {
        "date": date,
        "n": len(out),
        "method": "4因子等权打分（低波+反转+ROE+净利同比），实证方向；Top100 为观察池，决策池需技术确认（pool_layers）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "top": out,
    }


def build_reason(row, fscore, basic) -> str:
    """规则生成买入理由（离线；AI 增强版在 api_server 提供）"""
    parts = []
    if pd.notna(row.get("roe")) and row["roe"] > 0.1:
        parts.append(f"ROE {row['roe']*100:.1f}% 质量良好")
    if pd.notna(row.get("nyoy")) and row["nyoy"] > 20:
        parts.append(f"净利润同比 +{row['nyoy']:.0f}% 成长强劲")
    if pd.notna(row.get("vol60")) and row["vol60"] < 0.03:
        parts.append(f"60日波动 {row['vol60']*100:.1f}% 低波稳健")
    if pd.notna(row.get("mom120")) and row["mom120"] < -0.1:
        parts.append(f"120日 {row['mom120']*100:.0f}% 超跌待反转")
    if not parts:
        parts.append("四因子综合评分进入 Top 精选")
    strengths = "、".join(parts[:2])
    name = basic.get("name", row.name)
    return f"{name}（{row.name}）：{strengths}；四因子综合分 {row['score']:.1f}，居全市场前 {max(1, int((1 - fscore.mean()) * 100))}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    r = rank(args.date, args.n)
    out = BASE / "output" / "ranking_top.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"排名已生成：{out}（Top {len(r.get('top', []))}）")
    for t in r.get("top", [])[:8]:
        print(f"  #{t['rank']} {t['code']} {t['name']} [{t['industry']}] 分{t['score']} "
              f"ROE {t['factors'].get('roe')}% 净利同比 {t['factors'].get('nyoy')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
