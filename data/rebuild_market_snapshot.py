"""data/rebuild_market_snapshot.py — 重建市场快照（外包 factorpool 缺失时的本地近似）

★2026-08-25 背景：外包 data/factorpool/ 整目录丢失 → market_snapshot_ext_*.json 无源，
  timing_system._style_state 风格判定"未知"、门户"市场择时"风格栏空。
  本脚本用本地数据近似重建两维（口径与外包实证阈值配套）：
    divergence_60（大小盘分化，H27）= 近 60 日均成交额前 50（大盘代理）等权 60 日收益
                                       − 全市场等权 60 日收益（>0 大盘强 / <-0.02 小盘强）
    ind_breadth_60（行业轮动宽度，H30）= 申万一级 31 行业指数 60 日正收益占比（0-1，
                                       阈值 <0.2 冰点 / >0.5 活跃）
  ★crowding_pctile（C-10 量化拥挤）不重建：0.8/0.6 过热阈值按外包因子池口径实证标定，
    本地近似数值分布不同会误触发过热预警 → 留空 = timing_system 跳过过热叠加（保守）。

数据源：data/cache/bars.db（qfq，剔 ST/北交所）+ data/cache/sw_industry/*.csv（申万一级 31 行业，离线）。
输出：data/factorpool/output/market_snapshot_ext_{ts}.json（glob 取最新，与外包命名兼容）。
用法（项目根）：python -X utf8 data/rebuild_market_snapshot.py

★2026-10-16 行业宽度离线化：原实现串行拉 ~86 个东财行业板块（每板块一次 HTTP + sleep 0.25s），
  东财接口变慢时 86×20s ≈ 30 分钟 → 撞晨更链 600s 超时（2026-10-15/16 实测），
  择时宽度/拥挤度因此缺位（日报显示“宽度 None”）。现改读本地申万一级 CSV（秒级、零网络）。
"""
import csv
import datetime
import json
import math
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR

BARS = CACHE_DIR / "bars.db"
OUT_DIR = BASE / "data" / "factorpool" / "output"
IND_CACHE = CACHE_DIR / "sw_industry"

# ★申万一级 31 个（2021 版分类）——兜底默认值；实际优先读 config/params.yaml
#   snapshot.sw_l1_industries（架构铁律：会变的清单配置化，代码不写死）。
SW_L1_INDUSTRIES = [
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "国防军工", "计算机", "传媒", "通信", "银行", "非银金融",
    "美容护理", "石油石化", "环保", "煤炭", "机械设备",
]


def _sw_l1_industries() -> list:
    """申万一级行业清单：config/params.yaml snapshot.sw_l1_industries 优先，缺省用内置 31 个
    （缺失/异常不崩溃，回退默认——保持缺省值合理兜底）。"""
    try:
        import yaml
        cfg = yaml.safe_load(
            (BASE / "config" / "params.yaml").read_text(encoding="utf-8")) or {}
        lst = (cfg.get("snapshot") or {}).get("sw_l1_industries")
        if isinstance(lst, list) and len(lst) >= 20:
            return [str(x) for x in lst]
    except Exception as e:
        # 配置读取失败不阻断（回退内置默认），但显式提示、不静默
        print(f"[快照] 读取 params.yaml 行业清单失败，用内置默认: {str(e)[:60]}")
    return list(SW_L1_INDUSTRIES)


def divergence_60():
    """大小盘分化：60 日均额前 50 等权 60 日收益 − 全市场等权 60 日收益。"""
    import sqlite3
    con = sqlite3.connect(f"file:{BARS.as_posix()}?mode=ro", uri=True)
    try:
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 130")]
        if len(days) < 61:
            return None, None
        days = sorted(days)
        df = pd.read_sql(
            "SELECT date, code, close, amount FROM daily_bar "
            "WHERE adjust='qfq' AND date>=? AND is_st=0 AND code NOT LIKE '%.BJ'",
            con, params=(days[0],))
    finally:
        con.close()
    close = df.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    amt = df.pivot_table(index="date", columns="code", values="amount", aggfunc="last")
    ret60 = close.iloc[-1] / close.iloc[-61] - 1
    avg_amt = amt.tail(60).mean()
    valid = ret60.dropna().index.intersection(avg_amt.dropna().index)
    if len(valid) < 100:
        return None, days[-1]
    big50 = avg_amt.loc[valid].nlargest(50).index
    return float(ret60.loc[big50].mean() - ret60.loc[valid].mean()), days[-1]


def _read_closes(p: Path) -> list:
    """读行业缓存 CSV 的收盘价序列（列名『收盘』；utf-8-sig 处理 BOM）。
    用标准库 csv 实现——单列价格提取无需 pandas，也避开其对单列转换的类型推断歧义。"""
    out = []
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    v = float(row.get("收盘") or "")
                except (TypeError, ValueError):
                    continue
                if not math.isnan(v):
                    out.append(v)
    except OSError as e:
        print(f"[快照] 读 {p.name} 失败: {e}")
    return out


def ind_breadth_60():
    """行业宽度：申万一级 31 行业指数 60 日正收益占比（0-1，阈值 <0.2 冰点 / >0.5 活跃）。

    ★2026-10-16 离线优先改造（根治 600s 超时）：直接读本地 data/cache/sw_industry/*.csv
      （申万一级 31 个，秒级、零网络）。原实现串行拉 ~86 个东财行业板块（每板块 HTTP +
      sleep(0.25)），东财变慢时 86×20s ≈ 30 分钟 → 撞晨更链 600s 超时（10-15/16 实测），
      择时宽度/拥挤度因此缺失（日报“宽度 None”）。
      口径同时修正：原实现用东财板块（~86 个）而注释声明申万一级 31 个，二者不符；
      现统一为申万一级 31 个，与 H30 阈值标定口径一致。
    缓存滞后 1 日（由行情管道维护）→ 宽度指标滞后 1 日，可接受。"""
    names = _sw_l1_industries()
    rets = []
    missing = []
    for nm in names:
        p = IND_CACHE / f"{nm}.csv"
        if not p.exists():
            missing.append(nm)
            continue
        closes = _read_closes(p)
        if len(closes) > 61:
            rets.append(closes[-1] / closes[-61] - 1)
        else:
            missing.append(nm)
    if missing:
        print(f"[快照] 本地缺 {len(missing)} 个一级行业缓存: "
              f"{missing[:6]}{'...' if len(missing) > 6 else ''}")
    if not rets:
        return None
    return sum(1 for r in rets if r > 0) / len(rets), len(rets)


def crowding():
    """市场拥挤度（★仅展示，不进 timing_system 过热判定——安全边界保持）：
    主指标 = 个股 20 日收益与等权市场收益的截面平均相关（同涨同跌=拥挤/抱团），
    pctile_252 = 当前值在过去 252 日的分位（口径无关语义），
    n_crowded_stocks = 20 日动量 z>1.5 且 20 日均成交额分位>0.8 的个股数。"""
    import sqlite3
    con = sqlite3.connect(f"file:{BARS.as_posix()}?mode=ro", uri=True)
    try:
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 330")]
        if len(days) < 80:
            return None, None
        days = sorted(days)
        df = pd.read_sql(
            "SELECT date, code, close, amount FROM daily_bar "
            "WHERE adjust='qfq' AND date>=? AND is_st=0 AND code NOT LIKE '%.BJ'",
            con, params=(days[0],))
    finally:
        con.close()
    close = df.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    ret = close.pct_change()
    mkt = ret.mean(axis=1)
    corr = ret.rolling(20).corr(mkt)
    cross = corr.mean(axis=1).dropna()
    if len(cross) < 60:
        return None, days[-1]
    cur = float(cross.iloc[-1])
    hist = cross.tail(252)
    pctile = float((hist < cur).mean())
    # 拥挤个股计数（高动量 + 高成交分位）
    mom20 = close.iloc[-1] / close.iloc[-21] - 1
    z = (mom20 - mom20.mean()) / mom20.std()
    amt = df.pivot_table(index="date", columns="code", values="amount", aggfunc="last").tail(20).mean()
    amt_pct = amt.rank(pct=True)
    n_crowded = int(((z > 1.5) & (amt_pct > 0.8)).sum())
    zone = ("过热（警惕泡沫）" if pctile >= 0.8 else
            "偏热" if pctile >= 0.6 else
            "正常" if pctile >= 0.4 else "冷清（机会区）")
    return {"crowding_mkt": round(cur, 4), "crowding_pctile_252": round(pctile, 4),
            "zone": zone, "n_crowded_stocks": n_crowded,
            "source": "local_approx"}, days[-1]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dv, data_date = divergence_60()
    r = ind_breadth_60()
    ib, n_ind = (r if isinstance(r, tuple) else (r, 0))
    if dv is None and ib is None:
        print("[快照] 两维均计算失败，不写文件")
        return 1
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "date": data_date,
        "divergence_60": round(dv, 4) if dv is not None else None,
        "ind_breadth_60": round(ib, 4) if ib is not None else None,
        "source": "local_approx",
        "note": f"外包快照缺失本地近似（成交额前50/等权 + {n_ind} 行业正收益占比）；crowding_pctile 未重建（阈值系外包口径实证标定）",
    }
    f = OUT_DIR / f"market_snapshot_ext_{ts}.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[快照] date={data_date} divergence_60={out['divergence_60']} "
          f"ind_breadth_60={out['ind_breadth_60']}（{n_ind} 行业）")
    print(f"[快照] 已存 {f}")
    # 拥挤度（★仅因子页展示，独立文件——timing_system 只读快照不含此字段，不触发过热叠加）
    cd, _ = crowding()
    if cd:
        if data_date:
            cd["date"] = data_date
        fc = OUT_DIR / f"crowding_{ts}.json"
        fc.write_text(json.dumps(cd, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[拥挤] mkt={cd['crowding_mkt']} pctile_252={cd['crowding_pctile_252']} "
              f"zone={cd['zone']} 拥挤个股 {cd['n_crowded_stocks']} 只")
        print(f"[拥挤] 已存 {fc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
