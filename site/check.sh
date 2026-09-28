#!/bin/bash
# 通信台官网自测：起本地站 → curl 200 → grep 7 个关键文案
# 用法：bash check.sh
set -u
cd "$(dirname "$0")"

PORT=8801
PAGE=/tmp/warm-site-check.html
LOG=/tmp/warm-site-server.log

python3 -m http.server $PORT --bind 127.0.0.1 >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT
sleep 1

CODE=$(curl -s -o "$PAGE" -w "%{http_code}" "http://127.0.0.1:$PORT/index.html")
echo "=== 通信台官网自测 ==="
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

check "产品名"         "温暖通信台"
check "不丢消息契约"   "不丢"
check "免费档价格"     "免费"
check "标准档价格"     "¥49"
check "团队档价格"     "¥199"
check "FAQ"            "FAQ"
check "MCP 即将支持"   "即将支持"

echo "---"
echo "共 $((PASS+FAIL)) 项断言：PASS $PASS，FAIL $FAIL"
exit $FAIL
