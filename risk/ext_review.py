# -*- coding: utf-8 -*-
"""risk/ext_review.py — EXT 白名单裁决辅助报告（2026-08-12 十轮第8轮 #174）

★用户需求（知识库 P-3）：EXT 十二强（白名单因子）由总指导裁决，需要证据支持——
  把分散数据整合成"裁决支持报告"：哪些因子该进白名单/保持/裁掉，附 ICIR/状态/命中证据。

数据源（外包 factor_pool output，全部 glob 取最新 + 容错）：
  1. factor_manifest_*.json —— 因子元数据（分类/家族/ICIR/状态）
  2. health/health_2026-08-12.csv —— 因子有效性（有效/衰减）
  3. daily_scores/ext_hits_*.json —— EXT 命中（factor_rank>=0.75 + consensus>=4）

用法：
  python risk/ext_review.py               # 生成裁决报告
  python risk/ext_review.py --top 15      # 只看前 N 强
输出：output/ext_review_{ts}.md + .json（时间戳写保护免疫）
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
import csv
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

EXT_OUT = Path("data/factorpool/output")


def _latest(pattern: str) -> Path | None:
    fs = sorted(EXT_OUT.glob(pattern), key=os.path.getmtime)
    return fs[-1] if fs else None


def load_manifest() -> dict:
    f = _latest("factor_manifest*.json")
    if not f:
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d
    except Exception:
        return {}


def load_health() -> dict:
    """health CSV → {factor: {status, icir, trend}}"""
    fs = sorted(EXT_OUT.glob("health/health_*.csv"), key=os.path.getmtime)
    if not fs:
        return {}
    out = {}
    try:
        with open(fs[-1], encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                f = row.get("factor") or row.get("name") or ""
                if f:
                    out[f] = row
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/ext_review.py:60 load_health")
    return out


def load_ext_hits() -> dict:
    fs = sorted(EXT_OUT.glob("daily_scores/ext_hits_*.json"), key=os.path.getmtime)
    if not fs:
        return {}
    try:
        return json.loads(fs[-1].read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="EXT 白名单裁决辅助")
    ap.add_argument("--top", type=int, default=0, help="只看前 N 强")
    args = ap.parse_args()
    mf = load_manifest()
    hf = load_health()
    eh = load_ext_hits()

    # 因子集合：manifest（全量）+ health 并集
    factors = {}
    mf_list = mf.get("factors") or mf.get("manifest") or []
    if isinstance(mf_list, dict):
        mf_list = [{"name": k, **v} for k, v in mf_list.items()]
    for f in mf_list:
        fn = f.get("code") or f.get("name") or f.get("factor") or ""
        if fn:
            factors[fn] = {
                "name": fn, "cat": f.get("category") or f.get("cat") or "",
                "family": f.get("family") or "",
                "icir120": f.get("icir120") or f.get("icir_60") or f.get("icir") or None,
                "direction": f.get("direction") or None,
                "status": f.get("status") or "",
            }
    for fn, row in hf.items():
        if fn not in factors:
            factors[fn] = {"name": fn, "cat": "", "family": "", "icir120": None,
                           "direction": None, "status": ""}
        # health 覆盖状态
        factors[fn]["status"] = row.get("status") or row.get("verdict") or factors[fn]["status"]
        factors[fn]["icir120"] = factors[fn]["icir120"] or _num(row.get("icir120") or row.get("icir"))
        factors[fn]["health_raw"] = {k: v for k, v in row.items() if k != "factor"}

    # 命中统计：consensus_ge4 的因子分布（从 hits 各因子列表长度）
    hits = eh.get("hits") or {}
    for fn, lst in hits.items():
        if fn in factors:
            factors[fn]["hit_n"] = len(lst) if isinstance(lst, list) else 0

    # 排序：ICIR120 降序（裁决核心证据）
    ranked = sorted(factors.values(),
                    key=lambda x: (x["icir120"] is not None, x["icir120"] or -999),
                    reverse=True)
    if args.top:
        ranked = ranked[:args.top]

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = {"ts": ts, "n_factors": len(factors),
           "ext_date": eh.get("date", ""), "consensus_n": len((eh.get("consensus_ge4") or {})),
           "top": ranked}
    p_json = BASE / "output" / f"ext_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # MD 报告
    L = [f"# EXT 白名单裁决辅助报告 · {ts}", "",
         f"因子池 {len(factors)} 因子 · EXT 数据日 {eh.get('date','—')} · 4+ 因子共识股 {len(eh.get('consensus_ge4') or {})} 只", "",
         "| # | 因子 | 分类 | ICIR120 | 方向 | 状态 | EXT 命中数 | 裁决建议 |",
         "|---|------|------|--------|------|------|-----------|---------|"]
    for i, f in enumerate(ranked, 1):
        icir = f"{float(f['icir120']):.2f}" if f["icir120"] is not None else "—"
        # 裁决建议规则：ICIR≥0.3 有效 → 白名单候选；0.15-0.3 → 观察；<0.15 或负 → 裁掉
        try:
            v = float(f["icir120"])
            rec = "✅ 白名单候选" if v >= 0.3 else ("👁 观察" if v >= 0.15 else "🗑 建议裁掉")
        except Exception:
            rec = "—"
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, f["name"], f["cat"] or "—", icir,
            f["direction"] or "—", f["status"] or "—",
            f.get("hit_n", "—"), rec))
    L += ["", "> 裁决标准：ICIR120≥0.3 白名单候选 / 0.15-0.3 观察 / <0.15 裁掉",
          "> 由 risk/ext_review.py 自动生成 · 最终裁决由总指导拍板"]
    p_md = BASE / "output" / f"ext_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    p_md.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n✅ 已存: {p_md.name} + {p_json.name}")


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


if __name__ == "__main__":
    main()
