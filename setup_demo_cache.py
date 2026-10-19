# -*- coding: utf-8 -*-
"""把演示数据铺设到项目默认数据目录（演示/评估用，非真实行情）。

背景：本项目存在两套数据路径解析——
  · data/cache.py 读 CACHE_DIR（环境变量 LWQUANT_CACHE_DIR，launcher.py 会设为 <base>/data）
  · 其余 136 处代码硬编码 data/cache/*.db
为了让演示模式两套都命中，本脚本把演示库同时铺到 <base>/data 与 <base>/data/cache。

用法（仓库根目录）：python setup_demo_cache.py
"""
import csv
import shutil
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEMO = BASE / "data" / "demo"
DATA = BASE / "data"
CACHE = BASE / "data" / "cache"
TARGETS = [DATA, CACHE]

for t in TARGETS:
    t.mkdir(parents=True, exist_ok=True)

# 1) 行情主库 + 增量库
src = DEMO / "demo_bars.db"
if not src.exists():
    raise SystemExit("缺少演示数据，请先运行: python data/demo/build_demo_db.py")
for t in TARGETS:
    shutil.copy2(src, t / "bars.db")
    inc = DEMO / "bars_incr.db"
    if inc.exists():
        shutil.copy2(inc, t / "bars_incr.db")

# 2) 股票基础信息表（名称/行业/上市日期）——演示名 + 演示行业
INDUSTRIES = ["银行", "白酒", "医药", "半导体", "新能源", "证券", "军工", "消费电子"]
rows = []
with open(DEMO / "demo_stock_basic.csv", encoding="utf-8") as f:
    for i, row in enumerate(csv.DictReader(f)):
        rows.append((row["code"], row["name"], INDUSTRIES[i % len(INDUSTRIES)],
                     "2020-01-02", "", "1"))   # ipo_date 早于演示窗口，避开次新过滤

for t in TARGETS:
    con = sqlite3.connect(str(t / "stock_basic.db"))
    con.execute("""CREATE TABLE IF NOT EXISTS stock_basic (
        code TEXT PRIMARY KEY, name TEXT, industry TEXT,
        ipo_date TEXT, out_date TEXT, status TEXT)""")
    con.execute("DELETE FROM stock_basic")
    con.executemany("INSERT OR REPLACE INTO stock_basic VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

# 3) 校验
for t in TARGETS:
    b = sqlite3.connect(str(t / "bars.db"))
    n = b.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    c = b.execute("SELECT COUNT(DISTINCT code) FROM daily_bar").fetchone()[0]
    lo, hi = b.execute("SELECT MIN(date), MAX(date) FROM daily_bar").fetchone()
    b.close()
    s = sqlite3.connect(str(t / "stock_basic.db"))
    ns = s.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    s.close()
    print(f"[ok] {t.relative_to(BASE)}/bars.db  行={n} 股票={c} 区间={lo}~{hi} | stock_basic={ns}")

print("下一步：直接 python launcher.py（或双击 启动DSHQuant.cmd）")
