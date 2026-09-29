#!/bin/bash
# 通信台官网 v2 自测：起本地站 → curl 200 → 23 条断言覆盖 8 屏 + 硬要求
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
echo "=== 通信台官网 v2 自测 ==="
echo "HTTP 状态码: $CODE"
[ "$CODE" = "200" ] && echo "PASS 首页可访问（200）" || echo "FAIL 首页不可访问（$CODE）"

PASS=0; FAIL=0
check() { # check <描述> <grep 模式>
  if grep -q "$2" "$PAGE"; then
    echo "PASS $1"
    PASS=$((PASS+1))
  else
    echo "FAIL $1（页面里找不到「$2」）"
    FAIL=$((FAIL+1))
  fi
}

# 屏 1 顶部
check "屏1 产品名"       "温暖通信台"
check "屏1 按钮"         "开始用"
# 屏 2 功能
check "屏2 群聊"         "群聊"
check "屏2 幂等去重"     "幂等去重"
# 屏 3 契约
check "屏3 不丢"         "不丢"
check "屏3 恰好一次"     "恰好一次"
check "屏3 SLA 赔付"     "SLA 赔付"
# 屏 4 开发中
check "屏4 开发中徽章"   "开发中"
check "屏4 多副本"       "多副本"
check "屏4 推送回调"     "推送回调"
# 屏 5 定价
check "屏5 标准档"       "¥49"
check "屏5 团队档"       "¥199"
check "屏5 超额"         "¥0.01"
# 屏 6 FAQ
check "屏6 FAQ"          "FAQ"
check "屏6 MCP 即将支持" "即将支持"
# 屏 7 底部 CTA：开始用出现至少 2 次
if [ "$(grep -o "开始用" "$PAGE" | wc -l)" -ge 2 ]; then
  echo "PASS 屏7 底部 CTA"
  PASS=$((PASS+1))
else
  echo "FAIL 屏7 底部 CTA（「开始用」只出现 1 次）"
  FAIL=$((FAIL+1))
fi
# 屏 8 页脚
check "屏8 联系邮箱"     "support@warmmsg.example.com"
# 设计令牌
check "主色 #1E3A5F"     "#1E3A5F"
check "辅色 #2D6A4F"     "#2D6A4F"
# 硬要求
check "栅格 minmax(0,1fr)" "minmax(0, 1fr)"
SIZE=$(wc -c < index.html)
if [ "$SIZE" -lt 102400 ]; then
  echo "PASS 文件大小 <100KB（${SIZE} 字节）"
  PASS=$((PASS+1))
else
  echo "FAIL 文件大小 <100KB（实际 ${SIZE} 字节）"
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
[ $FAIL -eq 0 ] && echo "注意：375px 不横滚由 CSS 保证（栅格 minmax(0,1fr)+媒体查询单列），本机无浏览器未做真渲染，建议验收时人工过一眼"
exit $FAIL
