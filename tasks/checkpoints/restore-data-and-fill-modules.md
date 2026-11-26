# 数据更新恢复 + 6 个空白模块补齐

## Goal
- P0：修 schtasks 调度（3 个 .bat 加健康检查 + PATH），让 DailyPipeline/DevDriver/AfterCloseScan 不再静默失败
- P1：跑 `data/after_close_scan.py` 补 `output/global_rotation.json`（8-24 旧 → 最新）
- P2：`deck/live_api.py` 让 `api_health` 给"未实现"模块加显式灰色提示（不显示 404 占位）
- P3：6 个空白模块（daily_signal/audit/factor_pool/pool_layers/sim_tracks）→ 选 daily_signal + factor_pool 优先实现（今天），其余明天 GSD-CC 干

## 启动时间
2026-09-02 00:30（用户确认 "今天全部一起干"，已告知 6 模块 4-6h 不可行 → 今天交付 P0+P1+P2+2 个 P3）

## Done
- ✅ 根因定位完成：8-25 schtasks 注册改 wscript 模式后失败（0x800704E0），调度挂了 6 个交易日
- ✅ 用户手动跑 `data/daily_pipeline.py` 成功补全：bars.db SH.000300 09-01、traffic_light 09-01 4611.44、timing 48.6
- ✅ Deck 8787 / HARNESS 3080 都在跑（API 已返回 09-01 新数据）
- ✅ 6 个空白模块定位：`report/daily_signal.py` 不存在、`data/audit_data_assets.py` 是资产不是运行器、`data/factorpool/output/` 无 manifest/health、`factors/pool/builtin_purify.py` 是因子池雏形（git untracked）

## Next（按执行顺序）
1. P0：改 3 个 .bat（pipeline/devdriver/afterclose_start.bat）加 PATH + 文件存在性检查 + 错误日志
2. P1（并行）：派 dsh 跑 `data/after_close_scan.py`
3. P2（并行）：派 dsh 改 `deck/live_api.py` api_health 标注"未实现"
4. P3 阶段 1（并行）：派 dsh 出 6 个 P3 模块的实施方案（含接口/伪代码/数据流）
5. P3 阶段 2：基于方案实现 daily_signal.py + 接 builtin_purify.py 到 factor_pool
6. 集成验证：跑一遍 daily_pipeline.py 看 api_health 全绿，git commit
7. 剩余 P3 模块（audit/pool_layers/sim_tracks）→ 明天 GSD-CC

## 关键文件
- `C:\Users\11296\quant-deck-autostart\quant_{pipeline,afterclose,devdriver}_start.bat`（P0 目标）
- `data/after_close_scan.py`（P1 目标）
- `deck/live_api.py:2892` 附近（api_health 改造点）
- `data/factorpool/output/`（P3 factor_pool 目标）
- `report/daily_signal.py`（P3 daily_signal 新建）
- `factors/pool/builtin_purify.py`（已存在，需接入）
