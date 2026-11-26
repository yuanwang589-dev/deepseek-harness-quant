# -*- coding: utf-8 -*-
"""统一数据路径解析（唯一入口）。

优先级与 `data/cache.py` 一致：环境变量 LWQUANT_CACHE_DIR > params.yaml data.cache_dir > data/cache。
所有需要 bars/finance/quality 等库的模块应从这里取路径，禁止再写 `data/cache/...` 相对字面量
（否则打包、演示库、移动数据目录、计划任务不同 cwd 时全部静默读错库）。
"""
# ★2026-11-26 静默异常统一记录（自动注入）
try:
    from dsh_log import swallow as _dsh_swallow
except ImportError:
    import sys as _dsh_sys
    from pathlib import Path as _dsh_Path
    for _d in [_dsh_Path(__file__).resolve().parent] + list(_dsh_Path(__file__).resolve().parents):
        if (_d / 'dsh_log.py').exists():
            _dsh_sys.path.insert(0, str(_d))
            break
    from dsh_log import swallow as _dsh_swallow
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def resolve_cache_dir() -> Path:
    env = os.environ.get("LWQUANT_CACHE_DIR")
    if env:
        return Path(env)
    try:
        import yaml
        cfg_path = BASE / "config" / "params.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            d = (cfg.get("data") or {}).get("cache_dir")
            if d:
                p = Path(str(d))
                return p if p.is_absolute() else BASE / p
    except Exception as _dsh_exc:
        _dsh_swallow(_dsh_exc, "data/paths.py:27 resolve_cache_dir")
    return BASE / "data" / "cache"


CACHE_DIR = resolve_cache_dir()
BARS_DB = CACHE_DIR / "bars.db"
INC_DB = CACHE_DIR / "bars_incr.db"
FINANCE_DB = CACHE_DIR / "finance.db"
QUALITY_DB = CACHE_DIR / "finance_quality.db"
STOCK_BASIC_DB = CACHE_DIR / "stock_basic.db"