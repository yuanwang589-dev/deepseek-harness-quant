# 更新日志

## 2026-10-18 工程化基线（最小集，不动业务逻辑）

主题：**把"有没有改坏"从人工记忆变成一条命令**。只加配置，零业务改动。

- 新增 `pyproject.toml`（ruff 规则 `E9,F63,F7,F82`，基线 0 告警）、`.editorconfig`、
  `.pre-commit-config.yaml`、`requirements-dev.txt`、`CONTRIBUTING.md`
- CI 增加 `lint` job（ruff），与既有 `offline-tests` 并列；push / PR 均触发
- 修 3 处 ruff `F821` 未定义名，均为死代码或日志分支，**行为不变**：
  - `deck/deck_server.py`：`_resolve_route()` 里调用未定义 `_latest_opp_pool()` 的分支永不可达
    （`/api/pool` 实际走 handler 的 `_latest_pool_path()`），删除以免将来变成 500
  - `factors/opportunities/scan.py`：告警 f-string 的 `_mx_n` → `_cnts.get(_mx, 0)`
    （原写法在该分支触发时会 NameError，把真正的告警吞掉）
  - `validation/gen_backtest_charts.py`：删除 `if False` 的死赋值（引用了尚未定义的 `cash`）
- 备份：`backups/lint_fix_20261018/`
- 验收：`ruff check .` 0 告警；140 用例仍为 1 条**已知锚点漂移**
  （`bars_codes` 5574→5575，来自 `920196.BJ` 仅 1 行的孤儿记录，非本次改动）

## 2026-10-18 例行审查修复（看板可用性 P0 + 巡检健壮性）

主题：**看板"能打开但会被一个接口拖死"**。本轮用真实浏览器逐页点开 13 个页面 + 逐个
GET 70 个路由采集基线，修掉 5 处实测可复现缺陷。全量回归 140 用例仅剩 1 条文档锚点漂移
（非代码缺陷，见报告 §六），冒烟 23/23 通过。

- **`deck/live_api.py` · `_bars_qfq_rows()`（新增）**：`live_funnel()` 里内联的
  `SELECT COUNT(*) ... WHERE adjust='qfq' AND code != 'SH.000300'` 查询计划为
  `SEARCH daily_bar USING INDEX idx_daily_bar_date (adjust=?)`——索引只过滤 `adjust`，
  再对 **847 万行逐行回表**取 `code` 比较，实测 **72s**（独立进程冷启 102s）。
  该接口被 `/help` 页直接调用：请求线程占满 → 浏览器 `ERR_CONNECTION_RESET` →
  watchdog 判死重启 → **全站 30~60s 不可用**（本轮复现 2 次）。
  改为强制覆盖索引 `INDEXED BY idx_daily_bar`（`SCAN ... USING COVERING INDEX`，不回表，
  实测 **1.21s**，结果与原写法逐位一致 8477451），并按 `bars.db` 的 mtime+size 做进程内
  缓存（该数字一天只变一次，稳态 0.03s）。
- **`deck/deck_server.py` · 仓库根自举**：`python deck/deck_server.py` 时 `sys.path[0]` 是
  `deck/`，`live_api.py` 的 `from data.io_utils import ...` 直接 `ModuleNotFoundError`
  → **36 个 `/api/live/*` 全 500**、启动预热恒跳过（`No module named 'data'`）。此前仅
  `scripts/watchdog.py` 用 `PYTHONPATH` 兜住，任何人按 docstring / 仓库地图 §7 启动都起不来。
  现于 `BASE` 定义后自举仓库根（幂等），预热恢复为"已触发"。
- **`data/health_check.py` · `check_tasks()` 容错**：原实现直接 `subprocess.run(["schtasks"...])`，
  非管理员或被安全策略拦截时抛 `PermissionError: [WinError 5]` → 异常冒到 `main()` →
  **整份巡检一个字都不输出**（本轮实测本机即如此）。改为 `OSError/PermissionError/
  SubprocessError` 全部捕获，标记 `unverified` 并单列"N 项无法查询，未验证"——
  不伪装成"计划任务缺失"（那是另一种假结论）。
- **`deck/live_api.py` · `live_data_assets()` 不再假成功**：`unified.db` 不存在时原来只写
  `error` 字段、`ok` 恒为 `True`，前端据 `ok` 判定成功 → 数据资产页 4 个板块
  （因子实战库/整合库表/资产清单/数据链路）全渲染成"无表数据"，把"库根本没建"伪装成
  "有库但没数据"。现如实返回 `ok:false` + `hint`（`python data/build_unified_db.py`）。
- **`deck/deck_server.py` · 板块研究台 503 降级**：`/api/sector_research/{analyze,stock}`
  依赖 `output/daily_close.parquet`（仅由 `data/subj_quant_stats._load_daily_close()` 懒生成），
  缺失时抛 `FileNotFoundError` → 500 + **把本机绝对路径原样回给浏览器**。改为 503 +
  可执行提示；刻意不在请求路径里自动生成（8.6M 行 pivot 会占满请求线程，同 funnel 教训）。
- **`tests/test_gaps.py` · `TestAuditFixes20261018`（新增 4 条回归护栏）**：覆盖上述
  4 个代码缺陷（funnel 耗时/缓存一致性、deck_server 自举、巡检容错、资产假成功）。
  修好不删，反向验证。

## 未发布（2026-10-15 企业微信推送 · 方案①「零端口暴露」）

主题：让用户**在微信上收到**量化系统内容，同时**不开放任何入站端口**——避开"直接暴露
8787 = 无鉴权直达全部写接口 + AI 代理面"的风险（`deck_server` 无任何鉴权，`do_POST` 含
`/api/decide`、`/api/manual_update`、`/api/proxy/*` 等可改数据/调 AI 的接口）。

- **`scripts/wecom_push.py`（新建）**：只做「读本机 deck → 外发微信」的单向推送。
  - 读：`/api/live/holdings`（持仓 + 现价 + 盈亏）、`/api/daily_report`（日报）。
  - 写：POST 到企业微信群机器人 webhook（仅出站，**无 bind/listen**，有用例守着这条安全契约）。
  - webhook 来源优先级：`--webhook` > 环境变量 `WECOM_WEBHOOK` > `config/wecom.yaml`。
  - 本机 API 调用显式绕代理（与 `watchdog`/`launcher` 的 `clean_env` 一致）；webhook 默认也绕代理，
    需走代理加 `--use-proxy`。
  - 消息内容按字节截断（≤4000B，且不切断多字节字符）；**单源失败不拖垮整条推送**
    （如日报未生成时仍推持仓，并注明缺失来源）。
  - 子命令：`--selftest`（校验配置与连通性）、`--dry-run`（预览不发）、
    `--type {all,holdings,daily}`、`--format {markdown_v2,markdown}`。
  - 退出码：0 成功 / 1 发送或读取失败 / 2 未配置 webhook。
- **`config/wecom.yaml.example`（新建）**：配置模板；真实 `config/wecom.yaml` 已加入 `.gitignore`
  「本地配置（含真实密钥，勿提交）」段。
- **持仓盈亏（2026-10-15 新增）**：持仓段改读 `/api/live/holdings` 取现价与收益率，并**读模拟账户
  `data/cache/paper.db`（只读）取真实股数/现金**，据此报**账户口径的真金额盈亏**：
  `净值 = 持仓市值 + 现金`，对比 `initial_cash` 得盈亏与收益率（实测：净值 ¥9,699.30 / 本金 ¥10,000
  → **-¥300.70 / -3.01%**）。每只另给 `股数 成本→现价 · 收益率 · 金额 · 🎯目标 · 🛡止损`。
  涨跌配色遵循中国习惯（盈利=🔴、亏损=🟢）；现价来源标注（盘中 `实时价` / 非交易时段 `收盘价`）。
  **为什么必须读 paper.db**：`logs/portfolio.json` 只记成本价、**不记股数**，而 `portfolio_pnl` 的
  `total_cost/total_value` 是"1 股等权"的伪金额——直接用它会得到看着像钱、其实是假的数字。
  `paper.db` 缺失时自动退化为"只报收益率"并注明。
- **`tests/test_wecom_push.py`（新建，28 用例，全绿、全程离线）**：消息组装、手机端排版契约、
  字节截断不切多字节、webhook 来源优先级、单源失败优雅降级、发送异常不冒泡、
  涨跌配色与金额格式化、模拟账户读取（临时库，不碰真实 paper.db），以及三条契约——
  "脚本不含任何 bind/listen"（零暴露）、"消息里不出现 WeCom 不渲染的语法"（表格/分割线/半角缩进）、
  "候选池已下线且不会被悄悄加回来"。
- 验证：全套件 **96 用例 / 6 红**（6 红仍是既有数据缺口）；`--dry-run` 实测 702 字节，
  真实推送 **errcode 0**。

### 补数据源缺口：五个库从 0 到有（2026-10-15 深夜，按用户选择）

背景：`test_gaps` 的 5 条红里有 4 条是"库不存在"。本轮把库建出来。

**新建两个构建脚本（此前这五个库只有消费方、没有生产方）**
- `data/build_finance_ts.py` → `data/cache/finance_ts.db` / 表 `financials_ts`
  - 必需列 `code/end_date/ann_date/total_revenue/n_income/total_hldr_eqy_exc_min_int`，
    消费方 `bt_runner._load_fin`、`backtest_script1.load_revenue_growth`、`sector_research._break_net`(PB)
  - 利润表走 akshare `stock_lrb_em`（按报告期批量，10~20 s/期）；
    归母权益走 `stock_financial_report_sina`（逐股，12 进程约 4 股/s）
  - 实测 275,332 行 / 5,539 只 / 67 报告期（2010-03-31 ~ 2026-09-30）·
    营收 255,715 · **归母权益 261,554（95%）** · 公告日 99.99%
- `data/build_event_dbs.py` → `lhb_full.db` / `rzrq_full.db` / `gdhs_full.db` / `shebao.db`
  - 幂等 `INSERT OR REPLACE`，lhb 按月区间、rzrq 按日、gdhs/shebao 按报告期，**均可断点续传**
  - 实测：龙虎榜 139,871 行（6,302 只 / 1,886 日）、两融 4,891,831 行（626 MB / 1,885 日）、
    股东户数 140,241 行（5,460 只 / 31 期）

**★数据源真相（与文档声称不符，已在 `docs/资产盘点.md` §4.1 更正）**
- 仓库里的 Tushare token **只有日线档**：`daily` 正常（5,563 行/日），但
  `top_list`/`income`/`balancesheet`/`cashflow` **全部返回"未开通"**。
  `validation/test_tushare_access.py` 头部声称的"15000 积分版"**与实测权限不符**。
- 本项目财务数据**历来来自 akshare 免费源**（`finance.db` 的 `source` 列全是 `akshare_ths`）。
  `finance_quality.db` 的 `pub_date` 列**全为 NULL**（声明的 PIT 对齐从未落地）。
- `stock_lrb_em` 的"净利润"**实测是归母口径**（茅台 445.17 亿 = `PARENT_NETPROFIT`，
  非合计 460.33 亿）→ 按归母口径写入 `n_income`。
  `stock_zcfz_em`（批量资产负债表）**无归母权益**（只有含少数股东的"股东权益合计"），
  故归母必须走新浪逐股接口。

**★修掉一个真 bug：`run_backtest` 成功路径不声明 `ok`**
`backtest/bt_runner.py` 的失败路径返回 `{"ok": False, "missing_db", "hint"}`，
但**成功路径没有 `ok` 字段**（主路径与 `factor_all` 批量路径各一处）。后果是调用方
`if r.get("ok")` 无法区分"成功"与"结构异常"——两者都取不到真值。直接导致
`test_backtest_script1_runs` 在 script1 实际跑通后仍判失败、
`test_script1_returns_structured_failure` 的 skip 守卫失效。补 `"ok": True` 后两条同时关闭。

**测试守卫同步**
- `tests/test_doc_claims.py`：`REALITY` 更新（bars.db 8,466,319→**8,471,884** 行、
  5,223→**5,574** 只）；`test_missing_databases_disclosed` 从硬编码"6 个全缺"
  重构为 **`ALL_TRACKED_DBS` / `KNOWN_MISSING_DBS` 双常量对账**——补库后该用例本会必红。
- 验证：全套件 **136 用例 / 6 红 → 2 红**（`test_atr_stop_implemented`、`test_minute_db_present`）。
  关闭 4 条：`test_finance_ts_db_present`、`test_lhb_rzrq_gdhs_shebao_present`、
  `test_backtest_script1_runs`、两条文档守卫；`test_script1_returns_structured_failure` 转为正确 skip。
- **script1 现在真的能跑回测了**（2023-01-01 ~ 2024-12-31，top5/50 股）：
  总收益 **-28.3%**、年化 **-15.4%**、最大回撤 **-41.3%**、夏普 **-0.48**，
  基准年化 +6.3% → **超额 -21.7 pp**。数据通了，但策略本身是亏的。



- **删除候选池段**：`fmt_pool()` 及其专用的 `OTYPE_EMOJI`/`_otype_emoji`/`_rank_emoji` 一并删除，
  `build()` 的 `want` 映射收敛为 `all=[holdings, daily]`，CLI 去掉 `--type pool` 与 `--top`，
  `config/wecom.yaml` 去掉 `top` 键。用例 `test_fmt_pool_removed` 守着它不被加回来。
- **消息类型从 `markdown` 升级为 `markdown_v2`（默认）**——这是本轮"手机上怎么推更好看"的结论：
  企业微信的旧 `markdown` 是**残缺子集**（只支持标题/加粗/链接/行内代码/引用/3 种字体颜色，
  **不支持列表、缩进、表格、分割线**），所以此前只能用 emoji + 全角空格 + `━` 制表符线**伪装**层次。
  `markdown_v2` 支持 **1~6 级标题、斜体/加粗、无序/有序列表（可嵌套）、多级引用、链接**，
  代价是**不支持字体颜色与 @群成员**。两者都支持标题与引用，故新排版是**两边通吃**的：
  - `# 📊 DSHQuant 每日推送` 一级标题 + 时间行
  - `## 💼 持仓 3/3 · 实时价` 分节标题（替代原来的 `━━━━━ 💼 持仓 ━━━━━`）
  - `> ` 引用块承载账户总账（净值 / 盈亏 / 持仓市值 / 现金）——手机上呈现为独立色块，一眼可见
  - `- ` 列表承载个股，子项 2 空格嵌套：行情盈亏一行、`🎯目标 · 🛡止损` 一行
  - 保留 emoji 涨跌配色（🔴盈利 / 🟢亏损，中国习惯）
  - 注意：`markdown_v2` 需客户端 ≥4.1.36（安卓 ≥4.1.38），更低版本会退化成纯文本；
    此时用 `--format markdown` 一键回退。
- **未采用 `template_card`（模板卡片）**：官方文档确认 `text_notice` 的 `card_action` 为**必填**，
  且 `type` 只能取 1（跳转 url，url 必填）或 2（打开小程序，appid 必填）；`news_notice` 的
  `card_image` 又需要**公网图片 URL**。本项目是零公网暴露的本机部署，两个前提都不成立
  （只能填无效链接），故不采用。若将来把 deck 发布到公网，再回来评估。


### 开源借鉴调研（2026-10-15 晚，按用户要求）

- **`docs/开源借鉴调研.md`（新建）**：不泛泛列量化框架，而是**按本项目已核实的具体缺口**
  去 GitHub 找可借鉴对象（搜索 API + 逐项目读 README/源码结构/**许可证**，星数与活跃度均为实测）。
- 核心结论：
  - **`VernonOY/alpha-skills`（119★, Apache-2.0）最该抄** —— 架构同构（AI 助手 + 一组
    SKILL.md + SQLite 因子库）。它的 `alpha-library` 建表比我们全（多 `expression` /
    `ic_mean` / `icir` / `best_holding_period`），`alpha-monitor` 正是我们缺的 IC 衰减监控，
    且**L0–L3 分级评估**（L1 抽样 200 股 × 2 年 <30s）直接解决我们"6 因子跑 20 分钟跑不完"。
  - `eliasswu/AlphaPurify`（587★, MIT）：Polars+Rust，400 万行 25 秒；**40+ 中性化/去极值方法**
    （我们只有家族归并与 FDR，没有系统中性化）。
  - `JustinF8/qlib-factor-zoo`（MIT）：**1000+ A股因子定义**（Alpha158/360/101、GTJA191、
    TDXGS、JQ110），纯价量的可直接抄公式补我们"本仓只能算 22 个"的短板。
  - `1nchaos/adata`（5278★, Apache-2.0）：**直接命中缺失数据源** —— 龙虎榜、两融、
    个股/概念资金流、申万一二级行业。注意是**在线接口非本地库**，且**不提供股东户数与历史分钟线**。
  - `cn-vhql/FactorHub`（430★）功能最贴我们的因子池（含因子版本控制），但**仓库无 LICENSE**
    → 只能借思路不能抄代码。同样无许可的还有 `QuantMLResearch/QuantML`。
  - 组合层风控：`Riskfolio-Lib`（BSD-3）/ `PyPortfolioOpt`（MIT）可解决 §9.5 的集中度与
    波动率仓位（现在"等权整手 → 30% 现金闲置"的根源）。
  - **明确不建议引入 `microsoft/qlib`**（49374★）：要求 qlib 自有数据格式 + 数据服务器，
    与"SQLite + 零端口暴露 + 标准库 HTTP server"的定位冲突（但因子定义可单独抄）。
- 给出**借鉴顺序表**（P1 因子表补字段 + 评估分级 + 补 adata 数据 → P2 抄因子定义 +
  组合层 HRP → P3 版本历史 / Newey-West / 经济学直觉分），与 `docs/资产盘点.md` §11 对齐。
- 同步：`README.md` 文档索引、`docs/资产盘点.md` §11 加交叉引用。

### 因子池登记：0 → 54 个因子（2026-10-15 晚，按用户要求"先改因子库数量质量"）

主题：`data/cache/factor_pool.db` 的 `factors` 表**长期 0 行** ——
`factors/pool/lifecycle.py` 的「挖→抓→测→决→报」机制齐全、
`params.yaml` 的 `factor_pool` 阈值（active 65 / candidate 40）也配好了，
但治理逻辑**没有对象**：状态机、漂移巡检、报告全部空转
（"装了完整的交通信号系统，路上没有车"）。本轮把因子登记入池并接上质量分。

- **`factors/pool/seed_pool.py`（新建）**：把散落四处的因子收进池（只做"挖"这一环）
  - ① `factor_engine.py::FACTOR_FUNCS` **6 个**（引擎/回测口径，本仓可算）
  - ② `scan.py::compute_factors` **16 个**（机会扫描面板真因子，本仓可算）
  - ③ `strong_factor_table.py::STRONG_TABLE` **27 个**（外部因子池名义强因子，**本仓算不出**）
  - ④ `policy/epu_factors.py::EPU_FAMILY` **6 个**（时序政策因子，`epu.db` 未下载）
  - 去重合并后 **54 个**；登记后 **active 17 / candidate 36 / retired 1**
  - 幂等（可反复跑）；`--dry-run` 预览、`--status` 查池
- **`derive_score()`：派生分不是实测分**。③ 那 27 个因子本仓没有计算通路
  （源目录 `data/factorpool/output/daily_scores/` 不存在），跑不了评估器。
  改用**固定权重**把 F1 已实测的 `icir120(0.40) / win_rate(0.25) / t120(0.20) / n_days(0.15)`
  折成 0-100 分（与 `eval_ts`/`factor_evaluator` 同量纲）；
  **原始四项指标全量写进 `last_eval_detail`**，可自行复算。
  用例 `test_strong_table_scores_match_formula` 会复算比对，防止手改分数。
- **★`EXPRESS_BANNED` 必须压状态**：`lhb_jg_cnt_20` / `limup_ex_5` 的截面 ICIR 全库前三
  （派生分 100），但组合层实证是**负超额**（-5.63pp / 6-7 个负年），已移出直通白名单。
  按分数自动流转会把它们标成 `active`（=可接入策略）——**那是错的**，故强制 `candidate`。
  用例 `test_express_banned_never_active` 守着。
- **`new_high_250` 标 `retired`**：`direction=0`（各口径 IC 均负，已剔除）→ 入池时按已剔除归档。

**顺带修掉三个真 bug**（前两个在评估通路上，属"质量"的一部分）：

- **`lifecycle.evaluate_factor` 的评分解析从未生效**：用 `line.split(":")[-1]` 解析
  `factor_evaluator` 的输出，而真实输出行 `  评分 54.0 ｜ 反向弱有效 ｜ ...` **没有冒号** →
  `float()` 必抛 `ValueError` → `score` 恒为 `None` → 下游
  `"active" if score and ... else "retired"` 把**每一个 cross_sectional 因子都判成 retired**。
  即"评估通路从来没 work 过，而且每次评估都在淘汰因子"。改为正则解析 + 拿不到分时**不流转状态**。
- **`registry.py` 连接泄漏**：11 个方法写 `with sqlite3.connect(...) as con:`，
  但那是**事务**上下文管理器，退出只 commit/rollback、**不关连接** → 句柄泄漏，
  Windows 上库文件被占用无法删除（测试里 `TemporaryDirectory` 清理直接 `WinError 32`）。
  改为退出即关闭的 `_conn()`；同时 `register()` 补上 `note` 更新
  （原来对已存在因子只更新 family/kind/source/freq/direction，描述改动永远写不进去）。
- **`seed_pool` 去重合并**：`vol_contract` 同属扫描面板（本仓可算）与 STRONG_TABLE（有实测指标），
  原实现直接丢弃后到者 → 它明明有 F1 实测指标却因为没有评估通路而留空分。
  改为保留可算的 `source`、**吸收外部源的分数与指标**。

- **`tests/test_factor_pool.py`（新建，25 用例，全绿）**：守卫 ① 池不能退回空池
  （登记数下限 + 各来源因子必须都在池里）；② 每个因子要么有分、要么有话说；
  ③ `retired` 必须有理有据（`direction=0` 才 retired、`EXPRESS_BANNED` 不得 active）；
  ④ 评估通路可用（守住上面两个 bug：解析真实输出格式、拿不到分不得 retired）；
  ⑤ 派生分确定性 + 可复算；⑥ 登记幂等。
- **`scripts/check_factor_coverage.py`（新建）**：把 `registry.py` 引用的 28 个因子与
  `scan.compute_factors` 实产列对表 —— 实测 **只有 16 个算得出，12 个算不出**
  （`event` 声称的"行业资金流+板块突破"在 `triggers()` 里根本没实现 → 已退化成纯基本面筛选）。
  这是**本轮新发现**，属"代码里的声称与实际不符"，已记入文档 §6.4 待修。
- 文档同步：`docs/资产盘点.md` 新增 §6.3（因子池登记）、§6.4（12 个算不出的引用因子），
  更新 §2/§3.1/§4.2/§7 中"池空"的表述与 §9 问题 16、§11 修复顺序表（两项标 ✅ 已完成）；
  `tests/test_doc_claims.py` 的 `pool_rows` 基线 0 → 54，
  `test_factor_pool_emptiness_disclosed` 改写为 `test_factor_pool_registration_disclosed`
  （原用例在池被填满后会静默 skip，守卫失效）。
- 验证：`tests.test_factor_pool` **25 用例全绿**、`tests.test_doc_claims` **15 用例全绿**。

### 资产盘点 v4.0 —— 逐条核实并改写为实话（2026-10-15 晚，按用户要求）

主题：`docs/资产盘点.md` 是给评审看的门面文档，但 v3.0 存在**多处"文档声称、仓库里没有"**的陈述。
本轮把每一条打开代码/数据库当场核对，能复现的标 ✅、数据缺失或降级的标 ⚠️、纯属虚构的标 ❌。

- **`docs/资产盘点.md` v3.0 → v4.0 全量重写（524 行）**
  - §0 新增**阅读须知**：三态标记说明（✅已落地 / ⚠️部分落地 / ❌未落地）、
    "评审者可以自己验"、以及本文档的**行文约定**（见下）。
  - §2 组件表 13 → 15 项并加"状态"列；更正"9 页"→ **79 条 API 路由 / 约 20 个看板页**。
  - §3.2 新增 Beneish 实测：`{'total': 3908, 'HIGH': 31, 'WATCH': 7, 'LOW': 3870}`，
    并说明这是 **4/8 指标降级版**（`DSRI`/`AQI`/`DEPI`/`SGAI` 四项置 0）。
  - §4.1 新增实测数据表：`bars.db` **8,466,319 行**（非 v3.0 写的 1398 万）、5,223 只、
    2019-01-02 ~ 2026-10-14；并逐个列出 **6 个缺失库**及其连带后果。
  - §6 因子体系改为实测口径表：面板可算因子 **6** / 名义强因子 **27** / 机会层引用 **28** /
    因子池登记 **0**；**"123+" 这个数字在仓库里找不到任何载体**，v4.0 起不再使用。
  - §8 skill 资产：项目 skill **实测 14 个**（v3.0 写 12）；点明 `factor-pool-methodology`、
    `skill-library` **全仓不存在**，属凭空条目。
  - §9 新增**核实发现的问题清单 19 条**（P0 文档失真 8 / P1 声称未落地 5 / 证据链断裂 3 /
    数据层缺失 / 组合层风控缺失 3），每条附**可执行的核实命令**，已逐条跑通。
  - §11 给出建议修复顺序表（P0 补 `finance_ts.db`、把状态标注落成可执行校验 …）。
- **`tests/test_doc_claims.py`（新建，14 用例，全绿）**：把"文档声称 vs 仓库实际"的落差
  **变成回归测试**，防止下次又漂移。守三件事：
  ① 已证伪的说法（`123+ 因子` / `1398万行` / `5501 只` / `factor-pool-methodology` /
  `skill-library` / `factor_{name}` 自动注册）不得回到**正文**；
  ② 关键数字与仓库实际一致（bars.db 行数与股票数、面板因子 6、强因子 27、因子池 0 行、
  项目 skill 14、缺失库 6 个）——**现实变了就红，提示同步文档**；
  ③ 关键缺口必须被点名（ATR 空壳、Beneish 降级、证据链断裂、组合层无集中度控制）。
- **豁免规则按结构判、不按关键词判**：对账文档天然要引用旧说法，第一版用
  `("原文","声称","实际"…)` 这类词表区分"引用并更正"与"当事实陈述"，结果 §0 一句
  「例如"123+ 因子"实际只有 6 个」既无标记又确实在陈述事实，词表怎么补都在打补丁。
  改为**结构约定**：旧说法只允许出现在 **引用块 `>`、核实命令的代码块、§9 问题清单** 三处，
  正文散文里出现即判违规。据此把 6 处"正文引旧说法"改写成引用块（文档本身也更清晰），
  规则同时写进 §0 与 `tests/README.md`。
- 验证：`tests.test_doc_claims` **14 用例全绿**；全套件 **110 用例 / 6 红**
  （6 红仍是既有数据缺口，与文档 §9 一一对应）。
- 顺带清理：`tests/README.md` 基线 96 → 110、套件表补 `test_doc_claims` 行、
  `test_gaps` 里"声称的 123+ 因子"改为证伪后的口径；`CHANGELOG.md` 删除一段已被
  `markdown_v2` 方案取代的旧排版说明（写着"87 用例"，与上文自相矛盾）。

**核实发现的主要问题（详见文档 §9）：**
| 类别 | 条数 | 代表 |
|---|---|---|
| 文档与事实不符（P0） | 8 | "123+ 因子"实际 6；"Beneish 8 指标全量"实际 4/8 降级；"1398万行"实际 846 万 |
| 声称有、实际未落地（P1） | 5 | reversal 的 ATR 3× 止损 `_atr()` 定义后从未被调用 → **该类型当前无止损** |
| 证据链断裂（P1） | 3 | 4 类机会的证据链引用 `logs/17年回测验证报告v2_20260810.md`，**该文件不存在** |
| 数据层缺失（P1） | 6 库 | `finance_ts.db` / `minute.db` / `lhb_full.db` / `rzrq_full.db` / `gdhs_full.db` / `shebao.db` |
| 组合层风控缺失（P1） | 3 | 无集中度上限（模拟盘 3 只 `otype` 全 `quality_gap`）、无波动率仓位、无市场体制过滤 |


## 未发布（2026-10-15 可靠性加固）

主题：让服务**稳定运行、连接不假死、资源不泄漏、失败可诊断**。不改变策略逻辑与收益假设。

- **`deck/deck_server.py` — 服务器层加固（原裸用 `ThreadingHTTPServer`）**
  - `DeckServer` 子类：`daemon_threads=True`（卡死线程不再挂住进程、重启不撞端口占用）、
    `allow_reuse_address=True`（关服后立刻重启不撞 WSAEADDRINUSE）、`request_queue_size=64`
    （backlog 5→64，集中刷新不再拒连）。
  - 覆写 `handle_error`：工作线程未捕获异常时**回 500 而非让客户端悬挂**；客户端主动断连
    （10053/10054/10058/10060…）静默跳过不计警；真异常落盘 `logs/deck_server_errors.log` 并进 60s 滑动窗口。
  - `Handler.timeout=120`：半开连接不再长期占用工作线程。
  - `signal.SIGINT/SIGTERM` 优雅退出 + `serve_forever(poll_interval=0.5)` + `finally: server_close()`：
    退出最多拖 0.5s，端口必定释放（原 SIGTERM 下 `serve_forever` 不返回、非守护线程挂住进程）。
  - **双开自检**（`_port_already_served`）：Windows 上 `SO_REUSEADDR` 允许第二个进程绑同一端口，
    造成「新进程空跑、旧进程继续服务」的**连接劫持**——本轮实测踩中：请求全落在加固前的旧进程上，
    新端点持续 404、新代码形同没部署。现启动前探测端口是否有存活实例在应答 HTTP，有则拒绝启动
    并给出 `netstat -ano | findstr :PORT` 的处置指引；空端口正常接管（watchdog 故障转移不受阻）。
  - 探测一律显式绕开系统代理：原 `/api/harness` 与自检都用裸 `urlopen`，有 `http_proxy` 时
    对 127.0.0.1 的探测被代理应答 → HARNESS 离线误报在线、空闲端口误判被占用（服务将永远起不来）。
  - 新增观测端点 `GET /api/live/server_errors`（近 60s 异常计数 + 日志尾部）。
  - `err_log` 改为可覆盖属性：测试/嵌入方把合成异常重定向到临时文件，不污染真实观测面。
  - **`launcher.py` 走同一入口**：一键启动原自己拼 `ThreadingHTTPServer`，绕过了 `DeckServer` 的
    全部加固（守护线程/异常回 500/backlog/双开自检/优雅退出）——加固只对 `python deck/deck_server.py`
    生效。现改为直接调用 `deck.deck_server.main()`，用户主入口与手工启动行为一致。
- **`data/io_utils.py`（新建）+ 全库句柄收口**
  - 统一 `read_json/read_text/read_json_safe/write_json_atomic/sqlite_ro/sqlite_rw`（纯标准库）；
    缺文件/坏 JSON 照样抛，纯重构零行为变化。
  - 批量修复 15 处 `json.load(open(...))`/`open(...).read()` 句柄不关（deck 7×24 常驻会累积泄漏，
    症状是"莫名打不开文件 / database is locked"且无报错指向真因）。
  - `deck/live_api.py` 两处 sqlite 连接在异常路径上泄漏（`close()` 在 try 内）→ `finally` 保证关闭。
- **`backtest/bt_runner.py` — 失败可诊断 + 成本口径**
  - 缺 `finance_ts.db` 时抛 `MissingDataError`（带库名/表名/补数指引）而非裸 `OperationalError`
    冒到 `/api/live/backtest_run` 刷 traceback；`run_backtest` 对数据缺失返回 `ok:False + hint` 结构化结果；
    `_load_fin()` 前置到 `_script1_score()` 入口，缺库立刻失败不白算。
  - 成本折算 `cost*2*60/max(len(ret),1)` → `2*cost/调仓间隔`（日/周/月/季/年 + 交易日数），
    与回测窗口长度解耦；原写法对 40 日调仓与季度调仓分别高估/失真。
- **`strategy/paper_account.py` — 撮合限幅按板块取幅**
  - 主板 ±10% / 创业板(300/301) ±20% / 科创板(688/689) ±20% / 北交所(430/830/870/920) ±30%；
    ST 自 2026-07-06 起与所在板块同幅（原恒 ±10%、ST ±5% → 创业板/科创板 10%~20% 正常涨跌被误判拒单）。
  - 买卖两侧统一走 `_limit_pct(code, is_st)`（原各抄一遍，改一处漏一处）。
- **测试脚手架（VS Code 就绪）**：`tests/` 三套件 68 用例（健康基线 19 全绿 / 待修清单 11→6 红 /
  可靠性回归 38 全绿，含双开自检 4 例）；`.vscode/` 的 launch/tasks/settings/extensions；`tests/_env.py`
  公共工具含 `code_only()`（源码断言剥注释，防历史缺陷描述误报）。
- **运维收敛**：实测 8787 上双实例端口劫持，已结束加固前的旧进程，仅保留加固版实例；
  被测试噪音污染的错误日志归档为 `logs/deck_server_errors.log.pre-cleanup-20261015.bak`（其中无一条真实异常）。
- 验证：全套件 68 用例 / 6 红（全部为尚未补齐的数据缺口与 ATR 未接线，见 `tests/README.md`）；
  冒烟测试 23/23；deck 40 个端点零 5xx；`/api/live/server_errors` 计数为 0；
  运行实例复检 8787 单实例无双开、19 个代表端点全 200、`/api/harness` 在线。

## 未发布（2026-10-15 P0 数据可信度修复）

主题：堵住"会写脏数据"的口子。**不涉及策略逻辑与收益假设的改动。**

- **`scripts/auto_rebalance.py` — 冻结自动成交 + 数据新鲜度门禁**
  - 成交日唯一真源改为 bars.db 的 `MAX(date)`（数据日），不再取 `datetime.now()` 墙上时钟。
    原缺陷：成交日与卖出价不同源，当日流水线失败时会把「昨天的收盘价」记成「今天的成交」并永久写入 `portfolio.history`。
  - 新增新鲜度门禁：数据日 ≠ 今天 → 默认拒绝成交，仅生成意向单。
  - 默认改为 dry-run：只写 `logs/rebalance_intents_{ts}.json`；真正落账必须显式 `--execute`，
    数据不新鲜时还需 `--allow-stale`（放行后仍按数据日记账，日期与价格同源）。
  - T+1 判定基准同步改为数据日（原用墙上时钟，与撮合日不一致 → 会把 T+1 锁定的持仓误判为可卖）。
- **`scripts/sim_live_1w.py` — 修复 otype 传递 + 按类型派生止盈止损**
  - `gather_candidates()` 的 `out.append()` 漏传 `otype`，导致 `_mk_pos()` 的 `b.get("otype") or "quality_gap"` 恒为
    `quality_gap`——所有持仓的止盈档位被统一成"质量修复股"（70% 目标 / 26 周），与短线定位直接冲突。
  - `target`/`stop`/`stop_plan`/`take_profit` 改为按真实 otype 从 `risk/type_stop_rules.py` 与
    `TAKE_PROFIT_RULES` 派生；类型原生无硬止损时保留原「保值覆盖」（补 -10% 硬止损 + -15% 回撤线）。
- **`risk/take_profit_check.py` — 止盈参数支持持仓级覆盖**
  - 原实现 `TAKE_PROFIT_RULES.get(otype or "", DEFAULT_TP)` 完全忽略 `pos["take_profit"]`，
    使持仓级止盈参数成为死配置。现优先级：持仓级 ＞ 类型档 ＞ 默认档，并输出 `tp_source` 标明来源。
  - 核心逻辑从 `main()` 拆出为 `run(positions=None, verbose=True)`，**不再触碰 `sys.argv`**。
    原缺陷：`main()` 内 `ap.parse_args()` 会吞掉调用方参数，`auto_rebalance.py --execute` 被判为非法参数直接退出。
- 验证：冒烟测试 23 通过 / 0 失败；`logs/portfolio.json` 在所有 dry-run 测试中 mtime 与大小均未变动。

## v1.0.9（2026-08-16）

- 牛散量化决策台（6 位牛散人格 + 全量数据快照）
- 排名引擎量价四强升级
- 控制页会话管理（筛选/删除/恢复）
- 同源代理稳定性
- ETF 嵌入排版修复

## v1.0.0（2026-08-16）

首个开源发布版本基线（代码 + HARNESS 运行时 + 演示数据 + 更新机制）。

- 全功能：Pitch 决策链 / 因子引擎 / 五池远期验证（含牛散主观）/ ETF 映射 / 全站 UI
- HARNESS 深度嵌入（桥接插件 + 7 空白牛散 skill + API Key 接入点）
- 动态化铁律落地：回测策略外部注册（config/strategies.yaml）、ETF 候选池配置化（config/etf_pool.yaml）
- 回测验收标准 skill（backtest-acceptance）
- 更新机制：scripts/update.py（manifest 驱动覆盖，用户配置/数据保护，应用前自动备份）

更新方式：见 docs/更新与发布.md
