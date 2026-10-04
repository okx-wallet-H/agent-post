"""a2a —— A2A（Agent-to-Agent）薄适配层（#34，第一步）

把邮局现有能力套上 A2A 外壳，最小可用：AgentCard 发现 + SendMessage/GetTask 两个方法。

════════════════════════════════════════════ 映射表（后续补 ListTasks/流式 时按这个加）════════════════════════════════════════════

| 我们的接口 | A2A 方法 | 说明 |
|---|---|---|
| GET /v1/cards（能力卡） | GET /.well-known/agent-card.json | 每张岗位卡的 can[] 一条 = 一个 skill；AgentCard.name=agent-post |
| POST /v1/send {to,text,...} | SendMessage | A2A message.role/parts[].text → text；to 支持 Agent 名字 / 人 / 群名（同 /v1/send 语义）|
| GET /v1/inbox?since=N（游标收） | （暂无，留给「流式/轮询」扩展） | 收件箱语义将来映射成 TaskPushNotification 或长轮询 |
| 消息 seq（全局单调） | taskId | SendMessage 返回 taskId=seq；GetTask 按 seq 查投递状态 |
| 收件人回话（同会话内 seq>taskId 的最新非本人消息） | GetTask 的 result 文本 | delivered=已投递；failed=查无此消息 |

版本协商：请求头 A2A-Version（缺省 1.0）；只认 1.x，其他大版本 → 400 + 支持列表。
鉴权：/a2a 要求 Authorization: Bearer（管理员 / 账号 / Agent 三种 token 都认，
同 api_v1 口径）；/.well-known/agent-card.json 公开（A2A 发现协议惯例）。

挂载方式（main.py 由 H 加，本模块只管导出）：
    import a2a
    a2a.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
               tenancy=api_v1._t(), now_iso=now_iso)
    app.include_router(a2a.router)
数据源：data/agent-cards.json（环境变量 CARDS_JSON 可覆盖，同 cards.py）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}

SUPPORTED_VERSIONS = ["1.0"]           # 只认 1.x 大版本

CARDS_FILE = os.environ.get(
    "CARDS_JSON", str(Path(__file__).resolve().parent.parent / "data" / "agent-cards.json")
)

_CACHE: Dict[str, object] = {"mtime": None, "cards": []}


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py / cards.py）。"""
    _S.update(state)


def _q(sql: str, args=()):
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()):
    return _S["ex"](sql, args)  # type: ignore[operator]


# ---------------------------------------------------------------- 鉴权（同 api_v1._who 三身份）


def _who(token: str) -> Optional[Dict[str, str]]:
    if not token:
        return None
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    ten = _S.get("tenancy")
    resolve = ten.get("resolve_token") if isinstance(ten, dict) else None
    if resolve is not None:
        try:
            r = resolve(token)
        except Exception:
            r = None
        if r is not None and r[0] == "human":
            return {"kind": "human", "id": r[1], "name": r[1]}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is not None:
        return {"kind": "agent", "id": row["id"], "name": row["name"]}
    acc = _q1("SELECT account_id FROM account_tokens WHERE token = ?", (token,))
    if acc is not None:
        return {"kind": "account", "id": acc["account_id"], "name": "人"}
    return None


def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    tok = ""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    who = _who(tok)
    if who is None:
        raise HTTPException(status_code=401, detail="token 不对：请在请求头带上 Authorization: Bearer <你的 token>")
    return who


def _load_cards() -> List[dict]:
    """读 data/agent-cards.json（mtime 变了重读，同 cards.py）。"""
    try:
        mtime = os.path.getmtime(CARDS_FILE)
    except OSError:
        return []
    if _CACHE["mtime"] != mtime:
        try:
            with open(CARDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _CACHE["cards"] = data.get("cards", []) if isinstance(data, dict) else []
            _CACHE["mtime"] = mtime
        except Exception:
            return _CACHE["cards"]  # type: ignore[return-value]
    return _CACHE["cards"]  # type: ignore[return-value]


# ---------------------------------------------------------------- AgentCard（发现）


@router.get("/.well-known/agent-card.json")
def agent_card(request: Request) -> Dict[str, object]:
    """A2A AgentCard：把岗位能力卡的 can[] 映射成 skills[]。公开（发现协议）。"""
    base = str(request.base_url).rstrip("/")
    skills: List[dict] = []
    for c in _load_cards():
        name = c.get("name", "?")
        role = c.get("role", "")
        how = c.get("how_to_call", "")
        for i, can in enumerate(c.get("can", []) or []):
            skills.append({
                "id": "%s-%d" % (name, i),
                "name": can[:30],
                "description": ("%s · %s" % (role, how))[:120],
                "tags": [name, role],
            })
    return {
        "name": "agent-post",
        "description": "智能体邮局：消息中台（单聊/群聊/@点名唤醒/补读/值班巡视）+ 岗位 Agent 协作",
        "url": base + "/a2a",
        "version": "0.1",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "skills": skills,
        "supportedInterfaces": [{"protocol": "HTTP+JSON", "url": base + "/a2a"}],
    }


# ---------------------------------------------------------------- /a2a（JSON-RPC 单端点）


class RpcIn(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    method: str
    params: dict = {}


def _rpc_error(code: int, message: str, rpc_id=None) -> Dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _rpc_ok(result: dict, rpc_id=None) -> Dict[str, object]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _check_version(version: str) -> None:
    major = (version or "1.0").strip().split(".")[0]
    if major != "1":
        raise HTTPException(
            status_code=400,
            detail="不支持的 A2A-Version %s，支持：%s" % (version, ", ".join(SUPPORTED_VERSIONS)),
        )


def _send(who: Dict[str, str], params: dict) -> Dict[str, object]:
    """SendMessage：A2A message → 我们 /v1/send 的语义（复用 api_v1.v1_send 本体）。"""
    import api_v1

    msg = params.get("message") or {}
    parts = msg.get("parts") or []
    text = ""
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            text += p["text"]
    text = text.strip() or params.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message.parts[].text 不能为空")
    to = (params.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="params.to 不能为空（Agent 名字 / 人 / 群名）")
    body = api_v1.SendIn(to=to, text=text, client_msg_id=params.get("client_msg_id"))
    r = api_v1.v1_send(body, who)      # 直接复用现有发送端点函数（同一段选路/幂等/计费逻辑）
    return {"taskId": str(r["seq"]), "status": "submitted", "seq": r["seq"]}


def _get_task(params: dict) -> Dict[str, object]:
    """GetTask：taskId=消息 seq，返回投递状态 + 收件人回话（有的话）。"""
    task_id = str(params.get("taskId") or "").strip()
    row = _q1("SELECT * FROM messages WHERE seq = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个 taskId：%s" % task_id)
    # 结果文本 = 同会话里 seq 更大、且不是发送者自己的最新一条（收件人回话）
    reply = _q1(
        "SELECT text FROM messages WHERE conversation_id = ? AND seq > ?"
        " AND NOT (from_kind = ? AND from_id = ?) ORDER BY seq DESC LIMIT 1",
        (row["conversation_id"], row["seq"], row["from_kind"], row["from_id"]),
    )
    return {
        "taskId": task_id,
        "status": "delivered",
        "to": row["conversation_id"],
        "sentText": row["text"],
        "resultText": reply["text"] if reply is not None else None,
    }


@router.post("/a2a")
def a2a(body: RpcIn, request: Request, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """JSON-RPC 风格单端点：SendMessage / GetTask。"""
    _check_version(request.headers.get("A2A-Version", "1.0"))
    method = body.method
    try:
        if method == "SendMessage":
            return _rpc_ok(_send(who, body.params), body.id)
        if method == "GetTask":
            return _rpc_ok(_get_task(body.params), body.id)
        raise HTTPException(status_code=404, detail="不支持的方法：%s（现有：SendMessage / GetTask）" % method)
    except HTTPException as e:
        # JSON-RPC 错误形状（版本错 / 参数错 / 404 都走这，保留 HTTP 状态码）
        raise HTTPException(status_code=e.status_code,
                            detail=_rpc_error(-32000, str(e.detail), body.id))
