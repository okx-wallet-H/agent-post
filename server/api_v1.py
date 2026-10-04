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

import re
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
    _ensure_mentions_schema(state.get("db"))


# ---------------------------------------------------------------- mentions（#29 群聊 @ 唤醒）
# messages.mentions 存逗号分隔的被点名者（@名字 或 @全体/@all = 全体）。
# 幂等：列存在才加；触发器兜底 main.py 的 /api/conversations/{id}/messages 直插路径
# （它不带 mentions 列，AFTER INSERT 触发器用 Python 函数从正文解析补上）。

_ALL_MENTION = ("all", "全体", "everyone")


def _mention_extract(text: Optional[str]) -> str:
    """从正文解析 @名字 / @全体 / @all → 逗号分隔串。"""
    if not text:
        return ""
    out: List[str] = []
    for m in re.findall(r"@([^\s@，。,.!?]+)", text):
        m = m.strip().strip("@").strip()
        if m and m not in out:
            out.append(m)
    return ",".join(out)


def mentions_from(text: str, explicit: Optional[List[str]] = None) -> str:
    """合并显式 mentions 与正文 @：显式传了就只用显式（含 @全体 → all）。"""
    if explicit:
        names: List[str] = []
        for x in explicit:
            x = str(x).strip().strip("@").strip()
            if x == "全体":
                x = "all"
            if x and x not in names:
                names.append(x)
        return ",".join(names)
    return _mention_extract(text)


def mentions_for(mentions: str, name: str, aid: str) -> bool:
    """这条消息有没有点名 name/id（all/全体 = 点名所有人）。"""
    if not mentions:
        return False
    for m in mentions.split(","):
        m = m.strip()
        if m in _ALL_MENTION or m == name or m == aid:
            return True
    return False


_MENTIONS_READY = False


def _ensure_mentions_schema(db) -> None:
    """幂等：messages 加 mentions 列；注册 Python 解析函数；建触发器兜底直插路径。
    注意 main.py 的 attach 在 db_init（建表）之前调用——所以除 attach 外，本模块的
    _q/_q1/_ex 第一次跑时还会懒执行一次，保证表建好之后再补列。"""
    global _MENTIONS_READY
    if db is None:
        return
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if exists is None:
            return  # messages 表还没建（main.db_init 未跑）——等懒执行再补
        cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
        if "mentions" not in cols:
            db.execute("ALTER TABLE messages ADD COLUMN mentions TEXT")
        try:
            db.create_function("mention_extract", 1, _mention_extract)
        except Exception:
            pass  # 同名函数已注册过
        db.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_messages_mentions
               AFTER INSERT ON messages
               WHEN NEW.mentions IS NULL
               BEGIN
                 UPDATE messages SET mentions = mention_extract(NEW.text) WHERE seq = NEW.seq;
               END"""
        )
        db.commit()
        _MENTIONS_READY = True
    except Exception:
        pass  # 加列/触发器失败不拦服务（mentions 功能降级为「无」）


def _q(sql: str, args=()) -> List[sqlite3.Row]:
    if not _MENTIONS_READY:
        _ensure_mentions_schema(_S.get("db"))
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    if not _MENTIONS_READY:
        _ensure_mentions_schema(_S.get("db"))
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()) -> sqlite3.Cursor:
    if not _MENTIONS_READY:
        _ensure_mentions_schema(_S.get("db"))
    return _S["ex"](sql, args)  # type: ignore[operator]


# ---------------------------------------------------------------- 多租户 / 计费接线
# 由 main.py 通过 attach(tenancy={...}) 注入（accounts/billing 两个交付模块的能力）。
# 没注入时下面这套缺省值就是旧行为：不隔离、不卡口（保证本模块单独跑也不炸）。

_NO_TENANCY: Dict[str, object] = {
    "owner_filter": lambda who, col="owner_account": ("", []),
    "can_see_owner": lambda owner, who: True,
    "account_of": lambda who: None,
    "billable_account": lambda who: None,
    "quota_guard": lambda account_id, kind: None,
    "record_usage": lambda account_id, kind, n=1: None,
    "resolve_token": None,
}


def _t() -> Dict[str, object]:
    ten = _S.get("tenancy")
    if isinstance(ten, dict):
        merged = dict(_NO_TENANCY)
        merged.update(ten)
        return merged
    return _NO_TENANCY


def _owner_of_row(r) -> Optional[str]:
    """取一行的 owner_account；没有这列（accounts 没挂）时当 None。"""
    try:
        return r["owner_account"]
    except (IndexError, KeyError, TypeError):
        return None


def _agent_visible(row, who: Optional[Dict[str, str]]) -> bool:
    return bool(_t()["can_see_owner"](_owner_of_row(row), who))  # type: ignore[operator]


def _has_col(tbl: str, col: str) -> bool:
    return col in [r[1] for r in _q("PRAGMA table_info(%s)" % tbl)]


def _who(token: str) -> Optional[Dict[str, str]]:
    """token → {"kind":"human"|"agent","id":...,"name":...}
    认三种：人总 token（HUB_USER_TOKEN，全权）/ 账号 token（id=账号）/ agent token。"""
    if not token:
        return None
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    resolve = _t()["resolve_token"]            # accounts.resolve：账号 token → ("human", account_id)
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


def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    tok = ""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    who = _who(tok)
    if who is None:
        raise HTTPException(status_code=401, detail="token 不对：请在请求头带上 Authorization: Bearer <你的 token>")
    if who["kind"] == "agent":                 # 心跳：控制台上的"在线"靠它
        try:
            import console
            console.touch(who["id"])
        except Exception:
            pass
    return who


class SendIn(BaseModel):
    to: str
    text: str
    client_msg_id: Optional[str] = None
    mentions: Optional[List[str]] = None   # 显式点名（不传就从正文 @ 解析）


class AgentIn(BaseModel):
    name: str


def _find_agent(needle: str, who: Optional[Dict[str, str]] = None) -> sqlite3.Row:
    """按 id 或名字找 Agent（名字唯一才认；重名就提示用 id）。
    多租户：只在调用方看得见的范围里找（账号 token 看不到别人的 Agent → 404）。"""
    needle = needle.strip()
    row = _q1("SELECT * FROM agents WHERE id = ?", (needle,))
    if row is not None and _agent_visible(row, who):
        return row
    rows = [r for r in _q("SELECT * FROM agents WHERE name = ?", (needle,)) if _agent_visible(r, who)]
    if not rows:
        raise HTTPException(status_code=404, detail="没有这个 Agent：%s（可以先用 GET /v1/agents 看名字）" % needle)
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="名字 %s 有多个，请改用 id" % needle)
    return rows[0]


def _find_conversation(needle: str, who: Optional[Dict[str, str]] = None) -> sqlite3.Row:
    """按 cid 或标题找会话（标题唯一才认；重名就提示用 cid）。群发目标。
    多租户：只在调用方看得见的范围里找（别人的会话 → 404）。"""
    needle = needle.strip()
    can_see = _t()["can_see_owner"]  # type: ignore[operator]
    row = _q1("SELECT * FROM conversations WHERE id = ?", (needle,))
    if row is not None and can_see(_owner_of_row(row), who):
        return row
    rows = [r for r in _q("SELECT * FROM conversations WHERE title = ?", (needle,))
            if can_see(_owner_of_row(r), who)]
    if not rows:
        raise HTTPException(status_code=404, detail="没有这个 Agent 或会话：%s" % needle)
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail="会话标题 %s 有多个，请改用会话 id" % needle)
    return rows[0]


def _conv_member_count(cid: str) -> int:
    row = _q1("SELECT COUNT(*) AS n FROM members WHERE conversation_id = ?", (cid,))
    return int(row["n"] or 0) if row else 0


def _dm_between(a_id: str, b_id: str, who: Optional[Dict[str, str]] = None) -> sqlite3.Row:
    """找两人之间的单聊；没有就建一条。'human' 表示人（本身不是 agent）。
    多租户：只认调用方可见的旧会话；新建的会话记 owner_account = 调用方账号（管理员/旧数据为 NULL）。"""
    can_see = _t()["can_see_owner"]  # type: ignore[operator]

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
            if conv is None or conv["kind"] != "dm" or not can_see(_owner_of_row(conv), who):
                continue
            have = {m["agent_id"] for m in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))}
            if have == want_ids():
                return conv
    else:
        for r in _q("SELECT conversation_id FROM members WHERE agent_id = ?", (a_id,)):
            cid = r["conversation_id"]
            conv = _q1("SELECT * FROM conversations WHERE id = ?", (cid,))
            if conv is None or conv["kind"] != "dm" or not can_see(_owner_of_row(conv), who):
                continue
            have = {m["agent_id"] for m in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))}
            if have == want_ids():
                return conv

    cid = _S["new_id"]("cv")  # type: ignore[operator]
    title = ("人 ↔ " + name_of(b_id)) if a_id == "human" else (name_of(a_id) + " ↔ " + name_of(b_id))
    owner = _t()["account_of"](who) if who else None  # type: ignore[operator]
    if _has_col("conversations", "owner_account"):
        _ex("INSERT INTO conversations (id, title, kind, created_at, owner_account) VALUES (?,?,?,?,?)",
            (cid, title.replace("人 ↔ 人", "人"), "dm", _S["now_iso"](), owner))  # type: ignore[operator]
    else:
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
    frag, fargs = _t()["owner_filter"](who, "owner_account")  # type: ignore[operator]
    rows = _q("SELECT id, name, created_at FROM agents WHERE 1=1%s ORDER BY created_at" % frag,
              tuple(fargs))
    return {"agents": [{"id": r["id"], "name": r["name"]} for r in rows]}


@router.post("/v1/agents")
def v1_create_agent(body: AgentIn, who: Dict[str, str] = Depends(me)) -> Dict[str, str]:  # type: ignore[assignment]
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="只有人能建 Agent")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名字不能空")
    _t()["quota_guard"](_t()["billable_account"](who), "agent")  # type: ignore[operator]  额度不够 → 402
    aid = _S["new_id"]("ag")  # type: ignore[operator]
    tok = _S["new_token"]()  # type: ignore[operator]
    owner = _t()["account_of"](who)  # type: ignore[operator]
    if _has_col("agents", "owner_account"):
        _ex("INSERT INTO agents (id, name, token, created_at, owner_account) VALUES (?,?,?,?,?)",
            (aid, name, tok, _S["now_iso"](), owner))  # type: ignore[operator]
    else:
        _ex("INSERT INTO agents (id, name, token, created_at) VALUES (?,?,?,?)",
            (aid, name, tok, _S["now_iso"]()))  # type: ignore[operator]
    _t()["record_usage"](_t()["billable_account"](who), "agent", 1)  # type: ignore[operator]
    return {"id": aid, "name": name, "token": tok, "note": "token 只显示这一次，存好"}


@router.post("/v1/send")
def v1_send(body: SendIn, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容不能空")
    to = (body.to or "").strip()
    conv: Optional[sqlite3.Row] = None
    group = False
    target_id, target_name = "", ""
    if to in ("人", "human", "老板", "我"):
        target_id, target_name = "human", "人"
    else:
        try:
            row = _find_agent(to, who)      # 多租户：只能发给看得见的 Agent（别人的 → 404）
        except HTTPException as e:
            if e.status_code in (404, 409):
                # 不是 Agent（或重名）：再试会话标题 / cid → 群发；都不是才把原错误抛回去
                try:
                    conv = _find_conversation(to, who)
                except HTTPException:
                    raise e
                group = True
            else:
                raise
        else:
            target_id, target_name = row["id"], row["name"]
    if group:
        target_id, target_name = conv["id"], conv["title"]  # type: ignore[index]
    else:
        me_id = "human" if who["kind"] == "human" else who["id"]
        if target_id == me_id:
            raise HTTPException(status_code=400, detail="别给自己发")
    acct = _t()["billable_account"](who)  # type: ignore[operator]  管理员/旧数据 → None（不受限）
    _t()["quota_guard"](acct, "msg")  # type: ignore[operator]       账号维度卡口：超额 → 402
    if not group:
        conv = _dm_between(me_id, target_id, who)
    cmid = (body.client_msg_id or "").strip() or None
    if cmid:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is not None:
            return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"], "conversation_id": conv["id"]}
    mid = _S["new_id"]("msg")  # type: ignore[operator]
    mentions = mentions_from(text, body.mentions)
    try:
        cur = _ex("INSERT INTO messages (id, conversation_id, from_kind, from_id, text, client_msg_id, created_at, mentions)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (mid, conv["id"], who["kind"], who["id"], text, cmid, _S["now_iso"](), mentions))
        seq = cur.lastrowid
    except sqlite3.IntegrityError:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is None:
            raise HTTPException(status_code=409, detail="写入冲突，请重试")
        return {"ok": True, "duplicate": True, "id": old["id"], "seq": old["seq"], "conversation_id": conv["id"]}
    _t()["record_usage"](acct, "msg", _conv_member_count(conv["id"]) if group else 1)  # type: ignore[operator]  按投递计量（群发 = 成员数）
    out: Dict[str, object] = {"ok": True, "duplicate": False, "id": mid, "seq": seq,
                              "conversation_id": conv["id"],
                              "to": {"id": target_id, "name": target_name}}
    if group:
        out["to"] = {"kind": "group", "id": target_id, "name": target_name}
    return out


@router.get("/v1/inbox")
def v1_inbox(since: int = 0, limit: int = 200, wait: int = 0,
             who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """`wait`>0 时长轮询：有新消息立刻返回，最多等 wait 秒（用来「唤醒」Agent，不用死循环轮询）"""
    if wait and wait > 0:
        deadline = time.time() + min(wait, 55)
        while True:
            got = _inbox_rows(since, limit, who)
            if got or time.time() >= deadline:
                return _inbox_out(got, since, who)
            time.sleep(0.5)
    return _inbox_out(_inbox_rows(since, limit, who), since, who)


def _inbox_rows(since: int, limit: int, who: Dict[str, str]) -> List[sqlite3.Row]:
    frag, fargs = _t()["owner_filter"](who, "c.owner_account")  # type: ignore[operator]  账号隔离
    if who["kind"] == "human":
        return _q("SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id"
                  " WHERE m.seq > ?%s ORDER BY m.seq LIMIT ?" % frag,
                  (since,) + tuple(fargs) + (min(limit, 500),))
    # 注意：agent 的收件箱**不含它自己发的**——否则守候进程会把自己的回话当成新消息，形成回环
    return _q("SELECT m.* FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id"
              " JOIN conversations c ON c.id = m.conversation_id"
              " WHERE mb.agent_id = ? AND m.seq > ? AND NOT (m.from_kind='agent' AND m.from_id = ?)%s"
              " ORDER BY m.seq LIMIT ?" % frag,
              (who["id"], since, who["id"]) + tuple(fargs) + (min(limit, 500),))


def _inbox_out(rows: List[sqlite3.Row], since: int, who: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """每条消息带 wake：True = 对当前收件人是「待处理」（单聊全 True；群里被 @/全体 True），
    False = 「已看未唤醒」（群消息没被点名）——守候和界面都用这个字段判定。"""
    out = []
    for r in rows:
        from_name = "人" if r["from_kind"] == "human" else (_q1("SELECT name FROM agents WHERE id = ?", (r["from_id"],)) or {"name": r["from_id"]})["name"]
        conv = _q1("SELECT title, kind FROM conversations WHERE id = ?", (r["conversation_id"],))
        mentions = r["mentions"] if "mentions" in r.keys() else ""
        wake = True
        if who is not None and who["kind"] == "agent" and conv and conv["kind"] == "group":
            wake = mentions_for(mentions, who["name"], who["id"])
        out.append({"seq": r["seq"], "from": from_name, "from_kind": r["from_kind"],
                    "text": r["text"], "ts": r["created_at"],
                    "conversation": conv["title"] if conv else r["conversation_id"],
                    "conversation_id": r["conversation_id"],
                    "conversation_kind": conv["kind"] if conv else "",
                    "mentions": [m for m in (mentions or "").split(",") if m],
                    "wake": wake})
    # 注意：不能把 since 原样回显成 latest（客户端把它存成游标后会永远收不到消息且不报错）
    # 真实最大 seq 由库决定（不受 since 影响）
    head = _q1("SELECT MAX(seq) AS m FROM messages")
    global_max = int(head["m"] or 0) if head else 0
    latest = max([r["seq"] for r in rows], default=0)
    latest = max(latest, global_max if since > global_max else 0, since if since <= global_max else 0)
    return {"messages": out, "latest": latest, "count": len(out)}
