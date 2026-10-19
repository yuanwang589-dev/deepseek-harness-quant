# -*- coding: utf-8 -*-
"""data/manual_update_worker.py — 手动全域更新 worker v2（由 manual_update.start() 后台启动）

★v2（2026-08-10 网络卡死教训）：
  1. 单实例锁（O_EXCL pid 文件 + 竞态保守退出）
  2. sys.stdout/stderr 重定向到时间戳日志（子进程 run_step 的 print 全部落日志）
  3. ★网络预检：baostock 不可用（挂起）时跳过日线增量，其余本地步骤照跑——手动更新不因外部网络卡死
  4. 步骤级容错：单步失败记录不中断，done 状态含 summary

流程：7z→parquet → [网络预检] 日线增量 → 健康巡检 → 竞价信号 → 机会扫描 → Pitch → 看板刷新
"""
import glob
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
PY = sys.executable


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def _net_probe() -> bool:
    """baostock 网络预检：拉 1 只，35s 超时 → 可用返回 True
    ★2026-08-10 15:06 修正：3 只实测 55s（login+query 均被 15s socket 超时限制）会误超时 → 改 1 只"""
    try:
        r = subprocess.run(
            [PY, "-X", "utf8", str(BASE / "data" / "incremental_daily.py"), "--limit", "1"],
            capture_output=True, text=True, timeout=35, encoding="utf-8", errors="replace")
        return r.returncode == 0 and "完成" in (r.stdout or "")
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def run(ts: str):
    # ---- 单实例锁（O_EXCL + 竞态保守退出）----
    pidfile = LOGS / f"mu_worker_{ts}.pid"
    try:
        fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        other = None
        for _ in range(5):
            try:
                other = int(Path(pidfile).read_text(encoding="utf-8").strip())
                break
            except Exception:
                time.sleep(0.3)
        sys.exit(0)   # 同 ts 只能一个 worker（读不到也保守退出）

    st = {"status": "running", "ts": ts, "pid": os.getpid(),
          "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (LOGS / f"manual_update_{ts}.json").write_text(
        json.dumps(st, ensure_ascii=False), encoding="utf-8")
    logf = LOGS / f"manual_update_{ts}.log"

    # ---- stdout 重定向到日志（run_step 的 print 全部落日志）----
    log_handle = open(logf, "w", encoding="utf-8")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log_handle

    # 复用 daily_pipeline 的步骤工具
    sys.path.insert(0, str(BASE))
    import data.daily_pipeline as dp
    from datetime import datetime as _dt, timedelta as _td

    failed_steps = []
    summary = []

    def step(name, fn, expect=(), fresh_hours=None):
        """★2026-10-15 反假成功加固：
        ① fn() 返回 False（dp.run_step 的 returncode 判定）→ 记失败（原实现丢弃返回值，
           子进程 exit 2 也打 ✓ ——「今日信号」因发行包缺 report/daily_signal.py 一直假成功）
        ② expect 产物校验：glob 相对 BASE，要求存在 mtime>=步骤开始 且 >2 字节的新文件，
           缺失 → 记失败（消灭「✓ 但没产出」——如扫描器读空面板 3 秒假成功退出）
        ③ fresh_hours：幂等步骤专用（如 scan #430：数据未变时故意不写新文件）——
           无新文件但最新产物在 fresh_hours 内 → 放行并注明『幂等跳过』"""
        print(f"▶ {name} ...")
        t0 = time.time()
        try:
            ok = fn()
            if ok is False:
                raise RuntimeError("run_step 返回失败（returncode != 0 或异常）")
            missing = []
            idem_ok = []
            for pat in expect:
                hits = [f for f in glob.glob(str(BASE / pat), recursive=True)
                        if os.path.getmtime(f) >= t0 - 1 and os.path.getsize(f) > 2]
                if hits:
                    continue
                if fresh_hours is not None:
                    all_f = sorted(glob.glob(str(BASE / pat), recursive=True),
                                   key=os.path.getmtime)
                    if all_f:
                        age_h = (time.time() - os.path.getmtime(all_f[-1])) / 3600
                        if age_h <= fresh_hours and os.path.getsize(all_f[-1]) > 2:
                            idem_ok.append(f"{pat}（最新 {age_h:.1f}h 前，幂等跳过）")
                            continue
                missing.append(pat)
            if missing:
                raise RuntimeError("产物校验失败（无新增非空文件）: " + "、".join(missing))
            note = (" | 幂等: " + "、".join(idem_ok)) if idem_ok else ""
            print(f"  ✓ {name} 完成 ({time.time()-t0:.0f}s){note}")
            summary.append(f"{name}: ✓{note}")
        except Exception as e:
            print(f"  ✗ {name} 失败: {str(e)[:120]} ({time.time()-t0:.0f}s)")
            failed_steps.append(name)
            summary.append(f"{name}: ✗ {str(e)[:60]}")

    try:
        print(f"=== 手动全域更新 {ts} 启动（{st['started_at']}）===")

        # 1) 分钟 7z 增量 → parquet（本地）
        def s1():
            return dp.run_step("分钟 7z 增量 → parquet",
                               [PY, str(BASE / "data" / "convert_7z_to_parquet.py")], timeout=3600)
        step("分钟 7z 增量 → parquet", s1)

        # 2) 日线增量 ★2026-08-10 双通道：Tushare 优先（秒级）→ baostock 预检兜底（35s/1 只）
        tushare_ok = False
        net_ok = None   # ★#336 修复：net_ok 仅在 baostock 兜底分支赋值，Tushare 成功时未定义 → done 构造 UnboundLocalError → 误标 failed
        try:
            r = subprocess.run(
                [PY, "-X", "utf8", str(BASE / "data" / "incremental_daily_tushare.py")],
                capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
            tushare_ok = r.returncode == 0 and "已入库" in (r.stdout or "")
            print(f"  日线增量(Tushare): {(r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else '无输出'}")
        except subprocess.TimeoutExpired:
            print("  ⚠ Tushare 日线增量超时")
        except Exception:
            pass
        if tushare_ok:
            summary.append("日线增量: Tushare 秒级完成")
        else:
            # ★2026-08-14 移除 baostock 全市场兜底（1500s，卡整链几小时）→ 失败即跳过，次日/后续链重试
            print("  ⚠ Tushare 日线增量失败（已重试）→ 跳过当日增量（数据保持 bars.db 现有；后续链自动重试）")
            summary.append("日线增量: 跳过（Tushare 失败，不再走 baostock 全市场兜底）")

        # 3) 健康巡检（本地）
        def s3():
            return dp.health_check()
        step("数据健康巡检", s3)

        # 4) 竞价强度信号（近 3 月动态区间，本地）
        _now = _dt.now()
        _start = (_now - _td(days=95)).strftime("%Y-%m")
        _end = _now.strftime("%Y-%m")
        def s4():
            # ★2026-10-15 显式预检查：分钟数据（1m_price_zip）是竞价信号硬依赖，
            #   未下载时直接报因（原版子进程 FileNotFoundError 被旧 worker 丢弃 → 假成功 ✓）
            if not (BASE / "data" / "minute" / "download" / "1m_price_zip").exists():
                raise RuntimeError("分钟数据未下载（data/minute/download/1m_price_zip 缺失）"
                                   "——竞价强度信号不可用，属数据边界，不影响其余步骤")
            return dp.run_step("竞价强度信号",
                               [PY, str(BASE / "factors" / "opportunities" / "auction_strength.py"),
                                "--start", _start, "--end", _end], timeout=1500)
        step(f"竞价强度信号（{_start}~{_end}）", s4, expect=("logs/auction_signal.json",))

        # 4.5) ★2026-10-15 免费竞价信号回填（akshare 新浪 1 分钟，主源 1m_price_zip 缺失/滞后时的免费替代源）
        #       软步骤：akshare 未装 / 网络波动 / 超时 → 打印警告并跳过，不影响主链成败（属增强源）
        def s4b():
            try:
                import importlib
                importlib.import_module("akshare")
            except Exception:
                print("  ⚠ akshare 未安装 → 免费竞价回填跳过（增强源；安装: pip install akshare）")
                return True
            try:
                r = subprocess.run(
                    [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "auction_sina_backfill.py"),
                     "--candidates"],
                    capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
                out = (r.stdout or "") + (r.stderr or "")
                if r.returncode == 0 and "无有效信号" not in out and "完成" in out:
                    last = [l for l in out.strip().splitlines() if l.strip()][-1]
                    print(f"  ✓ 免费竞价回填完成（{(last or '').strip()[:90]}）")
                else:
                    _last = [l for l in out.strip().splitlines() if l.strip()][-1] if out.strip() else "无输出"
                    print(f"  ⚠ 免费竞价回填未产出（网络/数据波动，属增强源）: {_last[:90]}")
            except subprocess.TimeoutExpired:
                print("  ⚠ 免费竞价回填超时（>1800s，属增强源，跳过）")
            except Exception as e:
                print(f"  ⚠ 免费竞价回填异常（属增强源）: {str(e)[:90]}")
            return True   # 软步骤：不影响主链
        step("免费竞价信号回填（新浪候选池）", s4b)

        # 5) 机会扫描 + Pitch（本地）
        def s5():
            return dp.run_step("机会扫描 --pitch",
                               [PY, str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"], timeout=1500)
        step("机会扫描 --pitch", s5, expect=("logs/opp_pool_*.json",), fresh_hours=8)

        def s6():
            return dp.run_step("Pitch v2 Deck",
                               [PY, str(BASE / "factors" / "opportunities" / "pitch_v2.py")], timeout=1500)
        step("Pitch v2 Deck", s6, expect=("logs/pitch_v2_*.json",))

        # 5.5) ★2026-10-15 补齐：科技突破 Pitch 短线（脚本原存在但未纳入管道 → 决策链"Pitch 短线"长期不亮）
        def s61():
            return dp.run_step("科技 Pitch 短线",
                               [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "tech_pitch.py")], timeout=900)
        step("科技 Pitch 短线", s61, expect=("logs/tech_pitch_*.json",))

        # 6.2) ★#351 三层池（观察/候选/决策）——补全手动更新管道（原缺这三步导致观察池/择时/信号永远滞后）
        def s62():
            return dp.run_step("三层池（观察/候选/决策）",
                               [PY, "-X", "utf8", str(BASE / "strategy" / "pool_layers.py")], timeout=1800)
        step("三层池（观察/候选/决策）", s62, expect=("output/pool_layers_*.json",))

        # 6.3) ★#351 今日信号（择时/审计）★2026-10-15 补 report/daily_signal.py（原发行包缺失）
        def s63():
            _script = BASE / "report" / "daily_signal.py"
            if not _script.exists():
                raise RuntimeError("脚本缺失 report/daily_signal.py（本发行包未含，今日信号环节不可用）")
            return dp.run_step("今日信号（择时/审计）",
                               [PY, "-X", "utf8", str(_script)], timeout=300)
        step("今日信号（择时/审计）", s63, expect=("output/daily_signal_*.json",))

        # 6.4) ★#351 新择时系统（适合买入判断）
        def s64():
            return dp.run_step("新择时系统（适合买入判断）",
                               [PY, "-X", "utf8", str(BASE / "factors" / "policy" / "timing_system.py")], timeout=300)
        step("新择时系统（适合买入判断）", s64, expect=("output/timing_system_*.json",))

        # 6.5) ★F4 远期收益池更新（Pitch 后实时刷新 T+1/5/20/60；空池合法，不做产物校验）
        def s65():
            return dp.run_step("远期收益池更新",
                               [PY, str(BASE / "factors" / "opportunities" / "pitch_track.py"),
                                "--update"], timeout=600)
        step("远期收益池更新", s65)

        # 6.6) ★F1 止损监测（待处理面板数据源）
        def s66():
            return dp.run_step("止损监测",
                               [PY, str(BASE / "risk" / "stop_monitor.py")], timeout=600)
        step("止损监测", s66, expect=("logs/stop_alerts_*.json",))

        # 6.65) ★2026-10-15 补齐：风控地图（stock_risk --scan，Beneish 财务造假检测 → 决策链"风控"环节）
        def s665():
            return dp.run_step("风控地图（Beneish 扫描）",
                               [PY, "-X", "utf8", str(BASE / "risk" / "stock_risk.py"), "--scan"], timeout=1200)
        step("风控地图（Beneish 扫描）", s665, expect=("logs/stock_risk_map_*.json",), fresh_hours=12)

        # 6.7) ★2026-08-14 时间戳文件归档（logs/output 旧版本移入桌面垃圾桶，保留最新 N 份）
        #       原机制仅 dev_auto 调用；手动更新链补齐，避免日志目录长期累积拖慢 glob
        def s67():
            return dp.run_step("时间戳文件归档",
                               [PY, str(BASE / "data" / "logs_archive.py")], timeout=300)
        step("时间戳文件归档", s67)

        print(f"=== 管道完成（失败步骤: {failed_steps if failed_steps else '无'}）===")
        log_handle.flush()

        done = {"status": "done" if not failed_steps else "done_with_issues",
                "ts": ts, "pid": os.getpid(),
                "started_at": st["started_at"],
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": summary,
                "failed_steps": failed_steps,
                "net_ok": net_ok,
                "log": logf.name}
    except Exception as e:
        traceback.print_exc()
        done = {"status": "failed", "ts": ts, "pid": os.getpid(), "error": str(e)[:200],
                "started_at": st["started_at"],
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": summary, "failed_steps": failed_steps, "log": logf.name}

    sys.stdout, sys.stderr = old_out, old_err
    log_handle.close()
    (LOGS / f"manual_update_{ts}_done.json").write_text(
        json.dumps(done, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    ts = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d_%H%M%S")
    run(ts)
