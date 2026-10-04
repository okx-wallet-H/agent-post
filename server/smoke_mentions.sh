#!/usr/bin/env bash
# @ 唤醒自测（#29）：群里 @甲 只唤醒甲；@全体 全唤醒；单聊不受影响；补读上下文生效
# 环境：只用系统 python3（3.9+ 兼容：标准库 + fastapi/uvicorn；开发机验证过 3.12.3）。
# 时序策略：不靠 sleep 猜——所有正向断言都轮询输出文件等「出现某行」，负向断言等
# 守候进程退出后（或超时后）再验「没有某行」，失败会打出输出文件全文当原因。
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
# 随机高位端口：固定端口会被本机常驻 hub 占用 → listen 连到别人的服务、/v1/me 401 全盘假 FAIL
PORT="${HUB_PORT:-$((8800 + RANDOM % 900))}"; BASE="http://127.0.0.1:$PORT"; T="h-dev-token"
TMPD="$DIR/.smoke-mentions-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"
HUB_PORT="$PORT" HUB_DB="$TMPD/m.db" python3 "$DIR/main.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!
for _ in $(seq 1 40); do curl -s -m 1 "$BASE/health" 2>/dev/null | grep -q '"ok"' && break; sleep 0.3; done
curl -s -m 2 "$BASE/health" | grep -q '"ok"' || { echo "服务没起来（端口 $PORT 可能被占）："; tail -20 "$TMPD/srv.log"; kill $SRV 2>/dev/null; exit 1; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

# listen 助手：$1=agent token $2=输出文件 $3=游标文件 $4=workdir（收件落盘目录）$5=--run 命令（可选）
start_listen(){
  if [ -n "${5:-}" ]; then
    AGENTPOST_URL="$BASE" AGENTPOST_TOKEN="$1" AGENTPOST_CURSOR="$TMPD/$3" \
    AGENTPOST_WORKDIR="$TMPD/${4:-wd-$3}" \
    python3 "$ROOT/cli/agentpost.py" listen --mention-only --once --timeout 5 --run "$5" > "$TMPD/$2" 2>&1 &
  else
    AGENTPOST_URL="$BASE" AGENTPOST_TOKEN="$1" AGENTPOST_CURSOR="$TMPD/$3" \
    AGENTPOST_WORKDIR="$TMPD/${4:-wd-$3}" \
    python3 "$ROOT/cli/agentpost.py" listen --mention-only --once --timeout 5 > "$TMPD/$2" 2>&1 &
  fi
}

# wait_for_line <文件> <grep模式> <超时秒> <失败原因>：轮询文件等出现某行，最多等 <超时秒>
wait_for_line(){ local f="$TMPD/$1" pat="$2" t="$3" why="$4"
  for _ in $(seq 1 $((t*2))); do
    grep -q "$pat" "$f" 2>/dev/null && return 0
    sleep 0.5
  done
  bad "$why（等了 ${t}s，子进程输出全文：$(cat "$f" 2>/dev/null)）"
  return 1
}
# wait_for_exit <超时秒>：等所有 agentpost.py listen 进程退出
wait_for_exit(){ local t=$1; for _ in $(seq 1 $((t*2))); do
    pgrep -f "agentpost.py listen" >/dev/null || return 0
    sleep 0.5
  done; return 1
}

echo "[1] 建甲乙、建群"
A=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"甲"}')
B=$(curl -s -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"name":"乙"}')
AT=$(echo "$A" | jq_ "['token']"); AID=$(echo "$A" | jq_ "['id']")
BT=$(echo "$B" | jq_ "['token']"); BID=$(echo "$B" | jq_ "['id']")
G=$(curl -s -X POST "$BASE/api/conversations" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d "{\"title\":\"测试群\",\"members\":[\"$AID\",\"$BID\"]}")
GID=$(echo "$G" | jq_ "['id']")
[ -n "$AT" ] && [ -n "$BT" ] && [ -n "$GID" ] && ok "甲乙 + 群 $GID 就绪" || bad "准备失败：$A $B $G"

echo "[2] 群里发 @甲 干活 → 该条 mentions 含甲、乙的守候不被唤醒"
start_listen "$AT" a1.out ca; start_listen "$BT" b1.out cb
wait_for_line a1.out "身份：name=甲" 20 "甲的守候没起来（没拿到身份行）" || true
wait_for_line b1.out "身份：name=乙" 20 "乙的守候没起来（没拿到身份行）" || true
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"to":"测试群","text":"@甲 去干这个活","client_msg_id":"m-1"}' >/dev/null
wait_for_line a1.out "去干这个活" 25 "甲的守候没被唤醒" && ok "甲的守候被唤醒（@甲）" || true
wait_for_exit 25
grep -q "去干这个活" "$TMPD/b1.out" && bad "乙被误唤醒：$(cat "$TMPD/b1.out")" || ok "乙的守候没被唤醒（没@乙）"
MA=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $AT")
M1=$(echo "$MA" | jq_ "['messages'][0]['mentions']")
echo "$M1" | grep -q "甲" && ok "该条 mentions 含甲（$M1）" || bad "mentions 不对：$M1"
WA=$(echo "$MA" | jq_ "['messages'][0]['wake']")
[ "$WA" = "True" ] && ok "甲视角 wake=true（待处理）" || bad "甲视角 wake 不对：$WA"
MB=$(curl -s "$BASE/v1/inbox?since=0" -H "Authorization: Bearer $BT")
WB=$(echo "$MB" | jq_ "['messages'][0]['wake']")
[ "$WB" = "False" ] && ok "乙视角 wake=false（已看未唤醒，待补读）" || bad "乙视角 wake 不对：$WB"

echo "[3] 群里发 @全体 → 两个都被唤醒"
start_listen "$AT" a2.out ca; start_listen "$BT" b2.out cb
wait_for_line a2.out "身份：name=甲" 20 "甲2没起来（没拿到身份行）" || true
wait_for_line b2.out "身份：name=乙" 20 "乙2没起来（没拿到身份行）" || true
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"to":"测试群","text":"@全体 开会了","client_msg_id":"m-2"}' >/dev/null
wait_for_line a2.out "开会了" 25 "甲没被全体唤醒" && ok "甲被 @全体 唤醒" || true
wait_for_line b2.out "开会了" 25 "乙没被全体唤醒" && ok "乙被 @全体 唤醒" || true
wait_for_exit 25

echo "[4] 单聊不受影响（人 → 乙 直接唤醒；乙的游标继续用 cb 接在 @全体 之后）"
start_listen "$BT" b3.out cb
wait_for_line b3.out "身份：name=乙" 20 "乙3没起来（没拿到身份行）" || true
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"to":"乙","text":"私聊一条","client_msg_id":"m-3"}' >/dev/null
wait_for_line b3.out "私聊一条" 25 "单聊没唤醒乙" && ok "单聊照旧全唤醒" || true
wait_for_exit 25

echo "[5] 没被 @ 的乙，被 @ 后能读到中间漏看的上下文（补读是重点）"
# 游标预置（消除 b4/b5 进程间时序纠缠）：b4 直接构造「已看 seq4 未唤醒」，
# b5 构造「seen=4 / wake=3（上次唤醒是私聊 seq3）」→ 被 @ 后应重拉 since=3 补读 seq4
echo 3 > "$TMPD/cb4"; echo 3 > "$TMPD/cb4.wake"
echo 4 > "$TMPD/cb5"; echo 3 > "$TMPD/cb5.wake"
start_listen "$BT" b4.out cb4            # 吃一条不 @ 乙的群消息：只推游标不唤醒
wait_for_line b4.out "身份：name=乙" 20 "乙4没起来（没拿到身份行）" || true
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"to":"测试群","text":"@甲 又喊一声","client_msg_id":"m-4"}' >/dev/null
wait_for_exit 25
grep -q "又喊一声" "$TMPD/b4.out" && bad "乙被不相关群消息唤醒：$(cat "$TMPD/b4.out")" || ok "不 @ 乙的群消息不唤醒乙"
RUN_B5='cat > '"$TMPD"'/b5_stdin.txt; echo "$AGENTPOST_CONTEXT" > '"$TMPD"'/b5_ctx.txt'
start_listen "$BT" b5.out cb5 wd-乙 "$RUN_B5"   # 再 @ 乙 → 唤醒：落盘 + 短摘要 + 触发条原文
wait_for_line b5.out "身份：name=乙" 20 "乙5没起来（没拿到身份行）" || true
curl -s -X POST "$BASE/v1/send" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"to":"测试群","text":"@乙 看上下文","client_msg_id":"m-5"}' >/dev/null
wait_for_line b5.out "触发本条的消息" 30 "乙没被 @ 唤醒" && ok "乙被 @ 唤醒（短摘要已给）" || true
wait_for_exit 20
INBOX_F=$(ls "$TMPD/wd-乙/收件/"*.md 2>/dev/null | head -1)
CNT=$(grep -c "^## seq" "$INBOX_F" 2>/dev/null || echo 0)
[ "$CNT" = "2" ] && ok "① 收件落盘 2 条（漏看 1 + 触发 1）" || bad "① 收件落盘条数不对：$CNT（文件 $INBOX_F）"
CTX_LINES=$(wc -l < "$TMPD/b5_ctx.txt" 2>/dev/null || echo 99)
[ "$CTX_LINES" -le 15 ] && grep -q "收件/" "$TMPD/b5_ctx.txt" && ok "② AGENTPOST_CONTEXT $CTX_LINES 行（≤15）且含收件路径" \
  || bad "② 短摘要不对：$CTX_LINES 行，内容 $(head -c 300 "$TMPD/b5_ctx.txt" 2>/dev/null)"
grep -q "@乙 看上下文" "$TMPD/b5_stdin.txt" && ok "③ 被 @ 的那条原文完整进了 stdin" \
  || bad "③ 触发条原文没完整给到：$(head -c 200 "$TMPD/b5_stdin.txt" 2>/dev/null)"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -15 "$TMPD/srv.log"; exit 1; }
