# -*- coding: utf-8 -*-
"""UI 页面字段校验工具（★2026-08-12 UI 整改 #146 沉淀）
用法: python deck/check_ui_fields.py
作用: 校验各页面 JS 消费的 API 字段 vs API 实际返回——防"前端用 d.xxx 但 API 无此字段"类显示 bug
原理: 按变量名绑定（const X = await j('/api/...')）分组校验，避免误报（d vs cd2 vs r 不同作用域）
"""
import urllib.request, json, re, glob, os, sys
from data.io_utils import read_json, read_text

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 页面 → 校验用 API（自动从页面里提取绑定，此处只列入口页面）
PAGES = ["dashboard_opp", "dashboard_watch", "dashboard_holdings",
         "dashboard_techpitch", "dashboard_actions", "dashboard_backtest",
         "dashboard_pitchtrack", "pitch", "dashboard_live", "dashboard_factors",
         "dashboard_dynamic", "dashboard_research_lib", "system_overview"]


def _page_files(pg):
    fs = glob.glob(os.path.join(BASE, "deck", f"{pg}_*.html")) \
        + [os.path.join(BASE, "deck", f"{pg}.html")]
    fs = [f for f in fs if os.path.exists(f)]
    return max(fs, key=os.path.getmtime) if fs else None


def _binds(html):
    """提取 const XX = await j('/api/...') 或 fetch 绑定"""
    binds = {}
    for m in re.finditer(r"const\s+(\w+)\s*=\s*await\s+j\('([^']+)'\)", html):
        binds[m.group(1)] = m.group(2)
    for m in re.finditer(r"const\s+(\w+)\s*=\s*await\s+fetch\('([^']+)'\)", html):
        binds[m.group(1)] = m.group(2)
    return binds


def _check_page(pg):
    f = _page_files(pg)
    if not f:
        return [], "无页面文件"
    html = read_text(f)
    binds = _binds(html)
    issues = []
    # map/forEach 回调变量（如 r）不校验——它们是行内元素字段，需人工确认
    callback_vars = set()
    for m in re.finditer(r"\.(?:map|forEach|filter)\(function\s*\((\w+)\)", html):
        callback_vars.add(m.group(1))
    for var, api in binds.items():
        try:
            d = json.load(urllib.request.urlopen(f"http://127.0.0.1:8787{api}", timeout=20))
        except Exception as e:
            issues.append(f"{api}: API 错误 {str(e)[:40]}")
            continue
        if not isinstance(d, dict):
            continue
        for m in re.finditer(rf"\b{var}\.([a-zA-Z_]+)", html):
            fd = m.group(1)
            if fd in ("then", "catch", "json", "map", "filter", "length",
                      "innerHTML", "style", "push", "toFixed", "slice", "ok", "ts"):
                continue
            if var in callback_vars:
                continue
            if fd not in d:
                issues.append(f"{api}: 用 {var}.{fd} 但 API 无此字段")
    return issues, os.path.basename(f)


def main():
    total = 0
    for pg in PAGES:
        issues, fname = _check_page(pg)
        if issues:
            print(f"{pg}: ⚠️ {len(issues)} 处（{fname}）")
            for i in issues[:8]:
                print(f"    {i}")
            total += len(issues)
        else:
            print(f"{pg}: ✅")
    print(f"\n总异常: {total} 处")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
