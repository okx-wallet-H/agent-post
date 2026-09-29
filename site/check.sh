#!/bin/bash
# 通信台官网 v2（按 H 验收单）自测：起本地站 → curl 200 → 断言验收单原句 + 硬要求
# 用法：bash check.sh
set -u
cd "$(dirname "$0")"

PORT=8801
PAGE=/tmp/warm-site-v2-check.html
LOG=/tmp/warm-site-v2-server.log

python3 -m http.server $PORT --bind 127.0.0.1 >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT
sleep 1

CODE=$(curl -s -o "$PAGE" -w "%{http_code}" "http://127.0.0.1:$PORT/index.html")
echo "=== 通信台官网 v2 自测（H 验收单原句） ==="
echo "HTTP 状态码: $CODE"
[ "$CODE" = "200" ] && echo "PASS 首页可访问（200）" || echo "FAIL 首页不可访问（$CODE）"

PASS=0; FAIL=0
check() { # check <描述> <grep 模式>
  if grep -q -- "$2" "$PAGE"; then
    echo "PASS $1"
    PASS=$((PASS+1))
  else
    echo "FAIL $1（页面里找不到「$2」）"
    FAIL=$((FAIL+1))
  fi
}

# 顶栏（验收单 8）
check "顶栏导航 定价"     "定价"
check "顶栏导航 可靠性契约" "可靠性契约"
check "顶栏导航 FAQ"      "FAQ"
check "顶栏导航 登录"     "登录"
# Hero（验收单 2）
check "Hero 主标"         "发出去，就一定到。"
check "Hero 副标"         "给一个人和他的一群 Agent 用的可靠消息层。"
check "Hero 按钮"         "免费开始"
check "Hero 灰字"         "14 天 / 2000 条，无需信用卡"
# Problem（验收单 3）
check "Problem 标题"      "别的消息层管“聊”，我们管“到”。"
check "事实短句1"         "全行业只有两家写了投递语义"
check "事实短句2"         "「不丢」没人做成契约"
check "事实短句3"         "全行业按人类席位收费"
# How（验收单 4）
check "How 标题"          "四条硬保证，写进 README。"
check "卡1 落盘才返回"    "落盘才返回"
check "卡2 离线补投"      "离线补投"
check "卡3 幂等去重"      "幂等去重"
check "卡4 可查轨迹"      "可查轨迹"
# Pricing（验收单 5）
check "Pricing 标题"      "按 Agent 数 × 消息条数，不按人类席位。"
check "免费券"            "免费券"
check "标准 ¥49"          "¥49"
check "团队 ¥199"         "¥199"
check "超额灰字"          "超额 ¥0.01/条"
# Contract（验收单 6）
check "Contract 标题"     "我们承诺做到的，和还在做的。"
check "左列 已上线"       "已上线"
check "右列 开发中徽章"   "开发中"
check "右列 SLA"          "SLA"
check "右列 多副本"       "多副本"
check "右列 推送回调"     "推送回调"
# FAQ（验收单 7）
if [ "$(grep -o "<details" "$PAGE" | wc -l)" -ge 5 ]; then
  echo "PASS FAQ 5 条折叠"
  PASS=$((PASS+1))
else
  echo "FAIL FAQ 5 条折叠（details 少于 5 个）"
  FAIL=$((FAIL+1))
fi
# Footer（验收单 7）
check "Footer GitHub"     "GitHub"
check "Footer 文档"       "文档"
check "Footer ©"          "© 2026"
# 令牌（验收单 9）
check "令牌 --primary"    "--primary: #1E3A5F"
check "令牌 --accent"     "--accent: #2D6A4F"
check "令牌 暖纸底"       "#FAFAF8"
# 品牌名（H 更正原文）
check "title 品牌名"       "<title>AgentPost · 智能体邮局</title>"
check "h1 品牌名"          "AgentPost · 智能体邮局"
# A2A 原文引用（H 更正原文）
check "A2A 引用原文"       "Messages MUST NOT be considered a reliable delivery mechanism for critical information"
# 品牌名占位不得残留
if grep -q "{{" "$PAGE"; then
  echo "FAIL 占位符残留（页面里还有 {{ 未替换）"
  FAIL=$((FAIL+1))
else
  echo "PASS 无占位符残留"
  PASS=$((PASS+1))
fi
# 硬要求
check "栅格 minmax(0,1fr)" "minmax(0, 1fr)"
SIZE=$(wc -c < index.html)
if [ "$SIZE" -lt 102400 ]; then
  echo "PASS 文件大小 ≤100KB（${SIZE} 字节）"
  PASS=$((PASS+1))
else
  echo "FAIL 文件大小 ≤100KB（实际 ${SIZE} 字节）"
  FAIL=$((FAIL+1))
fi
if grep -qE '(href|src)="https?://' index.html; then
  echo "FAIL 无外链（发现 http/https 引用）"
  FAIL=$((FAIL+1))
else
  echo "PASS 无外链"
  PASS=$((PASS+1))
fi

echo "---"
echo "共 $((PASS+FAIL)) 项断言：PASS $PASS，FAIL $FAIL"
echo "注意：规范文件 ui-notes-20260929.md 未取得，页面按 H 更正消息的逐字原文实现；375px 不横滚由 CSS 静态保证"
exit $FAIL
