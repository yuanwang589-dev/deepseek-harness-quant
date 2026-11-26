"""data/fetch_index_daily.py — 基准指数日线（Regime 择时数据源）

Regime 择时基于沪深300 日线（bars.db SH.000300），本脚本拉取并入库。

用法：
  python data/fetch_index_daily.py                    # 增量（最近 15 个交易日）
  python data/fetch_index_daily.py --days 250         # 最近 250 个交易日
  python data/fetch_index_daily.py --start 2018-01-01 --end 2018-12-31   # 指定区间（回补）

★2026-10-16 回补历史须知（实测结论）：
  baostock 单次查询范围过大（如一次拉 8 年）会被服务端拒（10002007 网络接收错误）；
  同一会话内连续多次查询同样会被拒。而「每段 ≤1 年 + 每段独立进程（新会话）」的
  成功率实测 100%（2018/2020/2022/2023 各期单查均成功）。
  故回补历史请按年分段、逐段调用本脚本（各段 upsert 幂等，可重复运行），例如：
    for y in 2018 2019 2020 2021 2022 2023 2024 2025; do
      python -X utf8 data/fetch_index_daily.py --start ${y}-01-01 --end ${y}-12-31
    done
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import DailyCache  # noqa: E402  （需先注入 BASE 到 sys.path）
from data.fetcher_baostock import fetch_daily  # noqa: E402

INDEX_CODES = ["sh.000300"]          # 沪深300（Regime 基准）
CODE_ALIAS = {"sh.000300": "SH.000300"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15, help="拉取最近 N 个交易日（单段）")
    ap.add_argument("--start", type=str, default=None, help="区间起点 YYYY-MM-DD（与 --end 搭配）")
    ap.add_argument("--end", type=str, default=None, help="区间终点 YYYY-MM-DD")
    args = ap.parse_args()

    if args.start and args.end:
        start, end = args.start, args.end
    else:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(args.days * 1.6) + 5)).strftime("%Y-%m-%d")

    cache = DailyCache()
    for code in INDEX_CODES:
        df = fetch_daily(code, start_date=start, end_date=end, adjust="none")
        if df is None or df.empty:
            print(f"{code}: 无数据（{start}~{end}）")
            continue
        n = cache.put_daily(CODE_ALIAS[code], df, adjust="none", source="baostock")
        rng = cache.get_meta(CODE_ALIAS[code], "none") or {}
        print(f"{code}: 写入 {n} 行（{start}~{end}）→ 累计 {rng.get('rows')} 行 "
              f"[{rng.get('start_date')} ~ {rng.get('end_date')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
