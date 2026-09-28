#!/usr/bin/env bash
# 温暖通信台 —— 自测脚本（smoke test）
#
# 用法： bash smoke.sh          （默认端口 8796，临时 DB，测完自动关服务）
#        HUB_PORT=8899 bash smoke.sh
#
# 注意：本地自测固定用 8796，8795 留给服务器。

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8796}"
BASE="http://127.0.0.1:${PORT}"
PY="${PYTHON:-python3}"
UTOK="${HUB_USER_TOKEN:-h-dev-token}"

TMPD="$DIR/.smoke-tmp"
rm -rf "$TMPD"
mkdir -p "$TMPD"
DB="$TMPD/hub.db"
LOG="$TMPD/server.log"
SRV_PID=""

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  ✅ %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$1"; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }

jcount() { # jcount '<json>' 'messages'
  "$PY" -c 'import sys,json;print(len(json.loads(sys.argv[1]).get(sys.argv[2]) or []))' "$1" "$2"
}

jget() { # jget '<json>' 'a.b.0'
  "$PY" -c 'import sys,json
d=json.loads(sys.argv[1]); p=sys.argv[2]
for part in [x for x in p.split(".") if x]:
    d = d[int(part)] if part.isdigit() else d[part]
print(d)' "$1" "$2"
}

cleanup() {
  if [ -n "$SRV_PID" ] && kill -0 "$SRV_PID" 2>/dev/null; then
    kill "$SRV_PID" 2>/dev/null
    wait "$SRV_PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$SRV_PID" 2>/dev/null || break; sleep 0.3; done
    kill -9 "$SRV_PID" 2>/dev/null
  fi
  pkill -f "uvicorn main:app --port $PORT" 2>/dev/null
  rm -rf "$TMPD"
}
trap cleanup EXIT

echo "=== 温暖通信台 自测 ==="
echo "目录：$DIR"
echo "端口：$PORT   DB：$DB（临时，测完删除）"

# ---------------------------------------------------------------- 0. 起服务
if curl -s -m 2 -o /dev/null "$BASE/health" 2>/dev/null; then
  echo
  echo "⚠ 端口 $PORT 上已经有服务在跑（可能是别的会话起的，或服务器上的实例）。"
  echo "  请先停掉它再自测，或换端口： HUB_PORT=8899 bash smoke.sh"
  exit 2
fi

step 0 "起服务 uvicorn main:app --port $PORT"
( cd "$DIR" && HUB_PORT="$PORT" HUB_DB="$DB" "$PY" -m uvicorn main:app --port "$PORT" >"$LOG" 2>&1 ) &
SRV_PID=$!

UP=0
for _ in $(seq 1 50); do
  code=$(curl -s -o "$TMPD/health.json" -w '%{http_code}' "$BASE/health" 2>/dev/null)
  if [ "$code" = "200" ]; then UP=1; break; fi
  sleep 0.3
done
if [ "$UP" = "1" ]; then
  echo "  服务已起（pid $SRV_PID）"
  echo "  GET /health → $(cat "$TMPD/health.json")"
  ok "GET /health 返回 200"
else
  echo "  服务没起来，日志末尾："; tail -20 "$LOG"
  bad "服务启动失败"
  exit 1
fi

H="Authorization: Bearer $UTOK"

# ---------------------------------------------------------------- 1. 建 Agent
step 1 "建 agent A、B（人 token）"
RA=$(curl -s -X POST "$BASE/api/agents" -H "$H" -H 'Content-Type: application/json' -d '{"name":"Agent A"}')
RB=$(curl -s -X POST "$BASE/api/agents" -H "$H" -H 'Content-Type: application/json' -d '{"name":"Agent B"}')
AID=$(jget "$RA" id); ATOK=$(jget "$RA" token)
BID=$(jget "$RB" id); BTOK=$(jget "$RB" token)
echo "  A: $RA"
echo "  B: $RB"
[ -n "$AID" ] && [ -n "$ATOK" ] && [ -n "$BID" ] && [ -n "$BTOK" ] && ok "两个 agent 建好了（token 只在此刻返回）" || bad "建 agent 失败"

LA=$(curl -s "$BASE/api/agents" -H "$H")
if echo "$LA" | grep -q "$ATOK"; then bad "GET /api/agents 泄露了 token"; else ok "GET /api/agents 不返回 token 值"; fi

# ---------------------------------------------------------------- 2. 建群
step 2 "建群（成员 A、B）"
RC=$(curl -s -X POST "$BASE/api/conversations" -H "$H" -H 'Content-Type: application/json' \
     -d "{\"title\":\"测试群\",\"members\":[\"$AID\",\"$BID\"],\"kind\":\"group\"}")
CID=$(jget "$RC" id)
echo "  POST /api/conversations → $RC"
[ -n "$CID" ] && ok "群会话建好了 cid=$CID" || bad "建会话失败"

# ---------------------------------------------------------------- 3. 人发言
step 3 "人以 human 身份发一条"
RM1=$(curl -s -X POST "$BASE/api/conversations/$CID/messages" -H "$H" -H 'Content-Type: application/json' \
      -d '{"text":"大家好，我是人（human）。"}')
echo "  → $RM1"
S1=$(jget "$RM1" seq)
[ -n "$S1" ] && [ "$S1" -ge 1 ] && ok "人的消息 seq=$S1（全局单调，非按会话从 1 起）" || bad "人的消息没拿到 seq：$S1"

# ---------------------------------------------------------------- 4. A 用自己 token 发
step 4 "A 以自己 token 发一条"
RM2=$(curl -s -X POST "$BASE/api/conversations/$CID/messages" \
      -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
      -d '{"text":"我是 Agent A，收到。","client_msg_id":"a-first-1"}')
echo "  → $RM2"
S2=$(jget "$RM2" seq)
[ -n "$S2" ] && [ "$S2" -gt "$S1" ] && ok "A 的消息 seq=$S2 > 人的 $S1（同会话内单调递增）" || bad "A 的消息 seq 不是递增：$S2（人的是 $S1）"

# ---------------------------------------------------------------- 5. A 的收件箱
step 5 "A 的 inbox 从 0 取（离线补投）"
IN=$(curl -s "$BASE/api/agents/$AID/inbox?since=0" -H "Authorization: Bearer $ATOK")
CNT=$(jcount "$IN" messages)
echo "  → $IN"
[ "$CNT" = "2" ] && ok "A 收到 2 条（人 1 条 + A 自己 1 条）" || bad "A 的 inbox 期望 2 条，实际 $CNT 条"

# B 的 inbox 也应该有 2 条（验证群广播）
INB=$(curl -s "$BASE/api/agents/$BID/inbox?since=0" -H "Authorization: Bearer $BTOK")
CNTB=$(jcount "$INB" messages)
[ "$CNTB" = "2" ] && ok "B 的 inbox 也是 2 条（群内广播正确）" || bad "B 的 inbox 期望 2 条，实际 $CNTB 条"

# ---------------------------------------------------------------- 6. 幂等
step 6 "同 client_msg_id 再发一次（幂等）"
RBEFORE=$(curl -s "$BASE/api/conversations/$CID/messages?since=0" -H "$H")
NB=$(jget "$RBEFORE" latest)
RM3=$(curl -s -X POST "$BASE/api/conversations/$CID/messages" \
      -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
      -d '{"text":"我是 Agent A，收到。","client_msg_id":"a-first-1"}')
AFTER=$(curl -s "$BASE/api/conversations/$CID/messages?since=0" -H "$H")
NA=$(jget "$AFTER" latest)
echo "  重发 → $RM3"
echo "  会话内最大 seq：重发前 $NB → 重发后 $NA"
if [ "$NB" = "$NA" ] && [ "$(jget "$RM3" seq)" = "$S2" ]; then
  ok "重复提交没有新增消息，返回第一次那条（seq=$S2）"
else
  bad "幂等失败：seq $NB → $NA"
fi

# ---------------------------------------------------------------- 7. 前端页面
step 7 "GET / 返回内联单页"
CODE=$(curl -s -o "$TMPD/index.html" -w '%{http_code}' "$BASE/")
if [ "$CODE" = "200" ] && grep -q "温暖通信台" "$TMPD/index.html"; then
  ok "GET / → 200，HTML 含「温暖通信台」（$(wc -c < "$TMPD/index.html" | tr -d ' ') 字节）"
else
  bad "GET / → $CODE，或缺「温暖通信台」"
fi

# ---------------------------------------------------------------- 8. 未鉴权
step 8 "不带 token 调 /api/agents"
C8=$(curl -s -o "$TMPD/401.json" -w '%{http_code}' "$BASE/api/agents")
echo "  HTTP $C8  body=$(cat "$TMPD/401.json")"
[ "$C8" = "401" ] && ok "未带 token → 401" || bad "期望 401，实际 $C8"

# 附：错 token 也 401
C8b=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/agents" -H 'Authorization: Bearer wrong-token-xxx')
[ "$C8b" = "401" ] && ok "错误 token → 401" || bad "错误 token 期望 401，实际 $C8b"

# ---------------------------------------------------------------- 汇总
printf '\n=== 结果：通过 %s 项，失败 %s 项 ===\n' "$PASS" "$FAIL"
if [ "$FAIL" = "0" ]; then echo "SMOKE: PASS"; else echo "SMOKE: FAIL"; fi

# ---------------------------------------------------------------- 关服务
step 9 "关掉本地服务进程"
cleanup
trap - EXIT
sleep 0.5
if curl -s -m 2 -o /dev/null "$BASE/health" 2>/dev/null; then
  echo "  ⚠ 端口 $PORT 还有响应，请手动检查"
else
  echo "  ✅ 服务已关闭，端口 $PORT 已释放"
fi

[ "$FAIL" = "0" ]
