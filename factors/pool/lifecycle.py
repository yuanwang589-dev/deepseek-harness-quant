# -*- coding: utf-8 -*-
"""factors/pool/lifecycle.py — 因子池生命周期驱动（挖→抓→测 闭环调度）

职责：把因子池变成"动态变化"的活系统：
  1. 挖（ingest）   ：注册新候选因子（研究巡航发现 / EPU 族批量注册）
  2. 抓（fetch）    ：刷新因子数据（EPU 数据源更新、未来扩展更多源）
  3. 测（evaluate） ：按 kind 分流评估：
                        cross_sectional → validation/factor_evaluator（8 维体检）
                        time_series     → factors/pool/eval_ts（时序评估器）
  4. 决（adjudicate）：评分 → active/candidate/retired 状态流转；active 因子进入 monitoring
                      定期重评，漂移/失效 → retired
  5. 报（report）   ：因子池状态报告（report/factor_pool_report.md + .json）

接入：dev_auto run_update 每轮调用 evaluate_pool()（新候选评估 + active 因子巡检）；
      params.yaml factor_pool 段控制阈值。

用法：
  python factors/pool/lifecycle.py --register-epu   # 注册 EPU 因子族（6 个）
  python factors/pool/lifecycle.py --evaluate        # 测评全部候选/active 因子
  python factors/pool/lifecycle.py --report          # 出因子池报告
  python factors/pool/lifecycle.py --status          # 查看池状态
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from factors.pool.registry import FactorRegistry
from factors.pool.eval_ts import evaluate_time_series, market_monthly_returns, THRESHOLDS

OUT_DIR = BASE / "report"


# ---------- 挖：注册 ----------
def register_epu_family(reg: FactorRegistry):
    """批量注册 EPU 政策因子族（元数据来自 factors/policy/epu_factors.EPU_FAMILY）"""
    from factors.policy.epu_factors import EPU_FAMILY
    n_new = n_exist = 0
    for name, meta in EPU_FAMILY.items():
        created = reg.register(
            name=name, family="政策", kind=meta["kind"], source=meta["source"],
            freq=meta["freq"], direction=1, note=meta["desc"])
        n_new += created
        n_exist += not created
    print(f"EPU 因子族注册完成: 新建 {n_new} / 已存在 {n_exist}")


# ---------- 抓：数据刷新 ----------
def fetch_policy_data():
    """刷新政策类因子数据（EPU FRED 全量重下，量小）"""
    from factors.policy.epu_fetcher import init_db, sync
    con = init_db()
    n = sync(con)
    con.close()
    print(f"EPU 数据刷新: {n} 条")
    return n


# ---------- 测：评估 ----------
def _eval_time_series_factor(name: str) -> dict:
    from factors.policy.epu_factors import get_factor
    series = get_factor(name, start="2015-01")
    mkt = market_monthly_returns()
    return evaluate_time_series(name, series, mkt)


# factor_evaluator 的评分行形如：  `  评分 54.0 ｜ 反向弱有效 ｜ 低权重（10-30%，反用）`
_SCORE_RE = re.compile(r"评分\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)")


def parse_evaluator_score(out: str):
    """从 factor_evaluator 的 stdout 解析评分；解析不出返回 None。

    ★2026-10-15 修复：原实现用 `line.split(":")[-1]` 解析这一行，而该行**没有冒号**
      → float() 必抛 ValueError → score 恒为 None → 下游一律判成 retired。
      即「cross_sectional 评估通路从未真正生效，且每次评估都在淘汰因子」。
    """
    for line in (out or "").splitlines():
        m = _SCORE_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def evaluate_factor(reg: FactorRegistry, f: dict) -> dict:
    """单因子评估 → 评分卡 → 状态流转"""
    kind = f["kind"]
    if kind == "time_series":
        res = _eval_time_series_factor(f["name"])
        status = res["status"]  # active/candidate/retired
        reg.update_score(f["name"], res["score"], status=status, detail=res)
        return res
    # cross_sectional：调 factor_evaluator（8 维体检）——通过子进程避免污染主进程
    import subprocess
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(BASE / "validation" / "factor_evaluator.py"),
         "--factors", f["name"]],
        capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
    out = (r.stdout or "")[-1500:]
    score = parse_evaluator_score(out)
    detail = {"stdout_tail": out, "score_parsed": score, "returncode": r.returncode}

    if score is None:
        # ★无有效分数时**不流转状态**：原实现在此把因子判成 retired，
        #   于是「评估器输出格式一变 / 因子无评估通路」= 好因子被静默淘汰。
        #   正确做法：保持原状态与原有分数，只记录本次尝试。
        cur = reg.get(f["name"]) or {}
        detail["note"] = "评估未产出分数 → 状态与分数保持原样（不判 retired）"
        reg.update_score(f["name"], cur.get("score"), status=None, detail=detail)
        return detail

    status = "active" if score >= 65 else ("candidate" if score >= 40 else "retired")
    reg.update_score(f["name"], score, status=status, detail=detail)
    return detail


def evaluate_pool(reg: FactorRegistry, only_candidate=False) -> list:
    """测评全部 候选 + active（巡检）因子；★locked 因子（人工裁决）跳过自动评估"""
    todo = reg.list_factors(status="candidate") if only_candidate else \
        [f for f in reg.list_factors() if f["status"] in ("candidate", "active", "monitoring")]
    skipped = [f["name"] for f in todo if f.get("locked")]
    todo = [f for f in todo if not f.get("locked")]
    print(f"待评估因子 {len(todo)} 个: {[f['name'] for f in todo]}" +
          (f"（跳过人工锁定 {len(skipped)} 个: {skipped}）" if skipped else ""))
    results = []
    for f in todo:
        print(f"\n=== 评估 {f['name']} ({f['kind']}) ===")
        try:
            res = evaluate_factor(reg, f)
            if f["kind"] == "time_series":
                print(f"  得分 {res['score']} → {res['status']} | IC_h1={res['ic_h1']} t={res['t_h1']} gap={res['gap_hl']}")
            results.append(res)
        except Exception as e:
            print(f"  评估失败: {e}")
    return results


# ---------- 报：报告 ----------
def write_report(reg: FactorRegistry, evaluate_results: list = None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    factors = reg.list_factors()
    stats = reg.stats()
    lines = [f"# 因子池报告 · {datetime.now():%Y-%m-%d %H:%M:%S}", "",
             f"**池规模**：{stats['total']} 个因子（{stats['by_kind']}）",
             f"**状态分布**：{stats['by_status']}", "",
             "## 活跃因子（active，可接入策略）", ""]
    for f in [x for x in factors if x["status"] == "active"]:
        lines.append(f"- **{f['name']}** [{f['kind']}] {f['source']} — score {f['score']}")
    lines += ["", "## 候选因子（candidate，观察中）", ""]
    for f in [x for x in factors if x["status"] == "candidate"]:
        lines.append(f"- {f['name']} [{f['kind']}] {f['source']} — score {f['score']}")
    lines += ["", "## 淘汰因子（retired）", ""]
    for f in [x for x in factors if x["status"] == "retired"]:
        lines.append(f"- {f['name']} — score {f['score']}（{f['note']}）")
    lines += ["", "---", "*因子池由 factors/pool/lifecycle.py 驱动（挖→抓→测→决→报），阈值见 params.yaml factor_pool 段*"]
    md_path = OUT_DIR / "factor_pool_report.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path = OUT_DIR / "factor_pool_report.json"
    json_path.write_text(json.dumps(
        {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats,
         "factors": [{k: f[k] for k in ("name", "family", "kind", "status", "score", "source", "note")} for f in factors]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {md_path}\n      {json_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="因子池生命周期")
    ap.add_argument("--register-epu", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    reg = FactorRegistry()
    if args.register_epu:
        register_epu_family(reg)
    if args.fetch:
        fetch_policy_data()
    if args.evaluate:
        evaluate_pool(reg)
    if args.report:
        write_report(reg)
    if args.status:
        for f in reg.list_factors():
            print(f"  [{f['status']:9}] {f['name']:<18} {f['kind']:<16} score={f['score']}")
