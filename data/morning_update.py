"""data/morning_update.py - 盘前数据更新链（2026-08-21 用户需求：交易日 09:10 计划任务，
2026-09-18 从 08:15 调晚，见下方★时序硬约束）

★定位：与 after_close_scan.py（17:35 盘后链）互补的晨更链：
  D 盘行情/财报同步 -> 机会扫描 -> Pitch v2 -> 科技线 v3 -> 日报。
  盘后链更新因子池评分+pitch；晨更链补 D 盘昨收数据并刷新当日决策面。

★非交易日策略（沿用 after_close_scan 惯例）：周末直接跳过；
  节假日空跑无害（bars 数据无变化 -> scan #430 幂等跳写，纯耗算力 ~15 分钟）。

★前置依赖：D:/quant_data 由掘金量化项目盘后管道更新（昨日收盘数据**北京 08:23~09:03 落库**——
  源库 created_at 存 UTC（SQLite datetime('now')），2026-09-18 实测）。
★时序硬约束：D盘行情同步启动必须晚于 09:03（现任务 09:10 触发）；若订在 08:15 这类更早时刻，
  每次同步都抢跑在源库写库前，bars.db 系统性滞后 1 交易日（9/16~9/18 连漏三天；
  2026-08-25 晨更"exit=0 但 8/24 未入库、10:40 手动重跑才成"的未解异常即同根因）。
  若掘金管道未跑，晨更同步到的仍是上一有效交易日数据，链照常跑（幂等）。
"""
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = BASE / "logs" / "morning_update.log"


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        # 日志文件写入失败不阻断主链（print 已输出），但必须显式提示、不静默
        print(f"[warn] 日志文件写入失败: {e}", flush=True)


def run_step(name, cmd, timeout=1800):
    log(f"-> {name}")
    try:
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        tail = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        log(f"  {name} exit={r.returncode} {tail[:120]}")
        if r.returncode != 0 and r.stderr:
            log(f"  stderr: {(r.stderr or '').strip()[:200]}")
    except subprocess.TimeoutExpired:
        log(f"  ⚠ {name} 超时（{timeout}s）")
    except Exception as e:
        log(f"  ⚠ {name} 异常: {str(e)[:120]}")


def main():
    import datetime as _dt
    _d = _dt.date.today()
    if _d.weekday() >= 5:
        log(f"非交易日（{_d} 周末）-> 跳过晨更链（pitch 保持上一交易日数据）")
        return
    log("=== 盘前数据更新链启动 ===")
    # 1) D 盘行情同步（只读挂载，幂等）
    # ★2026-10-15 增量改造：import_from_ddrive.py 默认只导新交易日（秒级）；
    #   原全量重导 13.86M 行耗时涨至 20+ 分钟，9/22 起连续 16 个交易日撞 1200s 超时
    #   → bars.db 停在 09-18。现超时 1200s 仅兜底极端情况（源库 malformed 重试等）。
    run_step("D盘行情同步", [PY, "-X", "utf8", str(BASE / "data" / "import_from_ddrive.py")],
             timeout=1200)
    # 1.5) ★2026-10-16 ST 标记同步：D 盘源库无 ST 历史 → 导入时 is_st 恒 0
    #   → load_st_codes() 回溯也找不到 → ST 过滤静默失效。用 stock_basic.name 补标记（本地秒级）。
    #   必须紧跟在行情同步后（增量导入会把新行 is_st 重置为 0）。
    run_step("ST标记同步", [PY, "-X", "utf8", str(BASE / "data" / "sync_st_flags.py"),
                        "--days", "10"], timeout=300)
    # 2) D 盘财报同步（幂等，约 20 秒）
    run_step("D盘财报同步", [PY, "-X", "utf8", str(BASE / "data" / "import_finance_from_ddrive.py")],
             timeout=600)
    # ★2026-09-09 圆环：五福轮动/ETF 缓存无日常调度（9/2 后停更至 09-01）。
    #   rebuild_wufu_cache.py 经 etf_map.fetch_etf 增量拉 ETF+指数至 data/cache/etf/*.csv，
    #   重建 wufu_etf/wufu_idx parquet 并刷新 global_rotation.json（门户五福卡每日更新）。
    #   东财指数失败会自动回退新浪；幂等（CSV 增量），约 1~5 分钟。
    run_step("ETF五福缓存重建", [PY, "-X", "utf8", str(BASE / "data" / "rebuild_wufu_cache.py")],
             timeout=1800)
    # 3) 市场快照重建（★2026-08-25 外包 factorpool 缺失，本地近似供风格/宽度判定）
    run_step("市场快照重建", [PY, "-X", "utf8", str(BASE / "data" / "rebuild_market_snapshot.py")],
             timeout=600)
    # ★2026-09-21 宏观/政策依赖补齐：此前晨更链不生产 macro.db（社融/国债）+ epu.db（FRED EPU），
    #   长期缺失导致 policy_score/macro_score 永久降级为中性兜底（联网 AkShare+FRED，失败不阻断链路）。
    run_step("宏观缓存重建", [PY, "-X", "utf8", str(BASE / "data" / "fetch_macro_cache.py")], timeout=600)
    run_step("EPU缓存重建", [PY, "-X", "utf8", str(BASE / "factors" / "policy" / "epu_fetcher.py")], timeout=600)
    # ★2026-09-21 择时评估刷新：晨更链此前不跑 timing_system，择时快照停在手动跑的旧日期；
    #   排在快照重建后保证宽度/风格字段新鲜（bars 已补 date 索引，评估秒级完成）。
    run_step("择时评估刷新", [PY, "-X", "utf8", str(BASE / "factors" / "policy" / "timing_system.py")],
             timeout=300)
    # 4) 机会扫描 + Pitch（核心决策面，约 8 分钟）
    run_step("机会扫描 --pitch", [PY, str(BASE / "factors" / "opportunities" / "scan.py"), "--pitch"],
             timeout=2700)
    run_step("Pitch v2 Deck", [PY, str(BASE / "factors" / "opportunities" / "pitch_v2.py")])
    run_step("科技线 Pitch v3", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "tech_pitch_v3.py")])
    # ★2026-10-16 纸上短线轮换（paper_rotation）：独立账户 rotation，不影响任何台账/页面；
    #   config paper_rotation.enabled=false 时内部直接跳过（零副作用）。
    run_step("纸上短线轮换", [PY, "-X", "utf8", str(BASE / "strategy" / "paper_rotation.py")],
             timeout=300)
    # ★2026-11-20 L2 台账自动买卖（持仓页）：止损/止盈信号自动卖 + pitch_v2 前 N 自动买；
    #   config auto_execute.enabled=false 时零副作用。放在止损/止盈引擎重跑之后。
    run_step("台账自动买卖", [PY, "-X", "utf8", str(BASE / "strategy" / "auto_execute.py")],
             timeout=300)
    # 5) 日报
    run_step("每日决策日报", [PY, "-X", "utf8", str(BASE / "data" / "daily_report_auto.py")])
    log("=== 盘前数据更新链完成 ===")


if __name__ == "__main__":
    main()
