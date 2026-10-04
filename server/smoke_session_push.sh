#!/bin/bash
# smoke_session_push.sh —— #35 手机登录 + Web Push 冒烟（临时库 + 临时端口）
# 断言：① 登录码 5 分钟内换到 cookie、用过一次再 redeem 401 ② 过期码 401
#       ③ 登录态（cookie）订阅落库、无 VAPID key 时 /v1/push/test 不抛错返回 skipped
#       ④ Bearer 和 cookie 都能过登录态
# 跑法： bash smoke_session_push.sh；SMOKE_PORT=8813 bash smoke_session_push.sh（换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/sesspush-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8812}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/session/me" 2>/dev/null; }
WANT="$PORT"
while busy "$PORT" && [ "$PORT" -lt $((WANT + 20)) ]; do PORT=$((PORT + 1)); done
if busy "$PORT"; then echo "端口 $WANT..$((WANT + 20)) 全被占，先腾一个出来再跑"; exit 2; fi

echo "解释器：$PY，端口 $PORT（注意：环境里没有 VAPID key，push 应走 skipped 降级）"

"$PY" - "$TMP" <<'PY'
import sqlite3, sys
tmp = sys.argv[1]
db = sqlite3.connect(f"{tmp}/smoke.db")
db.executescript("""
CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT);
""")
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import session, push
db = sqlite3.connect("$TMP/smoke.db", check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
def q(sql, args=()):
    with lock: return db.execute(sql, args).fetchall()
def q1(sql, args=()):
    with lock: return db.execute(sql, args).fetchone()
def ex(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur
def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
session.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex, now_iso=now_iso)
push.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex, now_iso=now_iso)
app = FastAPI()
app.include_router(session.router)
app.include_router(push.router)
PYEOF

cd "$TMP"
"$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 -H "Authorization: Bearer humantok" "http://127.0.0.1:$PORT/session/me" && break
    sleep 0.5
done

BASE="http://127.0.0.1:$PORT"
JAR="$TMP/cookies.txt"

CODE=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer humantok" "$BASE/login/code" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['code'])")
R1=$(curl -s --max-time 5 -c "$JAR" -X POST -H "Content-Type: application/json" -d "{\"code\":\"$CODE\"}" "$BASE/login/redeem")
R2=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -H "Content-Type: application/json" -d "{\"code\":\"$CODE\"}" "$BASE/login/redeem")
ME1=$(curl -s --max-time 5 -b "$JAR" "$BASE/session/me")
SUB=$(curl -s --max-time 5 -b "$JAR" -X POST -H "Content-Type: application/json" \
    -d '{"endpoint":"https://push.example.com/ep1","keys":{"p256dh":"BPdummy","auth":"adummy"}}' "$BASE/v1/push/subscribe")
TEST=$(curl -s --max-time 5 -b "$JAR" -X POST "$BASE/v1/push/test")
# ② 过期码：直接把库里该码 created_at 改到 10 分钟前
"$PY" - "$TMP/smoke.db" "$CODE" <<'PY'
import sqlite3, sys, datetime
db = sqlite3.connect(sys.argv[1])
old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
db.execute("UPDATE login_codes SET used_at=NULL, created_at=? WHERE code=?", (old, sys.argv[2]))
db.commit()
print("码已改过期")
PY
R3=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -H "Content-Type: application/json" -d "{\"code\":\"$CODE\"}" "$BASE/login/redeem")
DBROWS=$("$PY" - "$TMP/smoke.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0])
print(db.execute("SELECT count(*) FROM sessions").fetchone()[0])
PY
)

echo "--- ① redeem（应 200 + Set-Cookie）---"
echo "$R1"
echo "--- 同码再用（应 401）---"
echo "used_again=$R2"
echo "--- cookie 查 /session/me ---"
echo "$ME1"
echo "--- ② 过期码 redeem（应 401）---"
echo "expired=$R3"
echo "--- ③ 订阅（cookie 登录态）---"
echo "$SUB"
echo "--- ④ 无 VAPID key 的 push/test（应 skipped 不报错）---"
echo "$TEST"
echo "--- 落库：subscriptions / sessions 行数 ---"
echo "$DBROWS"

"$PY" - "$R1" "$R2" "$ME1" "$R3" "$SUB" "$TEST" "$DBROWS" <<'PY'
import json, sys
r1, r2, me1, r3, sub, test, dbrows = sys.argv[1:]
ok = True
# ① redeem 200 + 换到 cookie
d1 = json.loads(r1)
if not d1.get("ok"):
    print(f"FAIL: redeem 应 ok，实际 {r1[:200]}"); ok = False
# 用过一次就失效
if r2 != "401":
    print(f"FAIL: 同码再用应 401，实际 {r2}"); ok = False
# cookie 登录态
m = json.loads(me1)
if m.get("kind") != "human" or not m.get("id"):
    print(f"FAIL: cookie 应解析出 human，实际 {m}"); ok = False
# ② 过期码 401
if r3 != "401":
    print(f"FAIL: 过期码应 401，实际 {r3}"); ok = False
# ③ 订阅落库
s = json.loads(sub)
if not s.get("ok"):
    print(f"FAIL: subscribe 应 ok，实际 {sub[:200]}"); ok = False
lines = dbrows.strip().splitlines()
if lines[0] != "1":
    print(f"FAIL: push_subscriptions 应 1 行，实际 {lines[0]}"); ok = False
if lines[1] != "1":
    print(f"FAIL: sessions 应 1 行，实际 {lines[1]}"); ok = False
# ④ 无 VAPID key：skipped 且 HTTP 200
t = json.loads(test)
if not t.get("skipped"):
    print(f"FAIL: 无 VAPID key 应返回 skipped，实际 {test[:200]}"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
