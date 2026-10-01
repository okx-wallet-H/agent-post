#!/bin/bash
# smoke_metrics2.sh —— #15 投递口径拆两条的冒烟（临时库 + 临时端口）
# 造 3 条消息：m1 被取走（乙 5 分钟前心跳，延迟 300s ≤ 取走窗口 3600s）；
#             m2/m3 发给丙（丙从未心跳）→ 超 24 小时没人取（alerts 默认 stale_hours=24，降噪改）。
# 断言：/v1/metrics 的 delivery_latency_s.n == 1（延迟只算被取走的）；
#       stale_delivery.count == 2、oldest 是 m3 的写入时间；
#       /v1/alerts 触发 stale_delivery 告警（action 写明检查收件方/守候），
#       把 stale_delivery_max 改到 5 后该告警消失。
# 跑法： bash smoke_metrics2.sh；SMOKE_PORT=8805 bash smoke_metrics2.sh（换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/metrics2-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8804}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/v1/metrics" 2>/dev/null; }
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
def iso(hours_ago=0, min_ago=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours_ago, minutes=min_ago)).isoformat(timespec="seconds")
db.execute("INSERT INTO agents VALUES ('ag1','甲','tok1','x')")
db.execute("INSERT INTO agents VALUES ('ag2','乙','tok2','x')")
db.execute("INSERT INTO agents VALUES ('ag3','丙','tok3','x')")
db.execute("INSERT INTO conversations VALUES ('c1','甲乙','dm','x')")
db.execute("INSERT INTO conversations VALUES ('c2','甲丙','dm','x')")
db.execute("INSERT INTO members VALUES ('c1','ag1')")
db.execute("INSERT INTO members VALUES ('c1','ag2')")
db.execute("INSERT INTO members VALUES ('c2','ag1')")
db.execute("INSERT INTO members VALUES ('c2','ag3')")
# m1：10 分钟前 甲→乙；乙 5 分钟前心跳 → 已取走（延迟 300s）
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m1','c1','agent','ag1','收',?)", (iso(min_ago=10),))
db.execute("INSERT INTO presence VALUES ('ag2',?)", (iso(min_ago=5),))
# m2/m3：26/25 小时前 甲→丙；丙从未心跳 → 超 stale_hours=24 没人取
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m3','c2','agent','ag1','旧2',?)", (iso(hours_ago=26),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m2','c2','agent','ag1','旧1',?)", (iso(hours_ago=25),))
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import metrics, alerts
db = sqlite3.connect("$TMP/smoke.db", check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
def q(sql, args=()):
    with lock: return db.execute(sql, args).fetchall()
def q1(sql, args=()):
    with lock: return db.execute(sql, args).fetchone()
def ex(sql, args=()):
    with lock: return db.execute(sql, args)
metrics.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex)
alerts.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex)
app = FastAPI()
app.include_router(metrics.router)
app.include_router(alerts.router)
PYEOF

cd "$TMP"
"$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/v1/metrics" && break
    sleep 0.5
done

M=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/metrics")
A=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/alerts")
RULE=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer humantok" -H "Content-Type: application/json" \
    -d '{"stale_delivery_max": 5}' "http://127.0.0.1:$PORT/v1/alerts/rule")
A2=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/alerts")

echo "--- /v1/metrics ---"
echo "$M"
echo "--- /v1/alerts（改规则前）---"
echo "$A"
echo "--- 改 stale_delivery_max=5 后 ---"
echo "$A2"

"$PY" - "$M" "$A" "$RULE" "$A2" <<'PY'
import json, sys
m, a, rule, a2 = sys.argv[1:]
ok = True
d = json.loads(m)
lat = d.get("delivery_latency_s")
if lat.get("n") != 1:
    print(f"FAIL: 延迟样本应只算被取走的 1 条，实际 n={lat.get('n')}"); ok = False
if not (abs((lat.get("median_s") or 0) - 300) < 60):
    print(f"FAIL: 延迟中位应约 300s，实际 {lat.get('median_s')}"); ok = False
sd = d.get("stale_delivery")
if sd.get("count") != 2:
    print(f"FAIL: 超时未取走应 2 条，实际 {sd.get('count')}"); ok = False
if not sd.get("oldest_created_at"):
    print(f"FAIL: oldest_created_at 应有值，实际 {sd.get('oldest_created_at')}"); ok = False
al = json.loads(a).get("alerts", [])
stale_alerts = [x for x in al if x.get("type") == "stale_delivery"]
if len(stale_alerts) != 1:
    print(f"FAIL: 应恰好 1 条 stale_delivery 告警，实际 {[(x.get('type'), x.get('subject')) for x in al]}"); ok = False
else:
    sa = stale_alerts[0]
    if sa.get("value") != 2 or sa.get("since") is None:
        print(f"FAIL: stale 告警 value/since 不对：{sa}"); ok = False
    if "检查收件方" not in sa.get("action", "") or "守候" not in sa.get("action", ""):
        print(f"FAIL: stale 告警 action 没写清：{sa.get('action')}"); ok = False
other = [x for x in al if x.get("type") != "stale_delivery"]
if other:
    print(f"FAIL: 不应有其他告警：{[(x.get('type')) for x in other]}"); ok = False
rr = json.loads(rule).get("rules", {})
if rr.get("stale_delivery_max") != 5 or rr.get("stale_hours") != 24:
    print(f"FAIL: 改规则返回 {rr}"); ok = False
a2_alerts = json.loads(a2).get("alerts", [])
if any(x.get("type") == "stale_delivery" for x in a2_alerts):
    print(f"FAIL: 阈值改 5 后 stale 告警应消失，实际还在"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
