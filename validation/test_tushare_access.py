# -*- coding: utf-8 -*-
"""Tushare 主服务器连通性 & 权限边界测试（v2 · 2026-08-07 升级后）

验证：
1. 主服务器（api.tushare.pro）连通性 & 新 token 有效性
2. 已解锁能力盘点：财务三表/VIP、fina_indicator、涨跌停、资金流、筹码、技术因子、ST 名单
3. 未开通接口记录（rt_quote/stk_mins/热榜 → 需更高权限，业务不要依赖）
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

API_URL = "https://api.tushare.pro"


def check(name, fn):
    # ★2026-11-10：原名 test 会被 pytest 当用例收集并因签名报 fixture 'name' not found
    try:
        df = fn()
        n = 0 if df is None else len(df)
        print(f"  [OK]   {name}: 返回 {n} 行")
        return df
    except Exception as e:
        msg = str(e)
        if "purchased" in msg or "权限" in msg or "积分" in msg:
            print(f"  [未开通] {name}: {msg[:100]}")
        else:
            print(f"  [失败] {name}: {msg[:100]}")
        return None


def main():
    print("=" * 60)
    print("Tushare 主服务器可达性测试（15000 积分版）")
    print("=" * 60)
    # ★2026-11-02：官方 SDK 会拼 {url}/{api_name} → 对 https://api.tushare.pro 是 404，
    #   必须先装根路径补丁（同 data/fetcher_tushare._install_root_path_api），否则全部返回空。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from data.fetcher_tushare import _install_root_path_api

    _install_root_path_api()
    import tushare as ts
    pro = ts.pro_api(os.environ.get("LW_TUSHARE_TOKEN") or
                     __import__("yaml").safe_load(
                         open(r"config/params.yaml",
                              encoding="utf-8"))["data"]["tushare_token"])
    pro._DataApi__http_url = API_URL

    print("\n[1] 基础连通")
    check("trade_cal 交易日历", lambda: pro.trade_cal(exchange="SSE", start_date="20260801", end_date="20260810"))
    check("stock_basic 股票列表", lambda: pro.stock_basic(exchange="", list_status="L",
                                                        fields="ts_code,symbol,name,industry,list_date"))

    print("\n[2] 行情接口（按日全市场批量）")
    check("daily 全市场日线", lambda: pro.daily(trade_date="20260806"))
    check("daily_basic 全市场指标", lambda: pro.daily_basic(trade_date="20260806"))
    check("index_daily 指数日线", lambda: pro.index_daily(ts_code="000300.SH", start_date="20260801", end_date="20260806"))
    check("adj_factor 复权因子", lambda: pro.adj_factor(ts_code="600519.SH", start_date="20260701", end_date="20260806"))

    print("\n[3] 财报接口（原需 2000 积分）")
    check("income 利润表", lambda: pro.income(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("balancesheet 资产负债表", lambda: pro.balancesheet(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("cashflow 现金流量表", lambda: pro.cashflow(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("income_vip 利润表VIP", lambda: pro.income_vip(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("fina_indicator 财务指标", lambda: pro.fina_indicator(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("forecast 业绩预告", lambda: pro.forecast(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("express 业绩快报", lambda: pro.express(ts_code="600519.SH", start_date="20250101", end_date="20251231"))
    check("dividend 分红送股", lambda: pro.dividend(ts_code="600519.SH"))

    print("\n[4] 特色数据（新解锁）")
    check("moneyflow 资金流向", lambda: pro.moneyflow(trade_date="20260806"))
    check("limit_list_d 涨跌停", lambda: pro.limit_list_d(trade_date="20260806"))
    check("cyq_perf 筹码及胜率", lambda: pro.cyq_perf(ts_code="600519.SH", start_date="20260801", end_date="20260806"))
    check("stk_factor 技术因子", lambda: pro.stk_factor(ts_code="600519.SH", start_date="20260801", end_date="20260806"))
    check("top_list 龙虎榜", lambda: pro.top_list(trade_date="20260806"))
    check("stock_st ST名单", lambda: pro.stock_st(start_date="20260801", end_date="20260806"))
    check("stk_holdernumber 股东人数", lambda: pro.stk_holdernumber(ts_code="600519.SH", start_date="20250101", end_date="20260630"))

    print("\n[5] 未开通接口（记录边界，勿依赖）")
    check("rt_quote 实时快照", lambda: pro.rt_quote(ts_code="000001.SZ"))
    check("stk_mins 分钟线", lambda: pro.stk_mins(ts_code="000001.SZ", freq="1min",
                                                 start_date="20260806 09:30:00", end_date="20260806 15:00:00"))

    print("\n" + "=" * 60)
    print("完成。上方 [OK] = 主服务器可用；[未开通] = 需找客服升级。")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
