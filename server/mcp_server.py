# -*- coding: utf-8 -*-
"""
温暖通信台 MCP 端点（P1-C）：JSON-RPC 2.0 over HTTP + 按次付费骨架。

- 交付物：本文件（新文件，不改 main.py）。对外必须提供 `router = APIRouter()`，
  main.py 里 `app.include_router(mcp_server.router)` 即可挂上 `/mcp`。
- 依赖：只用 fastapi / pydantic / 标准库。
- 认证：请求头 `Authorization: Bearer <agent token 或 account token>`。
  骨架阶段没有 token 注册表，token 的 sha256 前 32 位即 account_id；正式版把
  _account_id() 换成 token→账号 解析即可，别处不用动。
- 收费点：只收 `tools/call`（initialize / tools/list 不计数）。
  免费额度读环境变量 MCP_FREE_CALLS（默认 100），计数存 SQLite 表
  mcp_calls(account_id, calls, updated_at)。额度内直接执行；超额返回 HTTP 402。
  只做骨架：不真的上链、不真收款。
- SQLite 路径：环境变量 MCP_DB（默认 ./mcp.sqlite3，测试用临时文件）。
  会话/消息两张表是本文件的私有存储（骨架版工具实现），正式版可把
  _TOOL_FN 里的实现换成 main.py 已有的业务函数。
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# ---------------------------------------------------------------- 配置

def _db_path() -> str:
    return os.environ.get("MCP_DB", "./mcp.sqlite3")


def _free_calls() -> int:
    try:
        return int(os.environ.get("MCP_FREE_CALLS", "100"))
    except ValueError:
        return 100


# ---------------------------------------------------------------- 存储

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_calls(
  account_id TEXT PRIMARY KEY,
  calls      INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations(
  conversation_id TEXT PRIMARY KEY,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  from_agent_id   TEXT,
  text            TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------- 认证

def _account_id(auth: Optional[str]) -> Optional[str]:
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    # 骨架：token 即账号（无注册表）；正式版在此处接 token→account 解析
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- 额度

def _consume_quota(account_id: str, free: int) -> bool:
    """额度内扣一次并返回 True；超额返回 False（不计数、不执行）。"""
    conn = _conn()
    try:
        with conn:  # 事务：读-判-写 原子
            row = conn.execute(
                "SELECT calls FROM mcp_calls WHERE account_id = ?", (account_id,)
            ).fetchone()
            used = row["calls"] if row else 0
            if used >= free:
                return False
            conn.execute(
                "INSERT INTO mcp_calls(account_id, calls, updated_at) VALUES(?, 1, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET calls = calls + 1, "
                "updated_at = excluded.updated_at",
                (account_id, int(time.time())),
            )
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------- 工具实现（骨架）

def _parse_since(since) -> int:
    if since is None:
        return 0
    if isinstance(since, bool):
        raise ValueError("since 要 ISO 8601 或 Unix 秒")
    if isinstance(since, (int, float)):
        return int(since)
    if isinstance(since, str):
        try:
            return int(datetime.fromisoformat(since).timestamp())
        except ValueError:
            pass
        try:
            return int(float(since))
        except ValueError:
            raise ValueError("since 要 ISO 8601 或 Unix 秒")
    raise ValueError("since 要 ISO 8601 或 Unix 秒")


def _tool_send_message(args: dict) -> dict:
    conversation_id = args.get("conversation_id")
    text = args.get("text")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id 必填（字符串）")
    if not isinstance(text, str) or not text:
        raise ValueError("text 必填（字符串）")
    from_agent_id = args.get("from_agent_id")
    now = int(time.time())
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations(conversation_id, created_at) "
                "VALUES(?, ?)",
                (conversation_id, now),
            )
            cur = conn.execute(
                "INSERT INTO messages(conversation_id, from_agent_id, text, created_at) "
                "VALUES(?, ?, ?, ?)",
                (conversation_id, from_agent_id, text, now),
            )
        return {
            "ok": True,
            "message_id": cur.lastrowid,
            "conversation_id": conversation_id,
            "created_at": now,
        }
    finally:
        conn.close()


def _tool_list_conversations(args: dict) -> dict:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT c.conversation_id, COUNT(m.id) AS message_count, "
            "MAX(m.created_at) AS last_message_at "
            "FROM conversations c LEFT JOIN messages m "
            "ON m.conversation_id = c.conversation_id "
            "GROUP BY c.conversation_id ORDER BY last_message_at DESC"
        ).fetchall()
        return {"conversations": [dict(r) for r in rows]}
    finally:
        conn.close()


def _tool_fetch_inbox(args: dict) -> dict:
    agent_id = args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id 必填（字符串）")
    since_ts = _parse_since(args.get("since"))
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, conversation_id, from_agent_id, text, created_at "
            "FROM messages WHERE created_at >= ? AND IFNULL(from_agent_id, '') != ? "
            "ORDER BY created_at, id LIMIT 200",
            (since_ts, agent_id),
        ).fetchall()
        return {"messages": [dict(r) for r in rows]}
    finally:
        conn.close()


TOOLS = [
    {
        "name": "send_message",
        "description": "往一个会话里发一条消息（会话不存在会自动创建）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "会话 ID"},
                "text": {"type": "string", "description": "消息正文"},
                "from_agent_id": {"type": "string", "description": "发件 Agent ID，可不填"},
            },
            "required": ["conversation_id", "text"],
        },
    },
    {
        "name": "list_conversations",
        "description": "列出全部会话（含消息数和最后活跃时间）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_inbox",
        "description": "取某个 agent 的收件箱：since 起、别人发来的消息（按时间升序，最多 200 条）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "收件 Agent ID"},
                "since": {
                    "type": ["string", "number"],
                    "description": "起始时间：Unix 秒或 ISO 8601",
                },
            },
            "required": ["agent_id"],
        },
    },
]

_TOOL_FN = {
    "send_message": _tool_send_message,
    "list_conversations": _tool_list_conversations,
    "fetch_inbox": _tool_fetch_inbox,
}


# ---------------------------------------------------------------- JSON-RPC

def _rpc_error(code: int, message: str, rid) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": rid}


def _handle(method: str, params: dict, rid):
    """返回 (result, None) 或 (None, (http_status, rpc_error_body))。"""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "warm-hub", "version": "0.1"},
        }, None
    if method == "tools/list":
        return {
            "tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]
        }, None
    if method == "tools/call":
        name = (params or {}).get("name")
        arguments = (params or {}).get("arguments") or {}
        if name not in _TOOL_FN:
            return None, (200, _rpc_error(-32602, f"unknown tool: {name}", rid))
        if not isinstance(arguments, dict):
            return None, (200, _rpc_error(-32602, "arguments 必须是对象", rid))
        try:
            result = _TOOL_FN[name](arguments)
        except ValueError as e:
            return {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            }, None
        except Exception as e:  # 内部错误不外抛，按工具错误回
            return {
                "content": [{"type": "text", "text": f"internal: {e}"}],
                "isError": True,
            }, None
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False,
        }, None
    return None, (200, _rpc_error(-32601, f"method not found: {method}", rid))


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    account_id = _account_id(request.headers.get("Authorization"))
    if account_id is None:
        return JSONResponse(
            status_code=401,
            content=_rpc_error(-32001, "未认证：需要 Authorization: Bearer <token>", None),
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=200, content=_rpc_error(-32700, "parse error", None))
    if not isinstance(body, dict):
        return JSONResponse(status_code=200, content=_rpc_error(-32600, "batch 请求不支持", None))

    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(method, str):
        return JSONResponse(status_code=200, content=_rpc_error(-32600, "invalid request", rid))

    # 收费点：只收 tools/call
    if method == "tools/call":
        if not _consume_quota(account_id, _free_calls()):
            return JSONResponse(
                status_code=402,
                content={
                    "detail": "需要付费",
                    "paymentId": "pay_" + uuid.uuid4().hex,
                    "accepts": [
                        {
                            "scheme": "exact",
                            "amount": "0.001",
                            "asset": "USDC",
                            "network": "eip155:196",
                        }
                    ],
                    "free_calls": _free_calls(),
                },
            )

    result, err = _handle(method, params, rid)
    if err is not None:
        return JSONResponse(status_code=err[0], content=err[1])
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "result": result, "id": rid})


# 独立跑（调试用）：python3 mcp_server.py
if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="warm-hub-mcp")
    app.include_router(router)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MCP_PORT", "8900")))
