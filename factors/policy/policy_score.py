# -*- coding: utf-8 -*-
"""factors/policy/policy_score.py — 政策面综合评分（宏观政策传导研究 2026-08-11 交付）

政策评分 = 防守触发器 + 仓位调节器（弱信号，不作进攻信号）
  policy_score = -0.35×z(EPU) - 0.20×z(EPU_chg3m) + 0.30×z(社融同比) + 0.15×z(利差)

档位：
  > +0.5  进攻区（政策暖：正常仓位/满仓允许）
  -0.5~+0.5 中性区（维持现有仓位）
  < -0.5  防守区（政策冷：降仓 1 档，如 full→half）

数据源：
  EPU: deepseek-harness-quant/data/cache/policy/epu.db（FRED CHNMAINLANDEPU，月更）
  社融: data/cache/macro.db social_finance（月更）
  国债: data/cache/macro.db bond_yield（日更）

用法：
  python factors/policy/policy_score.py          # 输出当前评分与档位
  python factors/policy/policy_score.py --check  # 回测检验（评分→次月收益）
"""
import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
from scipy import stats

# ★2026-10-18 修相对路径：原 MACRO_DB = Path(r"data\cache\macro.db") 依赖 cwd，
#   一旦从别的目录调用（计划任务 / deck / run_task）就 sqlite3 "unable to open database file"，
#   policy_hook 静默失败 → 择时里政策面永远退化成 50（中性）。改为 BASE 绝对路径。
MACRO_DB = BASE / "data" / "cache" / "macro.db"
BARS_DB = BASE / "data" / "cache" / "bars.db"
EPU_DB = BASE / "data" / "cache" / "policy" / "epu.db"

WEIGHTS = {"epu": -0.35, "epu_chg3m": -0.20, "sf_yoy": 0.30, "spread": 0.15}


def load_panel() -> pd.DataFrame:
    """构造政策面板（月度）：month, epu, epu_chg_3m, sf_yoy_growth, spread"""
    # EPU
    # ★2026-10-18：库缺失时给明确原因，不要抛 sqlite 的 "unable to open database file"
    #   （本机构建中 data/cache/policy/epu.db 就不存在 → 政策面恒退化中性，必须让人看见）
    if not EPU_DB.exists():
        raise FileNotFoundError(f"EPU 库缺失: {EPU_DB}")
    if not MACRO_DB.exists():
        raise FileNotFoundError(f"宏观库缺失: {MACRO_DB}")
    con = sqlite3.connect(str(EPU_DB))
    epu = pd.read_sql_query("SELECT month, epu FROM epu_monthly", con)
    con.close()
    epu["month"] = pd.to_datetime(epu["month"]).dt.strftime("%Y-%m")
    epu = epu.sort_values("month").reset_index(drop=True)
    epu["epu_chg_3m"] = epu["epu"] - epu["epu"].shift(3)
    # 社融
    con = sqlite3.connect(str(MACRO_DB))
    sf = pd.read_sql_query("SELECT month, sf_increment FROM social_finance", con)
    by = pd.read_sql_query("SELECT date, y10, y2 FROM bond_yield", con)
    con.close()
    sf["month"] = sf["month"].astype(str)
    sf["month"] = sf["month"].str[:4] + "-" + sf["month"].str[4:6]
    sf = sf.sort_values("month").reset_index(drop=True)
    sf["sf_yoy"] = sf["sf_increment"].rolling(12).sum()
    sf["sf_yoy_prev"] = sf["sf_yoy"].shift(12)
    sf["sf_yoy_growth"] = sf["sf_yoy"] / sf["sf_yoy_prev"] - 1
    # 国债利差（月末）
    by["date"] = pd.to_datetime(by["date"])
    by["month"] = by["date"].dt.to_period("M").astype(str)
    by["spread"] = by["y10"] - by["y2"]
    by_m = by.groupby("month")["spread"].last().reset_index()

    panel = epu[["month", "epu", "epu_chg_3m"]].merge(
        sf[["month", "sf_yoy_growth"]], on="month", how="left"
    ).merge(by_m, on="month", how="left")
    # 社融月度披露滞后（约1-1.5个月）→ 用最近可得值填充，保证最新月评分可算
    panel["sf_yoy_growth"] = panel["sf_yoy_growth"].ffill()
    return panel.sort_values("month").reset_index(drop=True)


def zscore(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / s.std()


def compute_score(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel.copy()
    p["z_epu"] = zscore(p["epu"])
    p["z_chg3"] = zscore(p["epu_chg_3m"])
    p["z_sf"] = zscore(p["sf_yoy_growth"])
    p["z_sp"] = zscore(p["spread"])
    p["score"] = (
        WEIGHTS["epu"] * p["z_epu"]
        + WEIGHTS["epu_chg3m"] * p["z_chg3"]
        + WEIGHTS["sf_yoy"] * p["z_sf"]
        + WEIGHTS["spread"] * p["z_sp"]
    )

    def bucket(s):
        if s > 0.5:
            return "进攻区"
        if s < -0.5:
            return "防守区"
        return "中性区"

    p["bucket"] = p["score"].apply(bucket)
    return p


def backtest(panel: pd.DataFrame):
    """评分 → 次月收益检验（需市场收益，独立构造）"""
    import sqlite3 as sq

    con = sq.connect(str(BARS_DB))
    bars = pd.read_sql(
        "SELECT date, code, close, is_st FROM daily_bar WHERE adjust='qfq' AND date>='2015-01-01' AND is_st=0",
        con,
    )
    con.close()
    bars["date"] = pd.to_datetime(bars["date"])
    bars["month"] = bars["date"].dt.to_period("M").astype(str)
    bars["ret"] = bars.groupby("code")["close"].pct_change()
    daily = bars.groupby("date")["ret"].mean().reset_index()
    daily["month"] = daily["date"].dt.to_period("M").astype(str)
    mkt = daily.groupby("month")["ret"].apply(lambda x: (1 + x).prod() - 1).reset_index()
    mkt.columns = ["month", "mkt_ret"]

    p = compute_score(panel)
    df = p.merge(mkt, on="month", how="left")
    df["next"] = df["mkt_ret"].shift(-1)
    df = df.dropna(subset=["score", "next"])
    rho, pval = stats.spearmanr(df["score"], df["next"])
    print(f"policy_score → 次月收益: rho {rho:+.3f} (p={pval:.3f}) n={len(df)}")
    b = df.groupby("bucket")["next"].agg(["count", "mean"])
    print(b.round(4).to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    panel = load_panel()
    if args.check:
        backtest(panel)
        return

    p = compute_score(panel)
    latest = p.dropna(subset=["epu"]).tail(3)
    cur = latest.iloc[-1]
    print("=== 政策面评分（最新）===")
    for _, r in latest.iterrows():
        print(
            f"{r['month']}: EPU {r['epu']:.0f} | chg3m {r['epu_chg_3m']:+.0f} | "
            f"社融同比 {r['sf_yoy_growth']*100:+.1f}% | 利差 {r['spread']:.3f} | score {r['score']:+.2f} ({r['bucket']})"
        )
    print(f"\n当前档位: {cur['bucket']} ｜ 动作: "
          + ("降仓1档（防守）" if cur["bucket"] == "防守区"
             else "维持仓位（中性）" if cur["bucket"] == "中性区"
             else "正常仓位/满仓允许（进攻）"))


if __name__ == "__main__":
    main()
