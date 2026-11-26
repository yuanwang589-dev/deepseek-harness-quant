# -*- coding: utf-8 -*-
"""发布包生成器（Release zip：全量含 HARNESS 运行时，解压即用）。

用法：
    python scripts/build_release.py [--version 1.0.8]

要点（历史教训）：
  - build/ 等目录名**只在仓库顶层排除**——node_modules 内各包的 build/ 目录（typebox/build、
    @opentelemetry/build 等）必须保留，否则 DSH 启动报 ERR_MODULE_NOT_FOUND
  - 不内嵌 zip/exe（EXE 作为单独 Release 附件）
"""
import argparse
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_SKIP = {"build", "backups", "updates", "__pycache__", ".venv"}
# 内部维护文档（含本机路径，不进公开发布包）
SKIP_REL = {"docs/上传GitHub.md", "docs/发布清单_v1.0.9.md"}
# ★2026-11-10 修复：原只排除 build/backups/updates/__pycache__/.venv → 3.2GB 行情库、
#   281MB 日志、.git 全部进包（违反「行情数据禁止再分发」）。
SKIP_PREFIX = ("data/cache/", "data/factorpool/output/", "data/trash/", "logs/", "output/",
               "report/", ".git/", ".venv/", ".pytest_cache/", ".ruff_cache/",
               ".arts/", ".pi/", ".codeartsdoer/", ".playwright-mcp/", "updates/", "backups/")
SKIP_DIRS = {"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache",
             ".arts", ".pi", ".codeartsdoer", ".playwright-mcp"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--out", default=r"D:\quant-release")
    args = ap.parse_args()
    ver = args.version or (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"DSHQuant-v{ver}-Release.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(ROOT):
            if root == str(ROOT):
                dirs[:] = [d for d in dirs if d not in TOP_SKIP and d not in SKIP_DIRS]
            else:
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith((".zip", ".exe")):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if rel in SKIP_REL or any(rel.startswith(x) for x in SKIP_PREFIX):
                    continue
                z.write(p, "DSHQuant/" + rel)
                n += 1
    print(f"release: {out}  ({n} files / {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
