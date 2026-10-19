# -*- coding: utf-8 -*-
"""data/fetch_quality_akshare.py — 财务质量 AkShare 备源（新增，2026-10-15）

背景：原 fetch_quality.py 走 baostock 三接口，但 2026-10-15 baostock 长时间不可用
      （10002007 网络接收错误），质量数据（F-Score / Beneish 依赖）无法获取。
      改用 AkShare 新浪「财务指标」接口，免费稳定。

★字段映射与单位（经真实财报交叉验证，务必勿改）：

  输出字段             akshare 列                          换算
  ------------------  ---------------------------------  ---------------------
  roe_avg             净资产收益率(%)                      /100  (百分数->小数)
  gp_margin           100 - 主营业务成本率(%)              /100  (见下方说明)
  np_margin           销售净利率(%)                        /100
  current_ratio       流动比率                             ——    (本身即比率)
  liability_to_asset  资产负债率(%)                        /100
  cfo_to_np           经营现金净流量与净利润的比率(%)        ——    ★已是比率，不缩放
  cfo_to_or           经营现金净流量对销售收入比率(%)        ——    ★已是比率，不缩放

单位实证（2026-10-15）：
  · 600519 茅台：经营现金净流量/净利润 = 0.3172（Q1 现金流88亿/净利268亿≈0.33）
    -> 该列虽标 (%)，实为小数比率；若再 /100 会变成 0.0032，Beneish 判断失效。
  · risk/beneish.py 用 cfo_to_np >= 1.0 判「现金覆盖净利」-> 库内必须是小数比率。
  · 600519: 净资产收益率=10.39 -> /100=0.1039；资产负债率=14.143 -> 0.14143。

毛利率替代说明：
  新浪「销售毛利率(%)」该列**恒为空**（实测 600519/000858/000002 全 NaN），不可用。
  改用「主营业务成本率(%)」反推：毛利率 = 100 - 成本率。
  实测：茅台 100-8.0264=91.97%（实际毛利率约 91.9%）；
        五粮液 100-22.2576=77.74%（实际约 77%）；万科 100-93.8997=6.10%（合理）。
  若成本率缺失，回退尝试「销售毛利率(%)」原列。

产出：data/cache/finance_quality.db 表 quality（与 fetch_quality.py 同 schema）
用法：
  python data/fetch_quality_akshare.py --workers 3
  python data/fetch_quality_akshare.py --workers 3 --limit 20   # 小样本验证
  python data/fetch_quality_akshare.py --workers 3 --all        # 全量重拉（覆盖错值）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

QD_DB = BASE / "data" / "cache" / "finance_quality.db"
STOCK_BASIC = BASE / "data" / "cache" / "stock_basic.db"
LOG_FILE = BASE / "logs" / "quality_akshare.log"
FAILED_FILE = BASE / "logs" / "quality_failed_akshare.json"
START_YEAR = "2025"
MIN_PERIOD = "2025-01-01"

COL_ROE = "净资产收益率(%)"
COL_NPM = "销售净利率(%)"
COL_CR = "流动比率"
COL_LTA = "资产负债率(%)"
COL_CFO_NP = "经营现金净流量与净利润的比率(%)"
COL_CFO_OR = "经营现金净流量对销售收入比率(%)"
COL_COST = "主营业务成本率(%)"
COL_GP_DIRECT = "销售毛利率(%)"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + chr(10))


def _conn():
    QD_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(QD_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS quality (
        code TEXT, period TEXT, roe_avg REAL, gp_margin REAL, np_margin REAL,
        current_ratio REAL, liability_to_asset REAL, cfo_to_np REAL, cfo_to_or REAL,
        pub_date TEXT, PRIMARY KEY(code, period))""")
    con.commit()
    return con


def load_codes():
    con = sqlite3.connect(str(STOCK_BASIC))
    codes = [r[0] for r in con.execute(
        "SELECT code FROM stock_basic WHERE code LIKE '%.SH' OR code LIKE '%.SZ' ORDER BY code")]
    con.close()
    return codes


def done_codes():
    if not QD_DB.exists():
        return set()
    con = sqlite3.connect(str(QD_DB))
    try:
        s = {r[0] for r in con.execute("SELECT DISTINCT code FROM quality")}
    except Exception:
        s = set()
    con.close()
    return s


def _num(r, col, scale, pd):
    v = pd.to_numeric(r.get(col), errors="coerce")
    if pd.isna(v):
        return None
    return float(v) * scale


def _gross_margin(r, pd):
    """毛利率：优先 100-主营业务成本率；回退 销售毛利率 原列"""
    cost = pd.to_numeric(r.get(COL_COST), errors="coerce")
    if not pd.isna(cost):
        return max(0.0, (100.0 - float(cost)) / 100.0)
    direct = pd.to_numeric(r.get(COL_GP_DIRECT), errors="coerce")
    if not pd.isna(direct):
        return float(direct) / 100.0
    return None


def _worker(code):
    """子进程：拉取+映射，返回行列表（不写库）。code 形如 600519.SH"""
    import akshare as ak
    import pandas as pd
    symbol = code.split(".")[0]
    last = None
    for attempt in range(3):
        time.sleep(0.2)
        try:
            df = ak.stock_financial_analysis_indicator(symbol=symbol, start_year=START_YEAR)
            if df is None or df.empty:
                return (code, [], None)
            rows = []
            for _, r in df.iterrows():
                period = str(r.get("日期"))[:10]
                if period < MIN_PERIOD:
                    continue
                rows.append((
                    code, period,
                    _num(r, COL_ROE, 0.01, pd),
                    _gross_margin(r, pd),
                    _num(r, COL_NPM, 0.01, pd),
                    _num(r, COL_CR, 1.0, pd),
                    _num(r, COL_LTA, 0.01, pd),
                    _num(r, COL_CFO_NP, 1.0, pd),   # ★已是比率，不缩放
                    _num(r, COL_CFO_OR, 1.0, pd),   # ★已是比率，不缩放
                    None,
                ))
            return (code, rows, None)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    return (code, None, last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="不跳过已入库（用于覆盖错值）")
    args = ap.parse_args()

    codes = load_codes()
    have = set() if args.all else done_codes()
    todo = [c for c in codes if c not in have]
    gap = len(todo)
    if args.limit:
        todo = todo[:args.limit]

    log(f"股票池 {len(codes)} 只，已入库 {len(have)} 只，缺口 {gap} 只，本次 {len(todo)} 只，{args.workers} 进程")
    if not todo:
        log("无缺口，跳过")
        return 0

    con = _conn()
    t0 = time.time()
    ok = empty = 0
    fails = []
    with Pool(args.workers) as pool:
        for i, (code, rows, err) in enumerate(pool.imap_unordered(_worker, todo), 1):
            if err:
                fails.append(code)
            elif not rows:
                empty += 1
            else:
                con.executemany(
                    "INSERT OR REPLACE INTO quality VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                ok += 1
            if i % 50 == 0 or i == len(todo):
                con.commit()
                el = time.time() - t0
                rate = i / el if el > 0 else 0
                left = (len(todo) - i) / rate / 60 if rate > 0 else 0
                log(f"[进度] {i}/{len(todo)} 成功{ok} 空{empty} 失败{len(fails)} 速率{rate:.2f}只/s 剩余约{left:.0f}分钟")
    con.commit()
    con.close()

    if fails:
        FAILED_FILE.write_text(json.dumps(fails, ensure_ascii=False), encoding="utf-8")
        log(f"失败 {len(fails)} 只已落盘 {FAILED_FILE}")
    log(f"== 完成 == 处理 {len(todo)} 只，成功 {ok}，空 {empty}，失败 {len(fails)}，耗时 {(time.time()-t0)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
