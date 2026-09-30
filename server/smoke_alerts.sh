#!/bin/bash
# smoke_alerts.sh —— alerts.py 冒烟测试（临时库 + 临时端口，不碰生产 hub.db）
# 造两种情形：乙 心跳过期（35 分钟前）→ 应报 heartbeat_stale；甲 心跳正常（1 分钟前）→ 不报；
#           延迟 p95 超阈（消息 2 小时前写入、乙心跳 35 分钟前 → 上界 85 分钟 > 1800s）→ 应报 latency_p95_high；
#           最近 1 小时有消息 → throughput_zero 不报；无脏数据无 FAIL → failures_positive 不报。
# 断言：alerts 恰好 [heartbeat_stale(乙), latency_p95_high]，规则默认值对；POST /v1/alerts/rule
#       人 token 改阈值生效、agent token 403。
# 跑法： bash smoke_alerts.sh；SMOKE_PORT=8803 bash smoke_alerts.sh（换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/alerts-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8802}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/v1/alerts" 2>/dev/null; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi

echo "解释器：$PY（$("$PY" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)），端口 $PORT"

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
def iso(min_ago):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=min_ago)).isoformat(timespec="seconds")
db.execute("INSERT INTO agents VALUES ('ag1','甲','tok1','x')")
db.execute("INSERT INTO agents VALUES ('ag2','乙','tok2','x')")
db.execute("INSERT INTO conversations VALUES ('c1','单聊','dm','x')")
db.execute("INSERT INTO members VALUES ('c1','ag1')")
db.execute("INSERT INTO members VALUES ('c1','ag2')")
# 甲正常心跳 1 分钟前；乙过期心跳 35 分钟前
db.execute("INSERT INTO presence VALUES ('ag1',?)", (iso(1),))
db.execute("INSERT INTO presence VALUES ('ag2',?)", (iso(35),))
# 消息：2 小时前 ag1→ag2（乙取走上界 = 35 分钟前心跳，延迟 ≈ 85 分钟 > 1800s → p95 超阈）；
#       10 分钟前 ag2→ag1（最近 1 小时有消息 → 吞吐告警不报）
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m1','c1','agent','ag1','早',?)", (iso(120),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m2','c1','agent','ag2','近',?)", (iso(10),))
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import alerts
db = sqlite3.connect("$TMP/smoke.db", check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
def q(sql, args=()):
    with lock: return db.execute(sql, args).fetchall()
def q1(sql, args=()):
    with lock: return db.execute(sql, args).fetchone()
def ex(sql, args=()):
    with lock: return db.execute(sql, args)
alerts.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex)
app = FastAPI()
app.include_router(alerts.router)
PYEOF

cd "$TMP"
"$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/v1/alerts" && break
    sleep 0.5
done

BODY=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/alerts")
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/alerts")
CODE401=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/v1/alerts")
RULE_BODY=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer humantok" -H "Content-Type: application/json" \
    -d '{"heartbeat_max_min": 10, "latency_p95_max_s": 6000}' "http://127.0.0.1:$PORT/v1/alerts/rule")
RULE_AGENT=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -H "Authorization: Bearer tok1" -H "Content-Type: application/json" \
    -d '{"heartbeat_max_min": 10}' "http://127.0.0.1:$PORT/v1/alerts/rule")
AFTER_BODY=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/alerts")

echo "--- GET /v1/alerts（人 token）---"
echo "$BODY"
echo "--- POST /v1/alerts/rule 后 ---"
echo "$RULE_BODY"
echo "$AFTER_BODY"

"$PY" - "$BODY" "$CODE" "$CODE401" "$RULE_BODY" "$RULE_AGENT" "$AFTER_BODY" <<'PY'
import json, sys
body, code, code401, rule_body, rule_agent, after_body = sys.argv[1:]
ok = True
d = json.loads(body)
if code != "200": print(f"FAIL: GET HTTP {code} != 200"); ok = False
if code401 != "401": print(f"FAIL: 无 token 应 401，实际 {code401}"); ok = False
if rule_agent != "403": print(f"FAIL: agent token 改规则应 403，实际 {rule_agent}"); ok = False
# 规则默认值
r = d.get("rules")
want = {"heartbeat_max_min": 30, "latency_p95_max_s": 1800, "zero_window_s": 3600, "failures_max": 0}
if r != want: print(f"FAIL: 默认规则 {r} != {want}"); ok = False
# 告警恰好两条：heartbeat_stale(乙) + latency_p95_high；不能有 throughput_zero / failures_positive / 甲的 heartbeat
alerts = d.get("alerts")
types = [(a.get("type"), a.get("subject")) for a in alerts]
if types != [("heartbeat_stale", "乙"), ("latency_p95_high", "全站")]:
    print(f"FAIL: 告警列表 {types} != 只报乙的心跳 + 延迟"); ok = False
hb = next((a for a in alerts if a["type"] == "heartbeat_stale"), {})
if hb.get("since") is None or "检查 agentpost@乙" not in hb.get("action", ""):
    print(f"FAIL: heartbeat 告警缺 since/action: {hb}"); ok = False
if hb.get("value", 0) < 30: print(f"FAIL: 乙心跳过期分钟数 {hb.get('value')} 应 ≥30"); ok = False
lat = next((a for a in alerts if a["type"] == "latency_p95_high"), {})
if lat.get("value", 0) <= 1800: print(f"FAIL: p95 {lat.get('value')} 应 > 1800"); ok = False
# 改阈值后：heartbeat_max_min=10 → 甲（1 分钟前）不报、乙仍报；latency 阈值 6000（> 5100）→ 不报
rr = json.loads(rule_body).get("rules", {})
if rr.get("heartbeat_max_min") != 10 or rr.get("latency_p95_max_s") != 6000:
    print(f"FAIL: 改阈值返回 {rr}"); ok = False
after = json.loads(after_body)
a_types = [(a.get("type"), a.get("subject")) for a in after.get("alerts", [])]
if a_types != [("heartbeat_stale", "乙")]:
    print(f"FAIL: 改阈值后告警 {a_types} != 只剩乙的心跳"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
