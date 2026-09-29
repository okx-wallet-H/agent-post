#!/usr/bin/env bash
# 长轮询（唤醒）自测：wait=15 的请求应当在对方发消息后立刻返回，而不是等满 15 秒
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8799}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
PY="${PYTHON:-python3}"     # 换解释器： PYTHON=/opt/homebrew/bin/python3 bash smoke_wait.sh
TMPD="$DIR/.smoke-wait-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/w.db" "$PY" "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }; bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

AT=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"守夜人"}' | jq_ "['token']")

echo "[1] 空收件箱 + wait=2（应当等满约 2 秒后返回空）"
S=$(date +%s); OUT=$(curl -s "$BASE/v1/inbox?since=0&wait=2" -H "Authorization: Bearer $AT"); E=$(date +%s)
[ "$((E-S))" -ge 2 ] && [ "$(echo "$OUT" | jq_ "['count']")" = "0" ] && ok "等满 2 秒返回空（$((E-S))s）" || bad "等待行为不对：$((E-S))s / $OUT"

echo "[2] wait=15，1 秒后有人发消息 → 应当立刻返回（<5 秒）"
( sleep 1; curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"to":"守夜人","text":"醒醒"}' >/dev/null ) &
S=$(date +%s); OUT=$(curl -s "$BASE/v1/inbox?since=0&wait=15" -H "Authorization: Bearer $AT"); E=$(date +%s)
D=$((E-S))
if [ "$D" -lt 5 ] && [ "$(echo "$OUT" | jq_ "['count']")" = "1" ]; then ok "长轮询被唤醒：${D}s 拿到「$(echo "$OUT" | jq_ "['messages'][0]['text']")」"
else bad "没被唤醒：耗时 ${D}s / $OUT"; fi
kill $SRV 2>/dev/null
echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
[ "$FAIL" = "0" ] && echo "SMOKE-WAIT: PASS" || { echo "SMOKE-WAIT: FAIL"; tail -15 "$TMPD/srv.log"; exit 1; }
