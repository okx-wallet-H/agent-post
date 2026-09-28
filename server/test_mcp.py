# -*- coding: utf-8 -*-
"""
P1-C 自测：临时 DB + MCP_FREE_CALLS=1 按「怎么算做完」逐条验。
用法：python3 test_mcp.py
"""
import os
import sys
import tempfile
import traceback

# 必须在 import mcp_server 之前设好环境（额度=1，临时库）
os.environ["MCP_FREE_CALLS"] = "1"
_tmpdb = tempfile.NamedTemporaryFile(prefix="mcp_test_", suffix=".sqlite3", delete=False)
_tmpdb.close()
os.environ["MCP_DB"] = _tmpdb.name

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import mcp_server  # 同目录

    app = FastAPI()
    app.include_router(mcp_server.router)
    client = TestClient(app)
    H = {"Authorization": "Bearer test-agent-token"}

    # 1) initialize 通
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, headers=H)
    assert r.status_code == 200, f"initialize HTTP {r.status_code}"
    body = r.json()
    assert body["result"]["serverInfo"] == {"name": "warm-hub", "version": "0.1"}, body
    assert "protocolVersion" in body["result"] and "tools" in body["result"]["capabilities"], body

    # 2) tools/list 列出 3 个工具
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=H)
    assert r.status_code == 200, f"tools/list HTTP {r.status_code}"
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["send_message", "list_conversations", "fetch_inbox"], names

    call = lambda name, args, rid: client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers=H,
    )

    # 3) 第 1 次 tools/call 成功
    r = call("send_message", {"conversation_id": "c1", "text": "你好", "from_agent_id": "a1"}, 3)
    assert r.status_code == 200, f"第 1 次 tools/call HTTP {r.status_code}: {r.text}"
    res = r.json()["result"]
    assert res["isError"] is False, res

    # 4) 第 2 次 tools/call 返回 402 且 body 有 paymentId 与 accepts
    r = call("send_message", {"conversation_id": "c1", "text": "第二条"}, 4)
    assert r.status_code == 402, f"第 2 次 tools/call 应 402，实际 {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("paymentId", "").startswith("pay_"), body
    assert isinstance(body.get("accepts"), list) and body["accepts"], body
    assert body["accepts"][0].get("asset") == "USDC" and body["accepts"][0].get("network") == "eip155:196", body

    # 5) 顺带验：未认证 401；initialize/tools/list 不耗额度
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}})
    assert r.status_code == 401, f"未认证应 401，实际 {r.status_code}"

    print("TEST: PASS")
except Exception:
    print("TEST: FAIL")
    traceback.print_exc()
    sys.exit(1)
finally:
    try:
        os.unlink(_tmpdb.name)
    except OSError:
        pass
