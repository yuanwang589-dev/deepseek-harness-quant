# 参与开发

> 面向"改这个仓库的人"，包括未来的自己。三分钟读完，照着敲即可。

## 1. 环境

```bash
# Python 3.11（不要用 3.13，vectorbt/quantstats 装不上）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # ruff + pre-commit
```

数据不进仓库（第三方协议禁止再分发），需自行获取：见 `docs/数据说明.md`。
没有数据也能跑测试——CI 只跑不依赖数据的子集。

## 2. 改完必跑的三条

```bash
cd DSHQuant

# ① 静态检查（约 1 秒，必须 0 告警）
../.venv/Scripts/python.exe -m ruff check .

# ② 全量回归（约 60 秒，必须 OK；skip 必须带理由）
../.venv/Scripts/python.exe -m unittest discover -s tests -q

# ③ 服务冒烟（约 30 秒，需服务在跑）
../.venv/Scripts/python.exe <dshquant-oneclick>/scripts/doctor.py verify
```

判定口径以 `docs/验收手册.md` 为准。**"能跑"不是验收标准，"口径可审计"才是。**

## 3. 目录职责

| 目录 | 职责 | 改动风险 |
|---|---|---|
| `data/` | 取数、清洗、建库、增量 | 高（会动 2.6GB 数据资产） |
| `factors/` | 因子计算与机会池扫描 | 中 |
| `backtest/` | backtrader / vectorbt 封装 | 中（易引入前视） |
| `risk/` | Beneish、假信号、止损、风险地图 | 中 |
| `strategy/` | 组合与调仓规则 | 中 |
| `deck/` | HTTP 服务与看板页面 | 高（线上入口） |
| `harness/` | DSH 运行时（第三方，勿改） | 极高 |

## 4. 红线

1. **不提交数据**：`data/cache/*.db`、`data/factorpool/`、`*.parquet` 一律不进 git。
2. **不提交密钥**：`config/params.yaml`、`config/wecom.yaml`、`harness/home/.credentials.yaml`。
3. **不为了变绿改测试**：唯一例外是断言锚点本身写错（如文档数字漂移），此时必须在 PR 里写明"这是锚点漂移，不是代码缺陷"，并同步更新文档。
4. **不在下载期开看板**：`immutable=1` 只读连接与并发写冲突，会报 `database disk image is malformed`（不是数据损坏）。
5. **不并发跑 baostock 任务**：有并发登录上限，会全部 `login fail`。

## 5. 提交与 PR

- 提交信息写"为什么"，不写"改了什么"（diff 已经说明了）。
- 一个 PR 只做一件事；附带的重构另开。
- 改动涉及数据口径（复权、PIT、T+1、单位）时，必须在描述里贴出验证证据（行数 / 抽样值 / 对照结果）。
- 已知缺口写成**故意红的测试**（`tests/test_gaps.py`），不要用注释代替。

## 6. 提交前钩子（可选）

```bash
../.venv/Scripts/pre-commit.exe install
```

钩子只做检查、不自动改文件——批量 `--fix` 会在首次提交时制造巨大 diff，掩盖真实改动。
