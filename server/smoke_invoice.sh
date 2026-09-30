#!/usr/bin/env bash
# smoke_invoice.sh —— 出账冒烟：临时库起服务，造「标准套餐 5 Agent + 21000 条」，断言 total = 59.00
set -u
cd "$(dirname "$0")"

python3 - > /tmp/invoice_smoke_srv.log 2>&1 <<'PY' &
import sqlite3, sys, threading
sys.path.insert(0, ".")
import billing, invoice
from fastapi import FastAPI
import uvicorn

conn = sqlite3.connect(":memory:", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
    " token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);"
)
conn.execute(
    "INSERT INTO agents (id, name, token, created_at) VALUES ('ag_test','冒烟号','tok_test',?)",
    (billing.now_iso(),),
)
billing.init_db(conn)
invoice.init_db(conn)

# 造数据：标准套餐 + 5 Agent + 21000 条（当月）
invoice.set_plan(conn, "ag_test", "标准")
conn.execute(
    "INSERT INTO usage_counters (account_id, period, msgs, agents) VALUES (?,?,?,?)",
    ("ag_test", billing.current_period(), 21000, 5),
)
conn.commit()

app = FastAPI(title="invoice 冒烟")
invoice.attach(
    db=conn,
    lock=threading.RLock(),
    user_token="h-test",
    q1=lambda sql, args=(): conn.execute(sql, args).fetchone(),
)
app.include_router(invoice.router)
uvicorn.run(app, host="127.0.0.1", port=8797, log_level="warning")
PY
SRV=$!
sleep 2

PERIOD=$(cd "$(dirname "$0")" && python3 -c "import sys; sys.path.insert(0,'.'); import billing; print(billing.current_period())")
echo "== GET /api/invoice?period=$PERIOD（账号 token）=="
RESP=$(curl -s "http://127.0.0.1:8797/api/invoice?period=$PERIOD" -H "Authorization: Bearer tok_test")
echo "$RESP"

echo "== 断言 =="
python3 - "$RESP" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert d["plan"] == "标准", d
assert d["agents"] == 5 and d["msgs"] == 21000, d
assert d["included_msgs"] == 20000, d
assert d["overage_msgs"] == 1000, d
assert d["plan_fee"] == 49.00, d
assert d["overage_fee"] == 10.00, d
assert d["total"] == 59.00, "total=%r 应为 59.00（49 + 1000×0.01）" % d["total"]
assert d["currency"] == "CNY", d
print("断言全过：plan_fee 49.00 + overage_fee 10.00 = total", d["total"])
print("TEST: PASS")
PY

kill $SRV 2>/dev/null
wait $SRV 2>/dev/null
exit 0
