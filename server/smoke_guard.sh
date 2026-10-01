#!/usr/bin/env bash
# 注册防刷自测（#17）：限流 429 / 坏邮箱 400 / 邀请码 403
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8799}"; BASE="http://127.0.0.1:$PORT"
TMPD="$DIR/.smoke-guard-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
code(){ curl -s -o /dev/null -w '%{http_code}' "$@"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }
start_srv(){
  if [ -n "${2:-}" ]; then
    HUB_PORT="$PORT" HUB_DB="$TMPD/$1.db" HUB_INVITE_CODE="$2" python3 "$DIR/main.py" >"$TMPD/$1.log" 2>&1 &
  else
    HUB_PORT="$PORT" HUB_DB="$TMPD/$1.db" python3 "$DIR/main.py" >"$TMPD/$1.log" 2>&1 &
  fi
  SRV=$!; sleep 4
}
stop_srv(){ kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; sleep 1; }

echo "===== 第一轮：不限邀请码（限流 + 邮箱校验）====="
start_srv g1
echo "[1] 坏邮箱 → 400"
C=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"not-an-email","password":"aaaaaa"}')
[ "$C" = "400" ] && ok "格式错误邮箱 400" || bad "格式错误邮箱返回 $C"
C=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"a@mailinator.com","password":"aaaaaa"}')
[ "$C" = "400" ] && ok "一次性邮箱 400" || bad "mailinator 返回 $C"

echo "[2] 连打 4 次注册 → 第 4 次 429"
R1=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"u1@hub.local","password":"aaaaaa"}')
R2=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"u2@hub.local","password":"aaaaaa"}')
R3=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"u3@hub.local","password":"aaaaaa"}')
[ "$R1" = "200" ] && [ "$R2" = "200" ] && [ "$R3" = "200" ] && ok "前 3 次注册成功（$R1/$R2/$R3）" || bad "前 3 次没全成功：$R1/$R2/$R3"
R4=$(curl -s -D - -o /dev/null -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"u4@hub.local","password":"aaaaaa"}')
C4=$(echo "$R4" | head -1 | awk '{print $2}')
RA=$(echo "$R4" | grep -i '^retry-after' | tr -d '\r' | awk '{print $2}')
[ "$C4" = "429" ] && ok "第 4 次 429（Retry-After=$RA）" || bad "第 4 次返回 $C4（应 429）"
stop_srv

echo "===== 第二轮：设邀请码 → 不带码 403 ====="
start_srv g2 "only-us"
C1=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"v1@hub.local","password":"aaaaaa"}')
[ "$C1" = "403" ] && ok "不带邀请码 403" || bad "不带码返回 $C1（应 403）"
C2=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"v2@hub.local","password":"aaaaaa","invite_code":"wrong"}')
[ "$C2" = "403" ] && ok "邀请码带错 403" || bad "错码返回 $C2（应 403）"
C3=$(code -X POST "$BASE/api/accounts/register" -H 'Content-Type: application/json' -d '{"email":"v3@hub.local","password":"aaaaaa","invite_code":"only-us"}')
[ "$C3" = "200" ] && ok "邀请码带对 200" || bad "对码返回 $C3（应 200）"
stop_srv

echo "===== 拦截日志抽查 ====="
grep -q "\[guard\] 拦截注册" "$TMPD/g1.log" && ok "第一轮拦截日志有写" || bad "g1 无拦截日志"
grep -q "邀请码不对" "$TMPD/g2.log" && ok "第二轮拦截日志有写" || bad "g2 无拦截日志"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; echo "--- g1.log ---"; tail -8 "$TMPD/g1.log"; echo "--- g2.log ---"; tail -8 "$TMPD/g2.log"; exit 1; }
