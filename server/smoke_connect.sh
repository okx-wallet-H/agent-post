#!/usr/bin/env bash
# 自助接入页（/onboard）自测：起临时库，断言页面元素，并按页面里的 curl 真发真收一条
# 用法：bash smoke_connect.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8797}"; BASE="http://127.0.0.1:$PORT"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-connect-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

# 临时启动器：main 的 app + onboard.router（不改 main.py）
cat > "$TMPD/launcher.py" <<EOF
import os, sys
sys.path.insert(0, "$DIR")
import main
import onboard
main.app.include_router(onboard.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
EOF

HUB_PORT="$PORT" HUB_DB="$TMPD/connect.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

echo "[1] GET /onboard 单页 200 + 关键元素"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/onboard")
[ "$CODE" = "200" ] && ok "GET /onboard → 200" || bad "GET /onboard → $CODE"
for kw in "三步" "复制" "第一步" "第二步" "第三步" "发第一条" "收"; do
  grep -q "$kw" "$TMPD/page.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done
grep -q "__HUB__" "$TMPD/page.html" && bad "HUB 占位符没替换干净" || ok "HUB 已按部署地址替换（无 __HUB__ 残留）"
grep -qF "$BASE/v1/send" "$TMPD/page.html" && ok "页面 curl 用的是本站地址（$BASE）" || bad "页面 curl 地址不对"

echo "[2] 按页面流程走三步：注册 → 建 Agent → 发第一条"
REG=$("$PY" -c "
import json,urllib.request
req=urllib.request.Request('$BASE/api/accounts/register',
    data=json.dumps({'email':'tester@example.com','password':'pass123'}).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
print(urllib.request.urlopen(req, timeout=10).read().decode())")
HUMAN=$(echo "$REG" | jq_ "['token']")
[ -n "$HUMAN" ] && ok "注册拿到账号 token（${#HUMAN} 位）" || bad "注册失败：$REG"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $HUMAN" -H 'Content-Type: application/json' -d '{"name":"小助手"}')
AGENT=$(echo "$A" | jq_ "['token']")
[ -n "$AGENT" ] && ok "建好 Agent，拿到 Agent token" || bad "建 Agent 失败：$A"

echo "[3] 从页面 HTML 提取 curl 模板，填 token/名字后真发"
"$PY" -c "
import re
src = open('$TMPD/page.html', encoding='utf-8').read()
m = re.search(r'<script type=\"text/plain\" id=\"curl-tpl\">(.*?)</script>', src, re.S)
assert m, '页面里找不到 curl 模板'
tpl = m.group(1).strip().replace('__TOKEN__', '$HUMAN').replace('__NAME__', '小助手')
open('$TMPD/run.sh', 'w', encoding='utf-8').write(tpl)
print('提取到模板：'); print(tpl)
" || { bad "提取 curl 模板失败"; kill $SRV 2>/dev/null; exit 1; }
SEND_OUT=$(bash "$TMPD/run.sh")
echo "$SEND_OUT" | grep -qE '"ok" *: *true' && ok "按页面 curl 发出一条（seq $(echo "$SEND_OUT" | jq_ "['seq']")）" || bad "按页面 curl 发送失败：$SEND_OUT"

echo "[4] 用 Agent token 收（页面第三步的收）"
IN=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AGENT")
CNT=$(echo "$IN" | jq_ "['count']")
[ "$CNT" = "1" ] && ok "收件箱收到 1 条（来自：$(echo "$IN" | jq_ "['messages'][0]['from']")，内容：$(echo "$IN" | jq_ "['messages'][0]['text']")）" || bad "收件箱不对：$CNT 条（$(echo "$IN" | head -c 200)）"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
