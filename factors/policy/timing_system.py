# -*- coding: utf-8 -*-
"""factors/policy/timing_system.py — 新择时系统（2026-08-11 百轮#3 实施）

★用户指示（08-11 13:00 + 20:48）：废弃旧"满仓/减仓/离场"仓位语言，
新择时 = **单纯判断当前市场是否适合买入**（不是仓位指导）。
★备忘录：《AI协作/新择时系统设计备忘录_20260811.md》

三档输出：
  🟢 适合买入  /  🟡 谨慎买入  /  🔴 不适合买入（+ 一句话原因）

融合公式（备忘录初稿，待政策因子全量接入后校准）：
  买入适宜度 = 政策 40% + 宏观 25% + 情绪 20% + 宽度 15%
  适宜度 ≥60 → 🟢；40-60 → 🟡；<40 → 🔴

数据源：
  政策：EPU（factors/policy/epu_factors.py，月度；z12 越低=政策越友好 + 3月趋势）
  宏观：daily_signal.json 的 regime_label（过渡期代理，宏观数据库接入后替换）
  情绪：bars.db 当日涨停密度（涨停家数/全市场）+ 成交温度
  宽度：bars.db 当日上涨家数占比

输出：output/timing_system_{ts}.json（时间戳文件名，写保护免疫）+ 固定名兼容
UI：Pitch 页顶部一行式状态条（live_patch /api/live/timing 消费）
"""
import glob
import json
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

BARS_DB = str(BASE / "data" / "cache" / "bars.db")   # ★2026-10-15 P1-A 绝对路径（原相对依赖 cwd）
OUT_DIR = BASE / "output"


def _f(v):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return float(v)
    except Exception:
        return None


def _bars_conn():
    return sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)


# ★2026-08-12 #135：双库合并探测（#102 教训复发点修复）
# 主库 bars.db 写保护 → 08-12 后增量进 bars_incr_*.db；单库 MAX(date) 会虚标旧日。
# timing_system 的 3 处（情绪/宽度兜底 + evaluate date 输出）统一改合并探测。
_merged_max_cache: dict = {"t": 0.0, "v": ""}


def _merged_max_date() -> str:
    """主库 + 最近 3 增量库合并探测最新交易日（60s 缓存，低频调用足够）"""
    import time as _t
    now = _t.time()
    if now - _merged_max_cache["t"] < 60 and _merged_max_cache["v"]:
        return _merged_max_cache["v"]
    mx = None
    conns = []
    try:
        conns.append(_bars_conn())
    except Exception:
        pass
    try:
        from pathlib import Path as _P
        for _p in sorted(_P(BARS_DB).parent.glob("bars_incr_*.db"))[-3:]:
            try:
                conns.append(sqlite3.connect(f"file:{_p}?mode=ro&immutable=1", uri=True, timeout=3))
            except Exception:
                pass
    except Exception:
        pass
    for _c in conns:
        try:
            r = _c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
            if r and (mx is None or r > mx):
                mx = r
        except Exception:
            pass
        finally:
            try:
                _c.close()
            except Exception:
                pass
    if mx:
        _merged_max_cache.update(t=now, v=mx)
    return mx or ""


def _snapshot_fresh() -> dict:
    """外包 market_snapshot_ext 内容日期新鲜度检查（#135 新增，与 #123 内容滞后感知同款）
    返回 {snap: 快照 dict 或 None, fresh: bool, lag_days: int, file: str}
    滞后 > 3 个交易日 → 视为过期（H27 降档/风格判定跳过，note 标注）"""
    import glob as _g
    from pathlib import Path as _P
    for _sd in ("data/factorpool/output",
                "data/factorpool/output/daily_scores"):
        _fs = sorted(_g.glob(_sd + "/market_snapshot_ext*.json"), key=lambda p: _P(p).stat().st_mtime)
        if not _fs:
            continue
        try:
            d = json.loads(_P(_fs[-1]).read_text(encoding="utf-8"))
            sdate = str(d.get("date") or "")
            mx = _merged_max_date()
            lag = 0
            if sdate and mx:
                try:
                    from datetime import date as _D
                    _d0, _d1 = _D.fromisoformat(sdate), _D.fromisoformat(mx)
                    # 简单日历日差（交易日近似，滞后 3 日历日 ≈ 3 交易日足够防御）
                    lag = ( _d1 - _d0).days
                except Exception:
                    lag = 0
            fresh = lag <= 3
            return {"snap": d, "fresh": fresh, "lag_days": lag, "file": _P(_fs[-1]).name}
        except Exception:
            continue
    return {"snap": None, "fresh": False, "lag_days": 999, "file": ""}


def policy_score() -> dict:
    """政策分 0-100：★2026-08-12 百轮#91 升级——优先读四因子政策综合评分
    （EPU+社融同比+利差，回测师宏观政策传导研究交付；policy_hook 12h 缓存）
    映射 score→0-100：进攻区 +0.5→92 / 中性 0→70 / 防守 -0.65→41
    失败/缺失 → 回退 EPU 单因子（z12 + 3 月趋势，原逻辑）"""
    try:
        from data.policy_hook import load as _ph_load
        _d = _ph_load()
        if _d and _d.get("score") is not None:
            _sc4 = float(_d["score"])
            _sc = round(max(25.0, min(98.0, 70 + _sc4 * 45)), 1)
            _bk = _d.get("bucket", "中性区")
            _feel = "政策暖" if _sc4 > 0.5 else ("中性" if _sc4 >= -0.5 else "政策冷")
            # ★#351 精简 note：结论式（公式细节 EPU+社融+利差 → 说明页），reason 用 short
            _note = f"{_feel}（{_bk} {_d.get('month','')}）"
            return {"score": _sc, "note": _note, "short": _feel}
    except Exception:
        pass
    try:
        from factors.policy.epu_factors import load_epu_monthly
        df = load_epu_monthly()
        if df is None or df.empty:
            return {"score": 50, "note": "EPU 无数据（中性）", "short": "中性"}
        s = df["epu"].dropna()
        if len(s) < 12:
            return {"score": 50, "note": f"EPU 样本不足（{len(s)}月）", "short": "中性"}
        cur = float(s.iloc[-1])
        z12 = (cur - s.iloc[-12:].mean()) / (s.iloc[-12:].std() + 1e-9)
        # 3 月趋势：cur 相对 3 月前变化（下降=改善）
        prev3 = float(s.iloc[-4]) if len(s) >= 4 else cur
        trend = -1 if cur < prev3 * 0.97 else (1 if cur > prev3 * 1.03 else 0)
        # z12 越低分越高：z=-1.5 → 90分，z=0 → 65，z=+1.5 → 30
        base = max(30, min(90, 65 - z12 * 18))
        bonus = 8 if trend == -1 else (-5 if trend == 1 else 0)
        sc = round(max(10, min(100, base + bonus)), 1)
        _trend_txt = "改善" if trend == -1 else ("收紧" if trend == 1 else "持平")
        note = f"EPU={cur:.0f}（z12={z12:+.2f}，{_trend_txt}）[EPU 兜底]"
        return {"score": sc, "note": note, "short": "政策" + _trend_txt if trend != 0 else "政策中性"}
    except Exception as e:
        return {"score": 50, "note": f"政策分异常（{str(e)[:40]}，中性）", "short": "中性"}


def macro_score() -> dict:
    """宏观分 0-100：★2026-08-12 百轮#105 升级——regime 代理为主 + 真实宏观库修正
    （macro.db：社融增量趋势 + 国债利率趋势——与政策维度用"同比/利差"视角互补）
    - 社融近3月均 vs 近6月均：恶化 -6 / 改善 +6（核心领先指标）
    - y10 近5日 vs 前5日：下行(宽松) +4 / 上行(收紧) -4
    失败回退 regime 代理（原逻辑）。★H27 等权口径修正保留（#66）。"""
    try:
        fs = sorted(glob.glob(str(OUT_DIR / "daily_signal_*.json")), key=os.path.getmtime)
        if not fs:
            return {"score": 50, "note": "无择时信号（中性）"}
        d = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
        rl = d.get("regime_label", "")
        mapping = {"strong_uptrend": 90, "uptrend": 75, "choppy": 55,
                   "downtrend": 30, "strong_downtrend": 15}
        sc = mapping.get(rl, 50)
        _rl_cn = {"strong_uptrend": "强势上行", "uptrend": "上行", "choppy": "震荡",
                  "downtrend": "下行", "strong_downtrend": "弱势下行"}.get(rl, "中性")
        note = f"Regime={rl}"
        # ★#105 真实宏观库修正（macro.db immutable）
        try:
            import sqlite3 as _sq
            _con = _sq.connect("file:data/cache/macro.db?mode=ro&immutable=1",
                               uri=True, timeout=3)
            # 社融趋势（近3月均 vs 近6月均；1 月季节性冲高用 6 月整体基准平滑）
            _sf = [r[0] for r in _con.execute(
                "SELECT sf_increment FROM social_finance ORDER BY month DESC LIMIT 6").fetchall()]
            if len(_sf) >= 6:
                _r3 = sum(_sf[:3]) / 3
                _a6 = sum(_sf) / 6
                if _r3 < _a6 * 0.95:
                    sc -= 6
                    note += "｜社融恶化"
                elif _r3 > _a6 * 1.05:
                    sc += 6
                    note += "｜社融改善"
                else:
                    note += "｜社融持平"
            # 利率趋势（y10 近5日 vs 前5日；下行=宽松）
            _y = [r[0] for r in _con.execute(
                "SELECT y10 FROM bond_yield ORDER BY date DESC LIMIT 20").fetchall()]
            _con.close()
            if len(_y) >= 20:
                _yr = sum(_y[:5]) / 5
                _yp = sum(_y[15:20]) / 5
                if _yr < _yp * 0.995:
                    sc += 4
                    note += "｜利率宽松"
                elif _yr > _yp * 1.005:
                    sc -= 4
                    note += "｜利率收紧"
                else:
                    note += "｜利率持平"
        except Exception:
            note += "（宏观库不可用，纯 regime 代理）"
        # ★H27 等权口径修正（★#135 加快照新鲜度防护：内容滞后>3 交易日跳过并标注）
        try:
            _sf = _snapshot_fresh()
            sp = _sf.get("snap")
            if sp:
                if not _sf.get("fresh"):
                    note += "｜快照滞后（H27 跳过）"
                else:
                    dv = _f(sp.get("divergence_60"))
                    m60 = _f(sp.get("mkt_mom60"))
                    if dv is not None and abs(dv) >= 0.095 and (m60 is None or m60 < 0):
                        sc = max(15, sc - 15)   # 分化极端(≥9.5pp)+等权弱 → 降 15 分
                        note += "｜等权分化降档"
        except Exception:
            pass
        sc = max(5, min(100, sc))
        return {"score": sc, "note": note, "short": _rl_cn}
    except Exception as e:
        return {"score": 50, "note": f"宏观分异常（{str(e)[:40]}）"}


def sentiment_score() -> dict:
    """情绪分 0-100：★v2（2026-08-11 百轮#66）优先消费外包市场情绪温度计
    （market_emotion_temp_v2.json：H5/H23/H30 五指标加权，**越低越恐慌=买入区**）。
    方向修正：原"涨停密度冰点=25分"与实证相反（H30/H31：恐慌底部=加仓区）。
    zone 映射：恐慌底部90 / 偏冷70 / 中性50 / 偏热30 / 过热15。外包缺失 → bars 兜底。"""
    # ★外包温度计优先（五指标：宽度/行业轮动/涨停情绪/动量/分化修正）
    try:
        for _sd in ("data/factorpool/output",
                    "data/factorpool/output/daily_scores"):
            _fs = sorted(glob.glob(_sd + "/market_emotion_temp*.json"), key=os.path.getmtime)
            if _fs:
                d = json.loads(Path(_fs[-1]).read_text(encoding="utf-8"))
                temp = _f(d.get("temp"))
                zone = str(d.get("zone", ""))
                if temp is not None:
                    zm = {"恐慌底部": 90, "偏冷": 70, "中性": 50, "偏热": 30, "过热": 15}
                    sc = 50
                    for k, v in zm.items():
                        if k in zone:
                            sc = v
                            break
                    # 无 zone 匹配时线性反向（越低越恐慌=高分）
                    if sc == 50 and "恐慌" not in zone and "偏冷" not in zone and "偏热" not in zone and "过热" not in zone:
                        sc = round(max(15, min(95, 100 - temp * 0.8)), 1)
                    # ★#351 精简 note：结论式（"越低越恐慌=买入区"实证 → 说明页）
                    note = f"市场温度 {temp:.0f}/100（{zone}）"
                    _short = (zone.replace("🔥", "").replace("🟢", "").replace("底部", "")
                              .replace("（加仓区）", "").replace("买入区", "").strip()) or "中性"
                    return {"score": sc, "note": note, "short": _short}
                break
    except Exception:
        pass
    # bars 兜底（外包温度计缺失）：保持原涨停密度逻辑，note 标注兜底
    try:
        con = _bars_conn()
        mx = _merged_max_date()   # ★#135 双库合并探测（增量库可能含更新日）
        rows = con.execute(
            "SELECT close, preclose, volume, amount FROM daily_bar WHERE date=?", (mx,)).fetchall()
        con.close()
        if not rows:
            return {"score": 50, "note": "bars 无数据（中性）"}
        n = len(rows)
        lu = sum(1 for r in rows if r[0] and r[1] and r[0] / r[1] >= 1.097)
        lu_pct = lu / n * 100
        if lu_pct >= 4:
            sc = max(20, 75 - (lu_pct - 4) * 15)
            note = f"涨停 {lu} 只（{lu_pct:.1f}%）过热降温"
        elif lu_pct >= 1.5:
            sc = min(85, 55 + lu_pct * 8)
            note = f"涨停 {lu} 只（{lu_pct:.1f}%）情绪活跃"
        elif lu_pct >= 0.5:
            sc = 45
            note = f"涨停 {lu} 只（{lu_pct:.1f}%）情绪平淡"
        else:
            sc = 25
            note = f"涨停 {lu} 只（{lu_pct:.1f}%）情绪冰点"
        vols = [r[2] for r in rows if r[2]]
        if vols:
            import statistics
            vm = statistics.median(vols)
            vs = statistics.median_high([v for v in vols])
            temp = "量能适中" if 0.8 < vs / (vm + 1e-9) < 1.3 else ("量能放大" if vs / (vm + 1e-9) >= 1.3 else "量能萎缩")
            note += f"｜{temp}"
        note += "（bars 兜底）"
        return {"score": round(sc, 1), "note": note, "short": "涨停情绪"}
    except Exception as e:
        return {"score": 50, "note": f"情绪分异常（{str(e)[:40]}）"}


def breadth_score() -> dict:
    """宽度分 0-100：★v2（2026-08-11 百轮#66）消费外包 market_snapshot_ext_v2：
    width5（H5b 反向：极弱<0.35=恐慌加仓 / 极强>0.7=亢奋减仓）+
    ind_breadth_60（H30 阈值型：<0.2=🔥恐慌底部加仓 1 档 / >0.8=高位钝化）。
    行业宽度优先（H30 比个股宽度强 2 倍，权重 0.6）。外包缺失 → bars 兜底。"""
    try:
        for _sd in ("data/factorpool/output",
                    "data/factorpool/output/daily_scores"):
            _fs = sorted(glob.glob(_sd + "/market_snapshot_ext*.json"), key=os.path.getmtime)
            if _fs:
                d = json.loads(Path(_fs[-1]).read_text(encoding="utf-8"))
                w5 = _f(d.get("width5"))
                ib = _f(d.get("ind_breadth_60"))
                note_parts = []
                # width5 反向（H5b：宽度极弱=未来 +11.9%）——note 精简结论式（实证 → 说明页）
                if w5 is not None:
                    if w5 < 0.35:
                        ws, wn = 85, "个股宽度 恐慌区"
                    elif w5 < 0.5:
                        ws, wn = 65, "个股宽度 偏冷"
                    elif w5 < 0.65:
                        ws, wn = 45, "个股宽度 中性"
                    elif w5 < 0.7:
                        ws, wn = 30, "个股宽度 偏热"
                    else:
                        ws, wn = 20, "个股宽度 亢奋区"
                    note_parts.append(wn)
                else:
                    ws = 50
                # ind_breadth_60 阈值（H30：<0.2=恐慌底部）
                if ib is not None:
                    if ib < 0.2:
                        isc, inn = 90, "行业宽度 恐慌底部"
                    elif ib > 0.8:
                        isc, inn = 25, "行业宽度 高位钝化"
                    else:
                        isc, inn = 50, "行业宽度 中性"
                    note_parts.append(inn)
                else:
                    isc = 50
                sc = round(0.4 * ws + 0.6 * isc, 1)   # 行业宽度优先（H30 信号强 2 倍）
                note = "｜".join(note_parts)
                _short = "恐慌" if sc >= 80 else ("偏冷" if sc >= 60 else ("中性" if sc >= 40 else ("偏热" if sc >= 30 else "亢奋")))
                return {"score": sc, "note": note, "short": _short}
    except Exception:
        pass
    # bars 兜底（外包快照缺失）：当日上涨家数占比
    try:
        con = _bars_conn()
        mx = _merged_max_date()   # ★#135 双库合并探测
        rows = con.execute(
            "SELECT close, preclose FROM daily_bar WHERE date=?", (mx,)).fetchall()
        con.close()
        if not rows:
            return {"score": 50, "note": "bars 无数据（中性）"}
        n = len(rows)
        up = sum(1 for r in rows if r[0] and r[1] and r[0] > r[1])
        pct = up / n
        sc = max(10, min(95, pct * 100 * 1.1))   # 50% 宽度 → 55 分
        state = "普涨" if pct >= 0.6 else ("涨多跌少" if pct >= 0.5 else ("跌多涨少" if pct >= 0.4 else "普跌"))
        return {"score": round(sc, 1), "note": f"上涨 {up}/{n}（{pct:.0%}）{state}（bars 兜底）"}
    except Exception as e:
        return {"score": 50, "note": f"宽度分异常（{str(e)[:40]}）"}


def _style_state() -> dict:
    """★2026-08-12 百轮后#127：风格状态（外包 C8 建议落地——风格门控输入）。
    综合外包 market_snapshot_ext 两维：ind_breadth_60（行业轮动强度，H30 实证）+
    divergence_60（大小盘分化，H27 实证）→ 三态风格：
      小盘反转行情（ind_breadth 冰点 <0.2 + 小盘相对强）→ 反转/短线类适配 +
      核心资产行情（ind_breadth 高位 >0.5 + 大盘相对强）→ 反转/短线类适配 --（C8：2020-2022
        核心资产期低换手/反转因子系统性失效 -1.69~-3.72pp）
      均衡 → 中性
    外包缺失 → 未知（不叠加适配，回归纯环境档）。
    """
    try:
        _sf = _snapshot_fresh()   # ★#135 快照内容日期新鲜度检查（复用，统一读取点）
        d = _sf.get("snap")
        if not d:
            return {"style": "未知", "note": "外包快照缺失", "divergence_60": None, "ind_breadth_60": None,
                    "fresh": False}
        if not _sf.get("fresh"):
            return {"style": "未知", "note": f"外包快照内容滞后 {_sf.get('lag_days')} 天（{d.get('date')}），风格暂不判定",
                    "divergence_60": _f(d.get("divergence_60")), "ind_breadth_60": _f(d.get("ind_breadth_60")),
                    "fresh": False, "snap_file": _sf.get("file")}
        ib = _f(d.get("ind_breadth_60"))
        dv = _f(d.get("divergence_60"))
        # ★2026-08-12 C-10 量化拥挤度（外包 crowding_signal → market_snapshot_ext 字段）：
        #   crowding_pctile>0.8=市场过热（C-10 实证：拥挤度 Q5 未来20日仅 +0.19% vs Q1 +2.45%）
        cp = _f(d.get("crowding_pctile"))
        if ib is None and dv is None and cp is None:
            return {"style": "未知", "note": "外包快照无风格字段", "divergence_60": dv, "ind_breadth_60": ib}
        # 判小盘/大盘相对强弱：divergence_60>0 = 大盘强（前50>等权），<0 = 小盘强
        small_strong = (dv is not None and dv < -0.02)
        big_strong = (dv is not None and dv > 0.02)
        # C8 风格判定（基于 H30 行业宽度 + H27 分化）
        if ib is not None and ib < 0.2 and not big_strong:
            style = "小盘反转行情"
            note = f"行业宽度 {ib:.1%} 冰点（H30 恐慌底部 +23.7%）" + (" + 小盘相对强" if small_strong else "")
        elif ib is not None and ib >= 0.5 and big_strong:
            style = "核心资产行情"
            note = f"行业宽度 {ib:.1%} 活跃 + 大盘相对强（C8：2020-2022 反转因子失效期特征）"
        else:
            style = "均衡"
            note = f"行业宽度 {ib:.1%} 分化 {dv:+.0%}" if ib is not None and dv is not None else "风格中性"
        # ★C-10 过热叠加：拥挤度分位 >0.8 → 过热预警（独立于风格的市场情绪温度）
        if cp is not None and cp > 0.8:
            if style != "核心资产行情":
                style = "过热预警"
            note = (note + f"；拥挤度分位 {cp:.0%} 过热（C-10：Q5 未来20日仅 +0.19%）")
        elif cp is not None and cp > 0.6:
            note = (note + f"；拥挤度分位 {cp:.0%} 偏高")
        return {"style": style, "note": note,
                "divergence_60": dv, "ind_breadth_60": ib, "crowding_pctile": cp,
                "fresh": True, "snap_file": _sf.get("file"),
                "evidence": "H30 行业轮动 + H27 大小盘分化 + C-10 量化拥挤（外包快照）"}
    except Exception:
        pass
    return {"style": "未知", "note": "快照读取失败"}


def _style_adjust(fit: dict, style: str) -> dict:
    """★2026-08-12 百轮后#127：风格叠加——按风格修正类型适配（C8 建议落地）。
    核心资产行情：反转/短线情绪降级（2020-2022 实证 -1.69~-3.72pp 系统性失效）；
    小盘反转行情：反转/短线情绪升级（恐慌底部反弹 alpha 最强）。"""
    adj = dict(fit)
    if style == "核心资产行情":
        for k in ("reversal", "tech_sentiment"):
            if k in adj:
                cur = adj[k]
                adj[k] = ("0" if cur in ("+", "++") else cur)   # 核心资产期降反转类
        adj.setdefault("reversal", "0")
    elif style == "小盘反转行情":
        for k in ("reversal", "tech_sentiment"):
            if k in adj:
                cur = adj[k]
                adj[k] = ("+" if cur in ("0", "--") else cur)   # 恐慌底部升级反转类
    return adj


def evaluate() -> dict:
    """融合四维 → 买入适宜度 + ★环境-类型适配（百轮#12：择时进审批轻量实现）"""
    dims = {
        "政策": policy_score(),
        "宏观": macro_score(),
        "情绪": sentiment_score(),
        "宽度": breadth_score(),
    }
    w = {"政策": 0.40, "宏观": 0.25, "情绪": 0.20, "宽度": 0.15}
    total = round(sum(dims[k]["score"] * w[k] for k in w), 1)
    if total >= 60:
        level, emoji = "适合买入", "🟢"
    elif total >= 40:
        level, emoji = "谨慎买入", "🟡"
    else:
        level, emoji = "不适合买入", "🔴"
    # ★#351 reason 精简：用 short 结论（政策冷/强势上行/偏冷/中性），公式细节 → 说明页
    reason = "；".join(f"{k} {dims[k]['score']:.0f}（{dims[k].get('short') or dims[k]['note']}）"
                      for k in ["政策", "宏观", "情绪", "宽度"])
    # ★2026-08-11 环境-类型适配（择时进审批：不同环境不同机会类型的历史适配度）
    #   依据：宏观分（Regime 过渡代理）映射环境档 → 各类型适配（++ 强势/ + 有利 / 0 中性 / - 不利 / -- 回避）
    _mc = dims["宏观"]["score"]
    if _mc >= 75:
        fit = {"breakout": "++", "event": "+", "revalue": "+", "pv_consensus": "0",
               "value": "0", "quality_gap": "0", "reversal": "--", "tech_sentiment": "++"}
        fit_note = "强势环境：弹性类（突破/短线情绪）历史适配最高"
    elif _mc >= 55:
        fit = {"breakout": "+", "event": "0", "revalue": "0", "pv_consensus": "+",
               "value": "0", "quality_gap": "+", "reversal": "--", "tech_sentiment": "+"}
        fit_note = "震荡环境：稳健类（质量折价/量价共识）+ 突破温和适配"
    else:
        fit = {"breakout": "--", "event": "-", "revalue": "-", "pv_consensus": "-",
               "value": "0", "quality_gap": "+", "reversal": "0", "tech_sentiment": "--"}
        fit_note = "防守环境：仅质量折价/低估值适配，弹性类回避"
    # ★2026-08-12 百轮后#127：风格状态（C8 建议落地）——外包快照 H30 行业宽度 + H27 分化
    #   叠加修正 regime_fit（核心资产行情降反转类 / 小盘反转行情升反转类）
    _style = _style_state()
    _fit_map = _style_adjust(fit, _style.get("style", "未知"))
    _style_tag = f"｜风格：{_style['style']}（{_style['note']}）" if _style.get("style") != "未知" else ""
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": _latest_trade_date(),
        "level": level,
        "emoji": emoji,
        "score": total,
        "reason": reason[:300],
        "dims": dims,
        "regime_fit": {"map": _fit_map, "note": fit_note + _style_tag},   # ★2026-08-11 环境-类型适配（Pitch 卡片参考）/#127 风格叠加
        "style_state": _style,   # ★2026-08-12 百轮后#127 风格状态（C8 门控输入）
        "formula": "政策40%+宏观25%+情绪20%+宽度15%；v3（08-12）：政策=四因子综合评分（EPU+社融+利差，#91）、情绪=外包温度计（越低越恐慌=买入区）、宽度=width5+行业宽度（H5b/H30 反向，行业优先）、宏观=regime+H27 等权降档；外包缺失自动回退 bars；#127 风格门控（H30+H27）叠加 regime_fit",
    }


def _latest_trade_date() -> str:
    return _merged_max_date()   # ★#135 双库合并探测（主库写保护后增量库含最新日）


def write_outputs(r: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = OUT_DIR / f"timing_system_{ts}.json"
    p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        (OUT_DIR / "timing_system.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return p


def latest() -> dict:
    fs = sorted(glob.glob(str(OUT_DIR / "timing_system_*.json")), key=os.path.getmtime)
    if fs:
        try:
            return json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    r = evaluate()
    p = write_outputs(r)
    print(f"{r['emoji']} {r['level']}（{r['score']}）| {r['reason'][:120]}")
    print(f"已存 {p}")
