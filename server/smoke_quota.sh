#!/usr/bin/env bash
# smoke_quota.sh —— 限速 + 402 + /v1/usage 冒烟（P2 #20）
# 临时库起服务：A 账号连打 61 条 /v1/send，断言第 61 条 429；C 账号 1 条额度打第 2 条断言 402；
# GET /v1/usage 字段齐。打印 TEST: PASS 并贴输出。
set -u
cd "$(dirname "$0")"

python3 - > /tmp/smoke_quota_srv.log 2>&1 <<'PY' &
import secrets, sqlite3, sys, threading
sys.path.insert(0, ".")
import api_v1, billing
from fastapi import FastAPI
import uvicorn

conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE members (conversation_id TEXT NOT NULL, agent_id TEXT NOT NULL, PRIMARY KEY (conversation_id, agent_id));
CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, conversation_id TEXT NOT NULL,
    from_kind TEXT NOT NULL, from_id TEXT NOT NULL, text TEXT NOT NULL, client_msg_id TEXT, created_at TEXT NOT NULL);
""")
for aid, name, tok in (("ag_a", "发信方", "tok_a"), ("ag_b", "收信方", "tok_b"), ("ag_c", "限额号", "tok_c")):
    conn.execute("INSERT INTO agents (id, name, token, created_at) VALUES (?,?,?,?)", (aid, name, tok, billing.now_iso()))
billing.init_db(conn)

def ex(sql, args=()):
    cur = conn.execute(sql, args); conn.commit(); return cur

# A 兑换 1000 条额度；C 兑换 1 条额度（402 实测用）
ca = billing.create_coupon(conn, days=2, msgs=1000); billing.redeem_coupon(conn, ca, "ag_a")
cc = billing.create_coupon(conn, days=2, msgs=1);    billing.redeem_coupon(conn, cc, "ag_c")

billing.attach(db=conn, lock=threading.RLock(), user_token="h-test",
               q1=lambda sql, args=(): conn.execute(sql, args).fetchone())

api_v1.attach(
    q=lambda sql, args=(): conn.execute(sql, args).fetchall(),
    q1=lambda sql, args=(): conn.execute(sql, args).fetchone(),
    ex=ex,
    new_id=lambda p: "%s_%s" % (p, secrets.token_hex(6)),
    now_iso=billing.now_iso,
    new_token=lambda: secrets.token_urlsafe(24),
    tenancy={
        "billable_account": lambda who: who["id"] if who["kind"] == "agent" else None,
        "quota_guard": lambda acct, kind: billing.guard_quota(conn, acct, kind),
        "record_usage": lambda acct, kind, n=1: billing.record_usage(conn, acct, kind, n),
    },
)

app = FastAPI(title="quota 冒烟")
app.include_router(api_v1.router)
app.include_router(billing.router)
app.add_middleware(billing.RateLimitMiddleware)
uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")
PY
SRV=$!
sleep 2

echo "== 1) A 连打 61 条 POST /v1/send（发给 B），断言第 61 条 429 =="
LAST=""
for i in $(seq 1 61); do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8799/v1/send \
        -H "Authorization: Bearer tok_a" -H "Content-Type: application/json" \
        -d "{\"to\":\"ag_b\",\"text\":\"msg $i\"}")
    if [ "$i" = "61" ]; then LAST=$CODE; fi
done
echo "第 61 条 HTTP = $LAST"
HDR=$(curl -s -o /dev/null -D - -X POST http://127.0.0.1:8799/v1/send \
    -H "Authorization: Bearer tok_a" -H "Content-Type: application/json" \
    -d '{"to":"ag_b","text":"one more"}' | grep -i "retry-after" | tr -d '\r')
echo "Retry-After 头 = $HDR"

echo "== 2) GET /v1/usage（A 的 agent token，60 条已记用量）=="
USAGE=$(curl -s http://127.0.0.1:8799/v1/usage -H "Authorization: Bearer tok_a")
echo "$USAGE"

echo "== 3) C 只有 1 条额度：第 1 条 200，第 2 条 402 =="
C1=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8799/v1/send \
    -H "Authorization: Bearer tok_c" -H "Content-Type: application/json" \
    -d '{"to":"ag_b","text":"c1"}')
C2=$(curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8799/v1/send \
    -H "Authorization: Bearer tok_c" -H "Content-Type: application/json" \
    -d '{"to":"ag_b","text":"c2"}')
echo "第 1 条 HTTP = $C1；第 2 条响应 = $C2"

echo "== 断言 =="
python3 - "$USAGE" "$LAST" "$HDR" "$C1" <<'PY'
import json, sys
u, last, hdr, c1 = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
for k in ("period","agents","msgs","quota_msgs","quota_agents","remaining_msgs","remaining_agents","expires_at"):
    assert k in u, "usage 缺字段 %s: %s" % (k, u)
assert u["quota_msgs"] == 1000 and u["remaining_msgs"] == 940, u   # 60 条已记用量
assert last == "429", "第 61 条应 429，实际 %s" % last
assert "retry-after" in hdr.lower() and hdr.strip(), "缺 Retry-After 头：%r" % hdr
assert c1 == "200", "C 第 1 条应 200，实际 %s" % c1
print("断言全过：/v1/usage 字段齐；第 61 条 429 + Retry-After；C 第 2 条 402（见上方响应）")
print("TEST: PASS")
PY

kill $SRV 2>/dev/null
wait $SRV 2>/dev/null
exit 0
