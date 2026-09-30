# -*- coding: utf-8 -*-
"""
温暖通信台 MCP 端点（#18 修复版）：JSON-RPC 2.0 over HTTP + 按次付费骨架。

修复内容（bug #18）：原来这个模块自建一个 MCP_DB 私库（conversations/messages 两
张私有表），MCP 发的消息在 /v1/inbox 里 count 永远是 0。现在改成：

- 用 attach(**state) 拿 q / q1 / ex / new_id / now_iso（和 api_v1 同一套 state），
  **不自建库**：会话与消息全部读写 conversations / members / messages —— 与
  /v1、/api 完全同表，MCP ↔ /v1 ↔ /api 三路互通。
- send_message 支持 to（Agent 名字或 id，或写「人」），逻辑对齐 /v1/send
  （按名字自动找/建单聊、client_msg_id 幂等、不许发给自己）。
- fetch_inbox 用 seq 游标 since（对齐 /v1/inbox）；agent 的收件箱**不含自己发的**
  （防守候进程回环）；人（user_token）的收件箱是全量消息。
- list_conversations 读 conversations + members（agent 只看到自己参与的会话）。
- 按次计费骨架保留：额度表 mcp_calls 建在**共享库**里（不是独立库文件），
  MCP_FREE_CALLS 默认 100，超额 HTTP 402 + paymentId + accepts；只收 tools/call。

接线说明：main.py 现在 include_router(mcp_server.router) 时没调 attach；本模块在
state 为空时回退用 api_v1._S（main.py 已在启动时给 api_v1 attach 好同一套
q/q1/ex/new_id/now_iso），所以不依赖 main.py 改动也能通。main.py 以后要显式接，
加一行 `mcp_server.attach(db=_db, ..., q=q, q1=q1, ex=ex, new_id=new_id, now_iso=now_iso)`
即可（本文件的 attach 是 dict update，可多次调）。
"""
import json
import sqlite3
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# ---------------------------------------------------------------- state（attach 注入）

_S: Dict[str, object] = {}


def attach(**state) -> None:
    """main.py 启动时调用：user_token / q / q1 / ex / new_id / now_iso（+可选 tenancy）。"""
    _S.update(state)


def _state() -> Dict[str, object]:
    if _S:
        return _S
    # 回退：main.py 还没给本模块 attach 时，用 api_v1 的 state（同一个进程、同一套函数）
    try:
        import api_v1

        if getattr(api_v1, "_S", None):
            return api_v1._S
    except Exception:
        pass
    raise RuntimeError("mcp_server 未 attach state（q/q1/ex/new_id/now_iso）")


def _q(sql: str, args=()):
    return _state()["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _state()["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()):
    return _state()["ex"](sql, args)  # type: ignore[operator]


def _free_calls() -> int:
    try:
        import os

        return int(os.environ.get("MCP_FREE_CALLS", "100"))
    except ValueError:
        return 100


# ---------------------------------------------------------------- 认证（与 /v1 同一套身份）

def _who(token: str) -> Optional[Dict[str, str]]:
    """token → {"kind":"human"|"agent","id":...,"name":...}（对齐 api_v1._who）。"""
    if not token:
        return None
    s = _state()
    if token == s.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    ten = s.get("tenancy")
    resolve = ten.get("resolve_token") if isinstance(ten, dict) else None
    if resolve is not None:
        try:
            r = resolve(token)
        except Exception:
            r = None
        if r is not None and r[0] == "human":
            row = _q1("SELECT email FROM accounts WHERE id = ?", (r[1],))
            return {"kind": "human", "id": r[1], "name": (row["email"] if row else r[1])}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is None:
        return None
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


# ---------------------------------------------------------------- 额度（共享库，骨架）

def _consume_quota(account_id: str, free: int) -> bool:
    """额度内扣一次并返回 True；超额返回 False。额度表不可用时放行（通信优先）。"""
    try:
        _ex(
            "CREATE TABLE IF NOT EXISTS mcp_calls ("
            " account_id TEXT PRIMARY KEY,"
            " calls INTEGER NOT NULL DEFAULT 0,"
            " updated_at TEXT NOT NULL)"
        )
        row = _q1("SELECT calls FROM mcp_calls WHERE account_id = ?", (account_id,))
        used = row["calls"] if row else 0
        if used >= free:
            return False
        _ex(
            "INSERT INTO mcp_calls(account_id, calls, updated_at) VALUES(?, 1, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET calls = calls + 1, "
            "updated_at = excluded.updated_at",
            (account_id, _state()["now_iso"]()),  # type: ignore[operator]
        )
        return True
    except Exception:
        return True


# ---------------------------------------------------------------- 会话/成员（对齐 /v1）

def _find_agent(needle: str) -> sqlite3.Row:
    """按 id 或名字找 Agent（名字唯一才认；重名提示用 id）。"""
    needle = needle.strip()
    row = _q1("SELECT * FROM agents WHERE id = ?", (needle,))
    if row is not None:
        return row
    rows = _q("SELECT * FROM agents WHERE name = ?", (needle,))
    if not rows:
        raise ValueError("没有这个 Agent：%s（可先用 /v1/agents 看名字）" % needle)
    if len(rows) > 1:
        raise ValueError("名字 %s 有多个，请改用 id" % needle)
    return rows[0]


def _dm_between(a_id: str, b_id: str) -> sqlite3.Row:
    """找两人之间的单聊；没有就建一条。'human' 表示人（不是 members 里的一行）。"""

    def name_of(i: str) -> str:
        if i == "human":
            return "人"
        r = _q1("SELECT name FROM agents WHERE id = ?", (i,))
        return r["name"] if r else i

    want = {a_id, b_id}
    want.discard("human")
    for r in _q("SELECT conversation_id FROM members WHERE agent_id = ?", (b_id if b_id != "human" else a_id,)):
        cid = r["conversation_id"]
        conv = _q1("SELECT * FROM conversations WHERE id = ?", (cid,))
        if conv is None or conv["kind"] != "dm":
            continue
        have = {m["agent_id"] for m in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))}
        if have == want:
            return conv
    cid = _state()["new_id"]("cv")  # type: ignore[operator]
    title = ("人 ↔ " + name_of(b_id)) if a_id == "human" else (name_of(a_id) + " ↔ " + name_of(b_id))
    _ex(
        "INSERT INTO conversations (id, title, kind, created_at) VALUES (?,?,?,?)",
        (cid, title.replace("人 ↔ 人", "人"), "dm", _state()["now_iso"]()),  # type: ignore[operator]
    )
    for aid in want:
        _ex("INSERT OR IGNORE INTO members (conversation_id, agent_id) VALUES (?,?)", (cid, aid))
    return _q1("SELECT * FROM conversations WHERE id = ?", (cid,))  # type: ignore[return-value]


# ---------------------------------------------------------------- 工具实现（读写与 /v1 同表）

def _tool_send_message(args: dict, who: Dict[str, str]) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text 必填")
    to = (args.get("to") or "").strip()
    if to in ("人", "human", "老板", "我"):
        target_id, target_name = "human", "人"
    else:
        row = _find_agent(to)
        target_id, target_name = row["id"], row["name"]
    me_id = "human" if who["kind"] == "human" else who["id"]
    if target_id == me_id:
        raise ValueError("别给自己发")
    conv = _dm_between(me_id, target_id)
    cmid = (args.get("client_msg_id") or "").strip() or None
    if cmid:
        old = _q1(
            "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
            (conv["id"], cmid),
        )
        if old is not None:
            return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"],
                    "conversation_id": conv["id"]}
    mid = _state()["new_id"]("msg")  # type: ignore[operator]
    try:
        cur = _ex(
            "INSERT INTO messages (id, conversation_id, from_kind, from_id, text, "
            "client_msg_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, conv["id"], who["kind"], who["id"], text, cmid, _state()["now_iso"]()),  # type: ignore[operator]
        )
        seq = cur.lastrowid
    except sqlite3.IntegrityError:
        if cmid:
            old = _q1(
                "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
                (conv["id"], cmid),
            )
            if old is not None:
                return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"],
                        "conversation_id": conv["id"]}
        raise ValueError("写入冲突，请重试")
    return {"ok": True, "duplicate": False, "id": mid, "seq": seq,
            "conversation_id": conv["id"], "to": {"id": target_id, "name": target_name}}


def _tool_fetch_inbox(args: dict, who: Dict[str, str]) -> dict:
    since = args.get("since") or 0
    if not isinstance(since, int):
        raise ValueError("since 要整数（seq 游标）")
    if who["kind"] == "human":
        rows = _q("SELECT m.* FROM messages m WHERE m.seq > ? ORDER BY m.seq LIMIT 200", (since,))
    else:
        # agent 的收件箱不含它自己发的（防守候进程回环）
        rows = _q(
            "SELECT m.* FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id "
            "WHERE mb.agent_id = ? AND m.seq > ? AND NOT (m.from_kind='agent' AND m.from_id = ?) "
            "ORDER BY m.seq LIMIT 200",
            (who["id"], since, who["id"]),
        )
    out = []
    for r in rows:
        from_name = "人" if r["from_kind"] == "human" else (
            _q1("SELECT name FROM agents WHERE id = ?", (r["from_id"],)) or {"name": r["from_id"]}
        )["name"]
        conv = _q1("SELECT title FROM conversations WHERE id = ?", (r["conversation_id"],))
        out.append({
            "seq": r["seq"], "from": from_name, "from_kind": r["from_kind"],
            "text": r["text"], "ts": r["created_at"],
            "conversation": conv["title"] if conv else r["conversation_id"],
            "conversation_id": r["conversation_id"],
        })
    head = _q1("SELECT MAX(seq) AS m FROM messages")
    global_max = int(head["m"] or 0) if head else 0
    latest = max([r["seq"] for r in rows], default=0)
    latest = max(latest, global_max if since > global_max else 0, since if since <= global_max else 0)
    return {"messages": out, "latest": latest, "count": len(out)}


def _tool_list_conversations(args: dict, who: Dict[str, str]) -> dict:
    if who["kind"] == "human":
        rows = _q("SELECT * FROM conversations ORDER BY created_at")
    else:
        rows = _q(
            "SELECT c.* FROM conversations c JOIN members mb ON mb.conversation_id = c.id "
            "WHERE mb.agent_id = ? ORDER BY c.created_at",
            (who["id"],),
        )
    out = []
    for c in rows:
        mids = [r["agent_id"] for r in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (c["id"],))]
        out.append({"id": c["id"], "title": c["title"], "kind": c["kind"],
                    "created_at": c["created_at"], "members": mids})
    return {"conversations": out}


TOOLS = [
    {
        "name": "send_message",
        "description": "发一条消息给「to」：Agent 名字或 id，或写「人」（自动找/建单聊）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人：Agent 名字或 id，或「人」"},
                "text": {"type": "string", "description": "消息正文"},
                "client_msg_id": {"type": "string", "description": "幂等键，可不填"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "list_conversations",
        "description": "列出会话（agent 只看到自己参与的；含成员列表）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_inbox",
        "description": "收件箱：since 起的新消息（seq 游标；agent 不含自己发的）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "integer", "description": "seq 游标，默认 0"},
            },
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


def _handle(method: str, params: dict, rid, who: Dict[str, str]):
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
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
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
            result = _TOOL_FN[name](arguments, who)
        except ValueError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}, None
        except Exception as e:
            return {"content": [{"type": "text", "text": f"internal: {e}"}], "isError": True}, None
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False,
        }, None
    return None, (200, _rpc_error(-32601, f"method not found: {method}", rid))


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    auth = request.headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    who = _who(token)
    if who is None:
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
        if not _consume_quota(who["kind"] + ":" + who["id"], _free_calls()):
            return JSONResponse(
                status_code=402,
                content={
                    "detail": "需要付费",
                    "paymentId": "pay_" + uuid.uuid4().hex,
                    "accepts": [
                        {"scheme": "exact", "amount": "0.001", "asset": "USDC",
                         "network": "eip155:196"}
                    ],
                    "free_calls": _free_calls(),
                },
            )

    result, err = _handle(method, params, rid, who)
    if err is not None:
        return JSONResponse(status_code=err[0], content=err[1])
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "result": result, "id": rid})


# 独立跑（调试用）：需要先 attach 才能用工具
if __name__ == "__main__":
    import os

    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="warm-hub-mcp")
    app.include_router(router)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MCP_PORT", "8900")))
