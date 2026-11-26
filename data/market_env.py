# -*- coding: utf-8 -*-
"""data/market_env.py — 市场环境快照生成器（2026-08-11 总指导拆分）

★背景：enhance_factor_report 的 market_env 段每次重算全市场因子（8-10 分钟）→ dev_auto
  页面链 300s 超时被杀。拆为独立生成器：24h 缓存（glob 最新 <24h 直接复用）+ 时间戳输出。
  消费方：enhance_factor_report（只读不重算）/ 因子监控页。
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
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
OUT_DIR = BASE / "report"
_CACHE_HOURS = 24


def _latest_cached() -> dict:
    fs = sorted(glob.glob(str(OUT_DIR / "market_env_*.json")), key=os.path.getmtime)
    if not fs:
        return None
    mt = os.path.getmtime(fs[-1])
    if time.time() - mt < _CACHE_HOURS * 3600:
        try:
            return json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/market_env.py:31 _latest_cached")
    return None


def build(force: bool = False) -> dict:
    if not force:
        cached = _latest_cached()
        if cached:
            return cached
    t0 = time.time()
    try:
        from factors.opportunities import scan as S
        import sqlite3
        # ★#143 immutable 只读（原普通连接在写保护下等锁 20s）
        con = sqlite3.connect(f"file:{S.BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
        d = con.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        con.close()
        # ★双库合并探测（与全系统一致）
        try:
            from data.cache import DailyCache
            _mx = DailyCache().latest_trade_date()
            if _mx:
                d = _mx
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "data/market_env.py:55 build")
        px, vx = S.load_panel(end=d, days=320)
        basic = S.load_basic()
        fin = S.load_fundamentals(d)
        st = S.load_st_codes()
        f = S.compute_factors(px, vx, fin, basic, st)
        n = len(f)
        nh = f["near_high_250"]
        out = {
            "date": d, "n_stocks": n, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "calc_seconds": round(time.time() - t0, 1),
            "near_high_5pct": round(float((nh > -0.05).mean()), 4),
            "deep_drawdown_20pct": round(float((nh < -0.20).mean()), 4),
            "low_pb_pct_30": round(float((f["pb_pct"] <= 0.30).mean()), 4),
            "note": "距52周高<5%=突破候选容量 / >20%回撤=价值修复主体（解释机会结构差异）",
        }
    except Exception as e:
        out = {"error": str(e)[:100], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"market_env_{ts}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"market_env: {out.get('date', '?')} {out.get('n_stocks', '?')} 只 {out.get('calc_seconds', '?')}s")
    return out


if __name__ == "__main__":
    build(force="--force" in sys.argv)
