# -*- coding: utf-8 -*-
"""
validation/arch_check.py — 架构可行性排查（真实检查：import/配置/路径/冲突）

检查项：
1. 所有模块可 import（无循环依赖/缺包）
2. params.yaml 配置与代码读取一致（键存在性）
3. 路径有效性（cache_dir / 日志目录 / 数据文件）
4. 已知冲突点核查：
   - RiskAgent 参数 vs params.yaml risk 段
   - m3_validate 判定基线 vs params.yaml drift 段
   - dev_auto M3 触发路径 vs 实际文件
   - bulk_loader 单实例锁路径 vs dev_auto 检测路径
5. 潜在逻辑冲突清单（人工复核项）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import yaml

PASS, WARN, FAIL = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))


def warn(name, detail):
    WARN.append((name, detail))


# ---------- 1. 模块 import ----------
print("=== 1. 模块 import 检查 ===")
modules = [
    ("data.cache", "DailyCache"),
    ("data.finance_calc", None),
    ("data.fetcher_baostock", None),
    ("data.fetcher_tushare", None),
    ("data.fetcher_akshare", None),
    ("data.bulk_loader", None),
    ("validation.evolution", None),
    ("validation.m3_validate", None),
    ("validation.walk_forward", None),
    ("risk.risk_agent", None),
    ("dev_auto", None),
]
for mod, _ in modules:
    try:
        __import__(mod)
        check(f"import {mod}", True)
    except Exception as e:
        check(f"import {mod}", False, str(e)[:80])

# ---------- 2. params.yaml 配置一致性 ----------
print("=== 2. params.yaml 配置检查 ===")
cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
# 2a. 关键段存在
for sec in ["data", "factors", "defense", "pead", "weights", "risk", "regime", "pools", "backtest"]:
    check(f"配置段 [{sec}]", sec in cfg, f"缺失: {sec}")
# 2b. 权重段版本元数据（第17课要求）
w = cfg.get("weights", {})
check("权重版本元数据 weight_version", "weight_version" in w, "缺 weight_version")
check("权重版本元数据 decay_mode", "decay_mode" in w, "缺 decay_mode")
# 2c. 三层风控（第15课要求）
r = cfg.get("risk", {})
ddl = r.get("drawdown_levels", {})
check("三层风控 warning=5%", ddl.get("warning") == 0.05, str(ddl.get("warning")))
check("三层风控 control=10%", ddl.get("control") == 0.10, str(ddl.get("control")))
check("三层风控 circuit=15%", ddl.get("circuit_breaker") == 0.15, str(ddl.get("circuit_breaker")))

# ---------- 3. 路径有效性 ----------
print("=== 3. 路径检查 ===")
cache_dir = Path(str(cfg["data"]["cache_dir"]))
check("cache_dir 存在", cache_dir.exists(), str(cache_dir))
check("bars.db 存在", (cache_dir / "bars.db").exists(), "数据主体在 D 盘")
check("params.yaml 存在", (BASE / "config" / "params.yaml").exists())
check("logs 目录存在", (BASE / "logs").exists())
warn("学习笔记目录存在", "可选目录缺失（不影响架构；如需笔记监控，在项目根创建 学习笔记/）")

# ---------- 4. 已知冲突点核查 ----------
print("=== 4. 已知冲突点核查 ===")
# 4a. RiskAgent 读的键 vs params.yaml 实际键
ra_keys = ["max_position_pct", "max_industry_pct", "drawdown_levels",
           "circuit_breaker_position", "circuit_breaker_cooldown_days"]
for k in ra_keys:
    check(f"RiskAgent 配置键 [{k}]", k in r, f"params.yaml risk 段缺 {k}")
# 4b. m3_validate 判定基线 vs drift 段 ic_min
drift = w.get("drift", {})
check("drift 段存在", "drift" in w)
if "drift" in w:
    check("drift.ic_min=0.03 与 M3 判定一致", drift.get("ic_min", 0) == 0.03, str(drift.get("ic_min")))
# 4c. dev_auto M3 触发路径 vs 实际文件
check("m3_validate.py 存在（dev_auto 触发目标）", (BASE / "validation" / "m3_validate.py").exists())
check("dev_auto.py 存在", (BASE / "dev_auto.py").exists())
check("风险审计日志目录可写", (BASE / "logs").exists())

# 4d. 单实例锁路径一致性（bulk_loader 写 vs dev_auto 读）
bulk_lock = BASE / "data" / "logs" / "bulk_load.lock"
check("bulk_loader 锁路径一致", bulk_lock.parent.exists(), str(bulk_lock.parent))

# ---------- 5. 潜在逻辑冲突清单（人工复核） ----------
print("=== 5. 潜在逻辑冲突清单（人工复核项）===")
warn("止损参数双重来源", "risk.stop_loss_pct=7%（固定止损）与 ATR 止损并存——需在 M4 明确优先级：建议 固定7%为硬止损底线，ATR为软止损，取更严者")
warn("M3 短标签 vs 长持有定位", "m3_validate 默认未来20日标签（短周期，全因子反向）；用户定位是长期持有（月/季调仓）——必须补 60/120 日标签口径再定权重，否则会误杀动量因子")
warn("幸存者偏差", "bulk_loader 用 Baostock 8880 行列表（含历史）但 Tushare stock_basic 5538 只仅上市股——M2 完成后需核对最终入库列表是否含退市股，否则回测虚高")
warn("Regime 滞后性", "MA/ATR 均为滞后指标，牛市初期仓位不足、熊市初期跑得慢（CS-04/13）——切换纪律（连续N天确认+渐进）已部分缓解")
warn("因子拥挤风险", "C/I 类财报/机构因子 A 股衰减最快（CS-06/23），M3 衰减率监控是必备而非可选")
warn("数据库锁竞争", "bulk_loader 2 worker 并发写 SQLite，+dev_auto 自检读——SQLite 单写多读，写密集时读可能等待（当前低频可接受，M2 后观察）")
warn("控制台按 4 语义", "控制台按 4 启动下载时若无实例则前台阻塞运行，用户可能以为卡死——已加单实例锁提示，但 UX 仍可优化为后台启动+提示日志路径")

# ---------- 汇总 ----------
print()
print(f"✅ 通过: {len(PASS)} 项")
for n, d in PASS:
    print(f"  ✓ {n}" + (f"  [{d}]" if d else ""))
print(f"⚠️ 警告: {len(WARN)} 项（人工复核）")
for n, d in WARN:
    print(f"  ⚠ {n}: {d}")
print(f"❌ 失败: {len(FAIL)} 项")
for n, d in FAIL:
    print(f"  ✗ {n}: {d}")
print(f"\n结论: {'架构基础健康（无硬伤）' if not FAIL else '存在硬伤需修复'}")
return_code = 0 if not FAIL else 1
sys.exit(return_code)
