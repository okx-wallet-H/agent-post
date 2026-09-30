#!/usr/bin/env bash
# 群聊界面（/chat）自测：起临时库 + 挂 chat.router，断言页面元素与数据接口
# 用法：bash smoke_chat.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8799}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-chat-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

# 临时启动器：main 的 app + chat.router（不改 main.py，挂载方式与正式部署一致）
cat > "$TMPD/launcher.py" <<EOF
import os, sys
sys.path.insert(0, "$DIR")
import main
import chat
chat.attach(db=main._db, lock=main._db_lock, user_token=main.USER_TOKEN,
            q=main.q, q1=main.q1, ex=main.ex, new_id=main.new_id,
            now_iso=main.now_iso, new_token=lambda: None)
main.app.include_router(chat.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
EOF

HUB_PORT="$PORT" HUB_DB="$TMPD/chat.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

echo "[1] GET /chat 单页 200 + 关键元素"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/chat")
[ "$CODE" = "200" ] && ok "GET /chat → 200" || bad "GET /chat → $CODE"
for kw in "会话" "以：" "排队中" "已送达" "成员" "发送" "minmax(0,1fr)"; do
  grep -q "$kw" "$TMPD/page.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done

echo "[2] 建 Agent + 用 /v1/send 建单聊会话"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"小助手"}')
AID=$(echo "$A" | jq_ "['id']")
[ -n "$AID" ] && ok "建好小助手（$AID）" || bad "建 Agent 失败：$A"
R=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"小助手","text":"在吗","client_msg_id":"smoke-1"}')
CID=$(echo "$R" | jq_ "['conversation_id']")
[ -n "$CID" ] && ok "单聊会话已建（$CID）" || bad "建会话失败：$R"

echo "[3] /chat/send：人发一条 + 以 Agent 身份回一条"
S1=$(curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CID\",\"text\":\"你好，这是人发的\",\"client_msg_id\":\"smoke-2\"}")
echo "$S1" | grep -qE '"ok" *: *true' && ok "人发成功（status $(echo "$S1" | jq_ "['status']")）" || bad "人发失败：$S1"
S2=$(curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CID\",\"text\":\"收到，我是小助手\",\"as_agent_id\":\"$AID\",\"client_msg_id\":\"smoke-3\"}")
echo "$S2" | grep -qE '"ok" *: *true' && ok "以 Agent 身份发成功（status $(echo "$S2" | jq_ "['status']")）" || bad "以 Agent 身份发失败：$S2"

echo "[4] 页面数据接口 /chat/state 能读到刚发的消息"
ST=$(curl -s "$BASE/chat/state?cid=$CID" -H "Authorization: Bearer $T")
echo "$ST" | grep -q "你好，这是人发的" && ok "state 读到人发的消息" || bad "state 缺人发消息：$(echo "$ST" | head -c 200)"
echo "$ST" | grep -q "收到，我是小助手" && ok "state 读到以 Agent 身份发的消息" || bad "state 缺 Agent 身份消息"
echo "$ST" | grep -q '"queued"' && ok "人发的消息标排队中（Agent 离线）" || bad "缺 queued 状态"
echo "$ST" | grep -q '"delivered"' && ok "Agent 发的消息标已送达" || bad "缺 delivered 状态"
echo "$ST" | jq_ "['conversations'][0]['title']" | grep -q "人 ↔ 小助手" && ok "会话列表含「人 ↔ 小助手」" || bad "会话列表不对"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
