#!/usr/bin/env bash
# v1 简化 API 自测：三行接入那条路
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8798}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
TMPD="$DIR/.smoke-v1-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/v1.db" python3 "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

echo "[1] 建 Agent（一行）"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"小助手"}')
AT=$(echo "$A" | jq_ "['token']"); AID=$(echo "$A" | jq_ "['id']")
[ -n "$AT" ] && ok "建好小助手，token 长度 ${#AT}" || bad "建 Agent 失败：$A"

echo "[2] 用名字直接发（不用先查 id）"
R=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"小助手","text":"你好，这是人发的","client_msg_id":"h-1"}')
if [ "$(echo "$R" | jq_ "['to']['name']")" = "小助手" ]; then ok "按名字送到：$(echo "$R" | jq_ "['seq']")"; else bad "按名字发失败：$R"; fi

echo "[3] Agent 直接给「人」发一条（不用知道 id）"
R3=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -d '{"to":"人","text":"收到，我是小助手"}')
echo "$R3" | grep -qE '"ok" *: *true' && ok "agent → 人 成功（seq $(echo "$R3" | jq_ "['seq']")）" || bad "agent → 人 失败：$R3"
echo "[3b] 给自己发应当被拒"
R2=$(curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $AT" -H 'Content-Type: application/json' -d '{"to":"小助手","text":"占位"}')
echo "$R2" | grep -q "别给自己发" && ok "拒绝给自己发" || bad "给自己发没被拒：$R2"

echo "[4] Agent 收件箱（离线也能收到）"
IN=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AT")
CNT=$(echo "$IN" | jq_ "['count']")
[ "$CNT" = "2" ] && ok "小助手收到 2 条（第一条来自：$(echo "$IN" | jq_ "['messages'][0]['from']")）" || bad "收件箱条数不对：$CNT（内容 $(echo "$IN" | head -c 200)）"

echo "[5] 幂等：同一条再发一次"
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"小助手","text":"你好，这是人发的","client_msg_id":"h-1"}' >/dev/null
CNT2=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AT" | jq_ "['count']")
[ "$CNT2" = "2" ] && ok "重复提交没有产生第三条" || bad "幂等失效：$CNT2 条"

echo "[6] me / agents 两个探查端点"
curl -s "$BASE/v1/me" -H "Authorization: Bearer $AT" | grep -qE '"kind" *: *"agent"' && ok "/v1/me 认得出身份" || bad "/v1/me 不对"
curl -s "$BASE/v1/agents" -H "Authorization: Bearer $T" | grep -q '小助手' && ok "/v1/agents 列得出名字" || bad "/v1/agents 不对"

echo "[7] 不带 token"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/inbox")" = "401" ] && ok "无 token → 401" || bad "无 token 没拦住"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "SMOKE-V1: PASS" || { echo "SMOKE-V1: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
