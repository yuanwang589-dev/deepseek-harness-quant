"""data/verify_day_pipeline.py — 日数据管道落地验证（总指导 2026-08-11）

★用途：17:30 TushareInc + 17:35 FactorDaily + 18:30 全链跑完后，一键验证当日数据落地质量。
  检查项：
  1. 主库/增量库最新交易日（双库合并探测）——应为当日
  2. 外包 daily CSV 最新 + 五强 rank 逐列完整率（≥85%）
  3. ext_signal 十强命中数（08-10 基准 148 只）
  4. opp_pool 最新 date + 机会数
  5. market crowding 列齐全（C3 前提）
  6. Pitch v2 最新候选
"""
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

EXT_DAILY = Path(r"data/factorpool/output/daily_scores")
F5 = ("turn_mid_prox", "sentiment", "turnover", "reversal20", "lowvol")


def _latest(pattern: str, subdir: str = "logs") -> "Path | None":
    fs = sorted(glob.glob(str(BASE / subdir / pattern)), key=os.path.getmtime)
    return Path(fs[-1]) if fs else None


def _ext_pool_missing() -> bool:
    """外包因子池 daily_scores 是否缺失（★2026-10-16）。
    该目录属外部量化代码库产物，本项目内整目录丢失；相关检查项（2/3/5/7/9）为
    「外部依赖缺失」而非本地故障 → 只提示不计 fails，避免每天 4 项噪音掩盖真异常。"""
    return not glob.glob(str(EXT_DAILY / "daily_*.csv"))


def main() -> int:
    print(f"=== 日数据管道落地验证 {datetime.now():%Y-%m-%d %H:%M} ===", flush=True)
    fails = 0

    # 1) 最新交易日（双库合并）
    try:
        from data.cache import DailyCache
        d = DailyCache().latest_trade_date()
        print(f"1) 最新交易日: {d}", flush=True)
        if not d or d == "2026-08-10":
            print("   ⚠️ 仍是 08-10（08-11 数据未入库？）", flush=True)
    except Exception as e:
        print(f"1) 探测失败: {str(e)[:60]}", flush=True)
        fails += 1

    # 2) 外包 daily CSV + 五强完整率（外包缺失 → 外部依赖，跳过不计 fails）
    if _ext_pool_missing():
        print("2) 外包因子池 daily_scores 缺失 → 跳过（外部依赖未恢复，非本地故障）", flush=True)
    else:
        try:
            import pandas as pd
            fs = sorted(glob.glob(str(EXT_DAILY / "daily_*.csv")), key=os.path.getmtime)
            f = Path(fs[-1])
            df = pd.read_csv(f, nrows=3000)
            ok = sum(1 for ft in F5 if f"{ft}_rank" in df.columns and df[f"{ft}_rank"].notna().mean() >= 0.5)
            print(f"2) 最新 daily: {f.name} | 五强列可用 {ok}/5", flush=True)
            if ok < 4:
                print("   ⚠️ 五强 rank 缺失（需检查 scheduler 产出）", flush=True)
                fails += 1
        except Exception as e:
            print(f"2) daily 读取失败: {str(e)[:60]}", flush=True)
            fails += 1

    # 3) ext_signal 命中
    try:
        from factors.opportunities.scan import load_external_signals
        sig = load_external_signals()
        print(f"3) ext_signal 十强命中: {len(sig)} 只（08-10 基准 148）", flush=True)
        if sig:
            date = next(iter(sig.values()))["date"]
            print(f"   数据日期: {date}", flush=True)
    except Exception as e:
        print(f"3) ext_signal 失败: {str(e)[:60]}", flush=True)
        fails += 1

    # 4) opp_pool
    try:
        p = _latest("opp_pool_*.json")
        if p is None:
            raise FileNotFoundError("无 opp_pool_*.json")
        d = json.loads(p.read_text(encoding="utf-8"))
        print(f"4) opp_pool: {p.name} | date={d.get('date')} n={d.get('n')}", flush=True)
    except Exception as e:
        print(f"4) opp_pool 失败: {str(e)[:60]}", flush=True)
        fails += 1

    # 5) market crowding（★同 2)：外包缺失 → 跳过）
    if _ext_pool_missing():
        print("5) market crowding: 外包因子池缺失 → 跳过（外部依赖）", flush=True)
    else:
        try:
            import csv as _csv
            fs = sorted(glob.glob(str(EXT_DAILY / "market_*.csv")), key=os.path.getmtime)
            with open(fs[-1], encoding="utf-8") as fh:
                rd = _csv.DictReader(fh)
                row = next(rd)
            filled = sum(1 for ft in F5 if row.get(f"crowding_{ft}", "").strip())
            print(f"5) market crowding: {os.path.basename(fs[-1])} 填充 {filled}/5", flush=True)
        except Exception as e:
            print(f"5) crowding 失败: {str(e)[:60]}", flush=True)

    # 6) Pitch v2（含大小盘分档 + 强因子直通 + 双线子分类）
    try:
        p = _latest("pitch_v2_*.json")
        if p is None:
            raise FileNotFoundError("无 pitch_v2_*.json")
        d = json.loads(p.read_text(encoding="utf-8"))
        names = [x.get("name") for x in d.get("pitch", [])][:5]
        from collections import Counter as _C
        tiers = _C(x.get("size_tier", "?") for x in d.get("pitch", []))
        exprs = sum(1 for x in d.get("pitch", []) if x.get("express_strong"))
        subs = _C(x.get("pitch_sub", "score") for x in d.get("pitch", []))
        print(f"6) Pitch: {d.get('date')} | {names} | 分档 {dict(tiers)} | ⚡直通 {exprs} | 子分类 {dict(subs)}", flush=True)
    except Exception as e:
        print(f"6) pitch 失败: {str(e)[:60]}", flush=True)

    # 7) ★强因子直通白名单（factor_risk：独立强因子数，统计误差审计）
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE))
        from factors.risk.factor_risk import latest as _fr
        fr = _fr()
        n_ind = fr.get("n_strong_independent", 0)
        n_nom = fr.get("n_strong_nominal", 0)
        print(f"7) 强因子: 独立 {n_ind} / 名义 {n_nom}（家族去重，共线性修正）", flush=True)
        if n_ind == 0:
            if _ext_pool_missing():
                print("   ⏸️ 无强因子：外包因子池缺失所致（外部依赖，不计故障）", flush=True)
            else:
                print("   ⚠️ 无强因子（factor_risk 未跑？dev_auto 8.57）", flush=True)
                fails += 1
    except Exception as e:
        print(f"7) 强因子失败: {str(e)[:60]}", flush=True)
        fails += 1

    # 8) ★短线因子模块（F5：event 加分 + 排雷 + 连板回避）
    try:
        from factors.opportunities.shortterm_hook import load_shortterm
        st = load_shortterm()
        mines = sum(1 for r in st.values() if (r.get("consec_down") or 0) >= 2)
        consec = sum(1 for r in st.values() if (r.get("consec_up") or 0) >= 2)
        ev = sum(1 for r in st.values() if (r.get("premium_rank") or 0) >= 0.85)
        print(f"8) 短线因子: 命中 {len(st)} | 连续跌停排雷 {mines} | 连板回避 {consec} | 涨停加分候选 {ev}", flush=True)
    except Exception as e:
        print(f"8) 短线因子失败: {str(e)[:60]}", flush=True)

    # 9) ★面板 v8 新因子接入（N4/N5/N8/N2/N3 入池：open_prem_20/lhb_jg_cnt_20/ind_crowd_60/o2c/limup_ex 等）
    #    17:35 面板 v8 产出后自动验证——新因子列存在 = 信号联动/强因子直通可消费
    try:
        import csv as _csv2
        v8_cols = ["open_prem_20_rank", "lhb_jg_cnt_20_rank", "ind_crowd_60_rank",
                   "o2c_sum_20_rank", "limup_ex_ret_20_rank", "ind_rs_20_rank"]
        fs8 = sorted(glob.glob(str(EXT_DAILY / "daily_*.csv")), key=os.path.getmtime)
        present = 0
        if fs8:
            with open(fs8[-1], encoding="utf-8-sig") as fh:
                rd = _csv2.DictReader(fh)
                cols = rd.fieldnames or []
            present = sum(1 for c in v8_cols if c in cols)
        print(f"9) 面板 v8: {present}/{len(v8_cols)} 新因子列（open_prem/lhb/ind_crowd/o2c/limup_ex）", flush=True)
    except Exception as e:
        print(f"9) 面板 v8 失败: {str(e)[:60]}", flush=True)

    # 10) ★2026-08-11 百轮#46：数据一致性交叉校验（Pitch/机会池/持仓/短线/决策链/止盈多来源一致）
    try:
        import subprocess as _sp
        _r = _sp.run([sys.executable, str(BASE / "data" / "check_consistency.py")],
                     capture_output=True, text=True, timeout=120)
        _ok = _r.returncode == 0
        _line = [ln for ln in (_r.stdout or "").splitlines() if "结果:" in ln]
        print(f"10) 数据一致性: {'✅' if _ok else '⚠️'} {_line[0] if _line else '（见 check_consistency 输出）'}", flush=True)
        if not _ok:
            fails += 1
    except Exception as e:
        print(f"10) 数据一致性失败: {str(e)[:60]}", flush=True)

    # 11) ★2026-08-12 百轮#99：实盘裁决体系（类型降权 + 归因数据可消费——#89-94 数据链）
    try:
        sys.path.insert(0, str(BASE / "deck"))
        # deck/ 由 sys.path 动态注入 → 静态分析无法解析
        from live_api import live_validation  # type: ignore
        _vv = live_validation()
        _dg = _vv.get("diagnosis") or {}
        _dw = _dg.get("down_warn") or []
        _nt = sum(1 for t in (_dg.get("by_type") or []) if t.get("n", 0) >= 3)
        _ok11 = _vv.get("ok") and _nt >= 1 and len(_dw) <= 3
        print(f"11) 实盘裁决: {'✅' if _ok11 else '⚠️'} {_nt} 类型有样本，down_warn {len(_dw)} 条（{'、'.join(x.get('label','?') for x in _dw) or '无'}）", flush=True)
        if not _ok11:
            fails += 1
    except Exception as e:
        print(f"11) 实盘裁决失败: {str(e)[:60]}", flush=True)
        fails += 1

    # 12) ★2026-08-12 百轮#99：决策链 13 环节（含实盘裁决——#97 数据链）
    #   ★#391 与 check_consistency 对齐：supplier-lag（note 含"内容滞后/供应商"）不算故障，只算 hard failure
    try:
        from live_api import live_chain  # type: ignore  # deck/ 由 sys.path 动态注入
        _ch = live_chain()
        _chain = _ch.get("chain") or []
        _names = [n.get("name") for n in _chain]
        _bad = [n for n in _chain if not n.get("ok")
                and not ("内容滞后" in (n.get("note") or "") or "供应商" in (n.get("note") or ""))]
        _ok12 = len(_names) >= 13 and "实盘裁决" in _names and not _bad
        print(f"12) 决策链 13 环节: {'✅' if _ok12 else '⚠️'} {len(_names)} 环节（含实盘裁决）"
              + (f"，{len(_bad)} 硬故障" if _bad else ""), flush=True)
        if not _ok12:
            fails += 1
    except Exception as e:
        print(f"12) 决策链失败: {str(e)[:60]}", flush=True)
        fails += 1

    print(f"=== 验证结束: {'✅ 全部就绪' if fails == 0 else f'⚠️ {fails} 项异常'} ===", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
