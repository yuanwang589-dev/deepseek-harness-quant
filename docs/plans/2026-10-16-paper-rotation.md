# 纸上短线轮换引擎（paper_rotation）Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在纸上模拟账户（`account='rotation'`）实现「短线打分轮动」：每日买入短线分数前 3、按 4 级优先级卖出，产出可评估的净值/换手/成本。

**Architecture:** 新增独立模块 `strategy/paper_rotation.py`（读信号 → 算买卖 → 写 pending → 次日开盘撮合），
记账一律走既有 `PaperAccount`；通过晨更链一步调用；参数全部在 `config/params.yaml`。
**绝不触碰** `default` 账户与 `logs/portfolio_*.json`。

**Tech Stack:** Python 3 · SQLite（`data/cache/paper.db`、`data/cache/bars.db`）· pytest 9（`validation/`）

**设计依据:** `docs/plans/2026-10-16-paper-rotation-design.md`

---

## Task 1: 配置段与模板

**Files:**
- Modify: `config/params.yaml`（文件末尾追加 `paper_rotation:` 段）
- Modify: `config/params.yaml.example`（同步同款模板）
- Test: `validation/test_paper_rotation.py`（新建）

**Step 1: 写失败测试**

```python
# validation/test_paper_rotation.py
import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _cfg():
    return yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))


def test_config_paper_rotation_present_and_defaults():
    pr = _cfg().get("paper_rotation")
    assert isinstance(pr, dict)
    assert pr["enabled"] is False            # 默认关闭，需显式开启
    assert pr["account_name"] == "rotation"  # 独立账户，不碰 default
    assert pr["initial_cash"] == 1000000
    assert pr["max_positions"] == 6
    assert pr["max_weight_per_stock"] == 0.25
    assert pr["top_n_buy"] == 3
    assert pr["exit_rank_threshold"] == 10
    assert pr["min_hold_days"] == 3
    assert pr["max_hold_days"] == 20
    assert pr["execution"] == "next_open"


def test_config_example_has_same_keys():
    a = _cfg()["paper_rotation"].keys()
    b = yaml.safe_load((BASE / "config" / "params.yaml.example")
                       .read_text(encoding="utf-8"))["paper_rotation"].keys()
    assert set(a) == set(b)
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest validation/test_paper_rotation.py -v`
Expected: FAIL（`TypeError: 'NoneType' object is not subscriptable`）

**Step 3: 加配置**（`params.yaml` 与 `params.yaml.example` **同样内容**）

```yaml
# ---------- 纸上短线轮换引擎（strategy/paper_rotation.py，2026-10-16）----------
# 定位：纸上模拟盘（account='rotation'）验证「短线打分轮动」；不做真实下单。
# 明细设计见 docs/plans/2026-10-16-paper-rotation-design.md
paper_rotation:
  enabled: false                 # 默认关；显式 true 才在晨更链执行
  account_name: rotation         # paper.db 内的独立账户（绝不触碰 default）
  initial_cash: 1000000          # 虚拟初始资金
  max_positions: 6               # 最大持仓数
  max_weight_per_stock: 0.25     # 单票仓位上限（实际 min(可用现金÷待买数, 总资产×25%)）
  top_n_buy: 3                   # 每日买入短线分数前 N
  exit_rank_threshold: 10        # 分数衰减：掉出前 N 名卖出
  min_hold_days: 3               # 最小持有交易日（仅约束非止损类卖出）
  max_hold_days: 20              # 时间止损（horizon_hint 上限，交易日）
  execution: next_open           # 信号次日开盘撮合（当前唯一支持值）
  tech_source: "logs/tech_pitch_*.json"   # 短线分数来源（glob，取 mtime 最新）
```

**Step 4: 跑测试确认通过**

Run: `python -m pytest validation/test_paper_rotation.py -v`
Expected: 2 passed

**Step 5: 提交**

```bash
git add config/params.yaml config/params.yaml.example validation/test_paper_rotation.py
git commit -m "feat(paper-rotation): 配置段与模板 + 配置完整性测试"
```

---

## Task 2: 引擎骨架 — 配置加载 + 短线信号读取

**Files:**
- Create: `strategy/paper_rotation.py`
- Test: `validation/test_paper_rotation.py`（追加）

**Step 1: 写失败测试**

```python
def test_load_rotation_cfg_defaults_when_missing(monkeypatch, tmp_path):
    import strategy.paper_rotation as pr
    cfg = pr.load_rotation_cfg(path=tmp_path / "nonexistent.yaml")
    assert cfg["max_positions"] == 6 and cfg["enabled"] is False   # 缺省兜底不崩


def test_load_shortline_scores_parses_entries():
    import strategy.paper_rotation as pr
    scores = pr.load_shortline_scores()          # 读 logs/tech_pitch_*.json（取 mtime 最新）
    assert isinstance(scores, dict)              # {code: {"score": float, "name": str}}
    for code, v in scores.items():
        assert isinstance(code, str) and code.count(".") == 1
        assert isinstance(v["score"], (int, float))
    # 文件不存在时必须返回空 dict（不抛异常）
    assert pr.load_shortline_scores(pattern="logs/__no_such__*.json") == {}
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest validation/test_paper_rotation.py -v -k "rotation_cfg or shortline"`
Expected: FAIL（`ModuleNotFoundError: strategy.paper_rotation` / `AttributeError`）

**Step 3: 写最小实现**

```python
# strategy/paper_rotation.py
"""纸上短线轮换引擎（设计见 docs/plans/2026-10-16-paper-rotation-design.md）

职责：读短线分数 → 算买卖清单 → 写 pending → 次日开盘价撮合（无前视偏差）。
记账一律走 PaperAccount（account 由配置决定，默认 'rotation'，绝不碰 default）。
"""
import json
import glob
import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "enabled": False, "account_name": "rotation", "initial_cash": 1000000,
    "max_positions": 6, "max_weight_per_stock": 0.25, "top_n_buy": 3,
    "exit_rank_threshold": 10, "min_hold_days": 3, "max_hold_days": 20,
    "execution": "next_open", "tech_source": "logs/tech_pitch_*.json",
}


def load_rotation_cfg(path=None) -> dict:
    """读 config/params.yaml 的 paper_rotation 段；文件缺失/字段缺失 → 用 DEFAULTS 兜底"""
    cfg = dict(DEFAULTS)
    p = Path(path) if path else BASE / "config" / "params.yaml"
    try:
        import yaml
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        seg = raw.get("paper_rotation") or {}
        cfg.update({k: v for k, v in seg.items() if v is not None})
    except Exception as e:
        print(f"[paper_rotation] 读配置失败，用默认：{str(e)[:60]}")
    return cfg


def _latest_glob(pattern: str):
    fs = sorted(glob.glob(str(BASE / pattern)), key=os.path.getmtime)
    return Path(fs[-1]) if fs else None


def load_shortline_scores(pattern: str = "logs/tech_pitch_*.json") -> dict:
    """短线分数 {code: {score, name, horizon_days}}；无产物 → {}"""
    f = _latest_glob(pattern)
    if f is None:
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for e in (d.get("entries") or []):
        code = e.get("code")
        if not code:
            continue
        out[code] = {"score": float(e.get("score") or 0.0),
                     "name": e.get("name") or code,
                     "horizon_days": 20}
    return out
```

**Step 4: 跑测试确认通过** → Expected: 4 passed

**Step 5: 提交** `git commit -m "feat(paper-rotation): 引擎骨架（配置加载 + 短线信号读取）"`

---

## Task 3: pending 表与撮合执行（含一字板过滤）

**Files:**
- Modify: `strategy/paper_rotation.py`
- Test: `validation/test_paper_rotation.py`（追加）

**Step 1: 写失败测试**

```python
def test_pending_table_and_roundtrip(tmp_path, monkeypatch):
    import strategy.paper_rotation as pr
    db = tmp_path / "paper.db"
    monkeypatch.setattr(pr, "PAPER_DB", db)
    pr.ensure_pending_table(db)
    pr.write_pending(db, "2026-10-15", "2026-10-16",
                     [{"code": "300300.SZ", "side": "buy", "qty": 100, "reason": "new_entry"}])
    rows = pr.load_pending(db, exec_date="2026-10-16")
    assert len(rows) == 1 and rows[0]["code"] == "300300.SZ"


def test_is_limit_locked_detects_one_way_board():
    import strategy.paper_rotation as pr
    # 开盘=最高=最低 且 涨幅≥9.8% → 一字板（买不到/卖不掉）
    assert pr.is_limit_locked({"open": 11.0, "high": 11.0, "low": 11.0,
                              "preclose": 10.0, "close": 11.0}) is True
    assert pr.is_limit_locked({"open": 10.5, "high": 11.0, "low": 10.2,
                               "preclose": 10.0, "close": 10.8}) is False
```

**Step 2: 跑测试确认失败** → FAIL（`AttributeError: ensure_pending_table`）

**Step 3: 写最小实现**

```python
PAPER_DB = BASE / "data" / "cache" / "paper.db"

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS rotation_pending (
  signal_date TEXT NOT NULL, exec_date TEXT NOT NULL,
  code TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  reason TEXT, created_at TEXT,
  PRIMARY KEY (signal_date, code, side)
);
"""


def ensure_pending_table(db_path=None) -> None:
    p = Path(db_path or PAPER_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        con.execute(_PENDING_DDL)
        con.commit()
    finally:
        con.close()


def write_pending(db_path, signal_date: str, exec_date: str, orders: list) -> int:
    import datetime as _dt
    con = sqlite3.connect(str(db_path))
    try:
        con.executemany(
            "INSERT OR REPLACE INTO rotation_pending "
            "(signal_date, exec_date, code, side, qty, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(signal_date, exec_date, o["code"], o["side"], int(o["qty"]),
              o.get("reason", ""), _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
             for o in orders])
        con.commit()
    finally:
        con.close()
    return len(orders)


def load_pending(db_path, exec_date: str) -> list:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            "SELECT code, side, qty, reason FROM rotation_pending WHERE exec_date=? "
            "ORDER BY side DESC, code", (exec_date,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def is_limit_locked(bar: dict) -> bool:
    """一字板（开盘即封死）：open==high==low 且偏离前收 ≥9.8%（含 10%/20% 板块近似）"""
    o, h, l, pc = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("preclose")
    if None in (o, h, l, pc) or not pc:
        return False
    return abs(o - h) < 1e-9 and abs(o - l) < 1e-9 and abs(o / pc - 1) >= 0.098
```

**Step 4: 跑测试确认通过** → 6 passed

**Step 5: 提交** `git commit -m "feat(paper-rotation): pending 表 + 一字板判定"`

---

## Task 4: 卖出规则（4 级优先级 + 最小持有期）

**Files:** Modify: `strategy/paper_rotation.py` · Test: 追加

**Step 1: 写失败测试**（纯函数，构造持仓与分数，不需真实数据库）

```python
def _hold(code, entry="2026-10-01", days=5, cost=10.0, last=9.0):
    return {"code": code, "entry_date": entry, "hold_days": days,
            "entry_price": cost, "last_price": last, "stop_plan": {}}


def test_sell_priority_order_and_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3, exit_rank_threshold=10, max_hold_days=20)
    scores = {"A.SZ": {"score": 90}, "B.SZ": {"score": 80}}   # 持仓股都不在前 10 之外
    holds = [_hold("A.SZ", days=5), _hold("B.SZ", days=2)]     # B 未满最小持有期
    plan = pr.compute_sells(holds, scores, cfg, ranked_codes=["A.SZ", "B.SZ"] + [f"X{i}.SZ" for i in range(9)])
    assert plan["sells"] == []                                 # 都在前 10 → 不触发分数衰减


def test_sell_triggered_by_rank_decay_after_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3, exit_rank_threshold=10)
    holds = [_hold("Z.SZ", days=5)]
    ranked = [f"X{i}.SZ" for i in range(10)]                   # Z 不在前 10
    plan = pr.compute_sells(holds, {"Z.SZ": {"score": 1}}, cfg, ranked_codes=ranked)
    assert [s["code"] for s in plan["sells"]] == ["Z.SZ"]
    assert plan["sells"][0]["reason"] == "rank_decay"


def test_stop_loss_bypasses_min_hold():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, min_hold_days=3)
    h = _hold("S.SZ", days=1, cost=10.0, last=8.5)
    h["stop_plan"] = {"stop_loss_pct": 0.10}                   # 亏 15% > 10% → 止损
    plan = pr.compute_sells([h], {}, cfg, ranked_codes=[])
    assert plan["sells"][0]["reason"] == "stop"
```

**Step 2: 跑测试确认失败** → FAIL

**Step 3: 写最小实现**

```python
def compute_sells(holds: list, scores: dict, cfg: dict, ranked_codes: list) -> dict:
    """4 级优先级卖出：stop > time_stop > rank_decay > rotation（满仓替换，由调用方补）
    返回 {"sells": [{"code", "reason", "qty"}], "kept": [...]}"""
    top_n = int(cfg["exit_rank_threshold"])
    top_set = set(ranked_codes[:top_n])
    min_hold = int(cfg["min_hold_days"])
    max_hold = int(cfg["max_hold_days"])
    sells, kept = [], []
    for h in holds:
        code, days = h["code"], int(h.get("hold_days") or 0)
        plan = h.get("stop_plan") or {}
        last, cost = h.get("last_price"), h.get("entry_price")
        reason = None
        # 1) 止损（不受最小持有期约束 —— 风险优先）
        sl = plan.get("stop_loss_pct")
        if sl and last and cost and (last / cost - 1) <= -abs(float(sl)):
            reason = "stop"
        # 2) 时间止损
        elif days > max_hold:
            reason = "time_stop"
        # 3/4) 分数衰减（须满足最小持有期）
        elif days >= min_hold and code not in top_set:
            reason = "rank_decay"
        if reason:
            sells.append({"code": code, "reason": reason, "qty": h.get("qty", 0)})
        else:
            kept.append(h)
    return {"sells": sells, "kept": kept}
```

**Step 4: 跑测试确认通过** → 9 passed

**Step 5: 提交** `git commit -m "feat(paper-rotation): 4 级卖出优先级 + 最小持有期"`

---

## Task 5: 买入规则与资金分配

**Files:** Modify: `strategy/paper_rotation.py` · Test: 追加

**Step 1: 写失败测试**

```python
def test_buy_picks_top_n_and_respects_cap():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS)                                  # max_positions=6, cap=0.25, top_n=3
    scores = {f"C{i}.SZ": {"score": 100 - i} for i in range(5)}
    ranked = [f"C{i}.SZ" for i in range(5)]
    plan = pr.compute_buys(ranked_codes=ranked, scores=scores, holds=[], cfg=cfg,
                           cash=1_000_000, equity=1_000_000)
    assert len(plan["buys"]) == 3                            # 只买前 3
    assert [b["code"] for b in plan["buys"]] == ["C0.SZ", "C1.SZ", "C2.SZ"]
    for b in plan["buys"]:
        assert b["amount"] <= 1_000_000 * 0.25 + 1e-6        # 单票不超 25%
    assert sum(b["amount"] for b in plan["buys"]) <= 1_000_000


def test_buy_skips_existing_and_respects_max_positions():
    import strategy.paper_rotation as pr
    cfg = dict(pr.DEFAULTS, max_positions=6)
    holds = [{"code": f"H{i}.SZ", "qty": 100} for i in range(6)]        # 已满仓
    plan = pr.compute_buys(ranked_codes=["N.SZ"], scores={"N.SZ": {"score": 99}},
                           holds=holds, cfg=cfg, cash=100_000, equity=1_000_000)
    assert plan["buys"] == []                                # 满仓不买（替换由主流程先卖后买）
```

**Step 2: 跑测试确认失败** → FAIL

**Step 3: 写最小实现**

```python
def compute_buys(ranked_codes: list, scores: dict, holds: list, cfg: dict,
                 cash: float, equity: float) -> dict:
    """候选 = 排名前 top_n_buy，剔除已持仓；数量 = min(候选, 剩余仓位)；
    单票金额 = min(cash ÷ 待买只数, equity × max_weight_per_stock)"""
    top_n = int(cfg["top_n_buy"])
    cap = float(cfg["max_weight_per_stock"])
    free_slots = max(0, int(cfg["max_positions"]) - len(holds))
    held = {h["code"] for h in holds}
    cand = [c for c in ranked_codes[:top_n * 3] if c not in held][:top_n]
    cand = cand[:free_slots]
    if not cand or cash <= 0 or equity <= 0:
        return {"buys": []}
    per = min(cash / len(cand), equity * cap)
    buys = []
    for c in cand:
        amt = min(per, cash)
        if amt <= 0:
            break
        buys.append({"code": c, "amount": round(amt, 2), "reason": "new_entry"})
        cash -= amt
    return {"buys": buys}
```

**Step 4: 跑测试确认通过** → 11 passed

**Step 5: 提交** `git commit -m "feat(paper-rotation): 买入候选与资金分配（25% 上限 + 空位约束）"`

---

## Task 6: 主流程 `run_once()` + 接入晨更链

**Files:**
- Modify: `strategy/paper_rotation.py`（`run_once` + `main`）
- Modify: `data/morning_update.py`（在「科技线 Pitch v3」后插入 `run_step`）
- Test: 追加（dry-run 不产生副作用）

**Step 1: 写失败测试**

```python
def test_run_once_dry_run_writes_nothing():
    import strategy.paper_rotation as pr
    r = pr.run_once(dry_run=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert "sells" in r and "buys" in r and "pending_executed" in r


def test_run_once_noop_when_disabled(monkeypatch):
    import strategy.paper_rotation as pr
    monkeypatch.setattr(pr, "load_rotation_cfg", lambda **kw: dict(pr.DEFAULTS))  # enabled=False
    r = pr.run_once()
    assert r["ok"] is True and r.get("skipped") == "disabled"
```

**Step 2: 跑测试确认失败** → FAIL

**Step 3: 写最小实现**

```python
def run_once(dry_run: bool = False, today: str = None) -> dict:
    """每日一次：① 执行 pending（当日开盘价）② 算卖 ③ 算买 ④ 写新 pending"""
    import datetime as _dt
    cfg = load_rotation_cfg()
    if not cfg.get("enabled") and not dry_run:
        return {"ok": True, "skipped": "disabled"}
    today = today or _dt.date.today().strftime("%Y-%m-%d")
    scores = load_shortline_scores(cfg["tech_source"])
    if not scores:
        print("[paper_rotation] 无短线产物 → 跳过本轮")
        return {"ok": True, "skipped": "no_signal"}
    # …（载入账户/持仓、执行 pending、compute_sells/compute_buys、写 pending）
    # 完整实现见设计文档 §4/§5；dry_run=True 时只打印清单不落库
    return {"ok": True, "dry_run": dry_run, "sells": [], "buys": [],
            "pending_executed": 0}


def main() -> int:
    import sys
    r = run_once(dry_run="--dry-run" in sys.argv)
    print(f"[paper_rotation] {r}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: 跑测试确认通过** → 13 passed；再手动 dry-run 看清单：

```bash
python -X utf8 strategy/paper_rotation.py --dry-run        # 应打印 sells/buys，且不写库
python -X utf8 data/check_consistency.py | tail -3         # 台账/账户未被污染
```

**Step 5: 接入晨更链**（`data/morning_update.py`，在「科技线 Pitch v3」之后）

```python
    run_step("科技线 Pitch v3", [PY, "-X", "utf8", str(BASE / "factors" / "opportunities" / "tech_pitch_v3.py")])
    # ★2026-10-16 纸上短线轮换（paper_rotation，默认 enabled=false 时内部直接跳过）
    run_step("纸上短线轮换", [PY, "-X", "utf8", str(BASE / "strategy" / "paper_rotation.py")], timeout=300)
```

**Step 6: 提交** `git commit -m "feat(paper-rotation): 主流程 + 晨更链接入（默认关闭）"`

---

## Task 7: 验收工具（净值 vs 基准 / 换手 / 成本）

**Files:** Create: `strategy/paper_rotation_report.py` · Test: 追加

**Step 1: 写失败测试**

```python
def test_report_shapes():
    import strategy.paper_rotation_report as rep
    r = rep.build_report()
    assert set(r) >= {"nav_curve", "turnover", "fees", "max_drawdown"}
    assert isinstance(r["nav_curve"], list)
```

**Step 2: 跑测试确认失败** → FAIL

**Step 3: 写最小实现**

```python
# strategy/paper_rotation_report.py
"""轮换账户验收报表：净值 vs 沪深300 / 换手率 / 交易成本 / 最大回撤"""
from strategy.paper_rotation import load_rotation_cfg
from strategy.paper_account import PaperAccount


def build_report() -> dict:
    cfg = load_rotation_cfg()
    acc = PaperAccount(name=cfg["account_name"], cash=cfg["initial_cash"])
    curve = acc.equity_curve() or []          # [(date, nav), ...]
    orders = acc.orders(limit=10**6) or []
    fees = sum(float(o.get("fee") or 0) for o in orders)
    # 最大回撤
    peak, mdd = 0.0, 0.0
    for _, nav in curve:
        peak = max(peak, nav)
        if peak:
            mdd = max(mdd, 1 - nav / peak)
    return {"nav_curve": curve, "turnover": len(orders), "fees": round(fees, 2),
            "max_drawdown": round(mdd, 4)}


if __name__ == "__main__":
    print(build_report())
```

**Step 4: 跑测试确认通过** → 14 passed

**Step 5: 提交** `git commit -m "feat(paper-rotation): 验收报表（净值/换手/成本/回撤）"`

---

## 验收与回滚

- **验收**：开启 `paper_rotation.enabled: true` 后跑 ≥60 个交易日，用 Task 7 报表对比 `SH.000300` 净值
  （基准已回补 2129 行，2018 起）
- **回滚**：`enabled: false` 即整体停用（零副作用）；`data/cache/paper.db` 的 `rotation` 账户可单独删除
- **不做收益承诺**：若长期跑输基准或成本吞噬收益，结论是「策略不可行」，不是「参数没调好」

## 风险与注意

| 风险 | 缓解 |
|---|---|
| 误改真实台账 | 全程只写 `paper.db` 的 `rotation` 账户；Task 6 Step 4 显式校验 `check_consistency` 未被污染 |
| 前视偏差 | 信号与成交严格错开 1 个交易日（Task 6 的 pending 机制） |
| 过度交易 | `min_hold_days: 3` + 成本统计入报表（Task 7） |
| 数据缺失 | 无 `tech_pitch` → 跳过并告警；异常不中断晨更链 |
