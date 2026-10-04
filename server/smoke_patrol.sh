#!/usr/bin/env bash
# 值班巡视自测（#32）：① 到点触发 --run 且 PATROL=1 ② [静默] 不发消息 ③ 抖动偏移符合预期
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PORT="${HUB_PORT:-8791}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
TMPD="$DIR/.smoke-patrol-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/p.db" python3 "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

run_patrol(){  # $1=run 脚本 $2=输出文件 $3=游标文件
  AGENTPOST_URL="$BASE" AGENTPOST_TOKEN="$AT" AGENTPOST_CURSOR="$TMPD/$3" \
  AGENTPOST_PATROL_ON=1 AGENTPOST_PATROL_GROUP="巡逻群" AGENTPOST_PATROL_JITTER=0 \
  python3 "$ROOT/cli/agentpost.py" listen --patrol-minutes 0.1 --once --timeout 3 --run "$1" > "$TMPD/$2" 2>&1 &
  for _ in $(seq 1 60); do pgrep -f "agentpost.py listen" >/dev/null || break; sleep 0.3; done
}
conv_count(){ curl -s "$BASE/api/conversations/$GID/messages" -H "Authorization: Bearer $T" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('messages',[])))" 2>/dev/null || echo 0; }

echo "[0] 建甲 agent + 巡逻群"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"甲"}')
AT=$(echo "$A" | jq_ "['token']"); AID=$(echo "$A" | jq_ "['id']")
G=$(curl -s -X POST "$BASE/api/conversations" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d "{\"title\":\"巡逻群\",\"members\":[\"$AID\"]}")
GID=$(echo "$G" | jq_ "['id']")
[ -n "$AT" ] && [ -n "$GID" ] && ok "甲 + 巡逻群 $GID 就绪" || bad "准备失败：$A $G"

echo "[1] 到点触发 --run，env 带 PATROL=1，发言进群"
run_patrol 'echo "PATROL=$PATROL"; if [ "$PATROL" = "1" ]; then echo "巡视发言：建议更快"; fi' p1.out cp1
grep -q "PATROL=1" "$TMPD/p1.out" && ok "巡视触发且 PATROL=1" || bad "PATROL 没带上：$(cat "$TMPD/p1.out")"
grep -q "\[巡视\] 定时自己醒了" "$TMPD/p1.out" && ok "日志能看出是巡视" || bad "巡视日志没有：$(cat "$TMPD/p1.out")"
sleep 0.5
C1=$(conv_count)
[ "$C1" -ge 1 ] && ok "巡视发言已发进群（群消息 $C1 条）" || bad "发言没进群：$C1"

echo "[2] 输出 [静默] 时不发消息，只推游标"
run_patrol 'echo "[静默]"' p2.out cp1
grep -q "\[静默\]，不发消息" "$TMPD/p2.out" && ok "桥识别 [静默] 不发消息" || bad "静默没生效：$(cat "$TMPD/p2.out")"
sleep 0.5
C2=$(conv_count)
[ "$C2" = "$C1" ] && ok "群消息没增加（还是 $C2 条）" || bad "静默却发了消息：$C1 -> $C2"

echo "[3] 抖动偏移符合预期（hash(名字) % 15 分钟）"
EXP=$(python3 -c "print(sum(ord(c) for c in '甲') % 15)")
grep -q "抖动 +$EXP 分钟" "$TMPD/p2.out" || grep -q "抖动 +$EXP 分钟" "$TMPD/p1.out" && ok "抖动偏移 +$EXP 分钟正确" || bad "抖动偏移不对（预期 +$EXP）：$(grep -h 抖动 "$TMPD"/p1.out "$TMPD"/p2.out)"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -15 "$TMPD/srv.log"; exit 1; }
