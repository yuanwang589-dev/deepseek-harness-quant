# -*- coding: utf-8 -*-
"""factors/risk/factor_risk.py — 因子风险评估 + 强因子判定（2026-08-11 用户指示）

★用户指示：因子多了以后，强因子应有直接进入 Deck 的特殊权限；
  交叉确认/多因子有局限性与统计学误差可能 → 需因子风险评估。

实现（主系统侧，消费外包 E7 health + manifest）：
  1. 读最新 health CSV（67 因子）→ 家族聚类（词根）→ 共线风险
  2. 强因子判定（2.1 协作确认书标准，外包确认后可调）：
     有效 + ICIR120≥0.5 + t≥4 + IC胜率≥60% + crowding<90 + 家族代表
  3. 每因子风险档案 → 输出 output/factor_risk_{ts}.json（时间戳文件名）
  4. 供 scan.py 直通分支消费：强因子清单（家族代表）

数据源：量化代码库思路库/因子池/output/health/health_*.csv（glob 取最新）
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
import csv
import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # → deepseek-harness-quant
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

HEALTH_DIR = Path(r"data/factorpool/output/health")

# ★2026-08-14 F1 强因子参数表（因子池现场重算：n_days/胜率/拥挤代理）——health CSV 无这些列
try:
    from factors.risk.strong_factor_table import STRONG_TABLE, MIN_N_DAYS, MIN_WIN_RATE, MAX_CROWD_PROXY, EXPRESS_BANNED
except Exception:
    STRONG_TABLE, MIN_N_DAYS, MIN_WIN_RATE, MAX_CROWD_PROXY = {}, 0, 0.0, 999.0

# 家族词根（统计误差审计：同族 = 同一信号重复计数）
FAMILY_ROOTS = ["limit_up", "limit_down", "consec", "alpha", "shebao", "sue", "o2c",
                "hi_vol", "turn", "turnover", "sentiment", "reversal", "lowvol",
                "crowding", "event", "near_high", "vol_ratio", "flag", "score"]

# ★2026-08-14 F3 实证家族字典（因子池交付：27 名义强因子 → 9 实证家族，3 年 727 截面相关 |ρ|≥0.5 归并）
#   —— 替代词根匹配（词根是启发式，实证家族是数据驱动；F3 报告见 combo_reports/F3_强因子家族归并与FDR校正）
F3_FAMILIES = {
    "F1 开盘溢价族": ["open_prem_20", "gap_ret_20"],                    # 0.995 同一信号
    "F2 涨停反转族": ["limup_ex_5", "limit_up_cnt_5d", "limup_ex_ret_20",
                    "limit_up_turn", "consec_limit_up", "limit_up_flag", "turn_std20",
                    "vol_contract", "vol_curvature", "turnover", "sentiment", "max_ret20"],
    "F3 龙虎榜族": ["lhb_jg_cnt_20"],
    "F4 社保族": ["shebao_chg_pct", "shebao_chg"],                      # 0.826
    "F5 Alpha003族": ["alpha003", "alpha006"],                          # 0.68
    "F6 Alpha015族": ["alpha015", "alpha050", "alpha044"],              # 0.504/0.537
    "F7 日内反转族": ["o2c_sum_20"],
    "F8 跌停排雷族": ["consec_limit_down", "limit_down_flag"],          # 1.00 完全冗余
    "F9 开盘缺口族": ["open_gap"],
    # ★2026-08-14 chip_concentration 组合层全库验证通过（+7.65pp/0 负年/OOS +5.11pp，与 amihud 同级）
    #   ——独立成族（原 F2 族成员，相关 0.55 链式归并；组合层证据 > 截面相关，28 步铁律）进直通白名单
    #   （拥挤 86 偏热 + F4 部分暴露 0.574：统一失效监控滚动 12 月超额 <0 降权）
    "F10 chip_concentration族": ["chip_concentration"],
}
# 因子 → F3 实证家族 反查表
_F3_MAP = {}
for _fam, _members in F3_FAMILIES.items():
    for _m in _members:
        _F3_MAP[_m] = _fam

# ★2026-08-14 F4 风格暴露（因子池中性化复核）：双中性后归零 = 风格暴露为主，非真实选股 alpha
#   ——shebao_chg_pct 1.43→0.07 / shebao_chg 1.39→0.22 / max_ret20 0.56→0.10（F4 CSV）
#   这些因子不进强因子直通白名单（不配直通特权），风险档案标记 style_exposed
STYLE_EXPOSED = {
    "shebao_chg_pct": {"neutral_icir": 0.07, "note": "社保加减仓=风格暴露为主（F4 双中性归零），建议限定域/降权"},
    "shebao_chg": {"neutral_icir": 0.22, "note": "同上"},
    "max_ret20": {"neutral_icir": 0.10, "note": "彩票因子双中性后大幅衰减，建议降权"},
}


def _family_of(name: str) -> str:
    """因子名 → 家族（★2026-08-14：优先 F3 实证家族字典，缺失回落词根）
    F3 实证（9 家族）是数据驱动归并（|ρ|≥0.5），比词根启发式更准：
      - open_prem_20×gap_ret_20=0.995 → 同族（词根会分成"开盘/缺口"两族，错误）
      - consec_limit_down×limit_down_flag=1.00 → 同族（词根只归跌停族）
      - chip_concentration 并入涨停反转族（词根会分到"筹码"）"""
    if name in _F3_MAP:
        return _F3_MAP[name]
    n = name
    if n.startswith(("limup", "limit_up", "consec_limit")):
        return "涨停族"
    if n.startswith("limit_down"):
        return "跌停族"
    if n.startswith("o2c") or n.startswith("hi_vol"):
        return "日内反转族"
    for root, fam in (("alpha", "Alpha101族"), ("shebao", "社保族"), ("sue", "基本面族"),
                      ("turn", "换手族"), ("sentiment", "情绪族"), ("reversal", "反转族"),
                      ("lowvol", "低波族"), ("crowding", "拥挤族"), ("consec", "连板族"),
                      ("event", "事件族"), ("bp", "估值族"), ("vol", "量比族"),
                      ("max_ret", "动量族"), ("near", "位置族"), ("score", "复合评分族"),
                      ("flag", "风险标记族")):
        if n.startswith(root):
            return fam
    return n.split("_")[0][:6]


def load_health() -> list:
    files = sorted(glob.glob(str(HEALTH_DIR / "health_*.csv")), key=lambda p: p)
    if not files:
        return []
    # ★2026-08-13 #250：外包 --only 增量重跑会覆盖当日 health_YYYY-MM-DD.csv（只剩少量因子行）
    #   → 取「因子数最多」的全量 CSV（按文件名取最后一个会读到 5 行残缺版 → 强因子判定全失）
    best, best_n = [], -1
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) > best_n:
                best, best_n = rows, len(rows)
        except Exception:
            continue
    return best


def _num(r, k, default=None):
    v = r.get(k)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build() -> Path:
    rows = load_health()
    if not rows:
        print("factor_risk: 无 health 数据")
        return None

    # 家族聚类
    fams = {}
    for r in rows:
        f = _family_of(r["factor"])
        fams.setdefault(f, []).append(r["factor"])

    # 强因子判定（协作确认书 2.1：有效 + ICIR120≥0.5 + t≥4 + 胜率≥60% + crowding<90 + 家族代表）
    # ★2026-08-12 C-12i：排除 fundamental_lowfreq（低频质量因子只宜 quality_gap/排雷，C-4/C-12 铁律）
    #   ★#384 方向已修：直通改 rank≥0.90（好因子 rank 大），原 rank≤0.10 是反向选股（sue 低分=业绩差/bp 低=估值贵）
    lowfreq = set()
    try:
        import glob as _gl, json as _js, os as _os
        # ★glob 返回 str（无 .stat）——须 os.path.getmtime（主系统百轮老坑）
        _mfs = sorted(_gl.glob(r"data/factorpool/output/factor_manifest_*.json"),
                      key=_os.path.getmtime)
        if _mfs:
            _md = _js.load(open(_mfs[-1], encoding="utf-8"))
            lowfreq = {x.get("code") for x in _md.get("factors", [])
                       if x.get("category") == "fundamental_lowfreq"}
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/risk/factor_risk.py:153 build")
    strong_all = []
    # ★2026-08-14 组合层负超额实证（因子池 12:15 留言）：lhb_jg_cnt_20 截面 ICIR 1.516 全库前三，
    #   但组合层 -5.63pp / 6/7 负年（中小盘域 Top10% 市值中性、季度调仓、含成本）——
    #   "截面强≠组合强"（涨停族同类：事件因子只宜事件确认，不宜独立买入）。
    #   → lhb 降级：移出直通白名单（strong），保留风险档案展示 + 机会池事件确认加分路径
    #     （apply_strong_hits 的 +3 加分不再由 lhb 触发；load_strong_hits 直通不再含 lhb 家族）
    #   名单定义在 strong_factor_table.EXPRESS_BANNED（单一事实源，恢复条件注释见彼处）
    for r in rows:
        if r["factor"] in lowfreq:
            continue
        if r["factor"] in EXPRESS_BANNED:
            continue
        # ★2026-08-14 F4 风格暴露因子不进直通（双中性归零 = 非真实 alpha，不配直通特权）
        #   shebao_chg_pct/shebao_chg/max_ret20（F4 复核：双中性后 0.07/0.22/0.10）
        if r["factor"] in STYLE_EXPOSED:
            continue
        st = r.get("status", "")
        if "有效" not in st:
            continue
        icir = _num(r, "icir120")
        t = _num(r, "t120")
        win = _num(r, "ic_win_rate")
        crowd = _num(r, "crowding")
        # ★2026-08-14 F1 最终口径：health CSV 缺 ic_win_rate/n_days/拥挤列 →
        #   用 F1 参数表（strong_factor_table.py，研究员现场重算）做最终门槛
        _f1 = STRONG_TABLE.get(r["factor"])
        if _f1:
            if _f1["n_days"] < MIN_N_DAYS:
                continue                                # n_days≥250 防早产
            if _f1["win_rate"] < MIN_WIN_RATE:
                continue                                # IC 胜率≥60%
            if _f1["style_exposed"] == "true":
                continue                                # F4 双中性归零=风格暴露
            win = _f1["win_rate"]
            # ★2026-08-14 拥挤动态化（因子池 15:45 落地）：health CSV 已有 crowding 列
            #   （近 60 日 |IC| 分位，每日自动刷新）→ 优先动态值；静态表 crowd_proxy 仅兜底
            #   （旧 health 无列时用 F1 静态；有列则每日自动更新，chip 86.0 与 F1 表 85.8 互证）
            if crowd is None:
                crowd = _f1["crowd_proxy"]
        if icir is None or icir < 0.5 or t is None or t < 4:
            continue
        if win is not None and win < 0.60:
            continue
        if crowd is not None and crowd >= 90:
            continue
        strong_all.append({"factor": r["factor"], "family": _family_of(r["factor"]),
                           "icir120": round(icir, 3), "t120": t, "ic_win_rate": win,
                           "crowding": crowd, "status": st})

    # 家族代表：同族只取 ICIR 最强 1 个（共线性去重）
    fam_best = {}
    for s in strong_all:
        cur = fam_best.get(s["family"])
        if cur is None or s["icir120"] > cur["icir120"]:
            fam_best[s["family"]] = s
    strong = list(fam_best.values())
    strong.sort(key=lambda x: -x["icir120"])

    # 风险档案
    archive = []
    for r in rows:
        fam = _family_of(r["factor"])
        icir = _num(r, "icir120")
        se = STYLE_EXPOSED.get(r["factor"])
        archive.append({
            "factor": r["factor"], "family": fam,
            "status": r.get("status", ""),
            "icir120": icir, "t120": _num(r, "t120"),
            "ic_win_rate": _num(r, "ic_win_rate"),
            "half_life": _num(r, "half_life_months"),
            "crowding": _num(r, "crowding"),
            "last_date": r.get("last_date", ""),
            # ★2026-08-14 F4 风格暴露（双中性归零标记——区分"真 alpha" vs "风格暴露"）
            "style_exposed": bool(se),
            "style_exposed_note": (se or {}).get("note", "") if se else "",
            "neutral_icir": (se or {}).get("neutral_icir") if se else None,
            # 共线风险：家族成员数（>1 说明同族重复计数）
            "family_size": len(fams.get(fam, [])),
            "family_members": fams.get(fam, [])[:6],
            "is_strong": any(s["factor"] == r["factor"] for s in strong),
            "is_family_rep": any(s["factor"] == r["factor"] for s in strong),
        })

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ★2026-08-14 低换手代理衰减监控（研究员 17:10 深挖）：turn_mid_prox 高位回落衰减通道
    #   （ICIR 3.01→1.53 未失效）——pv 双 rank「低换手」约束依赖它；跌破阈值需换代理
    #   （turnover/turn_std20，F3 同族）。监控输出供决策链/门户展示。
    _lth = None
    try:
        for _r in rows:
            if _r.get("factor") == "turn_mid_prox":
                _lth = {
                    "factor": "turn_mid_prox",
                    "status": _r.get("status", ""),
                    "icir120": _num(_r, "icir120"),
                    "ic60_short": _num(_r, "ic60_short"),
                    "t120": _num(_r, "t120"),
                    # 研究员 17:10 阈值：ICIR120<0.5 或近 60 日 IC 转负 → 低换手端换代理
                    "alert": bool(_num(_r, "icir120") is not None and _num(_r, "icir120") < 0.5)
                             or bool(_num(_r, "ic60_short") is not None and _num(_r, "ic60_short") < 0),
                    "replace_with": "turnover / turn_std20（F3 同族）" if (_num(_r, "icir120") is not None and _num(_r, "icir120") < 0.5) or (_num(_r, "ic60_short") is not None and _num(_r, "ic60_short") < 0) else "",
                }
                break
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "factors/risk/factor_risk.py:258 build")
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_health": files[-1] if (files := sorted(glob.glob(str(HEALTH_DIR / "health_*.csv")))) else "",
        "n_factors": len(rows),
        "n_strong_nominal": len(strong_all),     # 名义强因子（含同族重复）
        "n_strong_independent": len(strong),      # 独立强因子（家族代表）
        "family_count": len(fams),
        "families": {k: len(v) for k, v in sorted(fams.items(), key=lambda x: -len(x[1]))},
        "strong": strong,                          # 强因子清单（直通白名单）
        "archive": archive,                        # 全因子风险档案
        "low_turnover_monitor": _lth,              # ★2026-08-14 低换手代理衰减监控（turn_mid_prox）
        "note": "统计误差审计：24 名义强因子 → 家族去重后独立信号约 6-8 个（涨停族5/社保族2/Alpha族4 高相关）",
    }
    p = BASE / "output" / f"factor_risk_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"factor_risk: {p.name} | {len(rows)} 因子 → 名义强因子 {len(strong_all)} / 独立强因子 {len(strong)}")
    for s in strong:
        # ★2026-08-14 去 emoji：GBK 控制台 print ⚡ 会 UnicodeEncodeError 中断脚本
        print(f"  [strong] {s['factor']}: ICIR120={s['icir120']} t={s['t120']:.0f} | {s['family']}")
    return p


def latest() -> dict:
    """读最新 factor_risk 文件（供 scan 直通分支）"""
    files = sorted(glob.glob(str(BASE / "output" / "factor_risk_*.json")))
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception as _dsh_exc:
            _dsh_swallow(_dsh_exc, "factors/risk/factor_risk.py:288 latest")
    return {}


if __name__ == "__main__":
    build()
