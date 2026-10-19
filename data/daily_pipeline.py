# -*- coding: utf-8 -*-
"""data/daily_pipeline.py — 每日数据增量一键管道（2026-08-09 · T-5 核心交付）

用户数据每日 16-18 点更新 → 本脚本一键完成全链：
  1. 分钟 7z 增量入库（ingest_minute_7z.py --dir 每日数据目录）
  2. 日线增量补拉（incremental_daily.py，baostock 标准格式 ★已修复双格式）
  3. 数据健康巡检（重复/缺失/脏数据 → 报告）
  4. 机会扫描 --pitch → Pitch v2 Deck
  5. 日志汇总

用法（Windows 任务计划 18:30 或手动）：
  python data/daily_pipeline.py
  python data/daily_pipeline.py --minute-dir "D:/.../每日数据" --skip-scan
"""
import argparse

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception:
    pass

import subprocess
import sys
import time
import glob
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PY = sys.executable

# ★2026-08-10 自动发现最新更新目录：用户每日数据更新会生成新目录（如 8.10日更新）
#   硬编码会漏新数据 → glob 匹配 "*日更新*" 取修改时间最新者
_UPDATE_ROOT = Path(r"data/minute/download/【2】2026单年A股分钟日频-持续更新到年底")


def resolve_minute_dir() -> str:
    """最新更新目录下的 每日数据/（7z 增量）"""
    cands = []
    if _UPDATE_ROOT.exists():
        for p in _UPDATE_ROOT.glob("*日更新*"):
            if p.is_dir():
                # 找 每日数据 子目录（可能嵌套 2026(1)/）
                sub = list(p.glob("*/每日数据")) + list(p.glob("每日数据"))
                for s in sub:
                    if s.is_dir() and list(s.glob("*.7z")):
                        cands.append((p.stat().st_mtime, s))
    if cands:
        cands.sort(reverse=True)
        return str(cands[0][1])
    return r"data/minute/download/【2】2026单年A股分钟日频-持续更新到年底/8.9日更新/2026(1)/每日数据"


DEFAULT_MINUTE_DIR = resolve_minute_dir()


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run_step(name, cmd, timeout=3600):
    log(f"▶ {name} ...")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip().splitlines()
        tail = out[-3:] if out else []
        log(f"  ✓ {name} 完成 ({time.time()-t0:.0f}s)" + (f" | {' | '.join(tail)}" if tail else ""))
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  ✗ {name} 超时")
        return False
    except Exception as e:
        log(f"  ✗ {name} 异常: {str(e)[:80]}")
        return False


def health_check():
    """数据健康巡检：重复行/最新日期/覆盖率"""
    import sqlite3
    con = sqlite3.connect("file:data/cache/bars.db?mode=ro&immutable=1", uri=True, timeout=3)
    dup = con.execute("SELECT COUNT(*) FROM (SELECT code,date,adjust,COUNT(*) c FROM daily_bar GROUP BY code,date,adjust HAVING c>1)").fetchone()[0]
    # ★#143 双库合并探测最新日（08-12 起增量写 bars_incr_*.db，单库会误报旧日）
    latest = con.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
    try:
        from pathlib import Path as _P
        for _p in sorted(_P("data/cache").glob("bars_incr_*.db"))[-3:]:
            try:
                _c = sqlite3.connect(f"file:{_p}?mode=ro&immutable=1", uri=True, timeout=3)
                _m = _c.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
                _c.close()
                if _m and _m > latest:
                    latest = _m
            except Exception:
                pass
    except Exception:
        pass
    bs = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date=? AND adjust='qfq' AND (code LIKE 'sh.%' OR code LIKE 'sz.%')", (latest,)).fetchone()[0]
    con.close()
    log(f"  [巡检] bars.db 重复={dup} | 最新={latest}（双库合并） | 残留baostock格式={bs}")
    if bs > 0:
        log(f"  ⚠️ 发现 {bs} 行 baostock 格式残留（需清理）")
    if dup > 0:
        log(f"  ⚠️ 发现 {dup} 行重复（需清理）")
    return dup == 0 and bs == 0


def _acquire_lock():
    """拿管道互斥锁 → (是否继续执行, 需在结束时释放的锁 Path 或 None)

    ★2026-10-18 修锁泄漏：原实现把 `_lock.write_text()` 与「周末跳过」写在同一函数里，
      周末 return、异常 return 两条路径都不 unlink 锁文件 → 残留锁会让后续 2 小时内的
      运行误判"另一管道在跑"而静默跳过（本机 10-18 16:02 那次就是这样留下锁的）。
      现改为 main() 用 try/finally 统一释放，任何退出路径都不会漏。
    """
    _lock = BASE / "data" / "logs" / "daily_pipeline.lock"
    _lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if _lock.exists() and (time.time() - _lock.stat().st_mtime) < 7200:
            log("⚠ 另一管道（dev_auto/手动更新）正在运行中（锁未过期）→ 本轮跳过，避免并发写冲突")
            return False, None
        _lock.write_text(str(time.time()), encoding="utf-8")
        return True, _lock
    except Exception as _e:
        # 写不了锁就无锁运行（保持原有"继续执行"语义），但不持有锁 → 结束时不得 unlink，
        # 否则会误删别人正在用的锁。
        log(f"锁检查异常（无锁继续执行）: {_e}")
        return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minute-dir", type=str, default=DEFAULT_MINUTE_DIR)
    ap.add_argument("--skip-scan", action="store_true", help="跳过机会扫描/Pitch")
    args = ap.parse_args()

    ok, _lock = _acquire_lock()
    if not ok:
        return
    try:
        _body(args)
    finally:
        if _lock is not None:
            try:
                _lock.unlink(missing_ok=True)
            except Exception:
                pass


def _body(args):
    # ★2026-08-14 非交易日跳过：周末无新行情，分钟/日线/因子池/扫描全幂等但纯浪费算力
    #   （尤其因子池评分补跑 30+ 分钟），直接跳过；周一自动恢复。节假日（工作日）幂等无害，不拦。
    import datetime as _dt
    if _dt.date.today().weekday() >= 5:
        log("非交易日（周末）→ 跳过每日管道（数据保持 bars.db 现有，下一交易日正常跑）")
        return
    log("=== 每日数据管道启动 ===")
    # 1) 分钟 7z 增量 → parquet（★2026-08-09 切换：minute.db 被系统锁 → 走 parquet 绕行方案，
    #    convert_7z_to_parquet.py 输出到 incr_parquet/，minute_reader fallback 读取；旧 ingest_minute_7z 保留待锁释放）
    run_step("分钟 7z 增量 → parquet", [PY, str(BASE / "data" / "convert_7z_to_parquet.py")], timeout=7200)
    # 2) 日线增量 ★2026-08-10 双通道：Tushare 主服务器优先（按日全市场 0.8s，quantdata888 实测可用），
    #    baostock 半挂起（单只 40s）降级为兜底；Tushare 盘后数据未出（17:00 前）时自动跳过不卡链
    try:
        # ★2026-08-14 超时 120→300s：incremental_daily_tushare 含 trade_cal+daily+adj_factor×2+daily_basic
        #   多次全市场调用 + 代理服务器 间歇超时重试，120s 常超 → 误走 baostock 慢兜底（几小时）
        _rt = subprocess.run(
            [PY, "-X", "utf8", str(BASE / "data" / "incremental_daily_tushare.py")],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
        _tushare_ok = _rt.returncode == 0 and ("已入库" in (_rt.stdout or ""))
        log(f"日线增量(Tushare): {(_rt.stdout or '').strip().splitlines()[-1] if (_rt.stdout or '').strip() else '无输出'}")
    except subprocess.TimeoutExpired:
        _tushare_ok = False
        log("  ⚠ Tushare 日线增量超时")
    except Exception:
        _tushare_ok = False
    if not _tushare_ok:
        # ★2026-08-14 移除 baostock 全市场兜底（原 timeout=7200 几小时，Tushare 偶发失败就卡住整链）
        #   → 失败即跳过，数据保持 bars.db 现有；17:30 TushareInc / 次日链会自动重试。
        #   baostock 仍用于历史补拉（backfill_hist_bars.py 独立任务），不在此处做每日兜底。
        log("  ⚠ Tushare 日线增量失败（已重试）→ 跳过当日增量（数据保持 bars.db 现有；17:30/次日链自动重试）")
    # 2.5) ★2026-08-14 沪深300 指数刷新（baostock 单指数，秒级）——保证红绿灯数据实时性（收盘后拿到当日指数）
    run_step("沪深300指数刷新", [PY, "-X", "utf8", str(BASE / "data" / "fetch_index_daily.py")], timeout=120)
    # 2.6) ★2026-08-14 择时红绿灯（均线金叉6/12，依赖 bars.db 沪深300 日线，纯本地毫秒级）
    run_step("择时红绿灯", [PY, "-X", "utf8", str(BASE / "data" / "traffic_light.py")], timeout=120)
    # 2.7) ★因子池评分补跑（2026-08-10 总指导：C5 连续验证保底）——
    #     17:30 scheduler 若因 bars 未到当日而跳过，18:30 日线拉完后此处补跑；
    #     scheduler 幂等（latest<=done 自动跳过），bars 未更新时无副作用。
    #     放在扫描之前 → scan 的 ext_signal 能消费当日评分
    _sched = Path(r"data/factorpool/core/scheduler.py")
    if _sched.exists():
        # ★2026-08-11 超时 1800→2700s：scheduler 全量补跑（60 因子全流程）可能 >30 分钟
        #   （08-10 事故中修复脚本单日截面重算即 18 分钟）；幂等跳过时几秒返回，无副作用
        run_step("因子池评分补跑（C5 保底）",
                 [PY, "-X", "utf8", str(_sched), "daily"], timeout=2700)
    else:
        log("  ⚠ 外包因子池 scheduler 不存在（路径变更？）→ 跳过补跑")
    # 3) 健康巡检
    health_check()
    # 3.5) ★竞价强度信号（T-3 交付物，2026-08-09 接入：分钟增量入库后自动算近 3 月信号存档）
    #     用途：① 数据积累（每日 9:35 前信号）② ★反信号防守（2026-08-10：scan.py 读最新信号，
    #     strength≥6 机会池减分——60 天时效内真实生效）；★2026-08-10 修复：默认参数只算 2019 年
    #     → 动态算近 3 个月（含 20 日预热）
    from datetime import datetime as _dt, timedelta as _td
    _now = _dt.now()
    _start = (_now - _td(days=95)).strftime("%Y-%m")
    _end = _now.strftime("%Y-%m")
    run_step("竞价强度信号", [PY, str(BASE / "factors" / "opportunities" / "auction_strength.py"),
                              "--start", _start, "--end", _end], timeout=1800)
    # 4) 机会扫描 + Pitch（非必需步骤，可跳过）
    if not args.skip_scan:
        run_step("机会扫描 --pitch", [PY, str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"], timeout=1800)
        run_step("Pitch v2 Deck", [PY, str(BASE / "factors" / "opportunities" / "pitch_v2.py")], timeout=1800)
        # ★2026-08-14 科技线收敛（Pitch 改进规格 v2 ③）：tech_pitch_v3 原仅 dev_auto 8.57 调用，
        #   依赖其时序 → 加入主链保证每晚必跑（TECH_TOP_N=6 + 竞价反信号 + 短线标注）
        run_step("科技线 Pitch v3", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "tech_pitch_v3.py")], timeout=1800)
    # 5) ★实盘持仓止损扫描（B-11 落地 · 外包 AI-2 position_stop_check——2026-08-10 总指导接入）
    #    定位=持仓执行层（T+1 卖出信号）；池级预警层=dev_auto stop_monitor（stop_alerts）——双引擎分工不重复：
    #    ★#367 修复：读时间戳 glob 取最新（原读固定名 portfolio.json 是 08-10 旧残留 99 字节 holding=0，
    #      导致实盘止损/止盈扫描每天被跳过——用户持仓根本没被止损引擎扫到，与"写 v2 读 v1"同坑）
    _pfs = sorted([Path(p) for p in glob.glob(str(BASE / "logs" / "portfolio_*.json"))],
                  key=lambda x: x.stat().st_mtime)
    _pfc = _pfs[-1] if _pfs else None
    if _pfc and _pfc.exists():
        try:
            import json as _j
            _pd = _j.loads(_pfc.read_text(encoding="utf-8"))
            _n_hold = sum(1 for x in (_pd.get("positions") or []) if x.get("status") == "holding")
        except Exception:
            _n_hold = 0
        if _n_hold > 0:
            run_step("实盘止损扫描", [PY, str(BASE / "risk" / "position_stop_check.py")], timeout=600)
            run_step("实盘止盈扫描", [PY, str(BASE / "risk" / "take_profit_check.py")], timeout=600)   # ★2026-08-11 百轮#4 止盈引擎
        else:
            log(f"  实盘止损/止盈扫描跳过（portfolio 无 holding 持仓，当前 {_n_hold} 只）")
    else:
        log("  实盘止损扫描跳过（无 portfolio 时间戳文件，未买入）")
    # ★2026-08-11 观察池/决策池数据流补全（百轮#1：用户反馈"观察池数据不通"根因——
    #   pool_layers/daily_signal/远期池/突破监控 不在 17:30 管道链 → 页面停留旧数据 08-10）
    run_step("三层池（观察/候选/决策）", [PY, "-X", "utf8", str(BASE / "strategy" / "pool_layers.py"),
              "--n", "100", "--capital", "200000", "--regime-cash", "0.3"], timeout=1800)
    run_step("今日信号（择时/审计）", [PY, "-X", "utf8", str(BASE / "report" / "daily_signal.py")], timeout=1800)
    run_step("新择时系统（适合买入判断）", [PY, "-X", "utf8", str(BASE / "factors" / "policy" / "timing_system.py")], timeout=300)
    run_step("组合风控（集中度/行业上限）", [PY, "-X", "utf8", str(BASE / "risk" / "position_monitor.py")], timeout=300)   # ★2026-08-11 百轮#11
    run_step("远期池 T+1 填充", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "pitch_track.py")], timeout=1800)
    run_step("突破监控", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "breakout_monitor.py")], timeout=1800)
    # ★2026-08-11 管道落地验证（8 项：交易日/五强/共识/机会池/拥挤/Pitch分档直通/强因子/短线因子）
    try:
        run_step("管道落地验证", [PY, "-X", "utf8", str(BASE / "data" / "verify_day_pipeline.py")], timeout=300)
    except Exception as e:
        log(f"  管道验证失败: {str(e)[:80]}")
    log("=== 每日数据管道完成 ===")
    # 锁由 main() 的 finally 统一释放（见 _acquire_lock 注释）


if __name__ == "__main__":
    main()
