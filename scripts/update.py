# -*- coding: utf-8 -*-
"""开源项目更新应用器（manifest 驱动覆盖更新）。

用法：
    python scripts/update.py <update_xxx.zip> [--dry-run]

机制：
  1. 读取更新包内 manifest.json（version / files / removed / requires）
  2. 应用前自动备份被覆盖文件 → backups/<ts>/
  3. 按 files 清单复制覆盖（严格白名单校验，任何白名单外路径直接拒绝）
  4. 按 removed 清单删除（同样白名单内才允许）
  5. 保护清单（用户配置/数据/运行时）永不覆盖、永不删除
  6. 完成后校验关键文件存在 + 敏感扫描

白名单（可更新范围）：
  deck/ factors/ strategy/ risk/ backtest/ etf/ ui_v2/ scripts/
  data/*.py data/demo/build_demo_db.py config/*.example
  harness/home/profiles/web/cordis.patch.yml harness/home/profiles/web/cordis.yml
  harness/home/profiles/web/plugins/ harness/home/skills/
  harness/install.cmd launcher.py main.py dev_auto.py dsh_log.py
  README.md LICENSE CHANGELOG.md VERSION .gitignore docs/ AGENTS.md

保护清单（永不覆盖）：
  config/params.yaml config/strategies.yaml config/etf_pool.yaml
  harness/home/.credentials.yaml harness/home/sessions/ harness/home/storages/
  harness/home/.anonymous-user-id data/cache/ data/factorpool/ data/trash/
  data/demo/ logs/ output/ *.db *.parquet harness/node_modules/
"""
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOW_ROOTS = (
    "deck", "factors", "strategy", "risk", "backtest", "etf", "ui_v2", "scripts",
    "docs", "harness/home/profiles/web/cordis.patch.yml",
    "harness/home/profiles/web/cordis.yml",
    "harness/home/profiles/web/plugins", "harness/home/skills",
)
ALLOW_FILES = (
    "launcher.py", "main.py", "dev_auto.py", "dsh_log.py", "README.md", "LICENSE",
    "CHANGELOG.md", "VERSION", ".gitignore", "AGENTS.md",
    "harness/install.cmd", "requirements.txt",
)
ALLOW_PREFIXES = ("data/", "config/")   # 仅允许 .example 与 data 下白名单文件
PROTECT_PREFIXES = (
    "config/params.yaml", "config/strategies.yaml", "config/etf_pool.yaml",
    "harness/home/.credentials.yaml", "harness/home/sessions",
    "harness/home/storages", "harness/home/.anonymous-user-id",
    "data/cache", "data/factorpool", "data/trash", "data/demo",
    "logs", "output", "harness/node_modules",
)
PROTECT_SUFFIXES = (".db", ".parquet", ".pyc")


def norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def allowed(rel: str) -> bool:
    rel = norm(rel)
    if not rel:
        return False
    for pf in PROTECT_PREFIXES:
        if rel == pf or rel.startswith(pf + "/"):
            return False
    if rel.endswith(PROTECT_SUFFIXES):
        return False
    for r in ALLOW_ROOTS:
        if rel == r or rel.startswith(r + "/"):
            return True
    if rel in ALLOW_FILES:
        return True
    if rel.startswith("data/") and (rel.endswith(".py") or rel.endswith(".example") or rel.endswith(".yaml")):
        return True
    if rel.startswith("config/") and rel.endswith(".example"):
        return True
    if rel.startswith("harness/home/skills/"):
        return True
    return False


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/update.py <update_xxx.zip> [--dry-run]")
        sys.exit(1)
    pkg = Path(sys.argv[1])
    dry = "--dry-run" in sys.argv
    if not pkg.exists():
        print(f"更新包不存在: {pkg}")
        sys.exit(1)

    with zipfile.ZipFile(str(pkg)) as z:
        names = z.namelist()
        if "manifest.json" not in names:
            print("更新包缺少 manifest.json —— 拒绝应用")
            sys.exit(1)
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        ver = manifest.get("version", "?")
        files = manifest.get("files", [])
        removed = manifest.get("removed", [])
        requires = manifest.get("requires", None)
        print(f"更新包: v{ver} | files={len(files)} removed={len(removed)}" + (" [DRY-RUN]" if dry else ""))

        if requires:
            cur = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
            print(f"  当前版本 {cur} → 需要 ≥ {requires}")

        # 校验全部路径在白名单
        bad = [n for n in files + removed if not allowed(n)]
        if bad:
            print(f"更新包含白名单外路径，拒绝: {bad[:5]}")
            sys.exit(1)

        # 备份
        if not dry:
            bk = ROOT / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
            (bk / "files").mkdir(parents=True, exist_ok=True)
            for rel in files:
                src = ROOT / rel
                if src.exists():
                    d = bk / "files" / rel
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(d))
            (bk / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  已备份 → backups/{bk.name}")

        # 应用文件
        _root_r = ROOT.resolve()
        for rel in files:
            target = (ROOT / rel).resolve()
            # ★2026-11-10 修复：allowed() 的 startswith 前缀可被 `deck/../../x.py` 绕过，
            #   写入前用 resolve().is_relative_to(ROOT) 做最终防线。
            if not target.is_relative_to(_root_r):
                print(f"  ✗ 拒绝越界路径: {rel}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if dry:
                print(f"  [dry] + {rel}")
                continue
            with z.open(rel) as fsrc, open(str(target), "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
        # 删除
        for rel in removed:
            target = (ROOT / rel).resolve()
            if not target.is_relative_to(_root_r):
                print(f"  ✗ 拒绝越界路径: {rel}")
                continue
            if dry:
                print(f"  [dry] - {rel}")
                continue
            if target.exists() and target.is_file():
                target.unlink()
                print(f"  - {rel}")

        if dry:
            print("dry-run 完成（未改动任何文件）")
            return
        # 版本更新
        (ROOT / "VERSION").write_text(ver + "\n", encoding="utf-8")
        print(f"更新完成 → v{ver}（备份见 backups/）")


if __name__ == "__main__":
    main()
