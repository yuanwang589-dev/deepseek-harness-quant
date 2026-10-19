# tests/ — DSHQuant 测试脚手架

零第三方依赖，只用 Python 标准库 `unittest`（项目 venv 未装 pytest）。
**不需要 `pip install` 任何东西，开箱即可跑。**

---

## 怎么跑

```bash
# 全部（当前：136 个用例，其中 2 个红 —— 红的是尚未补齐的真实缺口）
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -t . -v

# 只跑健康基线（应全绿）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_health -v

# 只跑待修清单（红 = 该缺口未修）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_gaps -v

# 只跑可靠性回归（应全绿）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_reliability -v

# 只跑企业微信推送（应全绿，离线）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_wecom_push -v

# 只跑文档事实守卫（应全绿；红了说明 docs/资产盘点.md 与仓库实际脱节）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_doc_claims -v

# 只跑因子池守卫（应全绿；红了说明池被清空或质量分失真）
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_factor_pool -v

# 跑单个用例
.venv/Scripts/python.exe -X utf8 -m unittest tests.test_health.TestAccountRules.test_t1_enforced -v
```

VS Code 里：左侧「测试」面板（烧杯图标）会自动发现；或 `Ctrl+Shift+P` → `Tasks: Run Task`。

---

## 六个套件的语义

| 套件 | 含义 | 变红说明 |
|---|---|---|
| `test_health.py` | **健康基线**。断言系统应当具备的状态 | 系统**退化**了（不是测试写错） |
| `test_gaps.py` | **待修清单**。断言"应有的状态"，现在还没达到 | 该缺口**尚未修复**；修好后自动变绿 |
| `test_reliability.py` | **加固回归**。守住已落地的可靠性修复，防回退 | 某项加固**被改坏了**（不是测试写错） |
| `test_wecom_push.py` | **推送回归**。守住企业微信推送的消息组装/手机端排版/截断/降级/错误处理 | 推送逻辑**被改坏了**（全程离线，不联网） |
| `test_doc_claims.py` | **文档事实守卫**。断言 `docs/资产盘点.md` 的数字与仓库实际一致、证伪过的说法没回潮 | 文档**再次漂移**（或现实变了而文档没跟上） |
| `test_factor_pool.py` | **因子池守卫**。断言池非空、每个因子有分或有说明、`retired` 有理有据、派生分可复算、评估通路可用 | 池被清空，或质量分/状态失真 |

`test_gaps.py` 是一份**可执行的 TODO**：每条用例的失败信息里都写清了缺陷位置、后果和改法方向。
修完之后**不要删用例**，把它当回归护栏留着。

---

## 当前基线（2026-10-15 深夜）

```
tests.test_health       19 tests   OK
tests.test_gaps         11 tests   FAILED (failures=2)   ← 剩余 2 条全是真实缺口
tests.test_reliability  38 tests   OK
tests.test_wecom_push   28 tests   OK
tests.test_doc_claims   15 tests   OK
tests.test_factor_pool  25 tests   OK
全套件                  136 tests  2 红（~110s）
```

> **2026-10-15 深夜：5 红 → 2 红。** 数据源缺口已补 —— 五个库从"不存在"建成：
> `finance_ts.db`（261,026 行 / 67 报告期）、`lhb_full.db`（139,871 行）、
> `rzrq_full.db`（4,891,831 行 / 626 MB）、`gdhs_full.db`（140,241 行）、
> `shebao.db`。构建脚本 `data/build_finance_ts.py` 与 `data/build_event_dbs.py` 是本轮新增，
> **此前这五个库只有消费方、没有生产方**。关闭的 4 条：
> `test_finance_ts_db_present`、`test_lhb_rzrq_gdhs_shebao_present`、
> `test_backtest_script1_runs`、以及两条文档守卫。
>
> 顺带修掉一个真 bug：`backtest/bt_runner.py` 的 **成功路径不声明 `ok`**
> （失败路径有 `ok:False`），调用方 `if r.get("ok")` 无法区分"成功"与"结构异常"——
> 这正是 `test_backtest_script1_runs` 在 script1 跑通后仍红、
> `test_script1_returns_structured_failure` 的 skip 守卫失效的原因。
>
> **★注意：不要在 12 进程建库的同时跑本套件。** 构建用回滚日志模式，
> 读连接会在写事务进行中报 `database disk image is malformed`（实测 `quick_check` 为 `ok`，
> 不是真损坏）；反过来测试的读也会把构建进程的 IPC 管道搞断（`BrokenPipeError`）。
> 两者要串行。

`test_doc_claims.py` 守住三件事：① 已被证伪的说法（`123+ 因子` / `1398万行` /
`5501 只` / `factor-pool-methodology` / `skill-library` / `factor_{name}` 自动注册）
不得回到**正文**；② 文档关键数字（bars.db 行数与股票数、面板因子 6、强因子 27、
因子池 54、项目 skill 14、**受追踪的数据层库清单**）与仓库实际一致；③ 关键缺口（ATR 空壳、
Beneish 降级、证据链断裂、组合层无集中度控制）必须被点名。

> **缺失库清单用双常量对账，不硬编码。** `ALL_TRACKED_DBS` 是"必须被文档点名"的全集，
> `KNOWN_MISSING_DBS` 是"当前确实还没有"的子集，用例断言二者之差与磁盘一致。
> 此前这里硬编码"6 个全缺"，补库后该用例必红——那是守卫在正确报警，改常量即可。


> **豁免规则是按结构判的，不是按关键词判的。** 对账文档天然要引用旧说法，
> 用"原文/声称/实际"这类词表去区分"引用并更正"与"当事实陈述"只会不停打补丁。
> 所以约定为：旧说法只允许出现在 **引用块 `>`、核实命令的代码块、§9 问题清单** 三处，
> 正文散文里出现即判违规。该约定也写进了 `docs/资产盘点.md` §0。

`test_wecom_push.py` 另守住手机端排版契约：分节用 `##` 标题、账户总账用 `>` 引用块、
个股用 `-` 列表（子项 2 空格嵌套）、默认 `msgtype=markdown_v2`（`markdown` 可回退）、
且**不得出现表格 `|` / 分割线 `---` / 4 空格缩进**这些企业微信不渲染的语法。
候选池段已按需求下线，用例 `test_fmt_pool_removed` 守着它不会被悄悄加回来。

`test_reliability.py` 覆盖：deck 服务器加固（守护线程/异常回 500/backlog/双开自检）、
`data/io_utils` 资源管理（ResourceWarning 确定性检测 + 反向基线）、撮合限幅矩阵
（板块 × ST × 买卖两侧）、回测成本按调仓频率折算、`MissingDataError` 结构化失败。
其中合成异常一律重定向到临时日志，**不污染** `logs/deck_server_errors.log` 真实观测面。

### 待修清单：11 条 → 剩余 2 条（9 条已修复）

**数据缺口（1 条剩余）**
1. ~~`finance_ts.db` 缺失 → script1 策略不可用~~ → **已修（2026-10-15 深夜）**：新增
   `data/build_finance_ts.py`，利润表走 akshare `stock_lrb_em`（按报告期批量）、
   归母权益走 `stock_financial_report_sina`（逐股）。实测 261,026 行 / 5,270 只 /
   67 报告期（2010-03-31 ~ 2026-09-30）/ 公告日覆盖 99.98%。
   **script1 现在真能跑回测**（2023-01-01 ~ 2024-12-31，top5/50 股）：
   总收益 -28.3%、年化 -15.4%、最大回撤 -41.3%、夏普 -0.48，基准年化 +6.3% → 超额 -21.7 pp。
   ⚠️ 归母权益列仍在补齐（该列供 PB 用，与 script1 无关）。
2. `minute.db` 缺失 → 竞价强度信号不可用（属付费数据边界）
3. ~~`lhb_full.db` / `rzrq_full.db` / `gdhs_full.db` / `shebao.db` 缺失~~ → **已修
   （2026-10-15 深夜）**：新增 `data/build_event_dbs.py`，幂等 + 断点续传。
   实测龙虎榜 139,871 行（6,302 只 / 1,886 日）、两融 4,891,831 行（626 MB / 1,885 日）、
   股东户数 140,241 行（5,460 只 / 31 期）。**注意两融单库 626 MB**，属本仓最大的缓存库。
4. ~~`factor_pool.db` 的 `factors` 表 **0 行**~~ → **已修（2026-10-15）**：新增
   `factors/pool/seed_pool.py` 把四类因子登记入池（0 → **54 个**，active 17 / candidate 36 /
   retired 1），质量分对可评估的跑实测、对只有外部研究结论的用文档化公式派生
   （原始指标全量落 `last_eval_detail` 可复算）。顺带修掉评估通路上两个真 bug：
   评分解析因 `split(":")` 永远失败导致**所有因子被误判 retired**、
   `registry.py` 的 11 处连接泄漏。更细的守卫见 `tests/test_factor_pool.py`（25 条）。

**回测与风控（2，剩 1）**
5. ~~`bt_runner._load_fin()` 抛 `OperationalError`~~ → 已修：先查后连 + `MissingDataError`
6. ~~`script1` 回测~~ → 现返回 `ok:False + hint`；**数据补齐后本用例转绿**
7. ~~创业板/科创板 ±10% → ±20%~~ → 已修（`_limit_pct` 按板块取幅），10 条矩阵用例守着
8. ~~ST ±5% → ±10%（板块同幅）~~ → 已修
9. ~~`bt_runner.py` 硬编码 `cost * 2 * 60`~~ → 已修：按 `rebalance` 频率折算（日/周/月/季/年）
10. `position_stop_check._atr()` 定义后从未被调用 → `atr_stop_mult`（reversal 的 ATR 3× 宽止损）仍是空壳

---

## 写新用例的约定

1. **只读**：业务库一律 `mode=ro&immutable=1` 打开；需要写入的（如 `PaperAccount`）用 `tempfile` 临时库。
2. **快**：全套件应在 15 秒内跑完。大库查询必须走索引（`daily_bar` 有 `idx_daily_bar(code,adjust,date)` 与
   `idx_daily_bar_date(adjust,date)`）；要全表扫描的用 `bar_meta` 替代。
3. **不依赖网络**：需要网络/服务的用例用 `self.skipTest(...)`，让它在离线时优雅跳过而非变红。
   访问 localhost 必须显式禁用代理（办公网 `HTTP_PROXY` 会让 127.0.0.1 走代理而 502）：
   ```python
   op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
   ```
4. **断言要给"为什么"**：失败信息里写清后果，别只写 `assertEqual`。这份套件同时也是文档。
5. 公共工具放 `tests/_env.py`（下划线开头不会被当成测试用例收集）。
6. **搜源码前先过 `_env.code_only()`**：注释里描述历史缺陷时必然要提老常量名（`ST_LIMIT_PCT`）、
   老公式（`cost * 2 * 60`），字面搜全文会误报；`code_only` 剥掉注释和文档字符串，
   空格经 `ast.unparse` 规范化，再做子串断言，失败信息也不会 dump 整份源码。
7. **合成异常必须重定向 `srv.err_log`**：否则每轮测试都往真实观测面 `logs/deck_server_errors.log`
   写噪音，运维分不清真假异常。见 `test_reliability.TestServerRobustness._srv()`。

---

## 已知副作用

跑 `test_health.TestP0Regressions` 会真实执行 `scripts/auto_rebalance.main(execute=False)`，
因此会在 `logs/` 下生成时间戳的 `stop_signals_*.json` / `take_profit_signals_*.json` /
`rebalance_intents.json`。这些是设计内的只读产物，**不会改动 `logs/portfolio.json`**
（有用例专门守着这一点）。
