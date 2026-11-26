# 纸上短线轮换引擎设计（paper_rotation）

> 日期：2026-10-16 · 状态：**已确认**（方案 A + 设计①~⑦ 无修改）
> 规范：brainstorming → 本文档 → writing-plans → 实现

## 1. 背景与目标

**需求**：决策阶段自动买入「短线分数排名前 3」的标的；持仓中自动轮换卖出；如此不断循环。

**定位澄清**：项目**没有券商接口**（全库无 easytrader / miniQMT / 券商 API），因此本功能落在
**纸上模拟账户（paper_account）**，做的是「策略验证工具」，**不是**真实下单通道。

**目标**：验证「短线打分轮动」策略的有效性 —— 净值曲线、换手率、交易成本占比、最大回撤。

**明确非目标**：不产生真实交易委托；不改动 L2 决策卡片台账（`logs/portfolio_*.json`）；
不改变既有止损/止盈引擎的服务对象。

## 2. 关键决策（均已确认）

| 项 | 决策 |
|---|---|
| 作用层 | **A：纸上模拟账户**（`paper.db`，独立 `account='rotation'`） |
| 初始资金 | 1,000,000（虚拟） |
| 单票仓位 | **上限 25%**；实际 = `min(可用现金 ÷ 待买只数, 总资产 × 0.25)`（6 只时 ≈16.7%） |
| 最大持仓 | **6 只** |
| 买入标的 | `logs/tech_pitch_*.json` 的 `entries` 按 `score` 降序取**前 3** |
| 卖出规则 | 4 级优先级（见 §5.2），**买卖同源、都用短线分数** |
| 最小持有期 | **3 个交易日**（仅约束非止损类卖出） |
| 时间止损 | 持有 > `horizon_hint` 上限（**20 交易日**）且未达标 |
| 分数衰减 | 当前不在短线**前 10 名** |
| 触发时点 | **信号次日开盘撮合**（无前视偏差，见 §4） |
| 账户隔离 | **绝不触碰** `default` 账户，**绝不触碰** `portfolio.json` |

## 3. 架构

**新增模块**：`strategy/paper_rotation.py`

**依赖（只读）**：
- `logs/tech_pitch_*.json` — 短线分数（按 mtime 取最新）
- `data/cache/bars.db` — 次日开盘价（`open`）、一字板判定（`open==high==low`）
- `data/factorpool/output/stop_matrix_v2.json` — 止损矩阵（`stop_plan.source` 指向它）

**依赖（读写）**：`strategy/paper_account.py`（`PaperAccount(account='rotation')` 的 `buy/sell/positions/snapshot`）

**自身持久化**：`data/cache/paper.db` 新增 `rotation_pending` 表（由本模块自建，
**不改** `paper_account` 的 schema）：

```sql
CREATE TABLE IF NOT EXISTS rotation_pending (
  signal_date TEXT NOT NULL,   -- 信号日（T-1）
  exec_date   TEXT NOT NULL,   -- 拟成交日（T）
  code        TEXT NOT NULL,
  side        TEXT NOT NULL,   -- buy / sell
  qty         INTEGER NOT NULL,
  reason      TEXT,            -- stop / time_stop / rank_decay / rotation / new_entry
  created_at  TEXT,
  PRIMARY KEY (signal_date, code, side)
);
```

**调度**：接入晨更链 `data/morning_update.py`，在「科技线 Pitch v3」之后、「每日决策日报」之前
插入一步（1 行 `run_step`），受 `enabled` 开关控制。

## 4. 数据流与时序（无前视偏差）

```
T-1 日 09:10（晨更链）
  1. D盘行情同步 → bars.db 最新 = T-2
  2. 机会扫描 / Pitch / 科技线 v3 → tech_pitch_*.json（基于 T-2 收盘数据）
  3. 【轮换引擎·出单】读 T-2 分数 + T-2 收盘价
       → 用 T-2 open 撮合「T-2 日写入的 pending」（此前一日的意图）
       → 计算买卖清单 → 写入 pending（exec_date = T-1）
T   日 09:10（晨更链）
  → 同上：先执行 exec_date = T-1 的 pending（用 T-1 日 open），再出新的 pending
```

**要点**：信号在「数据可得之后」生成，成交用「信号次日的开盘价」，两者严格错开
→ 无 look-ahead；记账滞后 1 个交易日（模拟盘可接受）。

**一字板过滤**：拟买入标的在成交日 `open == high == low`（开盘即封板）→ 该单作废；
拟卖出同理（开盘即跌停无法卖出）→ 顺延。

## 5. 买卖规则

### 5.1 每日流程
1. 执行 pending（用当日开盘价撮合，含一字板过滤）
2. 计算卖出清单（§5.2）
3. 计算买入清单（§5.3，须在卖出释放资金**之后**计算可用现金）
4. 写新 pending（次日开盘执行）

### 5.2 卖出优先级（从高到低）
| 优先 | 规则 | 判据 |
|---|---|---|
| 1 | **止损** | 复用 `stop_plan`：`stop_loss_pct` / `max_drawdown_pct` / `trailing_ma` / `time_stop_weeks` 任一触发 |
| 2 | **时间止损** | 持有交易日 > 20 且未达目标收益 |
| 3 | **分数衰减** | 当前不在短线前 10 名 |
| 4 | **替换式轮动** | 满仓（= 6 只）且有新的高分标的待买 → 卖出**持仓中短线分数最低**的那只 |

**门槛**：规则 2/3/4 要求已持有 **≥3 个交易日**（规则 1 止损不受此限，风险优先）；
**制度**：当日买入的标的当日不可卖（A 股 T+1，顺带天然满足）。

### 5.3 买入与资金分配
1. 候选 = `tech_pitch.entries` 按 `score` 降序取前 3，剔除：已在持仓、成交日开盘涨停
2. 空位 = `max_positions(6) − 当前持仓数`；待买只数 = `min(候选数, 空位)`
3. 单票金额 = `min(可用现金 ÷ 待买只数, 总资产 × 0.25)`
4. 股数向下取整到 100 股（一手）；金额不足一手则跳过

### 5.4 撮合与费用
一律通过 `PaperAccount.buy/sell`（已内建佣金 + 卖出印花税，收盘价参数位传「开盘价」），
`date` 传成交日 → 账目可追溯、费用口径与既有模拟盘一致。

## 6. 配置（项目铁律：不写死，附 `.example` 模板）

`config/params.yaml` 新增：

```yaml
# ---------- 纸上短线轮换引擎（strategy/paper_rotation.py）----------
paper_rotation:
  enabled: false              # 默认关；显式开启才在晨更链执行
  account_name: rotation      # paper.db 内的独立账户（不碰 default）
  initial_cash: 1000000
  max_positions: 6            # 最大持仓数
  max_weight_per_stock: 0.25  # 单票仓位上限
  top_n_buy: 3                # 每日买入短线前 N
  exit_rank_threshold: 10     # 分数衰减：掉出前 N 名卖出
  min_hold_days: 3            # 最小持有交易日（非止损卖出门槛）
  max_hold_days: 20           # 时间止损（horizon_hint 上限）
  execution: next_open        # 信号次日开盘撮合（唯一支持值）
  tech_source: logs/tech_pitch_*.json   # 短线分数来源（glob，取 mtime 最新）
```

## 7. 边界与失败处理

| 情况 | 处理 |
|---|---|
| `tech_pitch_*.json` 不存在 / entries 为空 | 跳过本轮，打印告警，**不**空跑 |
| `enabled: false` | 直接退出（零副作用） |
| 账户不存在 | 自动初始化（`PaperAccount` 的 `_ensure_account`） |
| 标的停牌 / 无当日行情 | 该股跳过，pending 顺延一日（最多顺延 3 日，之后作废） |
| 一字板（买/卖不到） | 该单作废（记日志），不追单 |
| 卖出后现金不足以买一手 | 跳过该买入 |
| 数据缺失导致异常 | 捕获并记日志，**不中断晨更链**（`run_step` 语义） |

## 8. 验收标准

跑满 **3 个月**（≥60 个交易日）后评估：
1. **净值曲线 vs 沪深300**（`SH.000300`，基准数据已回补 2129 行覆盖 2018 起）
2. **换手率**与**交易成本占净值比**（费用由 `paper_account` 逐笔记录 → 可汇总）
3. **最大回撤**、胜率、盈亏比
4. 规则有效性：止损触发次数 / 分数衰减触发次数 / 替换轮动次数

**不做收益承诺**：本功能是验证工具。若净值长期跑输基准或成本吞噬收益，
则结论是「该策略不可行」，而不是「参数没调好」—— 这一判据必须在文档里固化，
避免为了好看而反复调参（过拟合）。

## 9. 非目标（YAGNI，明确不做）

- ❌ 真实券商下单通道
- ❌ 改动 `portfolio.json` 台账 / L2 决策卡片
- ❌ 改动既有 `position_stop_check` / `take_profit_check` 引擎
- ❌ 分钟级/日内交易（短线的 `horizon_hint` 是 ≤20 日，日频足够）
- ❌ 动态调参 / 机器学习择时（先验证规则本身）
- ❌ 多账户、杠杆、做空
