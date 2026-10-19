# -*- coding: utf-8 -*-
"""凭据轮换助手（config/credentials rotation helper）

用途：凭据轮换后，一键把**新值**写入所有引用位置，并校验一致性。
本脚本**不生成**凭据、不打印凭据明文，只做「写入 + 校验」。

设计原则（延续项目既有约定）：
- 只做检查/写入，不自动改业务逻辑
- 值从环境变量或交互输入取，**不落到命令行历史**（避免 shell history 泄露）
- 写入前自动备份到 backups/（已被 .gitignore 覆盖）

用法（PowerShell / bash 均可）：

  # 1) 先在各平台后台生成新凭据，然后设置环境变量（推荐，避免进历史）
  export TUSHARE_TOKEN=xxxx          # Windows: set TUSHARE_TOKEN=xxxx
  export DEEPSEEK_API_KEY=sk-xxxx
  export WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx

  # 2) 执行写入
  ../.venv/Scripts/python.exe scripts/rotate_credentials.py --apply

  # 3) 只做体检（不写入），确认当前状态与引用点是否一致
  ../.venv/Scripts/python.exe scripts/rotate_credentials.py --check

凭据对应关系见下方 CREDENTIALS。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# ── 凭据登记表：env=环境变量名，files=需要同步的配置文件（相对 BASE）──
CREDENTIALS = {
    "tushare": {
        "label": "Tushare token（付费积分制，A 股行情/财务）",
        "env": "TUSHARE_TOKEN",
        "files": ["config/params.yaml"],
        "key": "tushare_token",
        "pattern": re.compile(r"^(\s*tushare_token\s*:\s*)(\"?)([^\"\n]*?)(\2\s*(?:#.*)?)$", re.M),
        "where": "tushare.pro/user/token",
        "validate_url": None,   # 平台无公开校验接口
    },
    "deepseek": {
        "label": "DeepSeek API Key（LLM 推理）",
        "env": "DEEPSEEK_API_KEY",
        "files": ["harness/home/.credentials.yaml"],
        "key": "DEEPSEEK_API_KEY",
        "pattern": re.compile(r"^(\s*DEEPSEEK_API_KEY\s*:\s*)(\"?)([^\"\n]*?)(\2\s*(?:#.*)?)$", re.M),
        "where": "platform.deepseek.com/api_keys",
        "validate_url": None,
    },
    "wecom": {
        "label": "企业微信群机器人 Webhook",
        "env": "WECOM_WEBHOOK",
        "files": ["config/wecom.yaml"],
        "key": "webhook",
        "pattern": re.compile(r"^(\s*webhook\s*:\s*)(\"?)([^\"\n]*?)(\2\s*(?:#.*)?)$", re.M),
        "where": "企业微信群机器人设置页（删除重建机器人获取新 key）",
        "validate_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={}",
    },
}

BACKUP_DIR = BASE / "backups" / f"credential_rotate_{time.strftime('%Y%m%d_%H%M%S')}"


def _mask(v: str) -> str:
    """脱敏显示，绝不打印明文。"""
    if not v:
        return "(空)"
    if len(v) <= 12:
        return f"{v[:2]}***（len={len(v)}）"
    return f"{v[:6]}...{v[-4:]}（len={len(v)}）"


def check() -> int:
    """体检：报告每个凭据的当前状态与配置/代码引用点。"""
    print("=" * 78)
    print("凭据体检（--check）：只读，不修改任何文件")
    print("=" * 78)
    problems = 0
    for name, c in CREDENTIALS.items():
        print(f"\n■ {c['label']}")
        env_val = os.environ.get(c["env"], "")
        print(f"  环境变量 {c['env']}: " + (_mask(env_val) if env_val else "(未设置)"))
        for rel in c["files"]:
            p = BASE / rel
            if not p.exists():
                print(f"  ✗ {rel}: 文件不存在")
                problems += 1
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            m = c["pattern"].search(txt)
            cur = (m.group(3).strip() if m else "")
            placeholder = (not cur) or bool(re.search(r"<your|在这里|xxx|placeholder", cur, re.I))
            state = "占位符/空" if placeholder else f"已填值 {_mask(cur)}"
            print(f"  · {rel}: {state}")
            if not placeholder:
                print(f"      ⚠ 含真实凭据 —— 外发此文件即视为泄露（.gitignore 防不了 zip）")
        print(f"  轮换入口: {c['where']}")
    print("\n" + "=" * 78)
    print(f"体检完成，问题项：{problems}")
    return 0


def apply() -> int:
    """写入环境变量中的新值到所有配置文件。"""
    print("=" * 78)
    print("凭据轮换写入（--apply）")
    print("=" * 78)
    missing = [c["env"] for c in CREDENTIALS.values() if not os.environ.get(c["env"], "")]
    # 全部缺失 → 无从写入，安全退出并给出引导（此前误写成 `if not missing`，逻辑反了）
    if len(missing) == len(CREDENTIALS):
        print("⚠ 未检测到任何凭据环境变量，本次不做任何写入（这是安全的）")
        print("  请先设置下列环境变量，再重跑 --apply：")
        for k, c in CREDENTIALS.items():
            print(f"    {c['env']:<20} ← {c['label']}")
        return 1
    if missing:
        print(f"提示：{len(missing)} 个凭据未设置环境变量，本次跳过：{', '.join(missing)}\n")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, c in CREDENTIALS.items():
        new = os.environ.get(c["env"], "").strip()
        if not new:
            print(f"\n■ {c['label']}: 环境变量未设置 → 跳过")
            continue
        for rel in c["files"]:
            p = BASE / rel
            if not p.exists():
                print(f"\n■ {rel}: 不存在 → 跳过")
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            if not c["pattern"].search(txt):
                print(f"\n■ {rel}: 未找到 {c['key']} 键 → 跳过（请手工确认格式）")
                continue
            bak = BACKUP_DIR / rel
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, bak)
            new_txt, n = c["pattern"].subn(lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(4)}", txt)
            if n == 0:
                print(f"\n■ {rel}: 替换 0 处 → 已跳过")
                continue
            p.write_text(new_txt, encoding="utf-8")
            print(f"\n■ {c['label']}")
            print(f"  已写入 {rel}（{n} 处）→ {_mask(new)}")
            print(f"  备份   {bak.relative_to(BASE)}")
            written += 1

    print("\n" + "=" * 78)
    print(f"写入完成：{written} 个文件。备份目录：{BACKUP_DIR.relative_to(BASE)}")
    print("\n后续必做：")
    print("  1. 在旧凭据所属平台**吊销/重置**（仅改文件不等于轮换完成）")
    print("  2. 重启服务：Deck(8787) 与 dev_auto 调度，使新值生效")
    print("  3. 跑一次真实调用验证新凭据可用")
    print("  4. 对外分享前用 --check 复查，确保无真实值残留")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="凭据轮换助手")
    ap.add_argument("--check", action="store_true", help="只体检，不写入")
    ap.add_argument("--apply", action="store_true", help="把环境变量中的新值写入配置")
    a = ap.parse_args()
    if a.apply:
        return apply()
    return check()


if __name__ == "__main__":
    sys.exit(main())