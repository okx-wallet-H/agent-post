#!/bin/bash
# smoke_review.sh —— #31 群里评议机制冒烟（临时库 + 临时端口，不碰生产 hub.db）
# 场景：群 g1 有甲、乙。m1 甲发「交付：…」（结论型）；m2 甲发「今日巡检完成」（日常通知）；
#       m3 乙发「结论：…」（结论型）；m4 甲发「方案：…」且 review='{"by":["乙"]}'（显式点名评议）。
# 断言：① 乙的 pending 含 m1/m4（m4 带 wake=乙）、不含 m2；③ 甲的 pending 含 m3、不含 m2；
#       ② 乙对 m1 给 disagree 后 /v1/conflicts 列出 m1；乙再查 pending 不再有 m1（评过的不回）。
# 跑法： bash smoke_review.sh；SMOKE_PORT=8809 bash smoke_review.sh（换端口）
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TMP=$(mktemp -d /tmp/review-smoke-XXXXXX)
PORT=${SMOKE_PORT:-8808}
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

busy() { curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/v1/reviews/pending" 2>/dev/null; }
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
    from_kind TEXT NOT NULL, from_id TEXT NOT NULL, text TEXT NOT NULL, client_msg_id TEXT, created_at TEXT NOT NULL,
    review TEXT, reviews TEXT);
CREATE TABLE presence (agent_id TEXT PRIMARY KEY, last_seen TEXT);
""")
now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
db.execute("INSERT INTO agents VALUES ('ag1','甲','tok1','x')")
db.execute("INSERT INTO agents VALUES ('ag2','乙','tok2','x')")
db.execute("INSERT INTO conversations VALUES ('g1','策略群','group','x')")
db.execute("INSERT INTO members VALUES ('g1','ag1')")
db.execute("INSERT INTO members VALUES ('g1','ag2')")
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m1','g1','agent','ag1','交付：网格策略回测，胜率 52%',?)", (now,))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m2','g1','agent','ag1','今日巡检完成，无异常',?)", (now,))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m3','g1','agent','ag2','结论：动量因子比价值因子有效',?)", (now,))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at,review) VALUES ('m4','g1','agent','ag1','方案：仓位改用风险平价',?,'{\"by\":[\"乙\"]}')", (now,))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m5','g1','agent','ag1','@乙 帮我看下网格参数',?)", (now,))
db.execute("INSERT INTO messages (id,conversation_id,from_kind,from_id,text,created_at) VALUES ('m6','g1','agent','ag1','周报：' || ? || '。以上。',?)", ("长" * 250, now))
db.commit()
print("seed ok")
PY

cat > "$TMP/smoke_app.py" <<PYEOF
import sqlite3, threading, sys
sys.path.insert(0, "$PWD")
from fastapi import FastAPI
import review
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
review.attach(db=db, lock=lock, user_token="humantok", q=q, q1=q1, ex=ex, now_iso=now_iso)
app = FastAPI()
app.include_router(review.router)
PYEOF

cd "$TMP"
"$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" > smoke.log 2>&1 &
PID=$!
for i in $(seq 1 40); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/v1/reviews/pending" && break
    sleep 0.5
done

P_ET=$(curl -s --max-time 5 -H "Authorization: Bearer tok2" "http://127.0.0.1:$PORT/v1/reviews/pending")
P_JIA=$(curl -s --max-time 5 -H "Authorization: Bearer tok1" "http://127.0.0.1:$PORT/v1/reviews/pending")
R1=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer tok2" -H "Content-Type: application/json" \
    -d '{"stance":"disagree","reason":"回测没扣手续费，胜率虚高"}' "http://127.0.0.1:$PORT/v1/messages/m1/review")
R3=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer tok1" -H "Content-Type: application/json" \
    -d '{"stance":"improve","reason":"动量窗口换成 21 天并做多空分组，实现路径我可以补"}' "http://127.0.0.1:$PORT/v1/messages/m3/review")
R4=$(curl -s --max-time 5 -X POST -H "Authorization: Bearer tok2" -H "Content-Type: application/json" \
    -d '{"stance":"agree","reason":"附议，再压一层波动率目标"}' "http://127.0.0.1:$PORT/v1/messages/m4/review")
C=$(curl -s --max-time 5 -H "Authorization: Bearer tok1" "http://127.0.0.1:$PORT/v1/conflicts")
IMP=$(curl -s --max-time 5 -H "Authorization: Bearer tok1" "http://127.0.0.1:$PORT/v1/reviews/improvements")
P_ET2=$(curl -s --max-time 5 -H "Authorization: Bearer tok2" "http://127.0.0.1:$PORT/v1/reviews/pending")
D=$(curl -s --max-time 5 -H "Authorization: Bearer tok2" "http://127.0.0.1:$PORT/v1/group-digest?since=2")
ENC=$("$PY" -c "import urllib.parse; print(urllib.parse.quote('乙'))")
D403=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Authorization: Bearer tok1" "http://127.0.0.1:$PORT/v1/group-digest?since=0&agent=$ENC")

echo "--- 乙的 pending（应含 m1、m4，不含 m2）---"
echo "$P_ET"
echo "--- 甲的 pending（应含 m3，不含 m2）---"
echo "$P_JIA"
echo "--- 评议响应：乙 disagree m1 / 甲 improve m3 / 乙 agree m4 ---"
echo "$R1"; echo "$R3"; echo "$R4"
echo "--- /v1/conflicts（应只含 m1；improve/agree 不算分歧）---"
echo "$C"
echo "--- /v1/reviews/improvements（应含 m3 的 improve）---"
echo "$IMP"
echo "--- 乙评完后的 pending（m1、m4 都评过，应为空）---"
echo "$P_ET2"
echo "--- 乙的 group-digest since=2（应含 m4/m5/m6，不含自己发的 m3 和 seq≤2 的）---"
echo "$D"

"$PY" - "$P_ET" "$P_JIA" "$R1" "$R3" "$R4" "$C" "$IMP" "$P_ET2" "$D" "$D403" <<'PY'
import json, sys
p_et, p_jia, r1, r3, r4, c, imp, p_et2, d, d403 = sys.argv[1:]
ok = True
et = json.loads(p_et)
et_ids = {m["id"]: m for m in et.get("pending", [])}
# ① 结论型进 pending：m1（交付：）、m4（方案：+显式 by）；m2 日常通知不进
for mid, label in (("m1", "交付：结论型"), ("m4", "方案：+显式点名")):
    if mid not in et_ids:
        print(f"FAIL: 乙的 pending 缺 {label} 消息 {mid}，实际 {list(et_ids)}"); ok = False
if "m2" in et_ids:
    print("FAIL: 日常通知 m2 不该进 pending"); ok = False
# m4 的 wake 要标出 乙（显式 review_by）
w = et_ids.get("m4", {}).get("wake", [])
if "乙" not in w:
    print(f"FAIL: m4 wake 应含乙，实际 {w}"); ok = False
# ③ 甲的 pending：m3 结论型（乙发的）；m2 不进
jia = json.loads(p_jia)
jia_ids = {m["id"] for m in jia.get("pending", [])}
if "m3" not in jia_ids:
    print(f"FAIL: 甲的 pending 缺 m3，实际 {jia_ids}"); ok = False
if "m2" in jia_ids:
    print("FAIL: 甲的 pending 里日常通知 m2 不该出现"); ok = False
# 评议三种 stance 落库正确
d1 = json.loads(r1)
if not d1.get("ok") or d1["message"]["reviews"][0].get("stance") != "disagree":
    print(f"FAIL: 乙对 m1 的 disagree 记录不对：{r1[:200]}"); ok = False
d3 = json.loads(r3)
if not d3.get("ok") or d3["message"]["reviews"][0].get("stance") != "improve" or d3["message"]["reviews"][0].get("who") != "甲":
    print(f"FAIL: 甲对 m3 的 improve 记录不对：{r3[:200]}"); ok = False
d4 = json.loads(r4)
if not d4.get("ok") or d4["message"]["reviews"][0].get("stance") != "agree":
    print(f"FAIL: 乙对 m4 的 agree 记录不对：{r4[:200]}"); ok = False
# ② 只有 disagree 进 conflicts：应只含 m1（m3 improve / m4 agree 不进）
cf = json.loads(c)
c_ids = [x["id"] for x in cf.get("conflicts", [])]
if c_ids != ["m1"]:
    print(f"FAIL: conflicts 应只含 m1（improve/agree 不算分歧），实际 {c_ids}"); ok = False
else:
    x = next(x for x in cf["conflicts"] if x["id"] == "m1")
    if not any(d.get("who") == "乙" and d.get("reason") for d in x.get("disagrees", [])):
        print(f"FAIL: m1 的 disagree 记录应含乙和理由：{x.get('disagrees')}"); ok = False
# 新增断言：improve 出现在 improvements 里、且不进 conflicts
im = json.loads(imp)
im_ids = {x["id"]: x for x in im.get("improvements", [])}
if "m3" not in im_ids:
    print(f"FAIL: improvements 应含 m3，实际 {list(im_ids)}"); ok = False
else:
    x = im_ids["m3"]
    if not any(i.get("who") == "甲" and "21 天" in (i.get("reason") or "") for i in x.get("improvements", [])):
        print(f"FAIL: m3 的 improvements 应含甲的具体建议：{x.get('improvements')}"); ok = False
if "m4" in im_ids:
    print("FAIL: agree 评议不该出现在 improvements 里"); ok = False
# 评过的从 pending 消失（乙评了 m1、m4 → 空）
et2 = json.loads(p_et2)
et2_ids = {m["id"] for m in et2.get("pending", [])}
if et2_ids:
    print(f"FAIL: 乙评完 m1/m4 后 pending 应为空，实际 {et2_ids}"); ok = False
# group-digest：since=2 之后的新消息、不含自己发的（m3 是乙发的）
dg = json.loads(d)
d_msgs = dg.get("messages", [])
d_seqs = [m["seq"] for m in d_msgs]
if d_seqs != [4, 5, 6]:
    print(f"FAIL: digest since=2 应返回 seq [4,5,6]（不含乙自己发的 m3 和 seq≤2），实际 {d_seqs}"); ok = False
else:
    m5 = next(m for m in d_msgs if m["seq"] == 5)
    if "乙" not in m5.get("mentions", []):
        print(f"FAIL: m5 的 mentions 应含乙，实际 {m5.get('mentions')}"); ok = False
    m6 = next(m for m in d_msgs if m["seq"] == 6)
    if m6.get("full_len", 0) <= 200 or len(m6.get("text", "")) > 201:
        print(f"FAIL: m6 长文应截断到 200 字且 full_len 给原长：text_len={len(m6.get('text',''))} full_len={m6.get('full_len')}"); ok = False
    if dg.get("latest") != 6:
        print(f"FAIL: digest latest 应为 6，实际 {dg.get('latest')}"); ok = False
# agent 查别人的 digest 应 403（越权同 /v1/inbox）
if d403 != "403":
    print(f"FAIL: 甲查乙的 digest 应 403，实际 {d403}"); ok = False
print("TEST: PASS" if ok else "TEST: FAIL")
sys.exit(0 if ok else 1)
PY
RC=$?
exit $RC
