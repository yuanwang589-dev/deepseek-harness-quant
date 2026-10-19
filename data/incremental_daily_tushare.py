# -*- coding: utf-8 -*-
"""data/incremental_daily_tushare.py — Tushare 日线增量（主服务器按日批量 · 2026-08-10 总指导）

★背景：日线增量此前依赖 baostock（半挂起：单只 40s，全市场不可行）→ 数据停在 08-07。
  quantdata888 服务器实测可用（daily 全市场 5535 只 0.8s）→ 日线增量切 Tushare 通道。

用法：
  python data/incremental_daily_tushare.py            # 自动探测最新交易日并拉取（盘后数据未出则跳过）
  python data/incremental_daily_tushare.py --date 20260810   # 指定日期
  python data/incremental_daily_tushare.py --basic    # 附带拉 daily_basic 估值快照（供 stock_check）

写入：DailyCache.put_daily（主库被锁自动路由 bars_incr_*.db，增量覆盖）；幂等（已有日期跳过）。
"""
import argparse

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception:
    pass

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ★2026-08-10 计划任务 GBK 崩溃防护（F3 链同款教训：脚本打印 ⚠️✅ emoji，
#   计划任务环境 stdout 默认 GBK → UnicodeEncodeError → 任务静默失败）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _pro, _call
from data.cache import DailyCache

BARS_DB = str(BASE / "data" / "cache" / "bars.db")   # ★2026-10-15 P1-A 绝对路径（原相对依赖 cwd）


def latest_trade_date(pro=None) -> str:
    """服务器最新交易日（YYYYMMDD）；服务器盘后数据未出时返回空
    ★2026-08-14 修复：用单只股票轻量探测（ts_code 1 行，~0.5s）替代全市场 daily 查询——
      原全市场查询在 代理服务器 间歇超时下被误判为"盘后数据未出"→ 整链跳过 → 走 baostock 慢兜底。
    ★2026-10-15 适配 120 积分档：trade_cal 被限频(1次/小时) → 拉取失败时本地生成
      近 25 个自然日的工作日候选，仍用 000001.SZ 单只 daily 探测（daily 在 120 档可用）。"""
    pro = pro or _pro()
    dates = []
    try:
        # ★2026-10-15：单次直调不走 _call（120 积分档 trade_cal 限 1次/小时，重试无意义）
        df = pro.trade_cal(exchange="SSE", start_date="20260701",
                           end_date=time.strftime("%Y%m%d"), is_open="1")
        if df is not None and not df.empty:
            dates = sorted(df["cal_date"].astype(str).tolist())
    except Exception:
        pass
    if not dates:
        # trade_cal 限频/失败 → 本地工作日候选（周末/节假日由单只探测自然跳过）
        import datetime as _dt2
        d, cand = _dt2.date.today(), []
        for _ in range(25):
            if d.weekday() < 5:
                cand.append(d.strftime("%Y%m%d"))
            d -= _dt2.timedelta(days=1)
        dates = sorted(cand)
    if not dates:
        return ""
    # 从最近往回找：单只股票轻量探测（000001.SZ 平安银行，1 行即代表该日已就绪）
    for d in reversed(dates[-12:]):
        try:
            _one = _call(pro.daily, ts_code="000001.SZ", trade_date=d)
            if _one is not None and len(_one) > 0:
                return d
        except Exception:
            continue
    return ""


def fetch_day(pro, trade_date: str):
    """拉单日全市场 → 标准 bars DataFrame（★复权处理：Tushare daily 为未复权价，
    bars 主库为 qfq 前复权价——用 adj_factor 换算到上一交易日基准，保证价格口径连续；
    无除权（复权因子不变）时未复权价==前复权价，直接使用）
    ★2026-08-14 并行化：daily/adj_factor×2/daily_basic 4 路并行拉取（代理服务器 单调用 ~9s，
      串行 ~40-114s → 并行 ~12-20s）"""
    # 先取 prev（本地快，供 adj_factor 基准 + 并行拉取用）
    try:
        import sqlite3 as _s
        con = _s.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        prev = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
        con.close()
    except Exception:
        prev = None
    prev8 = str(prev).replace("-", "") if prev else None
    # 4 路并行：daily + adj_factor(t) + adj_factor(prev) + daily_basic(is_st)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as _ex:
        f_d = _ex.submit(lambda: _call(pro.daily, trade_date=trade_date))
        f_aft = _ex.submit(lambda: _call(pro.adj_factor, trade_date=trade_date)) if prev8 else None
        f_afp = _ex.submit(lambda: _call(pro.adj_factor, trade_date=prev8)) if prev8 else None
        f_basic = _ex.submit(lambda: _call(pro.daily_basic, trade_date=trade_date, fields="ts_code,is_st,turnover_rate"))
        try:
            df = f_d.result()
        except Exception:
            df = None
        try:
            af_t = f_aft.result() if f_aft else None
            af_p = f_afp.result() if f_afp else None
        except Exception:
            af_t = af_p = None
        try:
            _db = f_basic.result()
        except Exception:
            _db = None
    if df is None or df.empty:
        return None
    # 复权换算：qfq 价 = 未复权价 × adj[t] / adj[prev]（prev = bars 已有最近交易日）
    if prev:
        try:
            if af_t is not None and af_p is not None and len(af_t) and len(af_p):
                m_t = dict(zip(af_t["ts_code"], af_t["adj_factor"]))
                m_p = dict(zip(af_p["ts_code"], af_p["adj_factor"]))
                def _adj(code, row):
                    a_t, a_p = m_t.get(code), m_p.get(code)
                    if a_t and a_p and abs(a_t - a_p) > 1e-9:   # 除权除息日 → 换算
                        return row * a_t / a_p
                    return row
                df["open"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["open"])]
                df["high"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["high"])]
                df["low"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["low"])]
                df["close"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["close"])]
                # ★列名在 rename 前是 pre_close（勿写 preclose，会与 rename 后重复）
                df["pre_close"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["pre_close"])]
                n_adj = sum(1 for c in df["ts_code"] if abs(m_t.get(c, 0) - m_p.get(c, 0)) > 1e-9)
                if n_adj:
                    print(f"  ⚠ {n_adj} 只除权除息，已按复权因子换算（基准 {prev}）")
        except Exception as e:
            print(f"  ⚠ adj_factor 拉取失败（按未复权写入）: {str(e)[:60]}")
    out = df.rename(columns={
        "ts_code": "code", "trade_date": "date", "pre_close": "preclose",
        "vol": "volume", "pct_chg": "pct_chg"})
    out["date"] = out["date"].astype(str).str[:4] + "-" + out["date"].astype(str).str[4:6] + "-" + out["date"].astype(str).str[6:8]
    # ★2026-08-15 治本修复：Tushare daily 无换手率 → 增量行 turn 恒 NULL（08-14 全 tushare 后
    #   bars.turn 覆盖归零，因子池 turnover 因子 58%、scan 五强 2/5 降级）。
    #   daily_basic 并行调用已补 turnover_rate（%），映射写入 turn（与 baostock turn 同单位）。
    try:
        if _db is not None and len(_db) and "turnover_rate" in _db.columns:
            _tr_map = dict(zip(_db["ts_code"], _db["turnover_rate"]))
            out["turn"] = out["code"].map(lambda c: _tr_map.get(c)).astype("float64")
        else:
            out["turn"] = None
    except Exception:
        out["turn"] = None
    # ★2026-08-12 #136 治本修复：Tushare daily 无 is_st 字段（原硬编码 0 → 增量全丢 ST 标记，
    #   ST 过滤静默失效）。优先用并行拉到的 daily_basic is_st；失败回退继承 bars 该股最近一条。
    try:
        if _db is not None and len(_db):
            _st_map = dict(zip(_db["ts_code"], _db["is_st"].astype(int)))
            out["is_st"] = out["code"].map(lambda c: _st_map.get(c, 0)).astype(int)
        else:
            raise RuntimeError("daily_basic 空")
    except Exception as _e:
        _prev_st = {}
        try:
            import sqlite3 as _s2
            _cs = [_s2.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)]
            try:
                from data.cache import CACHE_DIR as _CD
                from pathlib import Path as _P2
                for _p in sorted(_P2(_CD).glob("bars_incr_*.db"))[-3:]:
                    try:
                        _cs.append(_s2.connect(f"file:{_p}?mode=ro&immutable=1", uri=True, timeout=3))
                    except Exception:
                        pass
            except Exception:
                pass
            for _c in _cs:
                try:
                    for _code, _mxd, _stv in _c.execute(
                            "SELECT code, MAX(date), MAX(is_st) FROM daily_bar WHERE is_st=1 GROUP BY code"):
                        if _stv == 1 and (_code not in _prev_st or _mxd > _prev_st[_code]):
                            _prev_st[_code] = _mxd
                except Exception:
                    pass
                finally:
                    try:
                        _c.close()
                    except Exception:
                        pass
        except Exception:
            pass
        # 继承前值：该股最后 ST 日期 ≤30 天 → 视为当前 ST（增量丢列一般 1-3 天，30 天窗口防御足够；
        #   摘帽 >30 天的不误拦）
        _cut = set()
        if _prev_st:
            from datetime import date as _D8
            _t0 = _D8.today()
            for _code, _mxd in _prev_st.items():
                try:
                    _lag = (_t0 - _D8.fromisoformat(str(_mxd))).days
                except Exception:
                    _lag = 999
                if _lag <= 30:
                    _cut.add(_code)
        out["is_st"] = out["code"].map(lambda c: 1 if c in _cut else 0).astype(int)
        print(f"  ⚠ daily_basic is_st 拉取失败（{str(_e)[:40]}），回退继承 {len(_cut)} 只当前 ST（前值 30 天窗）")
    out["adjust"] = "qfq"
    out["source"] = "tushare"
    return out[["code", "date", "open", "high", "low", "close", "preclose",
                "volume", "amount", "turn", "pct_chg", "is_st", "adjust", "source"]]


def fetch_basic_snapshot(pro, trade_date: str):
    """拉 daily_basic → 估值快照（供 stock_check 估值维度当日化）
    输出：logs/valuation_snapshot_{date}.json（与 stock_check 快照格式兼容）"""
    try:
        b = _call(pro.daily_basic, trade_date=trade_date)
        if b is None or b.empty:
            return False
        keep = [c for c in ("ts_code", "trade_date", "turnover_rate", "volume_ratio",
                            "total_mv", "circ_mv", "pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm")
                if c in b.columns]
        snap = {r["ts_code"]: {k: (None if r[k] != r[k] else r[k]) for k in keep if k != "ts_code"}
                for _, r in b.iterrows()}
        p = BASE / "logs" / f"valuation_snapshot_{trade_date}.json"
        p.write_text(json.dumps({"date": trade_date, "n": len(snap), "items": snap},
                                ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"  daily_basic 快照失败: {str(e)[:80]}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="指定交易日 YYYYMMDD（默认自动探测）")
    ap.add_argument("--basic", action="store_true", help="附带拉 daily_basic 估值快照")
    args = ap.parse_args()

    pro = _pro()
    date = args.date
    if not date:
        date = latest_trade_date(pro)
        if not date:
            print(f"[tushare] 服务器最新交易日盘后数据未出（现在 {datetime.now():%H:%M}）→ 跳过（等 18:30 链自动重试）")
            return 0
    cache = DailyCache()

    # 幂等：bars 已覆盖该日 → 跳过（★#143 双库合并探测——主库写保护后增量库含新数据）
    # ★2026-08-12 协同修复：MAX(date) 只有 183 只占位也判"已有" → 加覆盖率门槛（<4000 只视为残缺需重拉）
    try:
        con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        cur = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()
        has = cur[0] if cur else None
        if has:
            _n = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date=? AND adjust='qfq'",
                             (has,)).fetchone()[0]
            if _n < 4000:
                print(f"[tushare] {has} 仅 {_n} 只（残缺占位）→ 强制重拉 {date}")
                has = None
        con.close()
        # ★2026-08-12 协同修复：残缺（has=None）时不再用 latest_trade_date 覆盖（双库探测会拉回占位日）
        if has is not None:
            _mx = cache.latest_trade_date()
            if _mx and _mx > has:
                has = _mx
        covered = has is not None and str(has) >= f"{date[:4]}-{date[4:6]}-{date[6:]}"
    except Exception:
        covered = False
    if covered:
        print(f"[tushare] {date} 已在库（最新 {has}）→ 跳过")
        return 0

    t0 = time.time()
    df = fetch_day(pro, date)
    if df is None or df.empty:
        print(f"[tushare] {date} 服务器无数据（盘后未出）→ 跳过")
        return 0
    n = cache.put_daily_batch(df, adjust="qfq", source="tushare")
    el = time.time() - t0
    print(f"[tushare] ✅ {date} 全市场 {len(df)} 只已入库（{n} 行，{el:.1f}s）→ bars 最新 {date}")
    if args.basic:
        fetch_basic_snapshot(pro, date)
    return 0


if __name__ == "__main__":
    # ★2026-08-10 计划任务诊断：异常写日志文件（计划任务无控制台，异常被吞只剩返回码 1）
    import traceback as _tb
    _logf = BASE / "logs" / f"tushareinc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    try:
        _rc = main()
        sys.exit(_rc if isinstance(_rc, int) else 0)
    except Exception:
        try:
            _logf.write_text(_tb.format_exc(), encoding="utf-8")
            print(f"异常已写 {_logf.name}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)
