# -*- coding: utf-8 -*-
"""从主系统生成开源项目更新包（增量覆盖 zip，manifest 驱动）。

用法（主系统侧）：
    python <开源根>/scripts/build_update.py --from <主系统根> --version 1.0.1 [--out <dir>]

机制：
  1. 对比 主系统白名单文件 vs 开源包同名文件（内容不同/缺失 → files）
  2. 开源包白名单中存在但主系统缺失 → removed
  3. 产出 update_<version>.zip：manifest.json + 变更文件（白名单外一律跳过）
  4. 白名单/保护与 scripts/update.py 完全一致（同一套规则）
"""
import argparse
import filecmp
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPEN_ROOT = HERE.parent

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


def walk_allow(root: Path):
    """递归收集白名单内相对路径（保护清单/后缀自动排除）"""
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = norm(p.relative_to(root).as_posix())
        if allowed(rel):
            out.append(rel)
    return sorted(out)


import re as _re

# 脱敏：主系统私有路径/标识 → 开源相对路径（与开源包同一套规则）
_SAN_PATS = [
    (r"D:[\\/]+" + "劳威派" + "量化数据[\\/]+cache", "data/cache"),
    (r"D:[\\/]+" + "劳威派" + "量化数据", "data"),
    (r"C:[\\/]+Users[\\/]+" + "12985" + "[\\/]+Desktop[\\/]+" + "量化代码库思路库" + "[\\/]+因子池", "data/factorpool"),
    (r"C:[\\/]+Users[\\/]+" + "12985" + "[\\/]+Desktop[\\/]+" + "劳威派量化" + "[\\/]+deepseek-harness-quant", "."),
    (r"C:[\\/]+Users[\\/]+" + "12985", "<home>"),
    (r"129" + "85", "<home>"),
    (r"https?://quantdata888\.duckdns\.org", "https://api.tushare.pro"),
    (r"quantdata888\.duckdns\.org", "api.tushare.pro"),
    (r"DuckDNS", "代理服务器"),
    (r"datahubco\.com", "<your-backup-server>"),
    (r"datahubco", "备用HTTP服务器"),
    (r"tk_live_[A-Za-z0-9_]+", "<redacted>"),
    (r'canslim-quant', 'deepseek-harness-quant'),
]


def sanitize_text(text: str) -> str:
    for pat, rep in _SAN_PATS:
        text = _re.sub(pat, rep, text)
    return text


def sanitize_file(path: Path) -> bytes:
    """按扩展名读文件并脱敏（二进制文件原样）"""
    ext = path.suffix.lower()
    if ext in (".py", ".md", ".html", ".js", ".json", ".yaml", ".yml", ".txt", ".css", ".cmd"):
        try:
            return sanitize_text(path.read_text(encoding="utf-8", errors="ignore")).encode("utf-8")
        except Exception:
            pass
    return path.read_bytes()


def same_after_san(src: Path, dst: Path) -> bool:
    """主系统文件（脱敏后）与开源包文件是否一致"""
    if not dst.exists() or src.stat().st_size == 0 and dst.stat().st_size == 0:
        return dst.exists() and src.stat().st_size == dst.stat().st_size
    try:
        a = sanitize_file(src)
        b = dst.read_bytes()
        return hashlib.md5(a).hexdigest() == hashlib.md5(b).hexdigest()
    except Exception:
        return False


# 开源包特有文件（主系统没有也永不删除）：元数据/文档/配置模板/工具/运行时/演示数据
KEEP_ONLY = (
    "CHANGELOG.md", "LICENSE", "VERSION", "AGENTS.md", ".gitignore", "README.md",
    "requirements.txt", "launcher.py", "deck/__init__.py",
    "config/", "docs/", "scripts/", "harness/", "data/demo/",
    "backups/", "updates/", "data/factorpool/",
)


def removable(rel: str) -> bool:
    """是否允许标记 removed（主系统缺失时才考虑；开源包特有文件除外）"""
    rel = norm(rel)
    for k in KEEP_ONLY:
        if rel == k or rel.startswith(k):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="主系统根目录")
    ap.add_argument("--version", default=None, help="新版本号（默认 VERSION patch+1）")
    ap.add_argument("--out", default=str(OPEN_ROOT / "updates"))
    args = ap.parse_args()
    src = Path(args.src)
    if not src.exists():
        print(f"主系统目录不存在: {src}")
        sys.exit(1)

    cur_ver = (OPEN_ROOT / "VERSION").read_text(encoding="utf-8").strip() if (OPEN_ROOT / "VERSION").exists() else "1.0.0"
    ver = args.version
    if not ver:
        parts = cur_ver.split(".")
        ver = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    print(f"当前 {cur_ver} → 构建 v{ver}（源: {src}）")

    files, removed = [], []
    open_files = walk_allow(OPEN_ROOT)
    open_set = set(open_files)
    for rel in open_files:
        if not removable(rel):
            continue   # KEEP_ONLY：开源包专属/发布版文件，永不参与覆盖或删除
        s = src / rel
        if not s.exists():
            if removable(rel):
                removed.append(rel)
        elif not (OPEN_ROOT / rel).exists():
            files.append(rel)
        elif not same_after_san(s, OPEN_ROOT / rel):
            files.append(rel)
    # 主系统新增的白名单文件（脱敏后仍有内容才算；KEEP_ONLY 除外）
    for rel in walk_allow(src):
        if rel not in open_set and (src / rel).exists():
            if not removable(rel):
                continue
            data = sanitize_file(src / rel)
            if data and data.strip():
                files.append(rel)

    files.sort()
    removed.sort()
    print(f"变更: +{len(files)} / -{len(removed)}")
    for r in files[:8]:
        print(f"  + {r}")
    if len(files) > 8:
        print(f"  … 等 {len(files)} 个")
    for r in removed[:5]:
        print(f"  - {r}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg = out_dir / f"update_{ver}.zip"
    manifest = {"version": ver, "requires": cur_ver, "files": files, "removed": removed}
    with zipfile.ZipFile(str(pkg), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for rel in files:
            z.writestr(rel, sanitize_file(src / rel))   # ★脱敏后打包（主系统私有路径不进入开源包）
    print(f"更新包: {pkg}（{pkg.stat().st_size / 1024:.0f} KB）")
    print("应用: 解压后运行 python scripts/update.py <zip> （或直接 python scripts/update.py <zip>）")


if __name__ == "__main__":
    import sys
    main()
