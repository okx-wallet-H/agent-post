#!/usr/bin/env bash
# A2A 薄适配层自测（#34）：agent-card / SendMessage / GetTask / 版本协商
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-$((8800 + RANDOM % 900))}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
TMPD="$DIR/.smoke-a2a-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

# 临时启动脚本：import main（自带临时库），把 a2a 挂上再起 uvicorn（main.py 挂载由 H 加，这里只是自测组装）
cat > "$TMPD/srv.py" <<EOF
import os, sys
sys.path.insert(0, "$DIR")
os.environ["HUB_DB"] = "$TMPD/a2a.db"
os.environ["HUB_USER_TOKEN"] = "$T"
import main as hub
import api_v1
import a2a
a2a.attach(db=hub._db, lock=hub._db_lock, user_token=hub.USER_TOKEN,
           q=hub.q, q1=hub.q1, ex=hub.ex, tenancy=api_v1._t())
hub.app.include_router(a2a.router)
import uvicorn
uvicorn.run(hub.app, host="127.0.0.1", port=$PORT, log_level="error")
EOF
python3 "$TMPD/srv.py" > "$TMPD/srv.log" 2>&1 &
SRV=$!
for _ in $(seq 1 40); do curl -s -m 1 "$BASE/health" 2>/dev/null | grep -q '"ok"' && break; sleep 0.3; done
curl -s -m 2 "$BASE/health" | grep -q '"ok"' || { echo "服务没起来："; tail -20 "$TMPD/srv.log"; kill $SRV 2>/dev/null; exit 1; }

echo "[1] GET agent-card → name / skills 非空（公开，不用 token）"
CARD=$(curl -s "$BASE/.well-known/agent-card.json")
NAME=$(echo "$CARD" | jq_ "['name']")
SKILLS=$(echo "$CARD" | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('skills',[])))" 2>/dev/null || echo 0)
[ -n "$NAME" ] && [ "$SKILLS" -gt 0 ] && ok "card name=$NAME，skills=$SKILLS 条" || bad "card 不对：$(echo "$CARD" | head -c 200)"
echo "$CARD" | jq_ "['supportedInterfaces'][0]['protocol']" | grep -q "HTTP" && ok "supportedInterfaces 含 HTTP+JSON" || bad "supportedInterfaces 缺"

echo "[2] 建一个 Agent，SendMessage 发给「人」→ 返回 taskId"
AG=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"A2A测试员"}')
AGT=$(echo "$AG" | jq_ "['token']")
R=$(curl -s -X POST "$BASE/a2a" -H "Authorization: Bearer $AGT" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"to":"人","message":{"role":"user","parts":[{"kind":"text","text":"A2A 你好"}]}}}')
TID=$(echo "$R" | jq_ "['result']['taskId']")
ST=$(echo "$R" | jq_ "['result']['status']")
[ -n "$TID" ] && [ "$ST" = "submitted" ] && ok "taskId=$TID status=$ST" || bad "SendMessage 不对：$R"

echo "[3] GetTask 能查到该 taskId 状态"
G=$(curl -s -X POST "$BASE/a2a" -H "Authorization: Bearer $AGT" -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"GetTask\",\"params\":{\"taskId\":\"$TID\"}}")
GT=$(echo "$G" | jq_ "['result']['status']")
[ "$GT" = "delivered" ] && ok "GetTask status=$GT" || bad "GetTask 不对：$G"
G2=$(curl -s -X POST "$BASE/a2a" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"taskId":"999999"}}')
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/a2a" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"taskId":"999999"}}')" = "404" ] && ok "不存在的 taskId → 404" || bad "坏 taskId 没 404"

echo "[4] 坏 A2A-Version → 400 且给支持列表"
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/a2a" -H "Authorization: Bearer $T" -H 'A2A-Version: 2.0' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"SendMessage","params":{"to":"人","message":{"parts":[{"text":"x"}]}}}')
[ "$C" = "400" ] && ok "A2A-Version 2.0 → 400" || bad "坏版本返回 $C（应 400）"
C2=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/a2a" -H "Authorization: Bearer $AGT" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"SendMessage","params":{"to":"人","message":{"parts":[{"text":"缺版本头也通"}]}}}')
[ "$C2" = "200" ] && ok "不带 A2A-Version 按 1.0 处理（200）" || bad "缺版本头返回 $C2"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -15 "$TMPD/srv.log"; exit 1; }
