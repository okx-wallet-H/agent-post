#!/bin/bash
# smoke_cards.sh —— #33 能力卡接口冒烟（临时库 + 临时端口，读仓库真实 data/agent-cards.json）
# 断言：① /v1/cards 卡片数 == json 里 cards 数量 ② 每张 role/can 非空
#       ③ 造一个心跳后该卡 online=true（没心跳的 offline）④ 不存在的名字 404 ⑤ 无 token 401
# 跑法： bash smoke_cards.sh；SMOKE_PORT=8811 bash smoke_cards.sh（换端口）
set -u
cd "$(dirname "$0")"
CARDS_JSON_PATH="$(cd .. && pwd)/data/agent-cards.json"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/cards-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8810}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/v1/cards" 2>/dev/null; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi

echo "解释器：$PY，端口 $PORT"

"$PY" - "$TMP" <<'PY'
import sqlite3, sys, datetime
tmp = sys.argv[1]
db = sqlite3.connect(f"{tmp}/smoke.db")
db.executescript("""
CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE members (conversation_id TEXT NOT NULL, agent_id TEXT NOT NULL, PRIMARY KEY (conversation_id, agent_id));
CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, conversation_id TEXT NOT NULL,
    from_kind TEXT NOT NULL, from_id TEXT NOT NULL, text TEXT NOT NULL, client_msg_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE presence (agent_id TEXT PRIMARY KEY, last_seen TEXT);
""")
now = datetime.datetime.now(datetime.timezone.utc)
def iso(sec_ago=0):
    return (now - datetime.timedelta(seconds=sec_ago)).isoformat(timespec="seconds")
db.execute("INSERT INTO agents VALUES ('ag-warm','温暖','tok-warm','x')")
db.execute("INSERT INTO agents VALUES ('ag-data','数据岗','tok-data','x')")
# 温暖 30 秒前心跳（≤120s → online）；数据岗没有 presence 行
db.execute("INSERT INTO presence VALUES ('ag-warm',?)", (iso(30),))
# 温暖发过一条消息（近 24h out=1 + 预览）
db.execute("INSERT INTO conversations VALUES ('g1','总群','group','x')")
db.execute("INSERT INTO members VALUES ('g1','ag-warm')")
db.execute("INSERT INTO members VALUES ('g1','ag-data')")
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m1','g1','agent','ag-warm','交付：能力卡接口自测通过，等部署',?)", (iso(600),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m2','g1','agent','ag-data','收到',?)", (iso(500),))
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import cards
db = sqlite3.connect("$TMP/smoke.db", check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
def q(sql, args=()):
    with lock: return db.execute(sql, args).fetchall()
def q1(sql, args=()):
    with lock: return db.execute(sql, args).fetchone()
def ex(sql, args=()):
    with lock:
        cur = db.execute(sql, args); db.commit(); return cur
cards.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex)
app = FastAPI()
app.include_router(cards.router)
PYEOF

cd "$TMP"
"$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/cards" && break
    sleep 0.5
done

ALL=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/cards")
WARM=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/cards/温暖")
MISS=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/cards/不存在的人")
C401=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/v1/cards")

echo "--- /v1/cards（全部）---"
echo "$ALL" | "$PY" -c "import json,sys; d=json.load(sys.stdin); [print(f\"  {c['name']} role={c['role']} online={c['online']} in/out={c['msgs_in']}/{c['msgs_out']}\") for c in d['cards']]"
echo "--- /v1/cards/温暖（单张）---"
echo "$WARM"
echo "--- 不存在 404 / 无 token 401 ---"
echo "404_test=$MISS 401_test=$C401"

"$PY" - "$ALL" "$WARM" "$MISS" "$C401" "$CARDS_JSON_PATH" <<'PY'
import json, sys
all_b, warm, miss, c401, cards_json = sys.argv[1:]
ok = True
d = json.loads(all_b)
cards = d.get("cards", [])
# ① 卡片数 == json 里数量
n_json = len(json.load(open(cards_json, encoding="utf-8")).get("cards", []))
if str(len(cards)) != str(n_json):
    print(f"FAIL: /v1/cards 返回 {len(cards)} 张，json 里 {n_json} 张"); ok = False
# ② 每张 role/can 非空
for c in cards:
    if not c.get("role") or not isinstance(c.get("can"), list) or len(c["can"]) == 0:
        print(f"FAIL: {c.get('name')} 的 role/can 非空要求不满足：role={c.get('role')} can={c.get('can')}"); ok = False
# ③ 造了心跳的温暖 online=true、last_seen 有值；数据岗（无 presence）online=false
warm_card = next((c for c in cards if c["name"] == "温暖"), None)
if warm_card is None or warm_card.get("online") is not True or not warm_card.get("last_seen"):
    print(f"FAIL: 温暖应 online=true 且 last_seen 有值，实际 {warm_card}"); ok = False
if warm_card and warm_card.get("msgs_out") != 1:
    print(f"FAIL: 温暖近 24h msgs_out 应为 1，实际 {warm_card.get('msgs_out')}"); ok = False
if warm_card and "交付" not in (warm_card.get("last_message_preview") or ""):
    print(f"FAIL: 温暖最近消息预览应含「交付」：{warm_card.get('last_message_preview')}"); ok = False
data_card = next((c for c in cards if c["name"] == "数据岗"), None)
if data_card is None or data_card.get("online") is not False:
    print(f"FAIL: 数据岗（无 presence）应 online=false，实际 {data_card}"); ok = False
# 单张接口
w = json.loads(warm)
if w.get("name") != "温暖" or w.get("online") is not True:
    print(f"FAIL: /v1/cards/温暖 不对：{w}"); ok = False
# ④ 不存在 404
if miss != "404":
    print(f"FAIL: 不存在的名字应 404，实际 {miss}"); ok = False
# ⑤ 无 token 401
if c401 != "401":
    print(f"FAIL: 无 token 应 401，实际 {c401}"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
