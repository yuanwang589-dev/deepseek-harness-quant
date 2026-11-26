# -*- coding: utf-8 -*-
"""data/rank_live.py — 涨跌幅榜 + 引擎/风控对照（2026-08-10 用户需求）

★需求：① 涨幅榜/跌幅榜实时更新 ② 对照机会引擎：每天命中多少涨幅榜（命中率）
      ③ 对照风控层：拦截多少跌幅榜（拦截率）——验证"机会引擎选得出、风控挡得住"

实现：
  1. bars.db 最新交易日 → 全市场涨跌幅（qfq close 环比）
  2. 涨幅榜 Top N / 跌幅榜 Top N（过滤 ST/退市/次新 <60 日）
  3. 命中 = 涨幅榜 ∩ 最新机会池（opp_pool）
  4. 拦截 = 跌幅榜 ∩ 风控标记（Beneish HIGH/WATCH + 质量风控 BLOCK/WATCH）
  5. 输出 logs/rank_live_{ts}.json + deck 双写 + 实时接口 /api/live/ranks

对照指标：
  hit_rate    = 涨幅榜中机会池命中比例（机会引擎有效性）
  block_rate  = 跌幅榜中风控标记比例（风控层有效性）
  avg_gain_hit / avg_gain_miss = 命中 vs 未命中的平均涨幅（命中是否有 alpha）
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
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ★2026-08-10 API 缓存：页面 60s 轮询每次触发 compute() 会重算+写文件（文件爆炸+写保护风险）
#   ★15:28 升级：缓存键 = 数据文件版本（mtime）——数据源不变则 1h 命中（首次 13s 只发生一次）
_cache = {"ts": 0.0, "ver": 0.0, "data": None}
CACHE_SECONDS = 3600  # 1 小时（版本不变才命中）


def _bars_version() -> float:
    """数据源版本 = bars.db + 增量库最新 mtime（0.01s；MAX/DISTINCT 全表扫 900 万行要 6s+）"""
    try:
        fs = [BARS_DB] + glob.glob(str(Path(BARS_DB).parent / "bars_incr_*.db"))
        mt = [os.path.getmtime(f) for f in fs if os.path.exists(f)]
        return max(mt) if mt else 0.0
    except Exception:
        return 0.0

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

BARS_DB = r"data/cache/bars.db"
TOP_N = 100          # 榜规模
MIN_PRICE = 1.5      # 过滤仙股
MIN_DAYS = 60        # 次新过滤


def _ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=3)


def _latest(pattern, subdir="logs"):
    files = sorted(glob.glob(str(BASE / subdir / pattern)), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/rank_live.py:62 _latest")
    return {}


def _load_names():
    """code → name（stock_basic）"""
    try:
        import pandas as pd
        con = _ro(r"data/cache/stock_basic.db")
        df = pd.read_sql("SELECT code, name FROM stock_basic", con)
        con.close()
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}


def _risk_marks():
    """风控标记集合：Beneish HIGH/WATCH + 质量 BLOCK/WATCH → {code: mark}
    ★2026-08-10 修复：beneish full 结构 {results:{code:...}}；risk_map 在 logs/ 不在 report/
    """
    marks = {}
    # Beneish（full 优先；results 可能是 list[{code,level,...}] 或 dict{code:{level}}）
    try:
        ben = _latest("beneish_report_full.json")
        if not ben:
            ben = _latest("beneish_report*.json")
        if ben:
            data = ben.get("results", ben.get("data", ben))
            if isinstance(data, list):
                for v in data:
                    if isinstance(v, dict) and v.get("level") in ("HIGH", "WATCH"):
                        marks[v.get("code")] = f"Beneish {v.get('level')}"
            elif isinstance(data, dict):
                for c, v in data.items():
                    lv = v.get("level") if isinstance(v, dict) else None
                    if lv in ("HIGH", "WATCH"):
                        marks[c] = f"Beneish {lv}"
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/rank_live.py:100 _risk_marks")
    # 质量风控（logs/stock_risk_map*.json + output/stock_risk_map.json；results 为 dict）
    try:
        for pat, sub in (("stock_risk_map_v2*.json", "logs"), ("stock_risk_map*.json", "logs"),
                         ("stock_risk_map.json", "output")):
            rm = _latest(pat, sub)
            if rm:
                data = rm.get("results", rm.get("data", rm))
                if isinstance(data, dict):
                    for c, v in data.items():
                        lv = v.get("level") if isinstance(v, dict) else None
                        if lv in ("BLOCK", "WATCH"):
                            marks[c] = marks.get(c, "") + f" 风控{lv}"
                break
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/rank_live.py:115 _risk_marks")
    return marks


def compute(date: str = None) -> dict:
    """计算涨跌幅榜 + 引擎/风控对照
    ★2026-08-10 缓存：数据文件版本不变 → 1h 命中（重算 13s 只在数据更新后发生一次）"""
    import pandas as pd
    now = time.time()
    ver = _bars_version()
    if (date is None and _cache["data"] is not None and _cache.get("ver") == ver
            and now - _cache["ts"] < CACHE_SECONDS):
        return _cache["data"]
    con = _ro(BARS_DB)
    # 最新两个交易日
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 2").fetchall()]
    if not dates:
        con.close()
        return {"ok": False, "error": "bars.db 无数据"}
    cur, prev = dates[0], dates[1] if len(dates) > 1 else None
    rows = con.execute(
        "SELECT code, date, close FROM daily_bar WHERE adjust='qfq' AND date IN (?,?)",
        (cur, prev)).fetchall()
    con.close()
    df = pd.DataFrame(rows, columns=["code", "date", "close"])
    if df.empty:
        return {"ok": False, "error": "无行情"}
    piv = df.pivot(index="code", columns="date", values="close")
    if prev not in piv.columns:
        return {"ok": False, "error": f"缺少前一交易日 {prev}"}
    ret = piv[cur] / piv[prev] - 1
    ret = ret.dropna()

    # 基础过滤：价格/次新/ST
    names = _load_names()
    price_ok = piv[cur] >= MIN_PRICE
    ret = ret[price_ok.reindex(ret.index).fillna(False)]
    st_mask = ret.index.map(lambda c: "ST" in str(names.get(c, "")))
    ret = ret[~st_mask]

    gains = ret.sort_values(ascending=False)
    losses = ret.sort_values(ascending=True)

    top_gain = [(c, round(float(r) * 100, 2)) for c, r in gains.head(TOP_N).items()]
    top_loss = [(c, round(float(r) * 100, 2)) for c, r in losses.head(TOP_N).items()]

    # ---- 机会引擎对照（★口径：用榜日之前（含同日）的最近机会池——"池选出 → 涨没涨"）----
    pool_files = sorted(glob.glob(str(BASE / "logs" / "opp_pool_*.json")), key=os.path.getmtime)
    pool = {}
    for f in reversed(pool_files):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            if d.get("date", "9999") <= cur:   # 榜日之前/当日的池
                pool = d
                break
        except Exception:
            continue
    pool_codes = {o["code"] for o in pool.get("opportunities", [])}
    gain_codes = [c for c, _ in top_gain]
    loss_codes = [c for c, _ in top_loss]
    hit_gain = [c for c in gain_codes if c in pool_codes]
    hit_loss = [c for c in loss_codes if c in pool_codes]
    hit_rate = round(len(hit_gain) / len(gain_codes) * 100, 1) if gain_codes else 0

    # ---- 风控对照 ----
    marks = _risk_marks()
    blocked_loss = [c for c in loss_codes if c in marks]
    blocked_gain = [c for c in gain_codes if c in marks]
    block_rate = round(len(blocked_loss) / len(loss_codes) * 100, 1) if loss_codes else 0

    # ---- 命中是否有 alpha（涨幅榜上命中 vs 未命中）----
    gmap = dict(top_gain)
    hit_avg = round(sum(gmap[c] for c in hit_gain) / len(hit_gain), 2) if hit_gain else None
    miss_codes = [c for c in gain_codes if c not in pool_codes]
    miss_avg = round(sum(gmap[c] for c in miss_codes) / len(miss_codes), 2) if miss_codes else None

    out = {
        "ok": True,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": cur, "prev_date": prev,
        "n_stocks": int(len(ret)),
        "top_n": TOP_N,
        "top_gainers": [{"code": c, "name": names.get(c, ""), "ret_pct": r} for c, r in top_gain],
        "top_losers": [{"code": c, "name": names.get(c, ""), "ret_pct": r} for c, r in top_loss],
        "engine_check": {
            "pool_date": pool.get("date", ""),
            "pool_size": len(pool_codes),
            "hit_gain": len(hit_gain), "hit_rate_pct": hit_rate,
            "hit_gain_codes": hit_gain,
            "hit_loss": len(hit_loss), "hit_loss_codes": hit_loss,
            "avg_gain_hit_pct": hit_avg, "avg_gain_miss_pct": miss_avg,
        },
        "risk_check": {
            "blocked_loss": len(blocked_loss), "block_rate_pct": block_rate,
            "blocked_loss_codes": [{"code": c, "mark": marks.get(c, "")} for c in blocked_loss],
            "blocked_gain": len(blocked_gain),
        },
        "note": f"对照基准：机会池 {pool.get('date', '?')} 命中涨幅榜 Top{TOP_N} = {hit_rate}%"
                f"；风控标记跌幅榜 Top{TOP_N} = {block_rate}%（Beneish HIGH/WATCH + 质量 BLOCK/WATCH）",
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ★2026-08-10 逐日对照历史积累（单日命中率参考，逐日序列才有统计意义）——先积累再加入输出
    out["history"] = accumulate(out)
    p = BASE / "logs" / f"rank_live_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    try:
        (BASE / "deck" / f"rank_live_{ts}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/rank_live.py:225 compute")
    # ★缓存（数据版本 + 1h）
    _cache.update({"ts": time.time(), "ver": ver, "data": out})
    return out


def accumulate(result: dict) -> list:
    """把当日对照快照并入历史（按 date 幂等覆盖；时间戳文件写保护免疫，glob 最新读取）
    返回最近 N 天历史列表（供 API/页面趋势展示）"""
    import glob as _g
    rec = {
        "date": result.get("date", ""),
        "prev_date": result.get("prev_date", ""),
        "hit_rate_pct": (result.get("engine_check") or {}).get("hit_rate_pct"),
        "block_rate_pct": (result.get("risk_check") or {}).get("block_rate_pct"),
        "hit_gain": (result.get("engine_check") or {}).get("hit_gain"),
        "pool_size": (result.get("engine_check") or {}).get("pool_size"),
        "blocked_loss": (result.get("risk_check") or {}).get("blocked_loss"),
        "ts": result.get("ts", ""),
    }
    if not rec["date"]:
        return []
    hist = []
    try:
        fs = sorted(_g.glob(str(BASE / "logs" / "rank_history_*.json")), key=os.path.getmtime)
        if fs:
            hist = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
            if not isinstance(hist, list):
                hist = []
    except Exception:
        hist = []
    hist = [h for h in hist if h.get("date") != rec["date"]] + [rec]
    hist.sort(key=lambda h: str(h.get("date", "")))
    try:
        out_f = BASE / "logs" / f"rank_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_f.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/rank_live.py:262 accumulate")
    return hist[-30:]  # 最近 30 天


if __name__ == "__main__":
    r = compute()
    if not r.get("ok"):
        print("失败:", r.get("error"))
        sys.exit(1)
    print(f"涨跌幅榜: {r['date']} | 全市场 {r['n_stocks']} 只 | Top{r['top_n']}")
    print(f"  涨幅榜 Top5: {[(g['code'], g['ret_pct']) for g in r['top_gainers'][:5]]}")
    print(f"  跌幅榜 Top5: {[(l['code'], l['ret_pct']) for l in r['top_losers'][:5]]}")
    ec = r["engine_check"]
    print(f"  机会引擎命中涨幅榜: {ec['hit_gain']}/{r['top_n']} = {ec['hit_rate_pct']}%"
          f" | 命中均涨 {ec['avg_gain_hit_pct']}% vs 未命中 {ec['avg_gain_miss_pct']}%")
    rc = r["risk_check"]
    print(f"  风控拦截跌幅榜: {rc['blocked_loss']}/{r['top_n']} = {rc['block_rate_pct']}%")
