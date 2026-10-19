# -*- coding: utf-8 -*-
"""scripts/check_factor_coverage.py — 机会层因子覆盖率核对

## 干什么

把 `factors/opportunities/registry.py` 里 7 类机会**引用**的因子，
与 `factors/opportunities/scan.py::compute_factors` **实际产出**的面板列对表，
列出「引用了但算不出来」的因子。

## 为什么需要它

`registry.py` 的 `trigger_desc` 是给评审看的**能力描述**。如果它声称的触发条件
（如 event 的"行业资金流连续3日净流入 + 板块指数突破"）在实际 `triggers()` 里
根本没实现，评审会被误导——**声称与实际不符**，且这种不符不会报错、只会静默失效。

本脚本让这个落差可复现（文档 §6.4 引用它）。

## 用法

    python scripts/check_factor_coverage.py
    python scripts/check_factor_coverage.py --json   # 机器可读输出

退出码：0 = 引用因子全部可算；1 = 存在算不出来的引用因子。
"""
import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from factors.opportunities import scan as S
from factors.opportunities.registry import OPPORTUNITY_TYPES, ORDER


def coverage() -> dict:
    """返回 {refs, panel_cols, present, missing}"""
    refs = sorted({f for ot in ORDER for f in OPPORTUNITY_TYPES[ot]["factors"]})

    px, vx = S.load_panel(days=300)
    if px is None:
        raise RuntimeError("load_panel 返回空 —— 无法核对（检查 data/cache/bars.db）")
    fin = S.load_fundamentals()
    basic = S.load_basic()
    st_codes = S.load_st_codes()
    panel = S.compute_factors(px, vx, fin, basic, st_codes)
    cols = set(panel.columns)

    present = [r for r in refs if r in cols]
    missing = [r for r in refs if r not in cols]
    return {
        "panel_shape": list(panel.shape),
        "refs": refs,
        "panel_cols": sorted(cols),
        "present": present,
        "missing": missing,
        "missing_by_type": {
            ot: [f for f in OPPORTUNITY_TYPES[ot]["factors"] if f in missing]
            for ot in ORDER
            if any(f in missing for f in OPPORTUNITY_TYPES[ot]["factors"])
        },
    }


def main():
    ap = argparse.ArgumentParser(description="机会层因子覆盖率核对")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    r = coverage()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 1 if r["missing"] else 0

    print("=" * 68)
    print("机会层引用因子 vs 扫描面板实产列")
    print("=" * 68)
    print(f"  面板: {r['panel_shape'][0]} 只股票 × {r['panel_shape'][1]} 列因子"
          f"（scan.compute_factors 实产）")
    print(f"  registry 引用: {len(r['refs'])} 个（去重）")
    print(f"  ✅ 算得出: {len(r['present'])}")
    print(f"  ❌ 算不出: {len(r['missing'])}")
    print()

    if r["missing"]:
        print("算不出来的引用因子（按机会类型）：")
        for ot, fs in r["missing_by_type"].items():
            print(f"  {ot:14s} {fs}")
            print(f"                 声称: {OPPORTUNITY_TYPES[ot]['trigger_desc']}")
        print()
        print("→ 这些触发条件在 triggers() 里未实现，属「声称与实际不符」。")
        print("  详见 docs/资产盘点.md §6.4")
    else:
        print("引用因子全部可算。")

    return 1 if r["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
