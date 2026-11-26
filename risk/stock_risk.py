# -*- coding: utf-8 -*-
"""risk/stock_risk.py — 个股风控层（Pitch 前置风险筛查 · 2026-08-08 固化）

★定位（用户定调）：Pitch 前必须过风控——"pitch 有什么风险（财务造假、管理层不诚实等）"
  数据审计（data_audit.py）管"数据本身可不可信"；本模块管"标的公司有没有雷"。

设计借鉴（开源调研 2026-08-08）：
  - FraudWatch（A股舞弊检测）：Beneish M-Score 8 指标 + 现金流利润剪刀差 + 营收质量
  - edgarito（SEC红警）：25+ 红旗检查 5 大类（资产负债表/现金流/盈利质量/成长/估值）
  - financial-risk-screener：盈余质量 + 营运资本 + 0-100 风险评分

红旗清单（v1.0，基于现有数据库可算项）：
  R1 现金流利润剪刀差：cfo_to_np < 0.5 或 > 2.0（利润没有现金支撑 / 异常高）
  R2 高负债：liability_to_asset > 70%（偿债压力）
  R3 流动比率过低：current_ratio < 1.0（短期偿付危机）
  R4 盈利质量差：ROE>0 但 cfo_to_np 为负（账面利润、无现金流）
  R5 毛利率异常：gp_margin > 95%（造假高发区，如扇贝/菌菇类）
  R6 业绩预告缺席/大幅下修（数据未就绪时跳过）
  R7 审计意见非标准（fina_audit 数据未入库时跳过，接口已解锁）
  R8 股权质押/大股东减持（Tushare 数据接入后启用）

输出：0-100 风控分（越高越危险）+ 红旗明细 → Pitch 层强制检查
  PASS（<40）/ WATCH（40-60，需人工复核）/ BLOCK（>60，禁止进 Pitch）

用法：
  python risk/stock_risk.py --code 600519.SH      # 单只
  python risk/stock_risk.py --scan                 # 全市场批量 → risk/stock_risk_map.json
  python risk/stock_risk.py --status
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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

QD_DB = r"data\cache\finance_quality.db"
FIN_DB = r"data\cache\finance.db"
OUT = BASE / "logs" / "stock_risk_map_v2.json"   # v2（接入 Beneish 完整版 2026-08-09；v1 文件被外包占用锁）
# ★2026-08-11 写保护免疫：固定名多次写被锁（实测 scan_all PermissionError）→ 每次写时间戳文件；
#   读取端（deck_server /api/risk glob、portfolio._risk_map）取最新；固定名仅首写兼容
import glob as _gl_risk
OUT_TS = BASE / "logs" / f"stock_risk_map_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

# 红旗阈值（改这里不改代码）
RED_FLAGS = {
    "r1_cfo_np_low":   {"col": "cfo_to_np", "op": "lt", "val": 0.5, "weight": 20,
                        "desc": "现金流/净利 < 0.5：利润缺现金支撑"},
    "r1_cfo_np_high":  {"col": "cfo_to_np", "op": "gt", "val": 2.0, "weight": 10,
                        "desc": "现金流/净利 > 2.0：异常高（可能洗钱/预收堆积）"},
    "r2_high_liab":    {"col": "liability_to_asset", "op": "gt", "val": 0.70, "weight": 15,
                        "desc": "资产负债率 > 70%：偿债压力大"},
    "r3_low_current":  {"col": "current_ratio", "op": "lt", "val": 1.0, "weight": 15,
                        "desc": "流动比率 < 1.0：短期偿付危机"},
    "r4_roe_no_cfo":   {"col": "cfo_to_np", "op": "lt", "val": 0.0, "weight": 25,
                        "desc": "ROE>0 但经营现金流为负：账面利润无现金（★最重红旗）"},
    "r5_high_gp":      {"col": "gp_margin", "op": "gt", "val": 0.95, "weight": 15,
                        "desc": "毛利率 > 95%：造假高发区"},
    # R6 = Beneish M-Score（★权重最高，外包 AI 2026-08-08 接入，见 risk/beneish.py）
    #   在 _check_row 中按 m_level 触发（不在 RED_FLAGS 里走 col 比较逻辑）
    "r6_beneish_high":  {"weight": 30, "desc": "Beneish M > -1.78：财务操纵高嫌疑（★最重红旗）"},
    "r6_beneish_watch": {"weight": 15, "desc": "Beneish M ∈ (-2.22, -1.78]：财务操纵中嫌疑"},
}


def _conn_qd():
    return sqlite3.connect(QD_DB)


def latest_period(con) -> str:
    """取"最新且覆盖≥1000 只"的报告期（2026-08-09 修复：原逻辑在最新期<1000 时
    回退到"覆盖最全期"=2024-06-30，但 2026-03-31 已覆盖 1767 只 → 直接取最新覆盖期，时效性+2 年）"""
    r = con.execute(
        "SELECT period FROM quality WHERE period < '2026-07-01' "
        "GROUP BY period HAVING COUNT(*) >= 1000 ORDER BY period DESC LIMIT 1").fetchone()
    if r:
        return r[0]
    r = con.execute(
        "SELECT period, COUNT(*) c FROM quality WHERE period < '2026-07-01' "
        "GROUP BY period ORDER BY c DESC LIMIT 1").fetchone()
    return r[0] if r else None


def _check_row(code: str, period: str, roe, gp, cr, liab, cfo, m_level: str = None) -> dict:
    """单行数据 → 风控结果（无 IO，供批量与单只共用）
    m_level: Beneish M-Score 等级（HIGH/WATCH/LOW/None），R6 红旗输入
    """
    if liab is not None and liab > 1.5 or (gp is not None and abs(gp) > 1.0):
        return {"code": code, "score": None, "level": "NO_DATA",
                "flags": [{"id": "dirty_data", "desc": "质量数据口径异常（待主服务器恢复重拉）",
                           "weight": 0, "raw": {"liab": liab, "gp": gp}}],
                "period": period}
    flags = []
    score = 0
    for fid, spec in RED_FLAGS.items():
        # R6 走 m_level 判断（Beneish 结果），其余走字段比较
        if fid.startswith("r6_"):
            if m_level == "HIGH" and fid == "r6_beneish_high":
                flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                              "value": m_level, "threshold": "-1.78"})
                score += spec["weight"]
            elif m_level == "WATCH" and fid == "r6_beneish_watch":
                flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                              "value": m_level, "threshold": "(-2.22, -1.78]"})
                score += spec["weight"]
            continue
        v = {"cfo_to_np": cfo, "liability_to_asset": liab,
             "current_ratio": cr, "gp_margin": gp}.get(spec["col"])
        if v is None:
            continue
        hit = (v < spec["val"]) if spec["op"] == "lt" else (v > spec["val"])
        if hit:
            flags.append({"id": fid, "desc": spec["desc"], "weight": spec["weight"],
                          "value": round(v, 4), "threshold": spec["val"]})
            score += spec["weight"]
    if roe and roe <= 0:
        score = max(score - 25, 0)
        flags = [f for f in flags if f["id"] != "r4_roe_no_cfo"]
    score = min(score, 100)
    level = "PASS" if score < 40 else ("WATCH" if score < 60 else "BLOCK")
    return {"code": code, "score": score, "level": level, "flags": flags, "period": period}


def check_one(code: str) -> dict:
    """单只股票风控检查 → {code, score, level, flags[], period}"""
    con = _conn_qd()
    period = latest_period(con)
    row = con.execute(
        "SELECT roe_avg, gp_margin, current_ratio, liability_to_asset, cfo_to_np "
        "FROM quality WHERE code=? AND period=?",
        (code, period)).fetchone()
    con.close()
    if not row:
        return {"code": code, "score": None, "level": "NO_DATA",
                "flags": [{"id": "no_data", "desc": "质量数据未覆盖", "weight": 0}],
                "period": period}
    # R6：Beneish M-Score（★2026-08-09 升级：优先完整版 beneish_full（本地三表 8 指标），
    #   失败/无数据时回落降级版 risk.beneish；两者均无 → None 跳过 R6）
    m_level = None
    try:
        from risk.beneish_full import check_one as beneish_full_check
        br = beneish_full_check(code)
        m_level = br.get("level") if br and br.get("m_score") is not None else None
    except Exception:
        m_level = None
    if m_level is None:
        try:
            from risk.beneish import check_one as beneish_check
            br = beneish_check(code)
            m_level = br.get("level") if br and br.get("m_score") is not None else None
        except Exception:
            m_level = None
    return _check_row(code, period, *row, m_level=m_level)


def scan_all(limit: int = None) -> dict:
    """全市场批量（单连接一次取全部，高效稳定）"""
    con = _conn_qd()
    period = latest_period(con)
    rows = con.execute(
        "SELECT code, roe_avg, gp_margin, current_ratio, liability_to_asset, cfo_to_np "
        "FROM quality WHERE period=?", (period,)).fetchall()
    con.close()
    # R6：Beneish 批量映射（★优先完整版 beneish_full 全量报告，回落降级版 compute_map）
    m_map = {}
    try:
        import json as _json
        br_full = _json.loads((BASE / "logs" / "beneish_report_full.json").read_text(encoding="utf-8"))
        m_map = {x["code"]: x.get("level") for x in br_full.get("results", [])
                 if x.get("m_score") is not None}
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/stock_risk.py:174 scan_all")
    if not m_map:
        try:
            from risk.beneish import compute_map
            m_map = {k: v.get("level") for k, v in compute_map().items() if v.get("m_score") is not None}
        except Exception:
            m_map = {}
    if limit:
        rows = rows[:limit]
    results = [_check_row(code, period, roe, gp, cr, liab, cfo, m_level=m_map.get(code))
               for code, roe, gp, cr, liab, cfo in rows]
    results.sort(key=lambda x: -(x["score"] or -1))
    stats = {"total": len(results), "PASS": 0, "WATCH": 0, "BLOCK": 0, "NO_DATA": 0}
    for r in results:
        stats[r["level"]] = stats.get(r["level"], 0) + 1
    out = {"date": datetime.now().strftime("%Y-%m-%d"), "stats": stats, "results": results}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # ★2026-08-11 写保护免疫：时间戳文件主写（读取方 glob 取最新）；固定名失败不阻断（可能被锁）
    # ★2026-08-14 #429 幂等：quality 表未更新时本次结果与上次相同 → 跳过写时间戳文件
    #   （防 4h 链每轮重复写 2M 时间戳文件累积膨胀，#413 auction_strength 同款根治）
    _unchanged = False
    try:
        import glob as _g, os as _os
        _prev = sorted(_g.glob(str(OUT.parent / "stock_risk_map_*.json")),
                       key=_os.path.getmtime)
        _prev = [p for p in _prev if "v2" not in _os.path.basename(p)]  # 排除固定名主文件
        if _prev:
            with open(_prev[-1], encoding="utf-8") as _f:
                _old = json.load(_f)
            if _old.get("stats") == stats and _old.get("date") == out["date"]:
                _unchanged = True
    except Exception:
        _unchanged = False
    if not _unchanged:
        try:
            OUT_TS.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "risk/stock_risk.py:211 scan_all")
    try:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "risk/stock_risk.py:215 scan_all")
    return out


def risk_level(code: str) -> str:
    """Pitch 层调用入口：返回 PASS/WATCH/BLOCK（NO_DATA 按 WATCH 处理）"""
    r = check_one(code)
    return "WATCH" if r["level"] == "NO_DATA" else r["level"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=str, default=None)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.code:
        r = check_one(args.code)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif args.scan:
        r = scan_all()
        print(f"全市场风控扫描: {r['stats']}")
        print("\nBLOCK 名单（禁止进 Pitch）Top 10:")
        for x in [x for x in r["results"] if x["level"] == "BLOCK"][:10]:
            print(f"  {x['code']} score={x['score']} flags={[f['id'] for f in x['flags']]}")
        print(f"\n高风险样本已存 {OUT}")
    elif args.status:
        con = _conn_qd()
        n = con.execute("SELECT COUNT(DISTINCT code) FROM quality").fetchone()[0]
        con.close()
        print(f"风控数据覆盖: {n} 只（quality 表最新期）")


if __name__ == "__main__":
    main()
