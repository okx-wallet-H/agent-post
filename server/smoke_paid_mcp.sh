#!/bin/bash
# smoke_paid_mcp.sh —— #22 A2MCP 付费骨架冒烟（临时库 + 临时端口）
# MCP_FREE_CALLS=1：第 1 次 tools/call 成功；第 2 次 402 且 body 含 accepts 报价
#（scheme=exact / amount=0.001 / asset=USDC / network=eip155:196）+ paymentId；
# payment_intents 表多一行 pending；paid_status 工具能查到。
# 跑法： bash smoke_paid_mcp.sh；SMOKE_PORT=8807 bash smoke_paid_mcp.sh（换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/paid-mcp-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8806}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null -X POST -H "Content-Type: application/json" -d '{}' "http://127.0.0.1:$1/mcp" 2>/dev/null; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi

echo "解释器：$PY，端口 $PORT，MCP_FREE_CALLS=1"

cat > "$TMP/smoke_app.py" <<PYEOF
import sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import mcp_server
app = FastAPI()
app.include_router(mcp_server.router)
PYEOF

cd "$TMP"
MCP_DB="$TMP/smoke.sqlite3" MCP_FREE_CALLS=1 "$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 -X POST -H "Content-Type: application/json" -d '{}' "http://127.0.0.1:$PORT/mcp" && break
    sleep 0.5
done

TOKEN="smoke-token"
AUTH="Authorization: Bearer $TOKEN"
CALL='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_conversations","arguments":{}}}'
STATUS='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"paid_status","arguments":{}}}'

C1=$(curl -s --max-time 5 -o /tmp/paid1.json -w '%{http_code}' -X POST -H "$AUTH" -H "Content-Type: application/json" -d "$CALL" "http://127.0.0.1:$PORT/mcp")
B1=$(cat /tmp/paid1.json)
C2=$(curl -s --max-time 5 -o /tmp/paid2.json -w '%{http_code}' -X POST -H "$AUTH" -H "Content-Type: application/json" -d "$CALL" "http://127.0.0.1:$PORT/mcp")
B2=$(cat /tmp/paid2.json)
C3=$(curl -s --max-time 5 -o /tmp/paid3.json -w '%{http_code}' -X POST -H "$AUTH" -H "Content-Type: application/json" -d "$STATUS" "http://127.0.0.1:$PORT/mcp")
B3=$(cat /tmp/paid3.json)
DBROWS=$("$PY" - "$TMP/smoke.sqlite3" "$TOKEN" <<'PY'
import sqlite3, hashlib, sys
db, token = sys.argv[1], sys.argv[2]
acc = hashlib.sha256(token.encode()).hexdigest()[:32]
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, account_id, tool, amount, status FROM payment_intents").fetchall()
print([dict(r) for r in rows])
print("EXPECT_ACCOUNT=" + acc)
PY
)

echo "--- 第 1 次 tools/call（应 200 成功）---"
echo "$B1"
echo "--- 第 2 次 tools/call（应 402 + accepts）---"
echo "$B2"
echo "--- paid_status（应 pending=1）---"
echo "$B3"
echo "--- payment_intents 表 ---"
echo "$DBROWS"

"$PY" - "$C1" "$B1" "$C2" "$B2" "$C3" "$B3" "$DBROWS" <<'PY'
import json, sys, re
c1, b1, c2, b2, c3, b3, dbraw = sys.argv[1:]
ok = True
# 1) 第 1 次 200 且成功
if c1 != "200": print(f"FAIL: 第1次应 200，实际 {c1}"); ok = False
elif json.loads(b1).get("result") is None: print("FAIL: 第1次应有 result"); ok = False
# 2) 第 2 次 402 + accepts 报价 + paymentId
if c2 != "402": print(f"FAIL: 第2次应 402，实际 {c2}"); ok = False
else:
    d2 = json.loads(b2)
    acc = d2.get("accepts") or []
    if len(acc) != 1: print(f"FAIL: accepts 应 1 项，实际 {acc}"); ok = False
    else:
        a = acc[0]
        for k, v in {"scheme": "exact", "amount": "0.001", "asset": "USDC", "network": "eip155:196"}.items():
            if a.get(k) != v: print(f"FAIL: accepts[{k}] = {a.get(k)} != {v}"); ok = False
    if not d2.get("paymentId", "").startswith("pay_"):
        print(f"FAIL: paymentId 缺/格式错：{d2.get('paymentId')}"); ok = False
# 3) payment_intents 多一行 pending
import ast
lines = dbraw.strip().splitlines()
rows = ast.literal_eval(lines[0])
exp_acc = lines[1].split("=")[1]
if len(rows) != 1: print(f"FAIL: payment_intents 应恰好 1 行，实际 {len(rows)}"); ok = False
else:
    r = rows[0]
    if r.get("status") != "pending" or r.get("tool") != "list_conversations":
        print(f"FAIL: 落账内容不对：{r}"); ok = False
    if r.get("account_id") != exp_acc:
        print(f"FAIL: account_id 不匹配：{r.get('account_id')} vs {exp_acc}"); ok = False
    if not r.get("id", "").startswith("pay_"): print(f"FAIL: 落账 id 格式错：{r.get('id')}"); ok = False
# 4) paid_status 能查到
if c3 != "200": print(f"FAIL: paid_status 应 200，实际 {c3}"); ok = False
else:
    d3 = json.loads(b3)
    text = (d3.get("result", {}).get("content") or [{}])[0].get("text", "{}")
    ps = json.loads(text)
    if ps.get("by_status", {}).get("pending") != 1:
        print(f"FAIL: paid_status.by_status.pending 应 1，实际 {ps}"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
