# -*- coding: utf-8 -*-
"""data/fetch_quality_backup.py — 质量因子补拉（备用服务器通道，2026-08-09）

★重大发现：备用HTTP服务器 备用服务器 fina-indicator 权限已开通（商家升级），
字段与主服务器完全一致（roe/grossprofit_margin/.../ocfps），响应 0.1-0.3s
（主服务器 6.7s 的 20 倍）→ 质量补拉不再依赖主服务器！

与 fetch_quality_tushare.py 的关系：
- 本脚本：HTTP API 通道（备用HTTP服务器），写入同一张 quality 表（口径一致：百分数 /100）
- 原脚本：tushare pro 通道（quantdata888），主服务器恢复后仍可用
- 两者可互换/续传（INSERT OR REPLACE + _done_codes 跳过已拉）

用法：
  python data/fetch_quality_backup.py --workers 4          # 全市场续传
  python data/fetch_quality_backup.py --limit 20           # 小样本验证
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
import concurrent.futures
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ["NO_PROXY"] = "*"

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests

QD_DB = r"data\cache\finance_quality.db"
BACKUP_URL = "http://<your-backup-server>/app-api/openapi/v1/tushare"
BACKUP_KEY = os.environ.get("LWQUANT_TUSHARE_BACKUP_KEY", "")  # 备用服务器 api_key（环境变量读取，勿硬编码）
START_QUARTER = "20250101"
END_QUARTER = "20260630"

LOG = BASE / "logs" / "quality_backup.log"

# 需要的关键字段（与主服务器 fina_indicator 一致）
NEED_FIELDS = ["ts_code", "end_date", "ann_date", "roe", "grossprofit_margin",
               "netprofit_margin", "current_ratio", "debt_to_assets",
               "eps", "ocfps", "revenue_ps"]


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/fetch_quality_backup.py:54 log")


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _load_data_cfg():
    import yaml
    return yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))["data"]


def load_codes():
    """全市场代码（stock_basic，带后缀）"""
    con = sqlite3.connect(r"data\cache\stock_basic.db")
    codes = [r[0] for r in con.execute("SELECT code FROM stock_basic").fetchall()]
    con.close()
    return codes


def _done_codes():
    """已入库的股票集合（续传跳过）"""
    con = sqlite3.connect(QD_DB)
    done = {r[0] for r in con.execute("SELECT DISTINCT code FROM quality").fetchall()}
    con.close()
    return done


def _conn():
    return sqlite3.connect(QD_DB)


def _fetch_one(code):
    """拉单股 fina-indicator（备用 HTTP API）→ 质量指标行列表
    ★40203 偶发（实测成功率 ~80%，同股票重试必成功）+ 空返回（临时）→ 重试最多 4 次
    ★返回格式：data.fields（列名）+ data.items（值数组，与 fields 对齐）
    """
    params = {"ts_code": code, "start_date": START_QUARTER, "end_date": END_QUARTER}
    for attempt in range(4):
        try:
            r = requests.get(f"{BACKUP_URL}/fina-indicator",
                             headers={"X-API-Key": BACKUP_KEY},
                             params=params, timeout=25)
            d = r.json()
            if d.get("code") == 0:
                data = d.get("data") or {}
                fields = data.get("fields") or []
                items = data.get("items") or []
                if fields and items:
                    break
                # 空返回（偶发）→ 重试
            elif attempt == 3:
                return code, None, f"code={d.get('code')} {str(d.get('msg'))[:60]}"
        except Exception as e:
            if attempt == 3:
                return code, None, str(e)[:100]
        time.sleep(2.0 * (attempt + 1))
    else:
        return code, [], None
    # 字段索引
    idx = {f: fields.index(f) for f in NEED_FIELDS if f in fields}
    if "end_date" not in idx:
        return code, None, "缺 end_date 字段"
    rows = []
    for it in items:
        row = {f: (it[i] if i < len(it) else None) for f, i in idx.items()}
        period = str(row.get("end_date") or "")
        if len(period) != 8:
            continue
        period = f"{period[:4]}-{period[4:6]}-{period[6:]}"
        eps = _num(row.get("eps"))
        ocfps = _num(row.get("ocfps"))
        rev_ps = _num(row.get("revenue_ps"))
        cfo_np = (ocfps / eps) if (eps and eps > 0 and ocfps is not None) else None
        cfo_or = (ocfps / rev_ps) if (rev_ps and rev_ps > 0 and ocfps is not None) else None
        # ★口径：百分数 /100（与主服务器通道一致；current_ratio 倍率不除）
        pct = lambda v: (v / 100.0) if v is not None else None
        rows.append((code, period,
                     pct(_num(row.get("roe"))), pct(_num(row.get("grossprofit_margin"))),
                     pct(_num(row.get("netprofit_margin"))), _num(row.get("current_ratio")),
                     pct(_num(row.get("debt_to_assets"))), cfo_np, cfo_or,
                     str(row.get("ann_date")) if row.get("ann_date") else None))
    return code, rows, None


def run(workers=4, limit=None):
    codes = load_codes()
    done = _done_codes()
    todo = [c for c in codes if c not in done]
    if limit:
        todo = todo[:limit]
    log(f"质量补拉(备用通道): 总 {len(codes)}, 已完成 {len(done)}, 待拉 {len(todo)}, workers={workers}")
    if not todo:
        log("无需补拉")
        return

    con = _conn()
    t0 = time.time()
    ok = empty = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, c): c for c in todo}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            code, rows, err = fut.result()
            if rows:
                con.executemany(
                    """INSERT OR REPLACE INTO quality
                       (code, period, roe_avg, gp_margin, np_margin, current_ratio,
                        liability_to_asset, cfo_to_np, cfo_to_or, pub_date)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
                con.commit()
                ok += 1
            elif err:
                fail += 1
                if fail <= 5:
                    log(f"  [失败] {code}: {err}")
            else:
                empty += 1
            if i % 100 == 0:
                el = time.time() - t0
                log(f"  进度 {i}/{len(todo)} ({el:.0f}s, 成功 {ok}, 空 {empty}, 失败 {fail}, 均 {el/i:.2f}s/只)")
    con.close()
    el = time.time() - t0
    log(f"完成: 成功 {ok} / 空 {empty} / 失败 {fail}, 耗时 {el:.0f}s ({el/60:.1f} 分钟)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(workers=args.workers, limit=args.limit)
