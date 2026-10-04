#!/usr/bin/env bash
# 群聊 @ 点名功能（#30）自测：页面关键串 + 数据接口 mentions 提取
# 用法：bash smoke_chat_mention.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8794}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-mention-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

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

HUB_PORT="$PORT" HUB_DB="$TMPD/mention.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

echo "[1] GET /chat 200 + @ 关键串"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/chat")
[ "$CODE" = "200" ] && ok "GET /chat → 200" || bad "GET /chat → $CODE"
for kw in "@全体" "仅共享" "不打断任何人" "点名" "待补读" "mentions" "mention-pop" "renderText" "最近处理"; do
  grep -q -- "$kw" "$TMPD/page.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done

echo "[2] 建 2 个 Agent + 建群"
A1=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"小助手"}')
ID1=$(echo "$A1" | jq_ "['id']")
A2=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"二助手"}')
ID2=$(echo "$A2" | jq_ "['id']")
G=$(curl -s -X POST "$BASE/api/conversations" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"title\":\"作战群\",\"members\":[\"$ID1\",\"$ID2\"]}")
GID=$(echo "$G" | jq_ "['id']")
[ -n "$GID" ] && ok "建群成功（$GID，2 成员）" || bad "建群失败：$G"

echo "[3] 发消息并验证 mentions 提取"
# 口径（与 #29 一致）：mentions 字段总是存在；有 @ 是名字列表，没 @ 是空数组 []
curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$GID\",\"text\":\"@小助手 看下数据\",\"client_msg_id\":\"mt-1\"}" >/dev/null
ST=$(curl -s "$BASE/chat/state?cid=$GID" -H "Authorization: Bearer $T")
R1=$(echo "$ST" | "$PY" -c "import sys,json;print('小助手' in json.load(sys.stdin)['messages'][-1]['mentions'])")
[ "$R1" = "True" ] && ok "mentions 提取到「小助手」" || bad "mentions 缺「小助手」：$(echo "$ST" | jq_ "['messages'][-1]['mentions']")"

curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$GID\",\"text\":\"@全体 集合开会\",\"client_msg_id\":\"mt-2\"}" >/dev/null
ST=$(curl -s "$BASE/chat/state?cid=$GID" -H "Authorization: Bearer $T")
R2=$(echo "$ST" | "$PY" -c "import sys,json;print('全体' in json.load(sys.stdin)['messages'][-1]['mentions'])")
[ "$R2" = "True" ] && ok "mentions 提取到「全体」" || bad "mentions 缺「全体」"

curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$GID\",\"text\":\"随便说说\",\"client_msg_id\":\"mt-3\"}" >/dev/null
ST=$(curl -s "$BASE/chat/state?cid=$GID" -H "Authorization: Bearer $T")
R3=$(echo "$ST" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['messages'][-1]['mentions'] == [])")
[ "$R3" = "True" ] && ok "没 @ 人时 mentions 是空数组 []" || bad "没 @ 人 mentions 应为 []：$(echo "$ST" | jq_ "['messages'][-1]['mentions']")"

echo "[4] 验证成员「最近被唤醒处理」时间（handled）"
curl -s -X POST "$BASE/chat/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$GID\",\"text\":\"收到，已处理\",\"as_agent_id\":\"$ID1\",\"client_msg_id\":\"mt-4\"}" >/dev/null
ST=$(curl -s "$BASE/chat/state?cid=$GID" -H "Authorization: Bearer $T")
H1=$(echo "$ST" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['handled'].get('$ID1',''))")
[ -n "$H1" ] && ok "小助手发言后 handled 有时间（$H1）" || bad "handled 缺小助手：$(echo "$ST" | jq_ "['handled']")"
H2=$(echo "$ST" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['handled'].get('$ID2',''))")
[ -z "$H2" ] && ok "没发言的二助手 handled 无记录（页面显示待补读）" || bad "二助手不该有 handled 记录：$H2"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
