#!/usr/bin/env bash
# v1 群发自测：/v1/send 的 to 写会话标题（或 cid）→ 群消息投给所有成员；原 DM 用例仍通
# 注意「agent 收件箱不含自己发的」是既定规则：两成员都取到 = 由第三方（人）发到群。
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8797}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
TMPD="$DIR/.smoke-v1-group-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/v1g.db" python3 "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

echo "[1] 建两个 Agent"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"甲"}')
B=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"乙"}')
AT=$(echo "$A" | jq_ "['token']"); AID=$(echo "$A" | jq_ "['id']")
BT=$(echo "$B" | jq_ "['token']"); BID=$(echo "$B" | jq_ "['id']")
[ -n "$AT" ] && [ -n "$BT" ] && ok "甲乙都建好" || bad "建 Agent 失败：$A $B"

echo "[2] 建群（主 API，成员甲乙）"
G=$(curl -s -X POST "$BASE/api/conversations" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d "{\"title\":\"策略讨论组\",\"members\":[\"$AID\",\"$BID\"]}")
GID=$(echo "$G" | jq_ "['id']")
[ -n "$GID" ] && ok "群建好：$GID" || bad "建群失败：$G"

echo "[3] /v1/send 写群名（人发）→ 两成员 inbox 都能取到"
R=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"策略讨论组","text":"群里喊一声","client_msg_id":"g-1"}')
[ "$(echo "$R" | jq_ "['to']['kind']")" = "group" ] && ok "按群名发出（seq $(echo "$R" | jq_ "['seq']")）" || bad "群发失败：$R"
IA=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AT")
IB=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $BT")
TA=$(echo "$IA" | jq_ "['messages'][0]['text']"); TB=$(echo "$IB" | jq_ "['messages'][0]['text']")
[ "$TA" = "群里喊一声" ] && [ "$TB" = "群里喊一声" ] && ok "甲、乙都收到群消息" || bad "群消息没到齐：甲=$TA 乙=$TB"

echo "[4] 写 cid 发群也通（甲发）→ 乙收到，甲 inbox 不含自己这条"
R2=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -d "{\"to\":\"$GID\",\"text\":\"用 cid 再喊一声\",\"client_msg_id\":\"g-2\"}")
[ "$(echo "$R2" | jq_ "['to']['kind']")" = "group" ] && ok "甲按 cid 发出" || bad "cid 群发失败：$R2"
TB2=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $BT" | jq_ "['messages'][-1]['text']")
[ "$TB2" = "用 cid 再喊一声" ] && ok "乙收到甲在群里发的" || bad "乙没收到：$TB2"
CNTA=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AT" | jq_ "['count']")
[ "$CNTA" = "1" ] && ok "甲 inbox 只有 1 条（不含自己发的，防回环）" || bad "甲 inbox 条数不对：$CNTA"

echo "[5] 原 DM 用例仍通（名字 / 人 / 拒绝给自己发）"
R3=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"甲","text":"私聊甲","client_msg_id":"dm-1"}')
[ "$(echo "$R3" | jq_ "['to']['name']")" = "甲" ] && ok "人 → 甲（按名字）" || bad "人→甲失败：$R3"
R4=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -d '{"to":"人","text":"回人一句"}')
echo "$R4" | grep -qE '"ok" *: *true' && ok "甲 → 人" || bad "甲→人失败：$R4"
R5=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -d '{"to":"甲","text":"占位"}')
echo "$R5" | grep -q "别给自己发" && ok "拒绝给自己发" || bad "给自己发没被拒：$R5"

echo "[6] 幂等：群消息重发不重复"
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"策略讨论组","text":"群里喊一声","client_msg_id":"g-1"}' >/dev/null
CNTB=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $BT" | jq_ "['count']")
[ "$CNTB" = "2" ] && ok "群消息幂等（乙还是 2 条）" || bad "幂等失效：$CNTB 条"

echo "[7] 找不到的目标 404"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"不存在的目标","text":"x"}')" = "404" ] && ok "未知目标 → 404" || bad "未知目标没拦"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
