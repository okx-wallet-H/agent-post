"""api_v1 —— 「三行接入」的简化 API（2026-09-29）

产品要求：**对接方式必须简单，不要搞很复杂**。
主 API（/api/...）是给控制台用的，概念多（会话 id、成员、from_agent_id…）；
这一层是给「想让自己的 Agent 接进来」的人用的，**只记两件事**：

    TOKEN=...
    curl -X POST "$HUB/v1/send"  -H "Authorization: Bearer $TOKEN" -d '{"to":"数据岗","text":"在吗"}'
    curl "$HUB/v1/inbox?since=0" -H "Authorization: Bearer $TOKEN"

要点：
- `to` 可以直接写**名字**（不用先查 id）→ 自动找/建一条单聊会话
- 收件用 `since` 游标（离线补投）
- 幂等：同一条 `client_msg_id` 不会重复投递

本模块由 main.py 调 `attach(...)` 注入连接与工具函数（避免循环 import）。
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}


def attach(**state) -> None:
    """main.py 在启动时调用：db / lock / user_token / q / q1 / ex / new_id / now_iso / is_member"""
    _S.update(state)


def _q(sql: str, args=()) -> List[sqlite3.Row]:
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()) -> sqlite3.Cursor:
    return _S["ex"](sql, args)  # type: ignore[operator]


def _who(token: str) -> Optional[Dict[str, str]]:
    """token → {"kind":"human"|"agent","id":...,"name":...}"""
    if not token:
        return None
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is None:
        return None
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    tok = ""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    who = _who(tok)
    if who is None:
        raise HTTPException(status_code=401, detail="token 不对：请在请求头带上 Authorization: Bearer <你的 token>")
    return who


class SendIn(BaseModel):
    to: str
    text: str
    client_msg_id: Optional[str] = None


class AgentIn(BaseModel):
    name: str


def _find_agent(needle: str) -> sqlite3.Row:
    """按 id 或名字找 Agent（名字唯一才认；重名就提示用 id）"""
    needle = needle.strip()
    row = _q1("SELECT * FROM agents WHERE id = ?", (needle,))
    if row is not None:
        return row
    rows = _q("SELECT * FROM agents WHERE name = ?", (needle,))
    if not rows:
        raise HTTPException(status_code=404, detail="没有这个 Agent：%s（可以先用 GET /v1/agents 看名字）" % needle)
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="名字 %s 有多个，请改用 id" % needle)
    return rows[0]


def _dm_between(a_id: str, b_id: str) -> sqlite3.Row:
    """找两人之间的单聊；没有就建一条。'human' 表示人（本身不是 agent）"""
    def name_of(i: str) -> str:
        if i == "human":
            return "人"
        r = _q1("SELECT name FROM agents WHERE id = ?", (i,))
        return r["name"] if r else i

    def want_ids() -> set:
        ids = {a_id, b_id}
        ids.discard("human")                     # 人不是 members 里的一行
        return ids

    if b_id != "human":
        for r in _q("SELECT conversation_id FROM members WHERE agent_id = ?", (b_id,)):
            cid = r["conversation_id"]
            conv = _q1("SELECT * FROM conversations WHERE id = ?", (cid,))
            if conv is None or conv["kind"] != "dm":
                continue
            have = {m["agent_id"] for m in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))}
            if have == want_ids():
                return conv
    else:
        for r in _q("SELECT conversation_id FROM members WHERE agent_id = ?", (a_id,)):
            cid = r["conversation_id"]
            conv = _q1("SELECT * FROM conversations WHERE id = ?", (cid,))
            if conv is None or conv["kind"] != "dm":
                continue
            have = {m["agent_id"] for m in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))}
            if have == want_ids():
                return conv

    cid = _S["new_id"]("cv")  # type: ignore[operator]
    title = ("人 ↔ " + name_of(b_id)) if a_id == "human" else (name_of(a_id) + " ↔ " + name_of(b_id))
    _ex("INSERT INTO conversations (id, title, kind, created_at) VALUES (?,?,?,?)",
        (cid, title.replace("人 ↔ 人", "人"), "dm", _S["now_iso"]()))  # type: ignore[operator]
    for aid in want_ids():
        _ex("INSERT OR IGNORE INTO members (conversation_id, agent_id) VALUES (?,?)", (cid, aid))
    return _q1("SELECT * FROM conversations WHERE id = ?", (cid,))  # type: ignore[return-value]


@router.get("/v1/me")
def v1_me(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    return {"kind": who["kind"], "id": who["id"], "name": who["name"],
            "hint": "POST /v1/send 发 · GET /v1/inbox 收"}


@router.get("/v1/agents")
def v1_agents(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    rows = _q("SELECT id, name, created_at FROM agents ORDER BY created_at")
    return {"agents": [{"id": r["id"], "name": r["name"]} for r in rows]}


@router.post("/v1/agents")
def v1_create_agent(body: AgentIn, who: Dict[str, str] = Depends(me)) -> Dict[str, str]:  # type: ignore[assignment]
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="只有人能建 Agent")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名字不能空")
    aid = _S["new_id"]("ag")  # type: ignore[operator]
    tok = _S["new_token"]()  # type: ignore[operator]
    _ex("INSERT INTO agents (id, name, token, created_at) VALUES (?,?,?,?)",
        (aid, name, tok, _S["now_iso"]()))  # type: ignore[operator]
    return {"id": aid, "name": name, "token": tok, "note": "token 只显示这一次，存好"}


@router.post("/v1/send")
def v1_send(body: SendIn, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容不能空")
    to = (body.to or "").strip()
    if to in ("人", "human", "主人", "我"):
        target_id, target_name = "human", "人"
    else:
        row = _find_agent(to)
        target_id, target_name = row["id"], row["name"]
    if target_id == who["id"]:
        raise HTTPException(status_code=400, detail="别给自己发")
    conv = _dm_between(who["id"], target_id)
    cmid = (body.client_msg_id or "").strip() or None
    if cmid:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is not None:
            return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"], "conversation_id": conv["id"]}
    mid = _S["new_id"]("msg")  # type: ignore[operator]
    try:
        cur = _ex("INSERT INTO messages (id, conversation_id, from_kind, from_id, text, client_msg_id, created_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (mid, conv["id"], who["kind"], who["id"], text, cmid, _S["now_iso"]()))
        seq = cur.lastrowid
    except sqlite3.IntegrityError:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is None:
            raise HTTPException(status_code=409, detail="写入冲突，请重试")
        return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"], "conversation_id": conv["id"]}
    return {"ok": True, "duplicate": False, "id": mid, "seq": seq, "conversation_id": conv["id"],
            "to": {"id": target_id, "name": target_name}}


@router.get("/v1/inbox")
def v1_inbox(since: int = 0, limit: int = 200, wait: int = 0,
             who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """`wait`>0 时长轮询：有新消息立刻返回，最多等 wait 秒（用来「唤醒」Agent，不用死循环轮询）"""
    if wait and wait > 0:
        deadline = time.time() + min(wait, 55)
        while True:
            got = _inbox_rows(since, limit, who)
            if got or time.time() >= deadline:
                return _inbox_out(got, since)
            time.sleep(0.5)
    return _inbox_out(_inbox_rows(since, limit, who), since)


def _inbox_rows(since: int, limit: int, who: Dict[str, str]) -> List[sqlite3.Row]:
    if who["kind"] == "human":
        return _q("SELECT * FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", (since, min(limit, 500)))
    return _q("SELECT m.* FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id"
              " WHERE mb.agent_id = ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
              (who["id"], since, min(limit, 500)))


def _inbox_out(rows: List[sqlite3.Row], since: int) -> Dict[str, object]:
    out = []
    for r in rows:
        from_name = "人" if r["from_kind"] == "human" else (_q1("SELECT name FROM agents WHERE id = ?", (r["from_id"],)) or {"name": r["from_id"]})["name"]
        conv = _q1("SELECT title FROM conversations WHERE id = ?", (r["conversation_id"],))
        out.append({"seq": r["seq"], "from": from_name, "from_kind": r["from_kind"],
                    "text": r["text"], "ts": r["created_at"],
                    "conversation": conv["title"] if conv else r["conversation_id"]})
    latest = max([r["seq"] for r in rows], default=since)
    return {"messages": out, "latest": latest, "count": len(out)}
