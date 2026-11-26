# -*- coding: utf-8 -*-
"""回测偏差回归测试（2026-11-26）。

覆盖：
- _load_pool 用 start 而非固定 2020-12（PIT，无 look-ahead）
- 动态选池：不同时点选出不同池；过去值不影响当前（PIT）
- _load_prices 加载起点随 start 变化（含 warmup，不固定 2020-06-01）
- 成本：印花税单边 + 从 config 读
- bt_engine 首日收益不被丢弃
- DailyCache.list_codes 合并双库
- run_backtest 动态池端到端不崩溃

不依赖 pytest（环境未安装）：标准库 tempfile + __main__ 入口。
"""
import sqlite3
import sys
import tempfile
import traceback
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backtest import bt_cost, bt_runner, bt_report
from backtest.bt_engine import BtEngine
from data import cache as cache_mod


def test_load_pool_uses_start_not_2020():
    """选池只用 start 之前可知的数据：2020-12 高成交额的股票不应被 2024 回测选中。"""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "bars.db"
        con = sqlite3.connect(str(db))
        con.execute("""CREATE TABLE daily_bar (
            code TEXT, date TEXT, adjust TEXT, open REAL, high REAL, low REAL, close REAL,
            preclose REAL, volume REAL, amount REAL, turn REAL, pct_chg REAL, is_st INTEGER,
            source TEXT)""")
        # look-ahead 陷阱：2020-12 成交额极高，但 start 前 30 天窗口外
        con.execute("INSERT INTO daily_bar (code,date,adjust,amount,source) VALUES (?,?,?,?,?)",
                    ("TRAP.SZ", "2020-12-15", "qfq", 999999.0, "tushare"))
        # 应选：start 前 30 天窗口内高成交额，且 start 之后有数据
        con.execute("INSERT INTO daily_bar (code,date,adjust,amount,source) VALUES (?,?,?,?,?)",
                    ("PICK.SZ", "2023-12-15", "qfq", 500000.0, "tushare"))
        con.execute("INSERT INTO daily_bar (code,date,adjust,amount,source) VALUES (?,?,?,?,?)",
                    ("PICK.SZ", "2024-01-15", "qfq", 100.0, "tushare"))
        con.execute("INSERT INTO daily_bar (code,date,adjust,amount,source) VALUES (?,?,?,?,?)",
                    ("LOW.SZ", "2023-12-15", "qfq", 100.0, "tushare"))
        con.execute("INSERT INTO daily_bar (code,date,adjust,amount,source) VALUES (?,?,?,?,?)",
                    ("LOW.SZ", "2024-01-15", "qfq", 100.0, "tushare"))
        con.commit()
        con.close()

        old = bt_runner.CACHE
        try:
            bt_runner.CACHE = str(td)
            got = bt_runner._load_pool(1, "2024-01-01", min_rows=1)
        finally:
            bt_runner.CACHE = old
        assert "PICK.SZ" in got, got
        assert "TRAP.SZ" not in got, got   # 不再用 2020-12 的成交额选池


def test_dynamic_pool_differs_by_date():
    """每期按过去 20 日成交额重选池；改 pos 之后的值不影响当前（PIT）。"""
    dates = pd.date_range("2024-01-01", periods=40, freq="B")
    amount = pd.DataFrame(1.0, index=dates, columns=["A", "B", "C"])
    amount.iloc[:10, amount.columns.get_loc("A")] = 100.0   # 早期 A 高
    amount.iloc[20:30, amount.columns.get_loc("B")] = 100.0  # 后期 B 高

    early = bt_runner._active_codes(amount, 9, ["A", "B", "C"], 1)
    late = bt_runner._active_codes(amount, 25, ["A", "B", "C"], 1)
    assert early == ["A"], early
    assert late == ["B"], late

    # PIT：把 pos 之后 C 抬到极高，不应改变 pos 时刻的选池
    amount.iloc[26:, amount.columns.get_loc("C")] = 1e9
    assert bt_runner._active_codes(amount, 25, ["A", "B", "C"], 1) == ["B"]


def test_load_prices_start_varies():
    """加载起点 = start - 400 天，而非固定 2020-06-01。"""
    calls = {}

    class _StubCache:
        def get_daily_batch(self, codes, start=None, end=None, adjust=None, fields=None):
            calls["start"] = start
            calls["fields"] = fields
            return {}

    old_cache, old_q = bt_runner.DailyCache, bt_runner._q
    try:
        bt_runner.DailyCache = _StubCache
        bt_runner._q = lambda *a, **k: []   # calendar 返回空
        bt_runner._load_prices(["A"], "2023-05-01", "2024-12-31")
    finally:
        bt_runner.DailyCache, bt_runner._q = old_cache, old_q

    expected = (pd.Timestamp("2023-05-01") - timedelta(days=400)).strftime("%Y-%m-%d")
    assert calls["start"] == expected, calls
    assert calls["start"] != "2020-06-01"
    assert "amount" in calls["fields"]


def test_round_trip_cost_single_stamp():
    """印花税仅卖出侧：sell - buy == stamp；默认 round ≈ 0.00302；全 0 → 0。"""
    cfg = {"commission_bps": 2.6, "stamp_tax": 0.0005, "slippage": 0.001}
    buy = bt_cost.one_way_cost(cfg, "buy")
    sell = bt_cost.one_way_cost(cfg, "sell")
    assert abs((sell - buy) - cfg["stamp_tax"]) < 1e-12
    assert abs(bt_cost.round_trip_cost(cfg) - 0.00302) < 1e-9
    assert bt_cost.round_trip_cost(
        {"commission_bps": 0, "stamp_tax": 0, "slippage": 0}) == 0.0


def test_bt_engine_first_day_not_dropped():
    """T+1 首日收益 = close[pos+1]/close[pos]-1，不再被 fillna(0) 吞掉。"""
    closes = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=pd.Index(["d0", "d1", "d2"]))
    seg = BtEngine._segment_return(closes, ["A"], 0, 2)
    assert abs(float(seg.iloc[0]["A"]) - 0.10) < 1e-9, seg
    assert abs(float(seg.iloc[1]["A"]) - 0.10) < 1e-9, seg


def test_list_codes_merges_dual_db():
    """list_codes 合并主库+增量库、去重、排除 sh./sz. 残留格式。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        main, inc = root / "bars.db", root / "bars_incr.db"
        old = (cache_mod.DEFAULT_DB, cache_mod.INC_DB)
        try:
            cache_mod.DEFAULT_DB, cache_mod.INC_DB = main, inc
            c = cache_mod.DailyCache()
            for db, code in ((main, "A.SZ"), (inc, "B.SZ"), (inc, "sh.600000")):
                con = sqlite3.connect(str(db))
                con.execute(
                    "INSERT INTO daily_bar (code,date,adjust,open,high,low,close,source) "
                    "VALUES (?,?,?,?,?,?,?,?)", (code, "2024-01-01", "qfq", 1, 1, 1, 1, "test"))
                con.commit()
                con.close()
            codes = c.list_codes()
            assert "A.SZ" in codes and "B.SZ" in codes, codes
            assert "sh.600000" not in codes, codes
        finally:
            cache_mod.DEFAULT_DB, cache_mod.INC_DB = old


def test_run_backtest_dynamic_pool_e2e():
    """小样本端到端：动态池接线后 run_backtest 能产出 nav，且成本口径已切换。"""
    dates = pd.date_range("2024-01-01", periods=120, freq="B")
    codes = ["S1", "S2", "S3", "S4", "S5"]
    rng = np.random.default_rng(7)
    n = len(dates)
    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0.0005, 0.02, (n, 5)), axis=0),
        index=dates, columns=codes)
    opens = close.shift(1).fillna(close.iloc[0]) * 0.999
    highs = pd.DataFrame(np.maximum(close.values, opens.values) * 1.002,
                         index=dates, columns=codes)
    amount = pd.DataFrame(rng.uniform(1e7, 1e9, (n, 5)), index=dates, columns=codes)
    panel = {"close": close, "open": opens, "high": highs, "amount": amount}

    old_panel = bt_runner._get_panel
    old_arch, old_latest = bt_report.archive, bt_report.save_latest
    try:
        bt_runner._get_panel = lambda *a, **k: panel
        bt_report.archive = lambda *a, **k: None
        bt_report.save_latest = lambda *a, **k: None
        r = bt_runner.run_backtest("tech3", topn=2, stocks=3,
                                   start="2024-03-01", end="2024-06-30")
    finally:
        bt_runner._get_panel = old_panel
        bt_report.archive, bt_report.save_latest = old_arch, old_latest

    assert "metrics" in r, r
    assert len(r.get("nav", [])) > 0, r
    assert len(r["nav"]) == len(r["dates"])


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
