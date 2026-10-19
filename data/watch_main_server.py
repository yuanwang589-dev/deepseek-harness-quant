# -*- coding: utf-8 -*-
"""
data/watch_main_server.py — ★主服务器恢复守护（2026-08-08）

背景：主服务器（api.tushare.pro）昨夜被并发打崩（10054 连接拒绝）。
      备用服务器已把 2010-2018 raw 数据拉完（490 万行），但复权转换需要主服务器
      adj_factor（备用/官方均限频 1 次/分钟）。

本脚本：每 5 分钟探测主服务器 → 恢复后依次执行：
  1) data/convert_backup_raw.py    raw → qfq 转换（2011-2018）
  2) data/fetch_quality_tushare.py 质量补拉续传（5542 只，断点续传）

用法：python data/watch_main_server.py（后台常驻）
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
LOG_FILE = BASE / "logs" / "watch_main.log"
INTERVAL = 300  # 5 分钟探测一次


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def probe():
    """探测主服务器 daily 接口，通返回 True"""
    try:
        import tushare as ts
        import yaml
        cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))["data"]
        p = ts.pro_api(cfg["tushare_token"])
        # ★2026-10-15 修复：空串兜底 + 官方可用网关 waditu dataapi（同 fetcher_tushare.py）
        p._DataApi__http_url = cfg.get("tushare_api_url") or "https://api.waditu.com/dataapi"
        d = p.daily(trade_date=time.strftime("%Y%m%d"))
        return d is not None and len(d) > 0
    except Exception:
        return False


def run_step(name, args, timeout=7200):
    log(f"执行 {name} ...")
    py = sys.executable
    try:
        r = subprocess.run([py, str(BASE / args[0]), *args[1:]], timeout=timeout)
        log(f"{name} 完成 exit={r.returncode}")
        return r.returncode == 0
    except Exception as e:
        log(f"{name} 异常: {str(e)[:80]}")
        return False


def main():
    log("守护启动，每 5 分钟探测主服务器...")
    recovered = False
    while True:
        if probe():
            if not recovered:
                log("★★★ 主服务器已恢复！开始执行收尾任务")
                recovered = True
                run_step("raw→qfq 转换", ["data/convert_backup_raw.py"])
                run_step("质量补拉续传", ["data/fetch_quality_tushare.py", "--workers", "2"])
                log("收尾任务执行完毕，守护继续监听（异常时人工介入）")
        else:
            if recovered:
                log("主服务器再次不可达（停止自动任务）")
                recovered = False
            time.sleep(INTERVAL)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
