"""轮换账户验收报表：净值 vs 沪深300 / 换手 / 交易成本 / 最大回撤

对应实施计划 docs/plans/2026-10-16-paper-rotation.md（Task 7）
用法：python -X utf8 strategy/paper_rotation_report.py
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))   # 支持 `python strategy/paper_rotation_report.py` 独立运行

from strategy.paper_account import PaperAccount  # noqa: E402
from strategy.paper_rotation import load_rotation_cfg  # type: ignore  # noqa: E402

BARS_DB = BASE / "data" / "cache" / "bars.db"
BENCH_CODE = "SH.000300"          # 基准：沪深300（已回补 2018 起 2129 行）


def _benchmark_curve(start: str, end: str) -> list:
    """基准指数区间收盘序列 [(date, close)]；库缺失 → []"""
    if not BARS_DB.exists():
        return []
    con = sqlite3.connect(f"file:{BARS_DB.as_posix()}?mode=ro&immutable=1",
                          uri=True, timeout=5)
    try:
        cur = con.execute(
            "SELECT date, close FROM daily_bar WHERE code=? AND date>=? AND date<=? "
            "ORDER BY date", (BENCH_CODE, start, end))
        return [(r[0], float(r[1])) for r in cur.fetchall() if r[1] is not None]
    finally:
        con.close()


def build_report() -> dict:
    """轮换账户验收指标：净值曲线 / 换手笔数 / 费用 / 最大回撤 / 基准与超额"""
    cfg = load_rotation_cfg()
    cash0 = float(cfg["initial_cash"])
    acc = PaperAccount(name=str(cfg["account_name"]), cash=cash0)
    curve = acc.equity_curve() or []
    orders = acc.orders(limit=10 ** 6) or []
    fees = sum(float(o.get("fee") or 0) for o in orders)

    nav, peak, mdd = [], 0.0, 0.0
    for row in curve:
        total = float(row.get("total") or 0)
        nav.append({"date": row.get("date"), "nav": total,
                    "ret": (total / cash0 - 1) if cash0 else None})
        peak = max(peak, total)
        if peak:
            mdd = max(mdd, 1 - total / peak)

    benchmark = {"code": BENCH_CODE, "start": None, "end": None, "ret": None}
    if curve:
        b = _benchmark_curve(str(curve[0].get("date")), str(curve[-1].get("date")))
        if len(b) >= 2:
            benchmark.update({"start": b[0][0], "end": b[-1][0],
                              "ret": round(b[-1][1] / b[0][1] - 1, 4)})

    total_ret = nav[-1]["ret"] if nav else None
    bench_ret = benchmark.get("ret")
    return {
        "account": acc.name,
        "n_days": len(curve),
        "nav_curve": nav,
        "turnover": len(orders),
        "fees": round(fees, 2),
        "fees_pct_of_cash": round(fees / cash0, 4) if cash0 else None,
        "max_drawdown": round(mdd, 4),
        "total_ret": total_ret,
        "benchmark": benchmark,
        "excess_ret": (round(total_ret - bench_ret, 4)
                       if total_ret is not None and bench_ret is not None else None),
    }


def main() -> int:
    print(f"[paper_rotation_report] {build_report()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
