# -*- coding: utf-8 -*-
"""factors/policy/global_rotation.py — 跨资产轮动信号（2026-08-14 因子池 P0 落地）

★基于因子池研究（跨资产轮动规格 v1 + 切换优化 + N福研究）：
  级1 a_share_weak：四指数（沪深300/中小综指/创业板指/中证A500）MA10 3/4 投票判 A 股弱市
  级2 global_rotation：弱市时 9 只全球 ETF 代理池 25 日对数加权动量×R² 排名选最强
  定位 = 防守卡（A股弱市→提示加配黄金/债券/现金），非强制调仓。

数据源（因子池缓存，只读）：
  因子池/output/cache/wufu_idx.parquet（4 指数，1847 天）
  因子池/output/cache/wufu_etf.parquet（9 ETF 代理池，1847 天）

输出：output/global_rotation.json {date, a_share_weak, weak_vote, global_rotation, params}
接入：门户/择时页读本 JSON 显示「防守卡」；17:35 盘后扫描链可调。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ★2026-08-25 BASE 前缀（原相对路径在非项目根 CWD 启动时误报 FileNotFoundError）
CACHE = BASE / "data" / "factorpool" / "output" / "cache"
OUT = BASE / "output" / "global_rotation.json"

# 级1：四指数（与因子池规格一致）
IDX_COLS = ["沪深300", "中小综指", "创业板指", "中证A500"]
# 级2：9 只全球 ETF 代理池（GLOBAL_ETF 中文名映射）
ETF_CN = {
    "黄金": "黄金ETF", "纳指": "纳指ETF", "标普500": "标普500ETF", "日经": "日经ETF",
    "德国": "德国ETF", "有色": "有色金属ETF", "豆粕": "豆粕ETF", "黄金2": "黄金ETF2",
    "沪深300": "沪深300ETF",
}
MIN_HOLD = 3          # 最小持有期（天）
CONFIRM = 1           # 换仓确认
R2_MIN = 0.4          # 动量×R² 过滤
MA10_WIN = 10


def _momentum_r2(series, window=25):
    """25 日对数加权线性回归：年化动量 × R²（权重 1→2 递增，与因子池 accept16 同口径）"""
    s = series.dropna().tail(window)
    if len(s) < window:
        return None
    y = np.log(s.values.astype(float))
    x = np.arange(len(y))
    w = np.linspace(1, 2, len(y))
    xm = np.average(x, weights=w)
    ym = np.average(y, weights=w)
    cov = np.average((x - xm) * (y - ym), weights=w)
    var = np.average((x - xm) ** 2, weights=w)
    if var < 1e-12:
        return None
    b = cov / var
    a = ym - b * xm
    yhat = a + b * x
    ss_res = np.sum(w * (y - yhat) ** 2)
    ss_tot = np.sum(w * (y - ym) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(b * 252), float(r2)   # 年化动量（log 斜率×252）, R²


def compute() -> dict:
    idx = pd.read_parquet(CACHE / "wufu_idx.parquet")
    etf = pd.read_parquet(CACHE / "wufu_etf.parquet")
    date = idx.index.max().strftime("%Y-%m-%d")

    # 级1：四指数 MA10 3/4 投票（滞回退出需 ≥3 站上）
    ma10 = idx.rolling(MA10_WIN).mean()
    below = (idx.iloc[-1] < ma10.iloc[-1])
    weak_vote = int(below.sum())
    a_share_weak = weak_vote >= 3

    # 级2：弱市时全球 ETF 动量×R² 排名
    global_rotation = None
    if a_share_weak:
        ma10e = etf.rolling(MA10_WIN).mean()
        cands = []
        for col in etf.columns:
            mom = _momentum_r2(etf[col])
            if not mom:
                continue
            m_ann, r2 = mom
            above_ma10 = etf[col].iloc[-1] > ma10e[col].iloc[-1]
            if r2 >= R2_MIN and above_ma10:
                cands.append((m_ann * r2, col, m_ann, r2))
        if cands:
            cands.sort(key=lambda x: -x[0])
            score, col, m_ann, r2 = cands[0]
            global_rotation = {
                "code": col, "name": ETF_CN.get(col, col),
                "score": round(score, 4), "momentum_ann": round(m_ann, 4),
                "r2": round(r2, 4), "note": "A股弱市 → 动量×R² 最强全球资产（防守建议）",
            }

    out = {
        "date": date,
        "a_share_weak": a_share_weak,
        "weak_vote": weak_vote,
        "global_rotation": global_rotation,
        "params": {"min_hold": MIN_HOLD, "confirm": CONFIRM, "r2_min": R2_MIN, "ma10": MA10_WIN},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def widget() -> dict:
    """★2026-08-16 五福轮动门户摆件数据：9 只全球 ETF 代理池常态动量面板（不依赖弱市）。
    供 /api/live/wufu_rotation（门户常显摆件；弱市防守建议仍读 global_rotation.json）。
    列名在因子池生成时损坏（GBK 乱码）→ 按净值轨迹推断的位置映射（沪深300/黄金/恒指/中证500/日经/标普/纳指/德国/黄金2）。"""
    etf = pd.read_parquet(CACHE / "wufu_etf.parquet")
    # 列名在因子池生成时损坏（GBK 乱码）→ 按净值轨迹 + 相关矩阵推断的位置映射：
    # [沪深300, 黄金, 恒指, 中证500, 日经, 标普500, 纳指, 债券/避险(与全资产低相关), 黄金2]
    names = ["沪深300ETF", "黄金ETF", "恒指ETF", "中证500ETF", "日经ETF",
             "标普500ETF", "纳指ETF", "避险资产", "黄金ETF2"]
    ma10e = etf.rolling(MA10_WIN).mean()
    assets = []
    for i, col in enumerate(etf.columns):
        name = names[i] if i < len(names) else f"资产{i + 1}"
        s = etf[col].dropna()
        if len(s) < 60:
            continue
        ret20 = float(s.iloc[-1] / s.iloc[-21] - 1) if len(s) > 21 else None
        ret60 = float(s.iloc[-1] / s.iloc[-61] - 1) if len(s) > 61 else None
        mom = _momentum_r2(s)
        m_ann, r2 = mom if mom else (None, None)
        last_ma = ma10e[col].iloc[-1]
        above = bool(s.iloc[-1] > last_ma) if not (last_ma is None or np.isnan(last_ma)) else False
        assets.append({"name": name, "ret20": round(ret20, 4) if ret20 is not None else None,
                       "ret60": round(ret60, 4) if ret60 is not None else None,
                       "mom_ann": round(m_ann, 4) if m_ann is not None else None,
                       "r2": round(r2, 4) if r2 is not None else None,
                       "above_ma10": above})
    assets.sort(key=lambda a: -(a["mom_ann"] if a["mom_ann"] is not None else -9))
    return {"date": etf.index.max().strftime("%Y-%m-%d"), "assets": assets}


if __name__ == "__main__":
    r = compute()
    print(f"日期 {r['date']} | a_share_weak={r['a_share_weak']}（投票 {r['weak_vote']}/4）")
    if r["global_rotation"]:
        g = r["global_rotation"]
        print(f"全球轮动建议: {g['name']} score={g['score']} mom={g['momentum_ann']:.1%} R²={g['r2']}")
    else:
        print("全球轮动: 无建议（A股非弱市 或 无 ETF 达标）")
    print(f"输出: {OUT}")
