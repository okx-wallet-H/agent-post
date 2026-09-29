#!/bin/bash
# smoke_metrics.sh —— metrics.py 冒烟测试（临时库 + 临时端口，不碰生产 hub.db）
# 断言：/v1/metrics 返回 200，四个字段都在、类型对；无 Authorization 返回 401。
#
# 跑法： bash smoke_metrics.sh
#        PYTHON=/opt/homebrew/bin/python3 bash smoke_metrics.sh   （换 3.13+ 解释器验一遍）
#        SMOKE_PORT=8802 bash smoke_metrics.sh                    （换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/metrics-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8801}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

# 8799 是 smoke_wait.sh 的地盘，这里默认 8801；被占就顺延，别去杀别人的进程
busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/v1/metrics" 2>/dev/null; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi
[ "$PORT" != "$WANT" ] && echo "（$WANT 被占，改用 $PORT）"

echo "解释器：$PY（$("$PY" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)）"

"$PY" - "$TMP" <<'PY'
import sqlite3, sys
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
import datetime
def iso(min_ago, sec=0):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=min_ago, seconds=sec)).isoformat(timespec="seconds")
db.execute("INSERT INTO agents VALUES ('ag1','甲','tok1','x')")
db.execute("INSERT INTO agents VALUES ('ag2','乙','tok2','x')")
db.execute("INSERT INTO conversations VALUES ('c1','单聊','dm','x')")
db.execute("INSERT INTO members VALUES ('c1','ag1')")
db.execute("INSERT INTO members VALUES ('c1','ag2')")
# 消息：最近 1 小时内 3 条 ag1→ag2（乙积压 3）、1 条 ag2→ag1（甲积压 1）；2 小时前 1 条（不算积压）
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m1','c1','agent','ag1','嗨',?)", (iso(2),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m2','c1','agent','ag1','a',?)", (iso(50),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m3','c1','agent','ag1','b',?)", (iso(40),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m4','c1','agent','ag1','c',?)", (iso(30),))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m5','c1','agent','ag2','回',?)", (iso(20),))
# 脏数据：from_kind 异常（failures 来源 a）
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m6','c1','broken','ag1','脏',?)", (iso(10),))
# 心跳：ag2 在 3 分钟前活跃 → m2/m3/m4 的延迟 = 写入到心跳的秒数；ag1 心跳 1 分钟前
db.execute("INSERT INTO presence VALUES ('ag2',?)", (iso(3),))
db.execute("INSERT INTO presence VALUES ('ag1',?)", (iso(1),))
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys, os
sys.path.insert(0, "$PWD")   # metrics.py 所在目录（本机 / 服务器都一样）
from fastapi import FastAPI
import metrics

db = sqlite3.connect("$TMP/smoke.db", check_same_thread=False)
db.row_factory = sqlite3.Row   # 与生产 main.py 的 q 一致（按列名取）
lock = threading.Lock()
def q(sql, args=()):
    with lock:
        return db.execute(sql, args).fetchall()
def q1(sql, args=()):
    with lock:
        return db.execute(sql, args).fetchone()
def ex(sql, args=()):
    with lock:
        return db.execute(sql, args)

metrics.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex)
app = FastAPI()
app.include_router(metrics.router)
PYEOF

cd "$TMP"
# 用当前解释器起服务（别写死服务器上的 venv 路径：本机没有 /opt/warm-cloud/venv）
nohup "$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
READY=0
for i in $(seq 1 40); do
    kill -0 "$PID" 2>/dev/null || break
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/v1/metrics"; then READY=1; break; fi
    sleep 0.5
done
if [ "$READY" != "1" ]; then
    echo "❌ 服务没起来（$PY -m uvicorn smoke_app:app --port $PORT），日志："
    tail -20 smoke.log
    echo "TEST: FAIL（服务未就绪）"
    exit 1
fi

echo "--- 带 token 请求 ---"
BODY=$(curl -s --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/metrics")
echo "$BODY"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/v1/metrics")
CODE401=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/v1/metrics")

"$PY" - "$BODY" "$CODE" "$CODE401" <<'PY'
import json, sys
body, code, code401 = sys.argv[1], sys.argv[2], sys.argv[3]
ok = True
try:
    d = json.loads(body)
except Exception as e:          # 服务没起来 / 返回的不是 JSON：给原因，别甩 traceback
    print("FAIL: 返回不是 JSON（%s）：%r" % (e, body[:200]))
    print("TEST: FAIL")
    sys.exit(1)
if code != "200": print(f"FAIL: HTTP {code} != 200"); ok = False
if code401 != "401": print(f"FAIL: 无 token 应 401，实际 {code401}"); ok = False
# backlog: dict[str, dict(count:int)]
b = d.get("backlog")
if not isinstance(b, dict): print("FAIL: backlog 类型错"); ok = False
else:
    for k, v in b.items():
        if not isinstance(k, str) or not isinstance(v.get("count"), int) or v.get("window_s") != 3600:
            print(f"FAIL: backlog[{k}] 类型错 {v}"); ok = False
# latency: median/p95 是 float 或 null，n 是 int
lat = d.get("delivery_latency_s")
if not isinstance(lat, dict): print("FAIL: delivery_latency_s 类型错"); ok = False
else:
    for k in ("median_s", "p95_s"):
        if not (isinstance(lat.get(k), (int, float)) or lat.get(k) is None):
            print(f"FAIL: delivery_latency_s.{k} 类型错 {lat.get(k)}"); ok = False
    if not isinstance(lat.get("n"), int): print("FAIL: delivery_latency_s.n 类型错"); ok = False
# failures: 三个 int，count == from_kind_bad + hc_fail
f = d.get("failures")
if not isinstance(f, dict): print("FAIL: failures 类型错"); ok = False
else:
    if not all(isinstance(f.get(k), int) for k in ("count", "from_kind_bad", "hc_fail")):
        print("FAIL: failures 字段类型错", f); ok = False
    if f.get("count") != f.get("from_kind_bad", 0) + f.get("hc_fail", 0):
        print("FAIL: failures.count 不等于两来源之和", f); ok = False
    if f.get("from_kind_bad") != 1: print(f"FAIL: 脏数据应 1 条，实际 {f.get('from_kind_bad')}"); ok = False
# throughput: 恰好 24 个键，值全 int
t = d.get("throughput_hourly")
if not isinstance(t, dict) or len(t) != 24:
    print(f"FAIL: throughput_hourly 应 24 个键，实际 {type(t)}"); ok = False
else:
    for k, v in t.items():
        if not isinstance(k, str) or not isinstance(v, int):
            print(f"FAIL: throughput_hourly[{k}] 类型错"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?                          # 先存住断言结果——后面的 kill 会覆盖 $?
kill "$PID" 2>/dev/null
PID=""
exit $RC
