# -*- coding: utf-8 -*-
"""从 D 盘股票数据库导入财报数据到本项目 finance.db / finance_quality.db

源（只读）：D:/quant_data/quant_data/enhanced_quant_database.db
  - financial_income（利润表：净利/营收累计值，report_type='1'）
  - financial_indicators（财务指标：ROE/毛利率/流动比率/资产负债率）
  - financial_cashflow（现金流量表：n_cashflow_act 经营现金流）

目标：
  data/cache/finance.db        finance_report（单季值/单季同比自算，对齐 fetcher_finance 口径）
  data/cache/finance_quality.db quality（对齐 fetch_quality_tushare 口径：比率存小数）

★口径（与项目自有 fetcher 完全一致，勿改）：
  finance_report: net_profit/revenue 单位亿；roe 百分数；sq_net_yoy/sq_rev_yoy 百分数
  quality: roe_avg/gp_margin/np_margin/liability_to_asset 小数；current_ratio/cfo_to_np/cfo_to_or 比值
  period 统一 'YYYY-MM-DD'
★幂等：INSERT OR REPLACE，重跑即同步。
★单季算法（对齐 finance_calc.py）：Q1 累计=单季；Q2/Q3/Q4 = 本期累计-上期累计；
  单季同比 = 单季值 / 去年同季单季值 × 100。

用法：python data/import_finance_from_ddrive.py
"""
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

SRC_DB = r"D:\quant_data\quant_data\enhanced_quant_database.db"
FIN_DB = BASE / "data" / "cache" / "finance.db"
QD_DB = BASE / "data" / "cache" / "finance_quality.db"


def _dedup_latest(con, table, cols):
    """同 (ts_code, end_date) 多行（update_flag 修订）-> 取最新一条"""
    sql = (f"SELECT {', '.join(cols)} FROM {table} "
           f"WHERE (ts_code, end_date, id) IN ("
           f"  SELECT ts_code, end_date, MAX(id) FROM {table} "
           f"  GROUP BY ts_code, end_date)")
    return pd.read_sql(sql, con)


def main() -> int:
    src = Path(SRC_DB)
    if not src.exists():
        print(f"[财报导入] 源库不存在: {src}")
        return 1
    t0 = time.time()
    src_con = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
    try:
        # ---------- finance.db / finance_report ----------
        inc = _dedup_latest(src_con, "financial_income",
                            ["ts_code", "end_date", "n_income", "total_revenue"])
        ind = _dedup_latest(src_con, "financial_indicators", ["ts_code", "end_date", "roe", "eps"])
        df = inc.merge(ind.drop_duplicates(subset=["ts_code", "end_date"]),
                       on=["ts_code", "end_date"], how="left")
        df["period"] = pd.to_datetime(df["end_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        df["y"] = df["period"].str[:4].astype(int)
        df["m"] = df["period"].str[5:7].astype(int)
        df = df.sort_values(["ts_code", "period"])

        g = df.groupby("ts_code", sort=False)
        # 累计 -> 单季：Q1 基期=0（累计即单季，对齐 finance_calc.py）；Q2-Q4 基期=上一报告期累计
        prev_cum_np = g["n_income"].shift(1).where(df["m"].ne(3), 0.0)
        prev_cum_rev = g["total_revenue"].shift(1).where(df["m"].ne(3), 0.0)
        df["sq_net_profit"] = df["n_income"] - prev_cum_np
        df["sq_revenue"] = df["total_revenue"] - prev_cum_rev
        # 去年同季单季（按 (code, 月份) 分组 shift 1 个报告期）
        gy = df.groupby(["ts_code", "m"], sort=False)
        df["sq_net_ly"] = gy["sq_net_profit"].shift(1)
        df["sq_rev_ly"] = gy["sq_revenue"].shift(1)
        # 同比增长率 = (本期/去年同季 - 1) × 100（茅台 2026Q2 实测 -6.9% 验证口径）
        df["sq_net_yoy"] = ((df["sq_net_profit"] / df["sq_net_ly"] - 1) * 100).where(
            df["sq_net_ly"].notna() & (df["sq_net_ly"].abs() > 1.0))
        df["sq_rev_yoy"] = ((df["sq_revenue"] / df["sq_rev_ly"] - 1) * 100).where(
            df["sq_rev_ly"].notna() & (df["sq_rev_ly"].abs() > 1.0))

        FIN_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(FIN_DB)
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS finance_report (
                code TEXT, period TEXT, net_profit REAL, revenue REAL,
                sq_net_profit REAL, sq_net_yoy REAL, sq_revenue REAL, sq_rev_yoy REAL,
                eps REAL, roe REAL, source TEXT,
                PRIMARY KEY (code, period))""")
            out = pd.DataFrame({
                "code": df["ts_code"], "period": df["period"],
                "net_profit": df["n_income"] / 1e8,       # 元 -> 亿
                "revenue": df["total_revenue"] / 1e8,
                "sq_net_profit": df["sq_net_profit"] / 1e8,
                "sq_net_yoy": df["sq_net_yoy"],
                "sq_revenue": df["sq_revenue"] / 1e8,
                "sq_rev_yoy": df["sq_rev_yoy"],
                "eps": df["eps"], "roe": df["roe"],
                "source": "local_import"})
            out = out.drop_duplicates(subset=["code", "period"], keep="last")
            con.executemany(
                "INSERT OR REPLACE INTO finance_report VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                out.itertuples(index=False, name=None))
            con.commit()
            print(f"[财报导入] finance_report 写入 {len(out)} 行")
        finally:
            con.close()

        # ---------- finance_quality.db / quality ----------
        qi = _dedup_latest(src_con, "financial_indicators",
                           ["ts_code", "end_date", "ann_date", "roe_avg", "grossprofit_margin",
                            "netprofit_margin", "current_ratio", "debt_to_assets"])
        cf = _dedup_latest(src_con, "financial_cashflow",
                           ["ts_code", "end_date", "n_cashflow_act"])
        q = qi.merge(cf.drop_duplicates(subset=["ts_code", "end_date"]),
                     on=["ts_code", "end_date"], how="left")
        # cfo 比值需要净利/营收，从 income 取
        q = q.merge(inc[["ts_code", "end_date", "n_income", "total_revenue"]],
                    on=["ts_code", "end_date"], how="left")
        q["period"] = pd.to_datetime(q["end_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        q["pub_date"] = pd.to_datetime(q["ann_date"], format="%Y%m%d", errors="coerce") \
            .dt.strftime("%Y-%m-%d")
        QD_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(QD_DB)
        try:
            con.execute("CREATE INDEX IF NOT EXISTS idx_quality_period ON quality (period)")
            con.execute("""CREATE TABLE IF NOT EXISTS quality (
                code TEXT, period TEXT, roe_avg REAL, gp_margin REAL, np_margin REAL,
                current_ratio REAL, liability_to_asset REAL, cfo_to_np REAL, cfo_to_or REAL,
                pub_date TEXT, PRIMARY KEY (code, period))""")
            outq = pd.DataFrame({
                "code": q["ts_code"], "period": q["period"],
                "roe_avg": q["roe_avg"] / 100.0,          # 百分数 -> 小数
                "gp_margin": q["grossprofit_margin"] / 100.0,
                "np_margin": q["netprofit_margin"] / 100.0,
                "current_ratio": q["current_ratio"],
                "liability_to_asset": q["debt_to_assets"] / 100.0,
                "cfo_to_np": q["n_cashflow_act"] / q["n_income"],
                "cfo_to_or": q["n_cashflow_act"] / q["total_revenue"],
                "pub_date": q["pub_date"]})
            outq = outq.drop_duplicates(subset=["code", "period"], keep="last")
            con.executemany(
                "INSERT OR REPLACE INTO quality VALUES (?,?,?,?,?,?,?,?,?,?)",
                outq.itertuples(index=False, name=None))
            con.commit()
            print(f"[财报导入] quality 写入 {len(outq)} 行")
        finally:
            con.close()
    finally:
        src_con.close()

    # 验证：最新报告期覆盖度 + 抽查茅台
    con = sqlite3.connect(FIN_DB)
    p, n = con.execute(
        "SELECT period, COUNT(*) FROM finance_report GROUP BY period "
        "HAVING COUNT(*)>=4000 ORDER BY period DESC LIMIT 1").fetchone()
    mt = con.execute(
        "SELECT period, net_profit, sq_net_yoy, roe FROM finance_report "
        "WHERE code='600519.SH' ORDER BY period DESC LIMIT 1").fetchone()
    con.close()
    print(f"[验证] finance_report 最新全覆盖报告期 {p}（{n} 只）；茅台最新: {mt}")
    con = sqlite3.connect(QD_DB)
    qp, qn = con.execute(
        "SELECT period, COUNT(*) FROM quality GROUP BY period "
        "HAVING COUNT(*)>=1000 ORDER BY period DESC LIMIT 1").fetchone()
    sample = con.execute(
        "SELECT roe_avg, gp_margin, liability_to_asset FROM quality "
        "WHERE code='600519.SH' AND period=?", (qp,)).fetchone()
    con.close()
    print(f"[验证] quality 最新覆盖报告期 {qp}（{qn} 只）；茅台指标: {sample}")
    print(f"[完成] 耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
