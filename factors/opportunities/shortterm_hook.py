# -*- coding: utf-8 -*-
"""factors/opportunities/shortterm_hook.py — 短线因子模块主系统对接（F5，2026-08-11 总指导）

★外包 F1-F4 已完成（factors_shortterm.py 11 因子 + 全量体检），F5 主系统对接总指导实施。
  主系统 compute_factors 无 open/high/low 算不了精确涨停 → 走外包 CSV 外接模式（B-8 同款）。

对接 4 项（P1）：
  1. event 类升级：event 机会 + 个股涨停维度（limit_up_premium 昨板今收 / consec_limit_up 连板
     / limit_up_cnt_5d 5日活跃 → rank 高分加分）
  2. 排雷：consec_limit_down≥2（连续跌停=基本面/资金断裂）→ 高危标记；open_limit_up_fail=1
     且 limit_up_cnt_5d≥3（高位炸板）→ 炸板风险标记
  3. 一字板披露：bars 日线 open==high==low 涨停日 = 一字板（当日不可买）→ pitch 标注（pitch_v2 侧）
  4. 竞价配合：昨涨停 + 今日竞价过热 → 反信号提示（auction 已有反信号，此处补充联动标注）

数据源：外包 daily_*.csv（rank=因子值百分位未取反；排雷用原始值）
"""
import csv
import glob
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

try:
    from data.cache import db_uri   # ★2026-10-15 P1-A：SQLite 只读 URI 单出口
except Exception:
    db_uri = None

# ★2026-10-15 P1-A：改绝对路径（原相对路径依赖 cwd，换目录运行即静默 glob 落空）
EXT_POOL_DIR = BASE / "data" / "factorpool" / "output" / "daily_scores"

_cache = {"ts": 0.0, "data": None}


def load_shortterm() -> dict:
    """读最新 daily CSV 短线因子 → {code: {premium, consec_up, cnt5d, consec_down, open_fail, limit_up_flag}}
    无文件/失败 → {}（降级：不影响主流程）"""
    global _cache
    import time
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < 300:
        return _cache["data"]
    out = {}
    try:
        # ★2026-08-14 #433：按 mtime 取最新（原 sorted 文件名 files[-1]——重跑补数据文件
        #   时间戳可能小于旧文件，文件名排序取到旧数据；铁律#3 glob 按 mtime）
        files = sorted(EXT_POOL_DIR.glob("daily_*.csv"), key=lambda p: p.stat().st_mtime)
        if not files:
            return out
        with open(files[-1], encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for row in rd:
                code = str(row.get("code", "")).upper()
                if "." not in code:
                    continue
                def _f(k):
                    v = row.get(k)
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
                rec = {
                    "premium": _f("limit_up_premium"),      # 昨板今收溢价（值大=涨停后高开/强势）
                    "consec_up": _f("consec_limit_up"),     # 连板数
                    "cnt5d": _f("limit_up_cnt_5d"),         # 5日涨停次数
                    "consec_down": _f("consec_limit_down"),  # 连续跌停数
                    "open_fail": _f("open_limit_up_fail"),   # 炸板近似
                    "limit_up_flag": _f("limit_up_flag"),    # 今日涨停
                    "premium_rank": _f("limit_up_premium_rank"),
                    "consec_up_rank": _f("consec_limit_up_rank"),
                    "cnt5d_rank": _f("limit_up_cnt_5d_rank"),
                }
                if any(v is not None for v in rec.values()):
                    out[code] = rec
        _cache.update({"ts": now, "data": out})
    except Exception as e:
        print(f"  [短线因子] 读取失败（降级空）: {str(e)[:60]}")
    return out


def apply_event_upgrade(opportunities: list) -> int:
    """★F5-1 event 类升级：event 机会 + 个股涨停维度加分
    limit_up_premium（昨板今收）/ consec_limit_up（连板）/ limit_up_cnt_5d（活跃）
    → rank 高分（≥0.85）各 +1，多维度叠加（同 B-8 模式，异常不阻断）"""
    st = load_shortterm()
    if not st:
        return 0
    n = 0
    for o in opportunities:
        if o.get("otype") != "event":
            continue
        r = st.get(o["code"])
        if not r:
            continue
        bonus = 0
        bits = []
        # ★2026-08-12 百轮后#141 score_event v2 落地（外包 C-7 实证）：
        #   consec_up_rank（连板）加分移除——rank 高=连板多=追高，C-7 实证连板≥2 后跑输 -0.78~-3.5pp
        #   → 改为"首板确认"（consec_up==1 低位启动）加分，与 v2 规范 0.20 首板确认同逻辑
        for k, label in (("premium_rank", "昨板今收"), ("cnt5d_rank", "5日活跃")):
            v = r.get(k)
            if v is not None and v >= 0.85:
                bonus += 1.0
                bits.append(label)
        cu = r.get("consec_up")
        if cu is not None and abs(cu) == 1:
            bonus += 1.0
            bits.append("首板确认")
        if bonus > 0:
            o["score"] = round(o["score"] + bonus, 1)
            o["note"] = (o.get("note") or "") + f"·短线涨停确认+{bonus:.0f}（{'/'.join(bits)}）"
            n += 1
    if n:
        print(f"  [短线因子] event 涨停维度加分 {n} 只")
    return n


def apply_limitdown_mine(opportunities: list) -> int:
    """★F5-2 排雷：consec_limit_down≥2（连续跌停=资金断裂）→ 高危标记+降权；
    open_limit_up_fail 且 cnt5d≥3（高位炸板）→ 炸板风险标记；
    ★FS-13 连板回避（O2 确认，研究 22.4：连板≥2 非一字 20 日 -3.9~-9.5%）→ 回避降权（异常不阻断）"""
    st = load_shortterm()
    if not st:
        return 0
    n = 0
    for o in opportunities:
        r = st.get(o["code"])
        if not r:
            continue
        cd = r.get("consec_down")
        # ★2026-08-12 百轮后#141：面板方向约定=负数触发（consec_limit_down=-1/-2 实测）——
        #   原 cd>=2 永不触发 → F5 连续跌停排雷静默失效；abs 适配方向无关
        if cd is not None and abs(cd) >= 2:
            o["score"] = round(o["score"] * 0.7, 1)          # 连续跌停 → 打 7 折
            o["risk_flags"] = list(o.get("risk_flags") or []) + ["consec_limit_down"]
            o["note"] = (o.get("note") or "") + f"·🔴连续跌停{int(abs(cd))}天排雷(F5)"
            n += 1
            continue
        if r.get("open_fail") == 1 and (r.get("cnt5d") or 0) >= 3:
            o["score"] = round(o["score"] * 0.85, 1)          # 高位炸板 → 打 8.5 折
            o["risk_flags"] = list(o.get("risk_flags") or []) + ["open_limit_up_fail"]
            o["note"] = (o.get("note") or "") + "·高位炸板(F5)"
            n += 1
            continue
        # ★FS-13 连板≥2 非一字回避（O2 落地，2026-08-11）：高位连板追高 20 日 -3.9~-9.5%
        #   ★#141 方向适配：面板 consec_limit_up 负值（-1=-1板/-2=2连板）→ abs 判断
        cu = r.get("consec_up")
        if cu is not None and abs(cu) >= 2 and r.get("premium", 1) < 0.095:
            o["score"] = round(o["score"] * 0.85, 1)          # 连板≥2 → 打 8.5 折
            o["risk_flags"] = list(o.get("risk_flags") or []) + ["consec_limit_up"]
            o["note"] = (o.get("note") or "") + f"·连板{int(abs(cu))}回避(FS-13)"
            n += 1
    if n:
        print(f"  [短线因子] 排雷 {n} 只（连续跌停/高位炸板/连板回避）")
    return n


def one_word_disclosure(entries: list) -> int:
    """★F5-3 一字板披露：pitch 条目若当日一字涨停（open==high==low 且涨停）→
    标注"一字板当日无法买入，T+1 考虑低吸/回避追高"（需 bars open/high/low）
    ★2026-08-12 百轮#102：add_date 为近期交易日，主库无该日时查增量库（双库兜底）"""
    try:
        import sqlite3
        import glob as _glob
        from pathlib import Path as _P
        # ★2026-10-15 修复：绝对路径（as_uri() 不接受相对路径，原写法静默失败）
        _db_paths = [str(BASE / "data" / "cache" / "bars.db")] + [
            str(p) for p in sorted((BASE / "data" / "cache").glob("bars_incr_*.db"))[-3:]]
        n = 0
        for e in entries:
            code, d = e.get("code"), e.get("add_date") or e.get("entry_date")
            if not code or not d:
                continue
            row = None
            for _p in _db_paths:
                try:
                    _uri = (db_uri(_p) if db_uri else _P(_p).resolve().as_uri() + "?mode=ro&immutable=1")
                    con = sqlite3.connect(_uri, uri=True, timeout=3)
                    row = con.execute(
                        "SELECT open, high, low, close FROM daily_bar WHERE code=? AND date=?",
                        (code, d)).fetchone()
                    con.close()
                except Exception:
                    continue
                if row:
                    break
            if row and row[0] and row[0] > 0 and abs(row[0] - row[1]) < 1e-6 and abs(row[1] - row[2]) < 1e-6:
                e["one_word"] = True
                e["risk_notice"] = (e.get("risk_notice") or "") + "·一字板当日无法买入（T+1 再评估）"
                n += 1
        return n
    except Exception:
        return 0


if __name__ == "__main__":
    st = load_shortterm()
    print(f"短线因子命中 {len(st)} 只")
    # 排雷扫描
    mines = [(c, r["consec_down"]) for c, r in st.items() if (r.get("consec_down") or 0) >= 2]
    print(f"连续跌停≥2 排雷: {len(mines)} 只")
    for c, v in mines[:5]:
        print(f"  🔴 {c}: 连续跌停 {int(v)} 天")
    # event 加分样本（★#141：consec_up_rank 连板加分已移除 → 只统计 premium/cnt5d + 首板）
    ev = [(c, r["premium_rank"], r.get("consec_up"), r["cnt5d_rank"]) for c, r in st.items()
          if (r.get("premium_rank") or 0) >= 0.85 or (r.get("cnt5d_rank") or 0) >= 0.85
          or (r.get("consec_up") or 0) == 1]
    print(f"涨停维度高分（event 可加分）: {len(ev)} 只")
    for c, p, cu, cn in ev[:5]:
        print(f"  ⚡ {c}: premium={p:.2f} 连板={cu:.2f} 活跃={cn:.2f}")
