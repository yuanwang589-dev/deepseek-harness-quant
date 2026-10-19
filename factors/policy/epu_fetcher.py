# -*- coding: utf-8 -*-
"""factors/policy/epu_fetcher.py — 政策不确定性指数（EPU）数据抓取器

数据源（免费公开，多源主备，2026-08-07 实测）：
  主源  FRED CHNMAINLANDEPU（Davis-Liu-Sheng 中国大陆报纸 EPU，1949 至今，月度，持续更新）
        https://fred.stlouisfed.org/graph/fredgraph.csv?id=CHNMAINLANDEPU （无需 API key）
  对照  Huang & Luk (2020) 中国 EPU（香港浸会大学，2000-01~2022-06，月度 + 分政策类型）
        https://cbade.hkbu.edu.hk/epu-mainland-china/
        分政策：CN_Fiscal 财政 / CN_Monetary 货币 / CN_Trade 贸易 / CN_EXR 汇率

入库：data/cache/policy/epu.db 表 epu_monthly
  (month TEXT PK, epu REAL, epu_hl REAL, epu_fiscal REAL, epu_monetary REAL, epu_trade REAL, epu_exr REAL)

用法：
  python factors/policy/epu_fetcher.py          # 下载 FRED 全量 + 解析本地 H&L xlsx → 入库
  python factors/policy/epu_fetcher.py --check  # 查看库中数据范围
"""
import argparse
import os
import sqlite3
import ssl
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

POLICY_DIR = BASE / "data" / "cache" / "policy"
DB_PATH = POLICY_DIR / "epu.db"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CHNMAINLANDEPU"
HL_FILES = {
    "epu_hl": "epu_hl_monthly.xlsx",
    "epu_hl_policy": "epu_hl_policy.xlsx",
}
_CTX = None


def _ctx():
    global _CTX
    if _CTX is None:
        # ★2026-10-19 H-4 修复：使用默认安全上下文，校验证书链与主机名。
        #   原 check_hostname=False + verify_mode=CERT_NONE 会让 HTTPS 流量可被中间人伪造，
        #   政策择时信号经此抓取，存在被篡改风险。
        _CTX = ssl.create_default_context()
    return _CTX


def _download(url: str, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx()).read()


def init_db() -> sqlite3.Connection:
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""CREATE TABLE IF NOT EXISTS epu_monthly (
        month TEXT PRIMARY KEY, epu REAL, epu_hl REAL,
        epu_fiscal REAL, epu_monetary REAL, epu_trade REAL, epu_exr REAL)""")
    con.commit()
    return con


def load_fred() -> list:
    """FRED CHNMAINLANDEPU → [(month, epu)]，格式 'YYYY-MM'"""
    csv_text = _download(FRED_URL).decode("utf-8", errors="ignore")
    rows = []
    for line in csv_text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or not parts[1] or parts[1] == ".":
            continue
        d = parts[0].strip()[:7]          # YYYY-MM-DD → YYYY-MM
        try:
            v = float(parts[1])
        except ValueError:
            continue
        rows.append((d, v))
    return rows


def load_hl() -> dict:
    """H&L xlsx → {month: {epu_hl/fiscal/monetary/trade/exr}}（若文件存在）"""
    out = {}
    try:
        import pandas as pd
    except ImportError:
        return out
    m_path = POLICY_DIR / HL_FILES["epu_hl"]
    p_path = POLICY_DIR / HL_FILES["epu_hl_policy"]
    if m_path.exists():
        df = pd.read_excel(m_path)
        for _, r in df.iterrows():
            d = str(r["Date"])
            if len(d) < 10:
                continue
            m = d[:10][:7]
            v = r["CNEPU"]
            if pd.notna(v):
                out.setdefault(m, {})["epu_hl"] = float(v)
    if p_path.exists():
        df = pd.read_excel(p_path)
        for _, r in df.iterrows():
            d = str(r["Date"])
            if len(d) < 10:
                continue
            m = d[:10][:7]
            rec = out.setdefault(m, {})
            for col, key in [("CN_Fiscal", "epu_fiscal"), ("CN_Monetary", "epu_monetary"),
                             ("CN_Trade", "epu_trade"), ("CN_EXR", "epu_exr")]:
                v = r.get(col)
                if pd.notna(v):
                    rec[key] = float(v)
    return out


def sync(con: sqlite3.Connection) -> int:
    """FRED 全量 upsert（921 行很小，直接全量）+ H&L 补列"""
    fred = load_fred()
    hl = load_hl()
    cur = con.cursor()
    n = 0
    for month, v in fred:
        cur.execute("INSERT OR REPLACE INTO epu_monthly (month, epu) VALUES (?,?)", (month, v))
        n += 1
    for month, rec in hl.items():
        cur.execute(
            "UPDATE epu_monthly SET epu_hl=?, epu_fiscal=?, epu_monetary=?, epu_trade=?, epu_exr=? WHERE month=?",
            (rec.get("epu_hl"), rec.get("epu_fiscal"), rec.get("epu_monetary"),
             rec.get("epu_trade"), rec.get("epu_exr"), month))
    con.commit()
    return n


def check():
    con = init_db()
    r = con.execute("SELECT MIN(month), MAX(month), COUNT(*), SUM(epu IS NOT NULL), SUM(epu_hl IS NOT NULL) FROM epu_monthly").fetchone()
    print(f"epu_monthly: {r[0]} ~ {r[1]}（{r[2]} 行）FRED {r[3]} 个点 / H&L {r[4]} 个点")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EPU 数据抓取")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        check()
    else:
        con = init_db()
        n = sync(con)
        con.close()
        print(f"EPU 入库完成：{n} 条（FRED 全量 + H&L 对照列）")
        check()
