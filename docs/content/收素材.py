#!/usr/bin/env python3
"""收素材 —— 把当天的事实抓成一份素材包（给宣传 Agent 用）

用法：
    python3 docs/content/收素材.py                # 今天
    python3 docs/content/收素材.py 2026-09-29     # 指定日期

来源：git log（当天提交）· docs/devlog/<日期>.md · docs/dispatch/*.md · GitHub 已关闭 Issue（需 GH_TOKEN）
输出：Markdown 到 stdout（重定向到 docs/content/<日期>-素材.md）
"""
import os, re, subprocess, sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


def sh(args):
    try:
        return subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        return "（取不到：%s）" % e


print("# 素材包 · %s（当天真实发生的事）" % day)
print()
print("## 代码提交（git log）")
log = sh(["git", "log", "--since=%s 00:00" % day, "--until=%s 23:59" % day, "--format=%h %s"])
print("```")
print(log or "（当天没有提交）")
print("```")
print()
dev = os.path.join(REPO, "docs", "devlog", "%s.md" % day)
print("## 开发日志草稿")
print(open(dev, encoding="utf-8").read() if os.path.exists(dev) else "（今天还没写 devlog）")
print()
dsp = os.path.join(REPO, "docs", "dispatch")
if os.path.isdir(dsp):
    files = sorted(f for f in os.listdir(dsp) if day.replace("-", "") in f.replace("-", "") or day[5:] in f)
    for f in files:
        print("## 派活记录：%s" % f)
        print(open(os.path.join(dsp, f), encoding="utf-8").read()[:3000])
        print()
print("## 已关闭的 Issue（当天）")
print("（用 GitHub API 抓：`gh issue list --state closed` 或脚本里加 token；没配就留空）")
