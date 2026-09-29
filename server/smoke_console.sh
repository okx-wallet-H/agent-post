#!/usr/bin/env bash
# console.py 自测：/v1/board（人 + 一群 Agent 的协作看板）+ /console（给人看的单页）
#
#   HUB_PORT=8797 HUB_DB=<临时> 起服务 → 建 2 个 Agent → 人派活 → Agent 回话
#   → 打心跳（A 现在 / B 10 分钟前）→ 断言看板 → 断言页面 → 收工
#
# 只用本机、临时 DB、8797 端口；不碰线上（8795）。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8797}"
PY="${PYTHON:-python3}"     # 换解释器： PYTHON=/opt/homebrew/bin/python3 bash smoke_console.sh

# 本机常驻着别的 warm-hub 开发实例（老产品 产品/通信台/main.py 也听 8797），
# 8797 被占就顺延找空端口——别去杀别人的进程。
busy() { curl -s -m 1 "http://127.0.0.1:$1/health" >/dev/null 2>&1; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi
[ "$PORT" != "$WANT" ] && echo "（$WANT 被占，改用 $PORT）"

BASE="http://127.0.0.1:$PORT"
T="h-dev-token"                       # 人 token（HUB_USER_TOKEN 默认值）
TMPD="$(mktemp -d)"
DB="$TMPD/console.db"
SRV=""

cleanup() {
  [ -n "$SRV" ] && { kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; }
  case "$TMPD" in
    /var/folders/*|/tmp/*) rm -rf "$TMPD" ;;
    *) echo "（临时目录留着没删：$TMPD）" ;;
  esac
}
trap cleanup EXIT

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
jq_() { "$PY" -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))" 2>/dev/null; }

# 发消息（body 走 python 拼，避免中文/引号踩坑）
send() {  # $1=token $2=to $3=text
  "$PY" -c "import json,sys;print(json.dumps({'to':sys.argv[1],'text':sys.argv[2]}))" "$2" "$3" \
    | curl -s --max-time 10 -X POST "$BASE/v1/send" \
        -H "Authorization: Bearer $1" -H 'Content-Type: application/json' --data-binary @-
}

# ---------------------------------------------------------------- 起服务
# main.py 里 console 还没挂（派活规定不许改 main.py），所以用一个 boot 壳子把 console 挂上去；
# 线上要做的就是在 main.py 里加 import console + console.attach(...) + include_router 三行。
cat > "$TMPD/boot.py" <<'PY'
import os, sys, secrets
sys.path.insert(0, sys.argv[1])
import main, console
console.attach(db=main._db, lock=main._db_lock, user_token=main.USER_TOKEN,
               q=main.q, q1=main.q1, ex=main.ex, new_id=main.new_id,
               now_iso=main.now_iso, new_token=lambda: secrets.token_urlsafe(24))
if not any(getattr(r, "path", None) == "/console" for r in main.app.routes):
    main.app.include_router(console.router)
import uvicorn
uvicorn.run(main.app, host="127.0.0.1",
            port=int(os.environ.get("HUB_PORT", "8797")), log_level="warning")
PY

echo "== 起服务：$BASE（HUB_DB=$DB）"
HUB_PORT="$PORT" HUB_DB="$DB" HUB_SITE_DIR="$TMPD/no-site" \
  "$PY" "$TMPD/boot.py" "$DIR" > "$TMPD/srv.log" 2>&1 &
SRV=$!

READY=0
for _ in $(seq 1 40); do
  if curl -s -m 1 "$BASE/health" | grep -q '"ok"'; then READY=1; break; fi
  sleep 0.5
done
if [ "$READY" != "1" ]; then
  echo "❌ 服务没起来，日志："; tail -20 "$TMPD/srv.log"; echo "TEST: FAIL（服务未就绪）"; exit 1
fi
ok "服务就绪（/health 通）"

# ---------------------------------------------------------------- 1 建两个 Agent
echo "[1] 建两个 Agent"
A=$(curl -s --max-time 10 -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" \
      -H 'Content-Type: application/json' -d '{"name":"Agent-A"}')
B=$(curl -s --max-time 10 -X POST "$BASE/v1/agents" -H "Authorization: Bearer $T" \
      -H 'Content-Type: application/json' -d '{"name":"Agent-B"}')
AID=$(echo "$A" | jq_ "['id']");  AT=$(echo "$A" | jq_ "['token']")
BID=$(echo "$B" | jq_ "['id']");  BT=$(echo "$B" | jq_ "['token']")
if [ -n "$AID" ] && [ -n "$AT" ] && [ -n "$BID" ] && [ -n "$BT" ]; then
  ok "建好 Agent-A($AID) / Agent-B($BID)"
else
  bad "建 Agent 失败：$A / $B"
  echo "TEST: FAIL（拿不到 Agent token，后面没法测）"; exit 1
fi

# ---------------------------------------------------------------- 2 人派活 → Agent 回话
echo "[2] 人 → Agent-A 派活"
R=$(send "$T" "Agent-A" "任务：把昨天的投放数据整理成一张表，今晚 8 点前给我。")
echo "$R" | grep -qE '"ok" *: *true' && ok "人发出（seq $(echo "$R" | jq_ "['seq']")）" \
  || bad "人发失败：$R"

echo "[3] Agent-A → 人 回话"
R=$(send "$AT" "人" "收到，我这就去拉数据，整理好发你。")
echo "$R" | grep -qE '"ok" *: *true' && ok "Agent-A 回话（seq $(echo "$R" | jq_ "['seq']")）" \
  || bad "Agent-A 回话失败：$R"

# ---------------------------------------------------------------- 4 心跳（在线判定）
echo "[4] 打心跳：Agent-A 现在 / Agent-B 10 分钟前"
NOW_ISO=$("$PY" -c "from datetime import datetime,timezone;print(datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'))")
OLD_ISO=$("$PY" -c "from datetime import datetime,timezone,timedelta;print((datetime.now(timezone.utc)-timedelta(minutes=10)).astimezone().isoformat(timespec='seconds'))")
TOUCH_OUT=$(HUB_DB="$DB" "$PY" -c "
import sys; sys.path.insert(0, '$DIR')
import console
console.touch('$AID')                  # 没 attach 也能用：自己开一条连接写 presence
console.touch('$BID', at='$OLD_ISO')   # 显式指定 10 分钟前 → 测 TTL 边界
print('touched')
" 2>&1)
[ "$TOUCH_OUT" = "touched" ] && ok "touch() 写入 presence（A=now，B=now-10min）" \
  || bad "touch() 失败：$TOUCH_OUT"

# ---------------------------------------------------------------- 5 看板
echo "[5] GET /v1/board"
BOARD="$TMPD/board.json"
curl -s --max-time 10 "$BASE/v1/board" -H "Authorization: Bearer $T" > "$BOARD"
"$PY" - "$BOARD" > "$TMPD/probe.sh" <<'PY'
import json, shlex, sys
b = json.load(open(sys.argv[1]))
ag = {x["name"]: x for x in b["agents"]}
tl = b["timeline"]; u = b["usage"]
a = ag.get("Agent-A", {}); bb = ag.get("Agent-B", {})
top = tl[0] if tl else {}
vals = {
  "n_agents": len(b["agents"]),
  "n_online": sum(1 for x in b["agents"] if x["online"]),
  "a_online": "true" if a.get("online") else "false",
  "b_online": "true" if bb.get("online") else "false",
  "b_has_seen": "yes" if bb.get("last_seen") else "no",
  "a_msgs_in": a.get("msgs_in"), "a_msgs_out": a.get("msgs_out"),
  "a_last_line": a.get("last_line", ""),
  "tl_len": len(tl),
  "kinds": "/".join(sorted({m.get("kind", "") for m in tl})),
  "top_from": top.get("from", ""), "top_to": top.get("to", ""),
  "top_len": len(top.get("text", "")),
  "today_msgs": u["today"]["msgs"], "today_agents": u["today"]["agents"],
  "month_msgs": u["month"]["msgs"], "month_agents": u["month"]["agents"],
}
for k, v in vals.items():
    print("%s=%s" % (k, shlex.quote(str(v))))
PY
# shellcheck disable=SC1090
. "$TMPD/probe.sh"

[ "$n_agents" = "2" ]      && ok "agents 2 个（名册）" || bad "agents 数量不对：$n_agents"
[ "$n_online" = "1" ]      && ok "在线 1 个（120 秒内有心跳才算在线）" || bad "在线数不对：$n_online"
[ "$a_online" = "true" ]   && ok "Agent-A 刚 touch → online:true" || bad "Agent-A 该在线却不在线"
[ "$b_online" = "false" ]  && ok "Agent-B 10 分钟前的心跳 → online:false" || bad "Agent-B 该离线却在线"
[ "$b_has_seen" = "yes" ]  && ok "Agent-B 仍有 last_seen（不是没记录）" || bad "Agent-B last_seen 丢了"
[ "$tl_len" -ge 2 ]        && ok "timeline $tl_len 条（≥2）" || bad "timeline 只有 $tl_len 条"
[ "$kinds" = "任务/回话" ]  && ok "消息归类：任务 / 回话 都识别出来了" || bad "归类不对：$kinds"
[ "$a_msgs_out" -ge 1 ]    && ok "Agent-A msgs_out=$a_msgs_out" || bad "msgs_out 不对：$a_msgs_out"
[ "$a_msgs_in" -ge 1 ]     && ok "Agent-A msgs_in=$a_msgs_in（派活那条算它收到）" || bad "msgs_in 不对：$a_msgs_in"
[ "$today_msgs" -ge 2 ]    && ok "usage.today.msgs=$today_msgs（≥2）" || bad "today.msgs 不对：$today_msgs"
[ "$today_agents" -ge 1 ]  && ok "usage.today.agents=$today_agents" || bad "today.agents 不对：$today_agents"
[ "$month_msgs" -ge 2 ]    && ok "usage.month.msgs=$month_msgs / agents=$month_agents" || bad "month 口径不对"

# ---------------------------------------------------------------- 6 截断 200 字
echo "[6] 超长消息应截断到 200 字"
LONG=$("$PY" -c "print('长消息截断测试' * 43)")   # 301 字
send "$T" "Agent-A" "$LONG" >/dev/null
curl -s --max-time 10 "$BASE/v1/board" -H "Authorization: Bearer $T" > "$BOARD"
L=$("$PY" -c "
import json;b=json.load(open('$BOARD'));t=b['timeline'][0]['text']
print(len(t), t[-1])")
set -- $L
[ "$1" = "200" ] && ok "首条 text 长度 = 200（原文 301 字）" || bad "没截断到 200：$1"
[ "$2" = "…" ]   && ok "截断处补了省略号" || bad "省略号不对：$2"

# ---------------------------------------------------------------- 7 /console
echo "[7] GET /console"
CODE=$(curl -s -o "$TMPD/console.html" -w '%{http_code}' --max-time 10 "$BASE/console")
[ "$CODE" = "200" ] && ok "/console → 200" || bad "/console 状态码 $CODE"
for kw in 'AgentPost' '发出去，就一定到' '协作' 'Agent 名册' '按 Agent 数 × 消息条数计费' '每 5 秒'; do
  grep -q "$kw" "$TMPD/console.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done
grep -qE 'src="http|href="http|cdn\.' "$TMPD/console.html" \
  && bad "页面里有外部 CDN 引用" || ok "无外部 CDN 引用（单文件内联）"
SIZE=$(wc -c < "$TMPD/console.html" | tr -d ' ')
[ "$SIZE" -lt 102400 ] && ok "页面 $SIZE 字节（<100KB）" || bad "页面太大：$SIZE 字节"

# ---------------------------------------------------------------- 8 鉴权
echo "[8] 不带 token"
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/v1/board")" = "401" ] \
  && ok "/v1/board 无 token → 401" || bad "/v1/board 没拦住无 token 请求"

# ---------------------------------------------------------------- 结果
echo
echo "--- /v1/board 实拍（截断显示） ---"
"$PY" -c "
import json;b=json.load(open('$BOARD'))
print(json.dumps({'agents':b['agents'],'timeline':b['timeline'][:2],'usage':b['usage']},
                 ensure_ascii=False, indent=1)[:1200])"
echo
echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
if [ "$FAIL" = "0" ]; then
  echo "TEST: PASS"
else
  echo "TEST: FAIL（$FAIL 项没过）"
  echo "--- 服务日志尾 ---"; tail -20 "$TMPD/srv.log"
  exit 1
fi
