# -*- coding: utf-8 -*-
"""risk/pitch_review.py — Pitch 批次复核报告生成器（2026-08-12 十轮第4轮 #170）

★用户需求（知识库 P-2）：Pitch 选股质量需 T+1/5/20/60 持续验证，T+5 首批 08-14 到期
  不能等当天现写报告——预建自动复核：入池批次到期即产出复核报告（命中率/收益/建议）。

用法：
  python risk/pitch_review.py                 # 全部批次复核（含未到期批次显示进度）
  python risk/pitch_review.py --batch 08-07   # 指定入池批次复核
  python risk/pitch_review.py --json          # 输出 JSON（供 API/UI）

输出：output/pitch_review_{ts}.md + output/pitch_review_{ts}.json
★写保护免疫：时间戳文件名（glob 取最新读）。
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
import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "output"
FWD_HORIZONS = [1, 5, 20, 60]


def load_pool():
    """读最新 pitch_track_pool_*.json（写保护免疫 glob）"""
    from factors.opportunities.pitch_track import load_latest
    return load_latest()


def review_batch(entries, batch_date=None):
    """单个入池批次的 T+5 复核：命中率/平均收益/最佳最差/建议"""
    if batch_date:
        # ★2026-08-13 #221：日期格式容错——远期池 entry_date 为完整日期（2026-08-07），
        #   命令行可传短格式（08-07）→ 归一化后匹配
        def _norm(d):
            s = str(d or "")
            if len(s) == 5 and s[2] == "-":
                return "2026-" + s
            return s
        _bd = _norm(batch_date)
        ents = [e for e in entries if _norm(e.get("entry_date")) == _bd]
    else:
        ents = entries
    if not ents:
        return None
    # T+5 已实现的（有 t5 数据）
    done = [e for e in ents if e["fwd"].get("t5")]
    pending = [e for e in ents if not e["fwd"].get("t5")]
    out = {
        "batch": batch_date or "全部",
        "n_total": len(ents),
        "n_done_t5": len(done),
        "n_pending_t5": len(pending),
        "t5_avg": None, "t5_win_rate": None,
        "best": None, "worst": None,
        "t1_avg": None, "t1_win_rate": None,
        "note": "",
        # ★2026-08-13 #264：类型构成（otype Counter）——批次复核双维度解读（类型归因 × 入池时点市场环境）
        "type_breakdown": dict(Counter(str(e.get("otype") or "?") for e in ents)),
    }
    if done:
        rets = [e["fwd"]["t5"]["ret"] for e in done]
        out["t5_avg"] = round(sum(rets) / len(rets), 4)
        out["t5_win_rate"] = round(sum(1 for r in rets if r > 0) / len(rets), 4)
        out["best"] = {"code": max(done, key=lambda e: e["fwd"]["t5"]["ret"])["code"],
                       "ret": max(e["fwd"]["t5"]["ret"] for e in done)}
        out["worst"] = {"code": min(done, key=lambda e: e["fwd"]["t5"]["ret"])["code"],
                        "ret": min(e["fwd"]["t5"]["ret"] for e in done)}
    # T+1（入池次日，当天可复核）
    t1s = [e["fwd"].get("t1") for e in ents if e["fwd"].get("t1")]
    if t1s:
        r1 = [v["ret"] for v in t1s]
        out["t1_avg"] = round(sum(r1) / len(r1), 4)
        out["t1_win_rate"] = round(sum(1 for r in r1 if r > 0) / len(r1), 4)
        # ★2026-08-13 #224：市场基准对照——全市场 T+1 中位数（入池日→次一交易日），
        #   区分"选股 alpha" vs "市场 beta"：批次平均 < 市场中位 → 选股跑输（下跌日放大亏损）
        #   实测案例：08-10 批次 25 只 T+1 -1.73% vs 市场中位 -0.68%（16/25 跑输，非 beta）
        try:
            import sqlite3 as _sq3, statistics as _st3
            _c3 = _sq3.connect("file:data/cache/bars.db?mode=ro&immutable=1",
                               uri=True, timeout=3)
            _d0 = ents[0].get("entry_date", "")
            _t1date = t1s[0].get("date") if t1s and isinstance(t1s[0], dict) else ""
            _m3 = None
            if _d0 and _t1date and _t1date > _d0:
                _m3 = _c3.execute(
                    "SELECT b.close/a.close-1 FROM daily_bar a JOIN daily_bar b ON a.code=b.code "
                    "WHERE a.date=? AND b.date=? AND a.adjust='qfq' AND b.adjust='qfq'",
                    (_d0, _t1date)).fetchall()
            _c3.close()
            if _m3:
                _v3 = sorted(r[0] for r in _m3 if r[0] is not None)
                if _v3:
                    out["mkt_t1_median"] = round(_st3.median(_v3), 4)
                    out["t1_vs_mkt"] = round(out["t1_avg"] - out["mkt_t1_median"], 4)
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "risk/pitch_review.py:101 review_batch")
    if pending and not done:
        out["note"] = f"批次 {batch_date or ''} T+5 未到期（{len(pending)} 只待核）"
    elif done and pending:
        out["note"] = f"T+5 已实现 {len(done)} 只，{len(pending)} 只待核"
    return out


def _pool_stats(entries) -> dict:
    """按池分型统计（auto_pitch/machine_top01/human_select 各自 T+5 效果）——#221 提取为公共函数：
    单批次模式（--batch）也统计该批次的三池分布（原仅 review_all 统计，单批次下三池表空白）。
    ★2026-08-13 #221 修正：pool_type 为 None 的旧批次（三池机制 #180 08-12 21:05 前入池）不再
      默认归入 🅰 自动入池（会高估自动池数量）——独立显示"🅾 三池前"类别，复核报告诚实反映。"""
    _POOL_CN = {"auto_pitch": "🅰 自动入池", "machine_top01": "🅱 机器强因子",
                "human_select": "🅲 人工选择", None: "🅾 三池前（未标记）"}
    pool_groups = {}
    for e in entries:
        _pt = e.get("pool_type") if e.get("pool_type") else None
        pool_groups.setdefault(_pt, []).append(e)
    out = {}
    for pt, pes in pool_groups.items():
        done = [e for e in pes if (e.get("fwd") or {}).get("t5")]
        st = {"pool_type": pt if pt else "unmarked", "name": _POOL_CN.get(pt, pt or "未标记"),
              "n": len(pes),
              "n_done_t5": len(done), "t5_avg": None, "t5_win_rate": None}
        if done:
            rets = [e["fwd"]["t5"]["ret"] for e in done]
            st["t5_avg"] = round(sum(rets) / len(rets), 4)
            st["t5_win_rate"] = round(sum(1 for x in rets if x > 0) / len(rets), 4)
        out[pt if pt else "unmarked"] = st
    return out


def review_all(entries):
    """按入池日期分组复核"""
    batches = {}
    for e in entries:
        batches.setdefault(e.get("entry_date", "?"), []).append(e)
    out = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "n_entries": len(entries), "batches": {}, "pool_stats": {}}
    for bd in sorted(batches):
        r = review_batch(batches[bd], bd)
        if r:
            out["batches"][bd] = r
    # ★2026-08-12 用户需求#180：按池分型统计（auto_pitch/machine_top01/human_select 各自 T+5 效果）
    out["pool_stats"] = _pool_stats(entries)
    return out


def render_md(data: dict) -> str:
    L = [f"# Pitch 批次复核报告 · {data['ts']}", "",
         f"远期池 {data['n_entries']} 条 · 按入池批次分组", "",
         "| 入池批次 | 数量 | T+5 已实现 | T+5 平均 | T+5 胜率 | T+1 平均 | 市场T+1中位 | 选股超额 | 最佳 | 最差 | 类型构成 |",
         "|---------|------|-----------|---------|---------|---------|------------|---------|------|------|---------|"]
    for bd, r in sorted(data["batches"].items()):
        def _pct(v, d=1):
            return "{:.{}f}%".format(v * 100, d) if v is not None else "—"
        best = r["best"]
        worst = r["worst"]
        best_txt = "{} {:+}%".format(best["code"], round(best["ret"] * 100, 1)) if best else "—"
        worst_txt = "{} {:+}%".format(worst["code"], round(worst["ret"] * 100, 1)) if worst else "—"
        # ★#224 市场对照列：T+1 平均 vs 全市场中位数 → 选股超额（正=选股优于市场，负=跑输）
        _ex = r.get("t1_vs_mkt")
        ex_txt = ("{:+}%".format(round(_ex * 100, 1)) if _ex is not None else "—")
        # ★#264 类型构成列（双维度解读：类型归因 × 入池时点市场环境）
        _tb = r.get("type_breakdown") or {}
        _tb_txt = " ".join(f"{k}:{v}" for k, v in sorted(_tb.items(), key=lambda x: -x[1]))
        L.append("| {} | {} | {}/{} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            bd, r["n_total"], r["n_done_t5"], r["n_total"],
            _pct(r["t5_avg"]), _pct(r["t5_win_rate"], 0), _pct(r["t1_avg"]),
            _pct(r.get("mkt_t1_median")), ex_txt, best_txt, worst_txt, _tb_txt))
    # ★#180 三池效果对比
    L += ["", "## 三池效果对比（T+5）", "",
          "| 池 | 入池 | T+5 已实现 | T+5 平均 | T+5 胜率 |",
          "|----|------|-----------|---------|---------|"]
    for st in data.get("pool_stats", {}).values():
        L.append("| {} | {} | {}/{} | {} | {} |".format(
            st["name"], st["n"], st["n_done_t5"], st["n"],
            _pct(st["t5_avg"]), _pct(st["t5_win_rate"], 0)))
    L += ["", "## 待核批次（T+5 未到期）", ""]
    pend = [r for r in data["batches"].values() if r["n_done_t5"] == 0 and r["n_total"] > 0]
    if pend:
        for r in pend:
            L.append("- **{}**：{} 只入池，T+5 待核（{}）".format(r["batch"], r["n_total"], r["note"]))
    else:
        L.append("- 无")
    L += ["", "> 由 risk/pitch_review.py 自动生成 · T+5 到期自动复核（08-14 首批）"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Pitch 批次复核报告")
    ap.add_argument("--batch", default=None, help="指定入池批次（如 08-07）")
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = ap.parse_args()
    pool = load_pool()
    entries = pool.get("entries", [])
    if args.batch:
        r = review_batch(entries, args.batch)
        if r is None:
            # ★2026-08-13 #221：批次无匹配时友好提示（原 data={} 导致 render_md KeyError 崩溃）
            print(f"⚠️ 未找到入池批次 {args.batch}（远期池 {len(entries)} 条；可用 --json 看全部批次）")
            sys.exit(1)
        data = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "n_entries": len(entries), "batches": {args.batch: r},
                "pool_stats": _pool_stats([e for e in entries
                                           if str(e.get("entry_date", "")).replace("-", "")[:8]
                                           == ("2026" + args.batch.replace("-", ""))])}
    else:
        data = review_all(entries)
    OUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p_json = OUT_DIR / f"pitch_review_{ts}.json"
    p_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.json:
        md = render_md(data)
        p_md = OUT_DIR / f"pitch_review_{ts}.md"
        p_md.write_text(md, encoding="utf-8")
        print(md)
    print(f"\n✅ 已存: {p_json.name}")


if __name__ == "__main__":
    main()
