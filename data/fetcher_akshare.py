# -*- coding: utf-8 -*-
"""AkShare 覆盖源 fetcher（M1 数据管道 · 覆盖主力/备源）

分工（主文档 4.5②）：
- 日线**备源**（Baostock 失败时兜底）：新浪 `stock_zh_a_daily`（快/稳）
- 财报快照：同花顺 `stock_financial_abstract_ths`（净利润/增速 → 与 finance_calc 反向校验）
- 业绩预告/快报（PEAD 提前量）：东财 `stock_yjyg_em`
- 机构调研（I 因子）：东财 `stock_jgdy_tj_em`

实测结论（2026-08-06）：
1. **东财行情域名 push2his.eastmoney.com 按 TLS 指纹封锁 python 客户端**（requests/http.client 直连
   均 RemoteDisconnected，curl 可通）→ `stock_zh_a_hist` 弃用，日线备源改用新浪/腾讯（CS-21 东财限流实证）
2. 东财 datacenter-web.eastmoney.com（业绩预告/机构调研）python 可通 ✅
3. 本机系统代理（127.0.0.1:7890）对 python 连接不稳定 → 本模块所有请求**绕过系统代理直连**
   （国内数据源本就不需要代理；用 `_no_proxy` 上下文临时剥离代理环境变量）

新浪日线列名：date, open, high, low, close, volume, amount, outstanding_share, turnover
→ 需自算 preclose/pct_chg；is_st 置 0（ST 判定以 Baostock 主源为准）。
"""
import os
import time
from contextlib import contextmanager

import pandas as pd

from .cache import DailyCache

# 统一代码 '600519.SH' → 新浪 'sh600519'；北交所 'bj430047'（baostock 不支持 → 本模块兜底）
_MKT_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

_NUM_COLS = ["open", "high", "low", "close", "preclose",
             "volume", "amount", "turn", "pct_chg"]

# 新浪/同花顺接口偶发风控 → 重试指数退避
_RETRY_SLEEP = (1.5, 3.0)


@contextmanager
def _no_proxy():
    """临时剥离系统代理环境变量（调用国内数据源用直连，规避本机代理不稳）"""
    saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY",
                                            "http_proxy", "https_proxy")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def to_ak_symbol(code):
    """统一代码 '600519.SH' → 新浪格式 'sh600519'（支持 SH/SZ/BJ）"""
    try:
        sym, mkt = str(code).strip().upper().split(".")
    except ValueError:
        raise ValueError(f"代码格式应为 '600519.SH'，收到: {code!r}")
    prefix = _MKT_PREFIX.get(mkt)
    if prefix is None:
        raise ValueError(f"AkShare 不支持市场 {mkt}: {code}")
    return f"{prefix}{sym}"


# ---------------- 日线（备源）----------------

def fetch_daily(code, start="2010-01-01", end=None, adjust="qfq", retries=2):
    """直接网络拉取单只日线（新浪源，不读写缓存）。失败重试 retries 次。

    返回标准列 DataFrame：date,code,open,high,low,close,preclose,volume,
    amount,turn,pct_chg,is_st（已排序，可空 df）。
    注：新浪复权接口缺失 preclose/pct_chg → 自算；is_st 置 0（主源为准）。
    """
    import akshare as ak

    if end is None:
        end = time.strftime("%Y-%m-%d")
    symbol = to_ak_symbol(code)
    start_yyyymmdd = start.replace("-", "")
    end_yyyymmdd = end.replace("-", "")

    last_err = None
    for i in range(retries + 1):
        try:
            with _no_proxy():
                df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_yyyymmdd,
                                         end_date=end_yyyymmdd, adjust=adjust)
            if df is None or df.empty:
                return pd.DataFrame(columns=["date", "code", "open", "high", "low",
                                             "close", "preclose", "volume", "amount",
                                             "turn", "pct_chg", "is_st"])
            df = df.rename(columns={"turnover": "turn"})
            # ★2026-10-15 修复（akshare 备源路径）：新浪返回 date 为 datetime.date，
            #   而 DailyCache.put_daily 的 bar_meta 累计覆盖逻辑会拿库内"字符串日期"
            #   与 df 日期做 min/max 比较 → TypeError: '<' not supported between
            #   'datetime.date' and 'str'。统一转 'YYYY-MM-DD' 字符串（与 baostock 源一致）。
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["code"] = code.upper()
            # 自算 preclose / pct_chg（首行无昨收 → NaN）
            df["preclose"] = df["close"].shift(1)
            df["pct_chg"] = df["close"] / df["preclose"] - 1.0
            df["is_st"] = 0
            for c in _NUM_COLS:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df[["date", "code", "open", "high", "low", "close", "preclose",
                     "volume", "amount", "turn", "pct_chg", "is_st"]]
            df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(_RETRY_SLEEP[min(i, len(_RETRY_SLEEP) - 1)])
    raise ConnectionError(f"AkShare(新浪) 拉取失败（重试 {retries} 次后）: {code} {last_err}")


def ensure_daily(code, start="2010-01-01", end=None, adjust="qfq",
                 cache=None, retries=2):
    """缓存优先的唯一取数入口（与 baostock.ensure_daily 同语义）。

    缓存已覆盖直接读；缺失则从新浪拉取补齐写缓存；返回 [start, end] 过滤后的标准日线。
    """
    cache = cache or DailyCache()
    if end is None:
        end = time.strftime("%Y-%m-%d")

    if cache.covers(code, start, end, adjust):
        return cache.get_daily(code, start=start, end=end, adjust=adjust)

    df = fetch_daily(code, start, end, adjust, retries=retries)
    if df is not None and not df.empty:
        cache.put_daily(code, df, adjust=adjust, source="akshare")

    full = cache.get_daily(code, adjust=adjust)
    if full is None or full.empty:
        return df if df is not None else pd.DataFrame()
    return full[(full["date"] >= start) & (full["date"] <= end)].reset_index(drop=True)


# ---------------- 财报快照（同花顺）----------------

def fetch_financial_abstract(code, indicator="按报告期", retries=2):
    """同花顺财务摘要（报告期维度）：净利润/增速/营收/ROE 等，供 finance_calc 反向校验。

    返回原始 df（列：报告期/净利润/净利润同比增长率/扣非净利润/营业总收入/...）。
    """
    import akshare as ak

    symbol = str(code).strip().split(".")[0]  # 同花顺仅需纯代码
    last_err = None
    for i in range(retries + 1):
        try:
            with _no_proxy():
                return ak.stock_financial_abstract_ths(symbol=symbol, indicator=indicator)
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(_RETRY_SLEEP[min(i, len(_RETRY_SLEEP) - 1)])
    raise ConnectionError(f"AkShare(同花顺财务) 拉取失败: {code} {last_err}")


# ---------------- 业绩预告 / 机构调研（东财 datacenter）----------------

def fetch_yjyg(report_date, retries=2):
    """东财业绩预告（按报告期，如 '20251231'）：全市场预告清单 → PEAD 提前量。

    列：序号/股票代码/股票简称/预测指标/业绩变动/预测数值/业绩变动幅度/
        业绩变动原因/预告类型/上年同期值/公告日期/报告日期
    """
    import akshare as ak

    last_err = None
    for i in range(retries + 1):
        try:
            with _no_proxy():
                return ak.stock_yjyg_em(date=report_date)
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(_RETRY_SLEEP[min(i, len(_RETRY_SLEEP) - 1)])
    raise ConnectionError(f"AkShare(业绩预告) 拉取失败: {report_date} {last_err}")


def fetch_jgdy(date, retries=2):
    """东财机构调研统计（按日期）：机构调研明细 → I 因子（近 60 日调研次数）。

    列：序号/代码/名称/最新价/涨跌幅/接待机构数量/接待方式/接待人员/接待地点/接待日期
    """
    import akshare as ak

    last_err = None
    for i in range(retries + 1):
        try:
            with _no_proxy():
                return ak.stock_jgdy_tj_em(date=date)
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(_RETRY_SLEEP[min(i, len(_RETRY_SLEEP) - 1)])
    raise ConnectionError(f"AkShare(机构调研) 拉取失败: {date} {last_err}")
