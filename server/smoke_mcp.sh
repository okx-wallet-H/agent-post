#!/usr/bin/env bash
# MCP ↔ /v1 同库互见自测（bug #18 修复验收）：
#   MCP send_message 发一条 → /v1/inbox 能收到；
#   /v1/send 发一条 → MCP fetch_inbox 也能收到。
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8797}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-mcp-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/hub.db" MCP_FREE_CALLS=100 "$PY" "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }
rpc(){ curl -s -X POST "$BASE/mcp" -H "Authorization: Bearer $2" -H 'Content-Type: application/json' -d "$3"; }

echo "[1] 建两个 Agent（甲、乙）"
AA=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"甲"}')
BB=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"乙"}')
AT=$(echo "$AA" | jq_ "['token']"); BT=$(echo "$BB" | jq_ "['token']")
[ -n "$AT" ] && [ -n "$BT" ] && ok "甲、乙建好" || bad "建 Agent 失败：$AA $BB"

echo "[2] MCP 协议握手"
INIT=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
echo "$INIT" | grep -q '"warm-hub"' && ok "initialize 通" || bad "initialize 失败：$INIT"
TL=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
echo "$TL" | grep -q 'send_message' && echo "$TL" | grep -q 'list_conversations' && echo "$TL" | grep -q 'fetch_inbox' \
  && ok "tools/list 三个工具齐" || bad "tools/list 失败：$TL"

echo "[3] MCP send_message（to=乙）→ 乙的 /v1/inbox 能收到"
S1=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"send_message","arguments":{"to":"乙","text":"MCP→乙"}}}')
echo "$S1" | grep -q '"isError": *false' && ok "MCP send_message 成功（$(echo "$S1" | jq_ "['result']['content'][0]['text']")）" || bad "MCP 发失败：$S1"
IN=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $BT")
CNT=$(echo "$IN" | jq_ "['count']")
if echo "$IN" | grep -q 'MCP→乙' && [ "$CNT" -ge 1 ]; then
  ok "乙的 /v1/inbox 收到 MCP 消息（count=$CNT）"
else
  bad "v1/inbox 没收到：$IN"
fi

echo "[4] /v1/send（乙→甲）→ 甲的 MCP fetch_inbox 也能收到"
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $BT" -H 'Content-Type: application/json' -d '{"to":"甲","text":"v1→甲"}' >/dev/null
F=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fetch_inbox","arguments":{"since":0}}}')
echo "$F" | grep -q 'v1→甲' && ok "MCP fetch_inbox 收到 v1 消息" || bad "fetch_inbox 没收到：$F"

echo "[5] 收件箱语义：甲的不含自己发的"
F2=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"fetch_inbox","arguments":{"since":0}}}')
if echo "$F2" | grep -q 'MCP→乙'; then
  bad "甲的收件箱含自己发的消息"
else
  ok "甲的收件箱不含自己发的（守候进程不回环）"
fi

echo "[6] list_conversations 读同库（甲能看到会话与成员）"
LC=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"list_conversations","arguments":{}}}')
TXT=$(echo "$LC" | jq_ "['result']['content'][0]['text']")
echo "$TXT" | grep -q '"kind": "dm"' && echo "$TXT" | grep -q '甲 ↔ 乙' && ok "list_conversations 读同库会话" || bad "list_conversations 不对：$LC"

echo "[7] MCP send_message 发给「人」→ 人的 /v1/inbox 收到"
S3=$(rpc /mcp "$AT" '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"send_message","arguments":{"to":"人","text":"MCP→人"}}}')
echo "$S3" | grep -q '"isError": *false' && ok "MCP → 人 成功" || bad "MCP → 人 失败：$S3"
HIN=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $T")
echo "$HIN" | grep -q 'MCP→人' && ok "人的 /v1/inbox 收到" || bad "人的 inbox 没收到：$HIN"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
