# -*- coding: utf-8 -*-
"""factors/pool/seed_pool.py — 因子池登记（把散落各处的因子收进 factor_pool.db）

## 为什么需要这个脚本

`factors/pool/lifecycle.py` 的「挖→抓→测→决→报」机制早已就绪，`params.yaml`
的 `factor_pool` 段也配好了阈值（active 65 / candidate 40）——但
`data/cache/factor_pool.db` 的 `factors` 表**一直是 0 行**：
治理逻辑没有对象，`registry.py` 的状态机、漂移巡检、报告全是空转。

本脚本把项目里**实际存在**的因子登记入池。它只做「挖（ingest）」这一环，
评估（测）交给 `lifecycle.py --evaluate`。

## 因子来源（四个，互有交集，按 canonical 名去重）

| 源 | 位置 | 数量 | 本仓能算？ | 质量数据从哪来 |
|---|---|---|---|---|
| ① 引擎因子 | `factors/factor_engine.py::FACTOR_FUNCS` | 6 | ✅ | `validation/factor_evaluator.py` 实测 |
| ② 扫描面板因子 | `factors/opportunities/scan.py::compute_factors` | 18 | ✅ | 暂无通路（评估器只认 ①） |
| ③ 外部因子池强因子 | `factors/risk/strong_factor_table.py::STRONG_TABLE` | 27 | ❌ | F1 已实测指标，本脚本派生 |
| ④ 时序政策因子 | `factors/policy/epu_factors.py::EPU_FAMILY` | 6 | ❌（数据未下载） | 需 `eval_ts.py` + FRED |

## ★三个诚实性设计（都是本轮踩过的坑）

**1. `computable` 概念**：③ 那 27 个因子在**本仓算不出来**（它们来自外部因子池的
`daily_scores`，而 `data/factorpool/output/daily_scores/` 目录不存在）。登记它们是为了
让「已知强因子」有账可查，但**不能假装本仓能复现**——所以 note 里写明，且
`source` 指到 STRONG_TABLE 而不是某个计算函数。

**2. `derive_score()` 是派生、不是实测**：③ 的 27 个因子没有可跑的评估通路，
但 F1 判定时已实测出 `icir120 / t120 / win_rate / n_days`。本脚本用**文档化的固定权重**
把它们折成 0-100 分，与 `eval_ts`/`factor_evaluator` 同量纲；**原始指标全量写入
`last_eval_detail`**，任何人可自行复算校验。不要把这个分数当成"重跑过评估"。

**3. `EXPRESS_BANNED` 必须压状态**：`lhb_jg_cnt_20` / `limup_ex_5` 的截面 ICIR 全库前三，
但组合层实证是**负超额**（-5.63pp / 6-7 个负年），已被移出直通白名单。
派生分给它们 100 分，若不特殊处理就会标成 `active`（可接入策略）——**这是错的**。
故这两个强制 `candidate` 并在 note 里说明原因。

## 用法

    python factors/pool/seed_pool.py --dry-run    # 只打印将要登记的内容
    python factors/pool/seed_pool.py              # 实际写入
    python factors/pool/seed_pool.py --status     # 查看池状态
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from factors.pool.registry import FactorRegistry

EVAL_JSON = BASE / "output" / "factor_evaluations.json"


# ============================================================
# 因子清单（canonical 名 + 元数据）
# ============================================================

# ---------- ① 引擎因子：factors/factor_engine.py::FACTOR_FUNCS ----------
# 名字与方向取自该模块的 DEFAULT_DIRECTION（实证驱动）
ENGINE_FACTORS = [
    # name, direction, note
    ("lowvol_60", -1, "60日波动率（低波正用，值越小越好）。scan 内别名 vol60（年化口径 ×√252）"),
    ("near_high_250", 1, "接近52周高点（收盘/250日最高）。120日口径唯一转正的价量因子"),
    ("mom_20", -1, "20日涨幅（A股短期反转显著）。scan/机会注册表内别名 mom20"),
    ("mom_120", -1, "120日涨幅（小盘池反转、大盘池可正用，CS-03 分池）。别名 mom120"),
    ("rps_120", -1, "相对强度（降权反转，大盘池可选正用）"),
    ("new_high_250", 0, "52周新高（0/1）—— 各口径 IC 均为负，已剔除（direction=0）"),
]

# ---------- ② 扫描面板因子：scan.py::compute_factors ----------
# 只收「有方向、有横截面区分度」的因子；close/code6/name/industry/high252/low252
# 等中间量与非因子列（non_st/non_lowprice 是过滤器）不登记
SCAN_FACTORS = [
    # name, family, direction, note
    ("drawdown_60d", "技术", -1, "60日回撤（负值，越深越好=反转）。reversal/quality_gap 触发用"),
    ("rsi14", "技术", -1, "14日 RSI。reversal 触发用"),
    ("vol_ratio", "技术", 1, "量比 = 20日均量/60日均量。reversal/breakout 触发用"),
    ("vol_contract", "技术", 1, "波动收缩（20日波动/60日波动，VCP 近似）。breakout 触发用；同时在 STRONG_TABLE"),
    ("ma50_up", "技术", 1, "MA50 向上（0/1）。breakout 触发用"),
    ("ma200_up", "技术", 1, "MA200 向上（0/1）。breakout 触发用"),
    ("pb_pct", "估值", -1, "PB 截面分位（越低越好）。value 触发用"),
    ("pe_pct", "估值", -1, "PE 截面分位（已排除负 PE）。value/revalue 触发用"),
    ("div_yield", "估值", 1, "股息率 %。value/quality_gap 触发用"),
    ("roe", "基本面", 1, "ROE（优先年化 roe_avg，缺失回落单季）。4 类机会触发用"),
    ("sq_nyoy", "基本面", 1, "单季净利同比（scan 内 sq_net_yoy 的别名）。revalue/event 触发用"),
    ("sq_rev_yoy", "基本面", 1, "单季营收同比"),
    ("gross_margin", "基本面", 1, "毛利率（来自 finance_quality.gp_margin）"),
    ("liability", "基本面", -1, "资产负债率（越低越好）。value/quality_gap 触发用"),
    ("cfo_health", "基本面", 1, "经营现金流/净利润 > 0（0/1）。quality_gap 触发用"),
    ("roe_avg", "基本面", 1, "年化 ROE（quality.roe_avg，覆盖约 5388 只）"),
]

# ---------- ④ 时序政策因子：factors/policy/epu_factors.py::EPU_FAMILY ----------
# 元数据（source/freq/desc）从 EPU_FAMILY 读，避免重复维护


# ============================================================
# ③ 外部因子池强因子：派生分
# ============================================================

def derive_score(icir120: float, t120: float, win_rate: float, n_days: int) -> float:
    """从 F1 已实测指标派生 0-100 质量分。

    ★这是**派生**不是实测：本仓没有这 27 个因子的计算通路，跑不了评估器。
      口径与 eval_ts / factor_evaluator 对齐（0-100），但权重按 F1 可得指标选取。
      原始四项指标会全量写入 last_eval_detail，可自行复算校验。

    权重：
      ICIR120  0.40 —— 信号强度（F1 门槛 ≥0.5）
      IC胜率   0.25 —— 稳定性（F1 门槛 ≥60%）
      t120     0.20 —— 显著性（F1 门槛 ≥4）
      n_days   0.15 —— 样本充分度（F1 门槛 ≥250）

    归一化上限：ICIR 1.5 / t 25 / 胜率 0.9 / n_days 365，各 clip 到 1.0。
    """
    ic = min(icir120 / 1.5, 1.0) * 100
    t = min(t120 / 25.0, 1.0) * 100
    win = min(max((win_rate - 0.5) / 0.4, 0.0), 1.0) * 100
    cov = min(n_days / 365.0, 1.0) * 100
    return round(0.40 * ic + 0.25 * win + 0.20 * t + 0.15 * cov, 1)


def strong_table_entries(computable: dict):
    """把 STRONG_TABLE 转成登记条目（含派生分与原始指标）。

    computable: {因子名: 本仓计算位置} —— 用于修正 note。
      有些因子（如 vol_contract）**同时**是本仓可算的扫描面板列，
      和外部因子池的 F1 强因子。此时 note 不能照抄"本仓无计算通路"。
    """
    from factors.risk.strong_factor_table import STRONG_TABLE, EXPRESS_BANNED
    out = []
    for name, m in STRONG_TABLE.items():
        score = derive_score(m["icir120"], m["t120"], m["win_rate"], m["n_days"])
        banned = name in EXPRESS_BANNED
        head = (f"外部因子池 F1 强因子白名单（{m['f3_family']}）；"
                f"风格暴露={m['style_exposed']}；拥挤代理={m['crowd_proxy']}")
        if name in computable:
            tail = f"；本仓可算（{computable[name]}），质量分取自 F1 实测指标"
        else:
            tail = ("；★本仓无计算通路（源 daily_scores 目录不存在），"
                    "质量分由 F1 实测指标派生")
        if banned:
            tail += "；★已移出直通白名单（截面 ICIR 高但组合层负超额），不得标 active"
        out.append({
            "name": name, "family": "外部因子池", "kind": "cross_sectional",
            "source": "factors/risk/strong_factor_table.py::STRONG_TABLE",
            "freq": "daily", "direction": 1, "note": head + tail,
            "score": score, "computable": name in computable, "express_banned": banned,
            "detail": {"derived_from": "F1 实测指标（非本仓重跑）",
                       "icir120": m["icir120"], "t120": m["t120"],
                       "win_rate": m["win_rate"], "n_days": m["n_days"],
                       "crowd_proxy": m["crowd_proxy"], "f3_family": m["f3_family"],
                       "style_exposed": m["style_exposed"]},
        })
    return out


def epu_entries():
    """EPU 时序因子族（元数据来自 epu_factors.EPU_FAMILY）"""
    try:
        from factors.policy.epu_factors import EPU_FAMILY
    except Exception as e:
        print(f"  ⚠ EPU_FAMILY 不可读（{e}），跳过 ④ 源")
        return []
    db = BASE / "data" / "policy" / "epu.db"
    has_data = db.exists()
    out = []
    for name, meta in EPU_FAMILY.items():
        note = f"{meta.get('desc', '')}；时序政策因子"
        if not has_data:
            note += "；★data/policy/epu.db 缺失（需联网从 FRED 拉取），当前无法评估"
        out.append({
            "name": name, "family": "政策", "kind": meta.get("kind", "time_series"),
            "source": meta.get("source", ""), "freq": meta.get("freq", "monthly"),
            "direction": 1, "note": note, "score": None,
            "computable": has_data, "express_banned": False,
            "detail": None,
        })
    return out


# ============================================================
# 主流程
# ============================================================

def build_entries(quiet: bool = False) -> list:
    """汇总全部登记条目（按 canonical 名去重；同名则合并质量指标）"""
    entries = []

    for name, direction, note in ENGINE_FACTORS:
        entries.append({
            "name": name, "family": "技术", "kind": "cross_sectional",
            "source": "factors/factor_engine.py::FACTOR_FUNCS",
            "freq": "daily", "direction": direction, "note": note,
            "score": None, "computable": True, "express_banned": False, "detail": None,
        })

    for name, family, direction, note in SCAN_FACTORS:
        entries.append({
            "name": name, "family": family, "kind": "cross_sectional",
            "source": "factors/opportunities/scan.py::compute_factors",
            "freq": "daily", "direction": direction, "note": note,
            "score": None, "computable": True, "express_banned": False, "detail": None,
        })

    # 本仓可算的因子名 → 计算位置（供 STRONG_TABLE 修正 note）
    computable = {e["name"]: e["source"].split("::")[-1] for e in entries}

    entries += strong_table_entries(computable)
    entries += epu_entries()

    # ★去重 + 合并：同一因子可能同时来自「本仓可算」（scan）与「外部因子池」（STRONG_TABLE）。
    #   不能简单丢弃后到者 —— 踩过：vol_contract 在两边都有，直接跳过导致它
    #   明明有 F1 实测指标却因为没有评估通路而留空分。
    #   规则：保留先到者（本仓可算，source 更准）；后到者若带分数则**吸收分数与指标**。
    by_name, order, merged = {}, [], []
    for e in entries:
        n = e["name"]
        if n not in by_name:
            by_name[n] = e
            order.append(n)
            continue
        merged.append(n)
        cur = by_name[n]
        if cur["score"] is None and e["score"] is not None:
            cur["score"] = e["score"]
            cur["detail"] = e["detail"]
            cur["express_banned"] = e["express_banned"]
            # 分数来自外部源 → note 也要跟着更新（去掉/保留都已由 strong_table_entries 处理）
            cur["note"] = f"{cur['note']}；〔质量分来自外部因子池〕{e['note']}"
        if e["direction"] == 0:
            cur["direction"] = 0
    if merged and not quiet:
        print(f"  （合并同源因子 {len(merged)} 个 → {sorted(set(merged))}）")
    return [by_name[n] for n in order]


def load_eval_scores() -> dict:
    """读 factor_evaluator 的产出（若已跑过），把真实评分贴到引擎因子上"""
    if not EVAL_JSON.exists():
        return {}
    try:
        data = json.loads(EVAL_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠ 读取 {EVAL_JSON.name} 失败：{e}")
        return {}
    out = {}
    for name, r in data.items():
        sc = r.get("scorecard") or {}
        if sc.get("score") is not None:
            out[name] = {"score": sc["score"], "verdict": sc.get("verdict"),
                         "weight_suggestion": sc.get("weight_suggestion"),
                         "ic": (r.get("ic") or {}).get("rank_ic_mean"),
                         "icir": (r.get("ic") or {}).get("icir"),
                         "ic_win_rate": (r.get("ic") or {}).get("ic_win_rate")}
    return out


def seed(reg: FactorRegistry, dry_run: bool = False, quiet: bool = False) -> dict:
    def say(msg):
        if not quiet:
            print(msg)

    entries = build_entries(quiet=quiet)
    eval_scores = load_eval_scores()
    if eval_scores:
        say(f"  已读到 factor_evaluator 产出：{sorted(eval_scores)}")
    else:
        say("  （未找到 output/factor_evaluations.json —— 引擎因子将留 candidate，待评估）")

    TH = {"active": 65, "candidate": 40}
    stat = {"new": 0, "exist": 0, "scored": 0, "active": 0, "candidate": 0, "retired": 0}

    for e in entries:
        name = e["name"]

        # 引擎因子：优先用真实评估分
        score, detail, src = e["score"], e["detail"], "派生" if e["score"] is not None else None
        if name in eval_scores:
            ev = eval_scores[name]
            score = ev["score"]
            detail = {"evaluator": "validation/factor_evaluator.py", **ev}
            src = "实测"

        # 方向 0 = 已剔除 → retired
        if e["direction"] == 0:
            status = "retired"
        elif score is None:
            status = "candidate"
        elif e["express_banned"]:
            status = "candidate"          # ★压状态：见模块 docstring 第 3 点
        else:
            status = "active" if score >= TH["active"] else (
                "candidate" if score >= TH["candidate"] else "retired")

        if dry_run:
            print(f"    [{status:9}] {name:20s} {e['kind']:16s} "
                  f"dir={e['direction']:2d} score={score if score is not None else '-':>6} "
                  f"src={src or '-'}")
            continue

        created = reg.register(name=name, family=e["family"], kind=e["kind"],
                               source=e["source"], freq=e["freq"],
                               direction=e["direction"], note=e["note"])
        stat["new" if created else "exist"] += 1
        reg.set_status(name, status, score=score, detail=detail)
        stat["scored" if score is not None else "unscored"] = stat.get("scored" if score is not None else "unscored", 0) + 1
        stat[status] = stat.get(status, 0) + 1

    return stat


def main():
    ap = argparse.ArgumentParser(description="因子池登记（挖）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--status", action="store_true", help="只看池状态")
    args = ap.parse_args()

    reg = FactorRegistry()
    if args.status:
        print("因子池统计:", reg.stats())
        for f in reg.list_factors():
            print(f"  [{f['status']:9}] {f['name']:20s} {f['kind']:16s} "
                  f"score={f['score']}  {f['source']}")
        return 0

    print(f"=== 因子池登记 · {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"目标库: {reg.db_path}")
    if args.dry_run:
        print("--- DRY RUN（不写库）---")
    print(f"登记前池内: {reg.stats()['total']} 个")
    print()

    stat = seed(reg, dry_run=args.dry_run)

    print()
    if args.dry_run:
        print("--- DRY RUN 结束，未写入 ---")
    else:
        print(f"登记完成: 新建 {stat.get('new', 0)} / 更新 {stat.get('exist', 0)}")
        print(f"有分 {stat.get('scored', 0)} 个 · 无分 {stat.get('unscored', 0)} 个")
        print(f"状态分布: {reg.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
