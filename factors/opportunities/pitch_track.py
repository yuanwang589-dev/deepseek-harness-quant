# -*- coding: utf-8 -*-
"""factors/opportunities/pitch_track.py — 历史 Pitch 远期收益池（2026-08-10 用户需求）

★需求（用户拍板）：只要进入 Pitch 的股票就进"历史 Pitch 远期收益池"，
  持续记录未来走势（T+1/5/20/60 实际收益），用于验证 Pitch 选股质量；
  现有回测池（1/2/3 年回测）保留为"人工复核回测池"。

机制：
  1. 入池：pitch_v2 生成后（dev_auto 8.6 之后）自动把当日 Pitch 股票追加进池
  2. 追踪：每日（dev_auto 每轮）用 bars.db 更新每只股票的远期实际收益
  3. 存储：logs/pitch_track_pool_{ts}.json（时间戳文件名，写保护免疫；读取方 glob 取最新）
  4. 展示：Deck /api/pitch_track + 门户卡

数据结构：
  {
    "ts": "2026-08-10 07:35",
    "entries": [
      {
        "code": "000650.SZ", "name": "仁和药业", "otype": "value", "score": 94.8,
        "entry_date": "2026-08-07",   // 入池时的数据日期（Pitch 基于的交易日）
        "entry_close": 5.43,          // 入池日收盘
        "fwd": { "t1": { "date": "2026-08-10", "ret": 0.0, "close": 5.43 },
                 "t5": null, "t20": null, "t60": null,   // null = 未到交易日
                 "latest": { "date": "2026-08-07", "ret": 0.0 } },
        "age_days": 1
      }
    ]
  }
"""
import glob
import json
import ast
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # factors/opportunities/ → deepseek-harness-quant
sys.path.insert(0, str(BASE))

BARS_DB = BASE / "data" / "cache" / "bars.db"   # ★2026-10-15 P1-A 绝对路径（原相对依赖 cwd）
FWD_HORIZONS = (1, 5, 20, 60)


def load_latest() -> dict:
    """读最新池文件（glob），无则返回空结构"""
    files = sorted(glob.glob(str(BASE / "logs" / "pitch_track_pool_*.json")))
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ts": "", "entries": []}


def _write(pool: dict):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pool["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = BASE / "logs" / f"pitch_track_pool_{ts}.json"
    p.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    return p


def append_pitch(pitch_file) -> dict:
    """pitch_v2 输出文件 → 把当日 Pitch 股票入池（已存在 code+entry_date 的跳过）
    ★2026-08-11 P-2：扩展——科技线主观确认（deck_decisions 里 action=buy 的 tech 条目）也入池，
      用户问题 5「科技 pitch 在主观确认后也应该进来」；从 tech_pitch + deck_decisions 交叉取。
    ★2026-08-11 修复：deck_decisions 已是时间戳文件名（写保护免疫），固定名文件不存在
      → 科技线主观确认永远失效；改 glob 取最新。入池条目标记 decided（buy/drop/''）供远期池展示审批状态。"""
    pitch_file = Path(pitch_file)
    pool = load_latest()
    d = json.loads(pitch_file.read_text(encoding="utf-8"))
    # ★审批记录映射（时间戳文件 glob 取最新；无则空）
    dec_map = {}
    try:
        _dfs = sorted((BASE / "logs").glob("deck_decisions_*.json"), key=lambda x: x.stat().st_mtime)
        if _dfs:
            for _r in json.loads(_dfs[-1].read_text(encoding="utf-8")):
                if isinstance(_r, dict) and _r.get("code") and _r.get("action") in ("buy", "drop"):
                    dec_map[_r["code"]] = _r["action"]
    except Exception:
        pass
    # ★entry_date 用 pool_date（pitch 基于的数据日期）而非 date（生成日）；异常回退 bars 最近交易日
    entry_date = d.get("pool_date") or d.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        # ★2026-08-11 修复：普通连接 D 盘 bars.db 等锁 20s+（实测 append_pitch 26.4s）→ immutable 秒开
        # ★#143 双库合并探测：主库写保护后增量库含最新日（08-12 的 entry 不能被错截为 08-11）
        from data.cache import DailyCache as _DC
        _mx = _DC().latest_trade_date()
        if not _mx:
            con0 = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
            _mx = con0.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
            con0.close()
        if _mx and entry_date > _mx:   # pool_date 虚标未来（如 08-10 > 08-07）→ 用最近交易日
            entry_date = _mx
    except Exception:
        pass
    # 入池日收盘（bars.db，immutable 只读连接秒开）
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    # ★2026-08-11 问题 5：跨日重复累积（同 code 多条）→ 按 code 唯一去重，保留最早入池条目
    existing_codes = {e["code"] for e in pool["entries"]}
    added = 0

    def _append(code, name, otype, score, risk, beneish, stop_plan=None, pool_type="auto_pitch"):
        nonlocal added
        if code in existing_codes:
            return
        row = con.execute(
            "SELECT close FROM daily_bar WHERE code=? AND date=?",
            (code, entry_date)).fetchone()
        pool["entries"].append({
            "code": code, "name": name or code,
            "otype": otype or "", "score": score,
            "risk_level": risk or "",
            "beneish": beneish or "",
            # ★2026-08-11 问题 5：保存定制止损方案（pitch_v2 的 stop_plan → 远期池）
            "stop_plan": stop_plan or {},
            # ★2026-08-12 用户需求#180：三池标记（auto_pitch 自动入池 / machine_top01 机器强因子 / human_select 人工选择）
            "pool_type": pool_type,
            # ★2026-08-11 审批状态标记（buy/drop/''；undo 撤回后置空）
            "decided": dec_map.get(code, ""),
            "entry_date": entry_date, "entry_close": row[0] if row else None,
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing_codes.add(code)
        added += 1

    for p in d.get("pitch", []):
        _append(p["code"], p.get("name", p["code"]), p.get("otype", ""), p.get("score"),
                p.get("risk_level", ""), (p.get("beneish") or {}).get("level", ""),
                p.get("stop_plan") or {}, "auto_pitch")
    # ★科技线主观确认入池：读 deck_decisions（action=buy 科技条目的 code）+ tech_pitch 交叉
    try:
        tech_pitch_f = sorted((BASE / "logs").glob("tech_pitch_*.json"))
        if dec_map and tech_pitch_f:
            tp = json.loads(Path(tech_pitch_f[-1]).read_text(encoding="utf-8"))
            tp_map = {x.get("code"): x for x in tp.get("entries", [])}
            for code, act in dec_map.items():
                if act != "buy":
                    continue
                t = tp_map.get(code)
                if t:   # 该 buy 记录对应科技池条目 = 科技主观确认
                    _append(t["code"], t.get("name", ""), t.get("otype", "breakout"),
                            t.get("score"), t.get("risk_level", ""), "")
        # 清理重复（交叉可能重复 append 同 code——但 existing 已挡）
    except Exception:
        pass
    con.close()
    if added:
        p = _write(pool)
        print(f"Pitch 远期池: 入池 {added} 只（{entry_date}，含科技主观确认）→ 池内共 {len(pool['entries'])} 条")
    else:
        print(f"Pitch 远期池: 无新入池（{entry_date} 已存在 {len(pool['entries'])} 条）")
    return pool


def dedupe() -> dict:
    """★2026-08-11 问题 5：清理历史重复——同 code 只保留最早入池一条（跟踪基准最早），
    多余条目（后续重复入池）删除；避免 100 天后池内 500 条里同股多条混翻。"""
    pool = load_latest()
    seen = {}
    for e in pool["entries"]:
        c = e.get("code")
        if c not in seen or e.get("entry_date", "") < seen[c].get("entry_date", ""):
            seen[c] = e
    cleaned = list(seen.values())
    removed = len(pool["entries"]) - len(cleaned)
    if removed:
        pool["entries"] = cleaned
        p = _write(pool)
        print(f"Pitch 远期池: 去重清理 {removed} 条重复（同 code 保留最早）→ 池内 {len(cleaned)} 条")
        return pool
    print(f"Pitch 远期池: 无重复（{len(cleaned)} 条）")
    return pool


def _merged_rows(code: str, date_from: str, conns: list) -> list:
    """★2026-08-12 百轮#102：跨主库+最近 3 个增量库合并取交易日序列（#65 双库模式）
    主库写保护 → 08-12 起数据在 bars_incr_*.db——单读主库会永远算不出 T+5（08-14 到期）
    返回 [(date, close)] 按日期升序去重"""
    merged = {}
    for _name, _c in conns:
        try:
            for _d, _cl in _c.execute(
                    "SELECT date, close FROM daily_bar WHERE code=? AND date>=?",
                    (code, date_from)).fetchall():
                if _cl:
                    merged[_d] = float(_cl)
        except Exception:
            continue
    return sorted(merged.items())


def update_fwd() -> dict:
    """用 bars.db（主库+增量库合并）更新所有入池股票的远期实际收益（每日调用）
    ★2026-08-12 百轮#102：改双库合并读取——原只读主库，08-12 起数据在增量库，
    08-14 T+5 到期日主库无 08-12~14 数据 → T+5 永远算不出（#65/69 教训复现）"""
    pool = load_latest()
    if not pool["entries"]:
        print("Pitch 远期池: 空池")
        return pool
    # 主库 + 最近 3 个增量库（immutable）
    conns = []
    try:
        conns.append(("main", sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1",
                                              uri=True, timeout=3)))
    except Exception:
        pass
    try:
        from data.cache import CACHE_DIR
        for _p in sorted(CACHE_DIR.glob("bars_incr_*.db"))[-3:]:
            try:
                conns.append((_p.name, sqlite3.connect(f"file:{_p.as_posix()}?mode=ro&immutable=1",
                                                       uri=True, timeout=3)))
            except Exception:
                continue
    except Exception:
        pass
    changed = 0
    # ★修复：entry_date 若为未来（>bars 最新）→ 回退最近交易日（2026-08-10 首版 bug）
    # ★2026-08-13 #221：改用 DailyCache.latest_trade_date()（≥4000 只完整性门槛）——
    #   原裸 MAX(date) 会读到 08-12 残缺占位日（183 只 baostock）→ 08-12 伪条目 entry_date
    #   不大于 _mx → 未来日期防御失效（6 只 entry_close=None 无法计算收益）
    try:
        from data.cache import DailyCache as _DC2
        _mx = _DC2().latest_trade_date()
    except Exception:
        _mx = None
    if not _mx:
        try:
            _mx = max((_c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0] or "0000-00-00"
                       for _n, _c in conns), default=None)
        except Exception:
            _mx = None
    for e in pool["entries"]:
        if e.get("entry_date", "") > (_mx or "9999"):
            old_d = e["entry_date"]
            e["entry_date"] = _mx
            row = _merged_rows(e["code"], _mx, conns)
            e["entry_close"] = row[0][1] if row else None
            e["fwd"] = {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None}
            e["age_days"] = 0
            changed += 1
    # 取入池日之后的所有交易日 close（跨库合并）
    for e in pool["entries"]:
        rows = _merged_rows(e["code"], e["entry_date"], conns)
        if not rows:
            continue
        entry_close = e.get("entry_close") or rows[0][1]
        if not entry_close:
            continue
        # ★2026-08-13 #222：entry_close 回填持久化——原 `or rows[0][1]` 仅算局部变量用于 fwd，
        #   不回写 e["entry_close"] → 池文件永久存 None（08-10 批次 12 只缺 close 影响 T+5 收益根基）；
        #   仅"未来日期回退"分支曾回写，正常路径漏了
        if not e.get("entry_close") and entry_close != e.get("entry_close"):
            e["entry_close"] = entry_close
            changed += 1
        # 建 date→idx 映射（fwd 用自然日索引：rows 为入池日起的连续交易日）
        # 远期收益 = close[horizon] / entry_close - 1（horizon = 交易日偏移）
        for h in FWD_HORIZONS:
            if h < len(rows):
                r_date, r_close = rows[h]
                ret = round(r_close / entry_close - 1, 4)
                if e["fwd"].get(f"t{h}") != {"date": r_date, "ret": ret, "close": r_close}:
                    e["fwd"][f"t{h}"] = {"date": r_date, "ret": ret, "close": r_close}
                    changed += 1
            # 未到 horizon → 保持 None
        # latest = 最新交易日
        if rows:
            l_date, l_close = rows[-1]
            l_ret = round(l_close / entry_close - 1, 4)
            if e["fwd"].get("latest") != {"date": l_date, "ret": l_ret}:
                e["fwd"]["latest"] = {"date": l_date, "ret": l_ret}
                changed += 1
        e["age_days"] = (datetime.strptime(rows[-1][0], "%Y-%m-%d") -
                         datetime.strptime(e["entry_date"], "%Y-%m-%d")).days
    for _n, _c in conns:
        try:
            _c.close()
        except Exception:
            pass
    if changed:
        p = _write(pool)
        print(f"Pitch 远期池: 更新 {changed} 项远期收益 → 池内 {len(pool['entries'])} 条（双库合并）")
    else:
        print(f"Pitch 远期池: 无变化（{len(pool['entries'])} 条，数据截至最新）")
    return pool


def summary() -> dict:
    """远期收益汇总（供 API/UI）：已实现 horizon 的平均收益 + 样本数"""
    pool = load_latest()
    entries = pool["entries"]
    out = {"ts": pool.get("ts", ""), "n_entries": len(entries), "horizons": {}}
    for h in FWD_HORIZONS:
        vals = [e["fwd"].get(f"t{h}") for e in entries if e["fwd"].get(f"t{h}")]
        if vals:
            rets = [v["ret"] for v in vals]
            out["horizons"][f"t{h}"] = {
                "n": len(vals), "avg_ret": round(sum(rets) / len(rets), 4),
                "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
            }
        else:
            out["horizons"][f"t{h}"] = {"n": 0, "avg_ret": None, "win_rate": None}
    # 最新收益分布
    lats = [e["fwd"].get("latest") for e in entries if e["fwd"].get("latest")]
    if lats:
        rets = [v["ret"] for v in lats]
        out["latest_avg"] = round(sum(rets) / len(rets), 4)
        out["latest_win_rate"] = round(sum(1 for r in rets if r > 0) / len(rets), 4)
        out["latest_date"] = lats[-1]["date"]
    return out


def append_machine_top01(n_top: int = 5) -> dict:
    """★2026-08-12 用户需求#180：机器强因子 top0.1% 池（machine_top01）
    从外包 ext_hits 取"信号最强 top0.1%"（≈5 只，0.1% 严格标准少数股票）→ 入池，带止损止盈。
    ★2026-08-13 修正（用户：信号最强，包括单强因子）：优先读 signal_top01（每只股票最强因子
      rank 的 top0.1%——单强因子股也能入选）；旧版 ext_hits 无该字段时 fallback consensus_ge4。
    纯机器客观选择，不依赖人工。"""
    pool = load_latest()
    _sd = "data/factorpool/output/daily_scores"
    _fs = sorted(glob.glob(_sd + "/ext_hits_*.json"), key=lambda x: Path(x).stat().st_mtime)
    if not _fs:
        print("机器池: 无 ext_hits 数据（外包因子池未产出）")
        return pool
    eh = json.loads(Path(_fs[-1]).read_text(encoding="utf-8"))
    # ★2026-08-13：信号最强优先（含单强因子），fallback 共识
    st = eh.get("signal_top01") or {}
    cg = eh.get("consensus_ge4") or {}
    if st:
        src = st
        src_name = "signal_top01（信号最强·含单强因子）"
    elif cg:
        src = cg
        src_name = "consensus_ge4（≥4 因子共识，旧版 fallback）"
    else:
        print("机器池: ext_hits 无 signal_top01/consensus_ge4")
        return pool
    # top N：信号值排序取前 n_top（key 是 "(date, code)" 字符串）
    top = sorted(src.items(), key=lambda x: -x[1])[:n_top]
    codes = [ast.literal_eval(k)[1] for k, v in top]
    entry_date = eh.get("date") or datetime.now().strftime("%Y-%m-%d")
    # ★#354 机器客观：机器池 = 最新交易日"当天最强 top0.1%"，旧批次先清（不累积）
    #   （原只追加不清旧 → 机器池越积越多，混入历史批次，"当天机器选了谁"不可读）
    _before = len(pool["entries"])
    pool["entries"] = [e for e in pool["entries"] if e.get("pool_type") != "machine_top01"]
    _cleared = _before - len(pool["entries"])
    if _cleared:
        print(f"机器池: 清旧批次 {_cleared} 条 → 只保留最新交易日 {entry_date} 的最强 top{n_top}")
    # 名称/类型（从 pitch_v2 候选找；无则从全市场名表补）
    names = {}
    _pf = sorted((BASE / "logs").glob("pitch_v2_*.json"), key=lambda x: x.stat().st_mtime)
    if _pf:
        try:
            _pd = json.loads(_pf[-1].read_text(encoding="utf-8"))
            for _p in (_pd.get("pitch") or []):
                names[_p.get("code")] = (_p.get("name"), _p.get("otype"), _p.get("stop_plan") or {},
                                         _p.get("risk_level", ""))
        except Exception:
            pass
    # ★2026-08-13 #321：机器池股票来自 ext_hits 共识（不在 pitch_v2）→ name 从全市场名表补
    _basic_names = {}
    try:
        _bc = sqlite3.connect("file:data/cache/stock_basic.db?mode=ro&immutable=1", uri=True, timeout=3)
        _basic_names = dict(_bc.execute("SELECT code, name FROM stock_basic").fetchall())
        _bc.close()
    except Exception:
        pass
    # ★#375 用户需求：带止损止盈条件的短线（tech_pitch）也应该进机器池
    #   tech_pitch 短线候选自带短线止损（trailing_ma/stop_loss_pct/atr_stop/short_line），
    #   与因子最强（machine_top01）并列，都是机器客观选择、带止损止盈 → 一并入机器池 B
    _short_list = []   # [{code,name,otype,score,stop_plan,risk_level}]
    try:
        _tfs = sorted((BASE / "logs").glob("tech_pitch_*.json"), key=lambda x: x.stat().st_mtime)
        if _tfs:
            _td = json.loads(_tfs[-1].read_text(encoding="utf-8"))
            for _t in (_td.get("entries") or []):
                _tcode = _t.get("code")
                if not _tcode or _tcode in codes:
                    continue   # 短线候选与因子最强重叠则跳过（去重）
                _short_list.append({
                    "code": _tcode,
                    "name": _t.get("name") or _basic_names.get(_tcode, _tcode),
                    "otype": _t.get("otype") or "tech_sentiment",
                    "score": _t.get("score"),
                    "stop_plan": _t.get("stop_plan") or {},
                    "risk_level": _t.get("risk_level") or "",
                    "add_date": _t.get("add_date") or "",
                })
    except Exception:
        pass
    # ★#180 止损止盈：机器池无定制 stop_plan → 用类型止损矩阵默认（stop_plan_for）
    try:
        from strategy.portfolio import stop_plan_for, _load_stop_matrix
        _MATRIX = _load_stop_matrix()
    except Exception:
        _MATRIX = {}
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    existing = {e["code"] for e in pool["entries"]}
    added = 0
    for code in codes:
        if code in existing:
            # ★#180 已存在但止损空 → 补止损（首次运行旧代码遗留）
            for _e in pool["entries"]:
                if _e["code"] == code and not (_e.get("stop_plan") or {}):
                    _e["stop_plan"] = stop_plan_for("machine_top01", _MATRIX) or {
                        "otype": "machine_top01", "stop_loss_pct": 0.07,
                        "time_stop_weeks": 8, "max_drawdown_pct": 0.12}
                    added += 1
            continue
        nm, ot, sp, rk = names.get(code, (code, "machine_top01", {}, ""))
        if not nm or nm == code:
            nm = _basic_names.get(code, code)
        if not sp:
            sp = stop_plan_for(ot or "machine_top01", _MATRIX) or {}
        if not sp:
            # ★#180 通用默认止损（机器池兜底：时间止损 8 周 + 回撤 -12% + 7% 硬止损）
            sp = {"otype": ot or "machine_top01", "stop_loss_pct": 0.07,
                  "time_stop_weeks": 8, "time_stop_min_gain": 0.0,
                  "trailing_ma": None, "max_drawdown_pct": 0.12}
        row = con.execute("SELECT close FROM daily_bar WHERE code=? AND date=?",
                          (code, entry_date)).fetchone()
        pool["entries"].append({
            "code": code, "name": nm, "otype": ot or "machine_top01",
            "score": float(top[[i for i, (k, _) in enumerate(top) if ast.literal_eval(k)[1] == code][0]][1]),
            "risk_level": rk or "", "beneish": "",
            "stop_plan": sp or {}, "pool_type": "machine_top01",
            "decided": "", "entry_date": entry_date,
            "entry_close": row[0] if row else None,
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing.add(code)
        added += 1
    # ★#375 短线候选入池（带止损止盈条件，otype=tech_sentiment，保留短线 stop_plan）
    for s in _short_list:
        code = s["code"]
        if code in existing:
            continue
        row = con.execute("SELECT close FROM daily_bar WHERE code=? AND date=?",
                          (code, s.get("add_date") or entry_date)).fetchone()
        pool["entries"].append({
            "code": code, "name": s["name"], "otype": s["otype"],
            "score": s["score"], "risk_level": s["risk_level"], "beneish": "",
            "stop_plan": s["stop_plan"], "pool_type": "machine_top01",
            "decided": "", "entry_date": s.get("add_date") or entry_date,
            "entry_close": row[0] if row else None,
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing.add(code)
        added += 1
    con.close()
    if added:
        _write(pool)
        print(f"机器池: 强因子 top{n_top} 入池 {added} 只（{entry_date}，因子 {len(codes)} + 短线 {len(_short_list)}）")
    else:
        print(f"机器池: 无新入池（{entry_date} 已存在）")
    return pool


def append_human_select() -> dict:
    """★2026-08-12 用户需求#180：人工选择池（human_select）
    从 deck_decisions 取 action=buy 且带 pitch_meta（= 人工审批通过）的股票入池——
    人在决策页点"买入"即人工选择，带该候选的止损止盈（pitch_meta.stop_plan）。"""
    pool = load_latest()
    _dfs = sorted((BASE / "logs").glob("deck_decisions_*.json"), key=lambda x: x.stat().st_mtime)
    if not _dfs:
        print("人工池: 无审批记录")
        return pool
    decisions = [r for r in json.loads(_dfs[-1].read_text(encoding="utf-8"))
                 if isinstance(r, dict) and r.get("action") == "buy"]
    if not decisions:
        print("人工池: 无人工 buy 记录")
        return pool
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    existing = {e["code"] for e in pool["entries"]}
    added = 0
    for r in decisions:
        code = r.get("code")
        if not code:
            continue
        # ★#180 人工池语义：同 code 已存在 → 升级标记为 human_select（人工选择最高优先级）
        _dup = [e for e in pool["entries"] if e["code"] == code]
        if _dup:
            if _dup[0].get("pool_type") != "human_select":
                _dup[0]["pool_type"] = "human_select"
                _dup[0]["decided"] = "buy"
                added += 1
            continue
        pm = r.get("pitch_meta") or {}
        entry_date = r.get("date") or datetime.now().strftime("%Y-%m-%d")
        row = con.execute("SELECT close FROM daily_bar WHERE code=? AND date=?",
                          (code, entry_date)).fetchone()
        pool["entries"].append({
            "code": code, "name": code,
            "otype": pm.get("otype", ""),
            "score": pm.get("score"),
            "risk_level": pm.get("risk_level", ""), "beneish": (pm.get("beneish") or ""),
            "stop_plan": pm.get("stop_plan") or {}, "pool_type": "human_select",
            "decided": "buy", "entry_date": entry_date,
            "entry_close": row[0] if row else None,
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing.add(code)
        added += 1
    con.close()
    if added:
        _write(pool)
        print(f"人工池: 人工选择 {added} 只入池（含止损止盈）")
    else:
        print(f"人工池: 无新入池（{len(decisions)} 条 buy 均已存在）")
    return pool


PERSONA_NAMES = {
    "linyuan": "林园", "fengliu": "冯柳", "chaoguyangjia": "炒股养家",
    "chenxiaoqun": "陈小群", "zhangmengzhu": "章盟主", "zhaolaoge": "赵老哥",
    "methodology": "方法论·牛散蒸馏",
}
POOL_NAMES = {
    "auto_pitch": "🅰 自动入池", "machine_top01": "🅱 机器强因子",
    "human_select": "🅲 人工选择", "ai_select": "🅳 AI 精选", "niu_select": "牛散主观",
}


def append_niu_select(persona: str, picks: list, date: str = "",
                      snapshot_date: str = "") -> dict:
    """★2026-08-15 用户需求：牛散主观决策池（niu_select——独立模块，按决策者分组远期验证）
    7 位牛散（林园/冯柳/炒股养家/陈小群/章盟主/赵老哥/方法论）在控制页对话中给出选股决策
    （基于量化 Pitch 快照），桥接记录后调用本函数入池：
      - pool_type='niu_select'（与 A/B/C/D 四池并列 = 第 5 池）
      - 每条目带 persona（决策者）+ niu_reason + niu_priority + snapshot_date（基于的快照）
      - 入池即享受 fwd 远期验证（T+1/5/20/60），summary_by_pool 按 persona 分组回测有效性
    同 code 不同 persona = 不同条目（按决策者独立追踪）；同 persona 同 code 当日重复 → 复核更新。
    picks: [{code, action, priority, reason_short}]  action∈buy/hold/sell/watch"""
    pool = load_latest()
    existing = {e["code"]: e for e in pool["entries"]}
    date = date or datetime.now().strftime("%Y-%m-%d")
    if not picks:
        print(f"牛散池: {persona} 今日无决策（空数组）")
        return pool
    # 名称补全（stock_basic）
    _basic_names = {}
    try:
        _bc = sqlite3.connect("file:data/cache/stock_basic.db?mode=ro&immutable=1",
                              uri=True, timeout=3)
        _basic_names = dict(_bc.execute("SELECT code, name FROM stock_basic").fetchall())
        _bc.close()
    except Exception:
        pass
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    added = rechecked = 0
    for pk in picks:
        code = str(pk.get("code") or "").strip()
        if not code:
            continue
        action = str(pk.get("action") or "watch").strip().lower()
        if action not in ("buy", "hold", "sell", "watch"):
            action = "watch"
        key = (code, persona)
        _dup = existing.get(code)
        if _dup and _dup.get("pool_type") == "niu_select" and _dup.get("persona") == persona \
                and _dup.get("entry_date") == date:
            # 同日同人同股 → 复核更新（理由/动作刷新，不重复建档）
            _dup["niu_action"] = action
            _dup["niu_reason"] = pk.get("reason_short", "")
            _dup["niu_priority"] = pk.get("priority", "")
            rechecked += 1
            continue
        if _dup and _dup.get("pool_type") == "niu_select" and _dup.get("persona") == persona:
            continue  # 历史已建过（不同日）——本轮入池只在当日新建一次；历史条目继续 fwd
        row = con.execute("SELECT close FROM daily_bar WHERE code=? AND date=?",
                          (code, date)).fetchone()
        pool["entries"].append({
            "code": code, "name": _basic_names.get(code, code),
            "otype": "niu_subject", "score": None,
            "risk_level": "", "beneish": "",
            "stop_plan": {}, "pool_type": "niu_select",
            "persona": persona, "persona_name": PERSONA_NAMES.get(persona, persona),
            "niu_action": action, "niu_reason": pk.get("reason_short", ""),
            "niu_priority": pk.get("priority", ""),
            "snapshot_date": snapshot_date, "decided": "",
            "entry_date": date, "entry_close": row[0] if row else None,
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing[code] = pool["entries"][-1]
        added += 1
    con.close()
    if added or rechecked:
        pool["last_action"] = {"date": date, "persona": persona,
                               "added": added, "rechecked": rechecked}
        _write(pool)
        print(f"牛散池: {persona} 决策入池 {added} 只 + 复核 {rechecked}（{date}）→ 池内 {len(pool['entries'])} 条")
    return pool


def summary_by_pool() -> dict:
    """★2026-08-15 主观多池远期：按池分组汇总（A/B/C/D/牛散 5 池 + 牛散按决策者细分）
    每池 t1/t5/t20/t60 avg_ret + win_rate + n；latest 均值。"""
    pool = load_latest()
    entries = pool["entries"]
    out = {"ts": pool.get("ts", ""), "n_entries": len(entries), "pools": {}, "niu_personas": {}}

    def _stat(sub):
        res = {}
        for h in FWD_HORIZONS:
            vals = [e["fwd"].get(f"t{h}") for e in sub if e["fwd"].get(f"t{h}")]
            if vals:
                rets = [v["ret"] for v in vals]
                res[f"t{h}"] = {"n": len(vals), "avg_ret": round(sum(rets) / len(rets), 4),
                                "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4)}
            else:
                res[f"t{h}"] = {"n": 0, "avg_ret": None, "win_rate": None}
        lats = [e["fwd"].get("latest") for e in sub if e["fwd"].get("latest")]
        if lats:
            rets = [v["ret"] for v in lats]
            res["latest"] = {"n": len(lats), "avg_ret": round(sum(rets) / len(rets), 4),
                             "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4)}
        return res

    pools = {}
    for e in entries:
        pt = e.get("pool_type") or "auto_pitch"   # 历史无标记 → 归 auto_pitch（四池优化）
        pools.setdefault(pt, []).append(e)
    for pt, sub in pools.items():
        out["pools"][pt] = {"name": POOL_NAMES.get(pt, pt), "n": len(sub), **_stat(sub)}
    niu = [e for e in entries if e.get("pool_type") == "niu_select"]
    for e in niu:
        p = e.get("persona") or "?"
        out["niu_personas"].setdefault(p, {"name": e.get("persona_name") or p, "n": 0, "entries": []})
        out["niu_personas"][p]["entries"].append(e)
    for p in out["niu_personas"].values():
        p["n"] = len(p["entries"])
        st = _stat(p["entries"])
        p.pop("entries", None)
        p.update(st)
    return out


def append_ai_select(picks: list, date: str = "", skip_reason: str = "") -> dict:
    """★2026-08-13 用户需求（#294 预留）：AI 主观选股池（ai_select——第 4 池）
    知识库 AI 每天跑数据库 → 从 pitch 里主观选几只 → 通过 /api/ai/select 调用本函数入池。
    入池即享受远期池 fwd 验证（T+1/5/20/60 自动追踪）——AI 结论自动复盘。
    ★#297 自由裁决权：AI 可选 0-5 只（可以不选）——picks 空 + skip_reason 记录"今日不选+理由"（算已回应，不告警）。
    picks: [{code, reason, confidence}]  reason=主观理由（复盘归因），confidence=0~1 置信度"""
    pool = load_latest()
    existing = {e["code"] for e in pool["entries"]}
    date = date or datetime.now().strftime("%Y-%m-%d")
    if not picks:
        # ★#297 今日不选（自由裁决权）：记录理由即履职
        _append_ai_insight([], date, 0, skip_reason=skip_reason)
        print(f"AI 池: 今日不选（{skip_reason or 'AI 判断无合适标的'}）——已记录，不告警")
        return pool

    # 从最新 pitch_v2 补全候选信息（otype/score/stop_plan——AI 从 pitch 里选，理应命中）
    _pf = None
    try:
        _fs = sorted((BASE / "logs").glob("pitch_v2*.json"), key=lambda x: x.stat().st_mtime)
        if _fs:
            _pf = {p.get("code"): p for p in (json.loads(_fs[-1].read_text(encoding="utf-8")).get("pitch") or [])}
    except Exception:
        pass

    added = 0
    rechecked = 0
    for pk in picks:
        code = str(pk.get("code") or "").strip()
        if not code:
            continue
        src = (_pf or {}).get(code) or {}
        _dup = [e for e in pool["entries"] if e["code"] == code]
        _ai_dup = [e for e in _dup if e.get("pool_type") == "ai_select"]
        if _ai_dup:
            # ★已在 D 池（历史自动精选过）→ 今日复核（last_ai_check 可见更新，不重复新建）
            for _e in _ai_dup:
                _e["last_ai_check"] = date
                _e["ai_check_note"] = pk.get("reason", "")
                if pk.get("confidence") is not None:
                    _e["ai_confidence"] = pk.get("confidence")
            rechecked += 1
            continue
        if _dup:
            # ★#300 防假象：池中有非 ai_select 旧条目（pool_type=None/auto_pitch 等）——
            #   **不把旧批标记成今日自动精选**，改为新建今日 ai_select 独立条目（entry_date=今日，
            #   独立 fwd 追踪；UI D 池可见）。2026-08-13 修复：此前直接跳过导致 D 池恒空——
            #   候选总是早批旧条目（08-11 入池）→ 自动精选从未真正产生 ai_select 条目
            pass  # 落到下方统一新建
        pool["entries"].append({
            "code": code, "name": src.get("name") or code,
            "otype": src.get("otype") or src.get("otype_name") or "ai_subject",
            "score": src.get("score"),
            "risk_level": (src.get("risk") or {}).get("level", "") if isinstance(src.get("risk"), dict) else "",
            "beneish": (src.get("beneish") or ""),
            "stop_plan": src.get("stop_plan") or {},
            "pool_type": "ai_select",
            "ai_reason": pk.get("reason", ""),
            "ai_confidence": pk.get("confidence"),
            "last_ai_check": date,
            "decided": "", "entry_date": date,
            "entry_close": None,  # update_fwd 自动补
            "fwd": {"t1": None, "t5": None, "t20": None, "t60": None, "latest": None},
            "age_days": 0,
        })
        existing.add(code)
        added += 1

    if added or rechecked:
        # ★2026-08-13：记录本次动作到池（last_action）——调用方（日报/接口）可精确读取
        #   本次新增/复核数，避免从条目反推（同日 entry_date/last_ai_check 会混淆历史）
        pool["last_action"] = {"date": date, "added": added, "rechecked": rechecked}
        _write(pool)
        _append_ai_insight(picks, date, added, rechecked=rechecked)
        _t = f"新增 {added} 只" if added else ""
        _t += (" + " if added and rechecked else "") + (f"复核 {rechecked} 只" if rechecked else "")
        print(f"AI 池: 自动精选 {_t}（{date}）——入池即 fwd 验证，复核即今日确认")
    else:
        print(f"AI 池: 无变化")
    return pool


def _append_ai_insight(picks: list, date: str, added: int, skip_reason: str = "", rechecked: int = 0):
    """AI 复盘记录（logs/ai_insights.json 追加——AI 结论 + 远期表现自动 join 复盘）
    ★#297：picks 空 + skip_reason → 记录"今日不选+理由"（type=skip，算已回应）
    ★2026-08-13：rechecked>0 → 记录 type=recheck 汇总条目（重复候选的今日复核）"""
    try:
        f = BASE / "logs" / "ai_insights.json"
        recs = []
        if f.exists():
            recs = json.loads(f.read_text(encoding="utf-8"))
        if not picks and skip_reason:
            recs.append({
                "date": date, "code": "", "type": "skip",
                "reason": skip_reason, "confidence": None,
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        for pk in picks:
            code = str(pk.get("code") or "").strip()
            if not code:
                continue
            recs.append({
                "date": date, "code": code, "type": "pick",
                "reason": pk.get("reason", ""),
                "confidence": pk.get("confidence"),
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        if rechecked > 0:
            recs.append({
                "date": date, "code": "", "type": "recheck",
                "reason": f"自动精选复核 {rechecked} 只（候选已在远期池，今日确认延续）",
                "confidence": None,
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        f.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="历史 Pitch 远期收益池")
    ap.add_argument("--append", type=str, default=None, help="pitch_v2 输出 json 路径（入池）")
    ap.add_argument("--update", action="store_true", help="更新远期收益")
    ap.add_argument("--summary", action="store_true", help="汇总统计")
    ap.add_argument("--dedupe", action="store_true", help="清理同 code 重复（保留最早）")
    ap.add_argument("--machine", type=int, default=0, help="机器强因子 top N 入池（0.1% ≈ 5）")
    ap.add_argument("--human", action="store_true", help="人工选择 buy 入池")
    ap.add_argument("--niu", type=str, default=None, help="牛散决策入池（persona id）")
    ap.add_argument("--niu-picks", type=str, default="", help="牛散决策: code:action:priority:reason,code2:...")
    ap.add_argument("--pool-summary", action="store_true", help="按池分组汇总（5 池 + 牛散按决策者）")
    args = ap.parse_args()
    if args.append:
        append_pitch(Path(args.append))
    if args.machine:
        append_machine_top01(args.machine)
    if args.human:
        append_human_select()
    if args.niu:
        picks = []
        for seg in [s for s in args.niu_picks.split(",") if s]:
            parts = seg.split(":")
            if len(parts) >= 2:
                picks.append({"code": parts[0], "action": parts[1],
                              "priority": parts[2] if len(parts) > 2 else "",
                              "reason_short": parts[3] if len(parts) > 3 else ""})
        append_niu_select(args.niu, picks)
    if args.update:
        update_fwd()
    if args.summary:
        print(json.dumps(summary(), ensure_ascii=False, indent=1))
    if args.pool_summary:
        print(json.dumps(summary_by_pool(), ensure_ascii=False, indent=1))
    if args.dedupe:
        dedupe()
    if not (args.append or args.update or args.summary):
        # 默认：更新 + 汇总
        update_fwd()
        print(json.dumps(summary(), ensure_ascii=False, indent=1))
