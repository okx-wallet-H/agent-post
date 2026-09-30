#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
温暖通信台 —— 人 + 多个 Agent 的消息台（单聊 / 群聊）
后端：FastAPI + SQLite（标准库 sqlite3，无 ORM）
前端：GET / 返回内联单页 HTML（无构建、无外部依赖）

环境变量：
  HUB_USER_TOKEN  人用的总 token，默认 h-dev-token
  HUB_PORT        监听端口，默认 8795（本地自测用 8796）
  HUB_DB          SQLite 文件路径，默认与本文件同目录的 hub.db

启动： python3 main.py   （等价于 uvicorn main:app --port $HUB_PORT）
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 配置

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_TOKEN = os.environ.get("HUB_USER_TOKEN", "h-dev-token")
DB_PATH = os.environ.get("HUB_DB", os.path.join(BASE_DIR, "hub.db"))
PORT = int(os.environ.get("HUB_PORT", "8795"))
SERVICE = "warm-hub"
VERSION = "0.1"

# ---------------------------------------------------------------- 数据库

_db_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    token      TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    conversation_id TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    PRIMARY KEY (conversation_id, agent_id)
);
CREATE TABLE IF NOT EXISTS messages (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT UNIQUE NOT NULL,
    conversation_id TEXT NOT NULL,
    from_kind      TEXT NOT NULL,
    from_id        TEXT NOT NULL,
    text           TEXT NOT NULL,
    client_msg_id  TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idem
    ON messages(conversation_id, client_msg_id) WHERE client_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_members_agent ON members(agent_id);
"""


def db_init() -> None:
    with _db_lock:
        _db.executescript(SCHEMA)
        _db.commit()


def q(sql: str, args=()) -> List[sqlite3.Row]:
    with _db_lock:
        return _db.execute(sql, args).fetchall()


def q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    with _db_lock:
        return _db.execute(sql, args).fetchone()


def ex(sql: str, args=()) -> sqlite3.Cursor:
    with _db_lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(6))


# ---------------------------------------------------------------- 数据模型（请求体）


class AgentIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class ConversationIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    members: List[str] = Field(default_factory=list)
    kind: Optional[str] = None  # dm | group，缺省按成员数推断


class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    from_agent_id: Optional[str] = None
    client_msg_id: Optional[str] = Field(default=None, max_length=120)


# ---------------------------------------------------------------- 鉴权


def authenticate(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    """请求头 Authorization: Bearer <token> → {"kind": "human"|"agent", "id": ...}"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")
    if secrets.compare_digest(token, USER_TOKEN):
        return {"kind": "human", "id": "human", "name": "人"}
    row = q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is None:
        raise HTTPException(status_code=401, detail="token 无效")
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


def require_human(who: Dict[str, str] = Depends(authenticate)) -> Dict[str, str]:
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="此操作只允许人（人 token）执行")
    return who


def conv_or_404(cid: str) -> sqlite3.Row:
    row = q1("SELECT * FROM conversations WHERE id = ?", (cid,))
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


def conv_member_ids(cid: str) -> List[str]:
    return [r["agent_id"] for r in q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))]


def is_member(cid: str, aid: str) -> bool:
    return q1("SELECT 1 FROM members WHERE conversation_id = ? AND agent_id = ?", (cid, aid)) is not None


def require_conv_access(cid: str, who: Dict[str, str]) -> sqlite3.Row:
    row = conv_or_404(cid)
    if who["kind"] == "agent" and not is_member(cid, who["id"]):
        raise HTTPException(status_code=403, detail="你不在这个会话里")
    return row


app = FastAPI(title="温暖通信台", version=VERSION, docs_url="/docs", redoc_url=None)

# 官网静态页（/site/）—— 同一台机、同一个域名，省一套部署
try:
    from fastapi.staticfiles import StaticFiles
    import os as _os
    _site = _os.environ.get("HUB_SITE_DIR", _os.path.join(BASE_DIR, "site"))
    if _os.path.isdir(_site):
        app.mount("/site", StaticFiles(directory=_site, html=True), name="site")
except Exception as _e:
    print("官网挂载失败：", _e)

# 计费出账与账单页（/api/invoice + /invoice）
try:
    import invoice
    invoice.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                   new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(invoice.router)
except Exception as _e:
    print("invoice 挂载失败：", _e)

# 指标告警（/v1/alerts）：把"不丢"的异常主动喊出来
try:
    import alerts
    alerts.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                  new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(alerts.router)
except Exception as _e:
    print("alerts 挂载失败：", _e)

# 群聊界面（/chat）：人 + 多 Agent 同处一个会话
try:
    import chat
    chat.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(chat.router)
except Exception as _e:
    print("chat 挂载失败：", _e)

# 控制台（/console + /v1/board）：给人类看"人 + 一群 Agent 在怎么协作"
try:
    import console
    console.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                   new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(console.router)
except Exception as _e:
    print("console 挂载失败：", _e)

# MCP 端点（/mcp）：给别人的 Agent 直接接进来用；按次计费骨架
try:
    import mcp_server
    app.include_router(mcp_server.router)
except Exception as _e:
    print("mcp_server 挂载失败：", _e)


# 「三行接入」的简化 API（/v1/...）——给想接进来的 Agent 用，见 api_v1.py
try:
    import api_v1
    api_v1.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                  new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(api_v1.router)
except Exception as _e:          # 简化层坏了也不能拖垮主服务
    print("api_v1 挂载失败：", _e)

# ---------------------------------------------------------------- 多租户账号层（交付模块 accounts.py）
# 挂载顺序：accounts → billing → metrics。都用上面的 try/except 惯例，坏了不拖垮主服务。

# accounts.py 的端点内部用 `import main as _m` 取连接；main.py 被当脚本直接跑时
# （python3 main.py —— smoke_v1 / smoke_wait / smoke_console 都这么起）模块名是 __main__，
# `import main` 会另起一份 main（另一个 sqlite 连接）。先把它指回自己，保证同一个连接。
try:
    import sys as _sys

    _sys.modules.setdefault("main", _sys.modules[__name__])
except Exception as _e:
    print("main 模块别名注册失败：", _e)

TENANT_OK = False
TENANT_AUTH = None

try:
    import accounts

    db_init()                        # 基础表先建好（文件底部的 db_init 跑在这之后）；幂等
    accounts.ensure_schema(_db)      # 幂等建表 + 给 agents / conversations 加 owner_account
    app.include_router(accounts.router)
    # 鉴权覆写：人总 token（HUB_USER_TOKEN）全权不变；账号 token → kind=human、id=账号
    TENANT_AUTH = accounts.make_authenticate(_db, USER_TOKEN)
    app.dependency_overrides[authenticate] = TENANT_AUTH
    # 会话访问判定：人总 token 全可见；账号 / agent 只见自己账号名下的会话（越权 → 403）
    require_conv_access = accounts.make_conv_access(_db)
    TENANT_OK = True
except Exception as _e:
    print("accounts 挂载失败：", _e)

try:
    import billing

    billing.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q1=q1)
    app.include_router(billing.router)
except Exception as _e:
    print("billing 挂载失败：", _e)

try:
    import metrics

    metrics.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                   new_id=new_id, now_iso=now_iso)
    app.include_router(metrics.router)
except Exception as _e:
    print("metrics 挂载失败：", _e)


# ---------------------------------------------------------------- 多租户隔离 + 计费卡口（接线）
# 口径（写死在这，改口径改这里）：
#   · 人总 token（HUB_USER_TOKEN，id = "human"）= 管理员：全权、不受限、不计费 —— 线上控制台靠它，别锁死
#   · 账号 token（kind=human，id=acct_xxx）：只能看 / 操作本账号名下的 Agent、会话、消息
#   · agent token：跟着它所属账号走（agents.owner_account）；越权 → 403
#   · 旧数据（owner_account 为 NULL 或 accounts.py 的固定默认账号）：人总 token 年代的数据，
#     不算任何租户、彼此可见、不计费 —— 这样线上老数据与老脚本（smoke_v1/wait/console）照旧跑

ADMIN_ID = "human"
DEFAULT_ACCOUNT_ID = "acct_default"        # accounts.py 里的固定默认账号（老数据都归它）
LEGACY_OWNERS = (None, DEFAULT_ACCOUNT_ID)


def is_admin(who: Optional[Dict[str, str]]) -> bool:
    """人总 token —— 全权、不受限"""
    return bool(who) and who["kind"] == "human" and who["id"] == ADMIN_ID


def account_of(who: Optional[Dict[str, str]]) -> Optional[str]:
    """调用方所属账号 id；管理员 / 旧数据 / accounts 没挂上 → None"""
    if not who or not TENANT_OK:
        return None
    if who["kind"] == "human":
        return None if who["id"] == ADMIN_ID else who["id"]
    row = q1("SELECT owner_account FROM agents WHERE id = ?", (who["id"],))
    return row["owner_account"] if row is not None else None


def billable_account(who: Optional[Dict[str, str]]) -> Optional[str]:
    """要计费的账号 id；管理员与旧数据 → None（不受限、不计量）"""
    acct = account_of(who)
    return None if acct in LEGACY_OWNERS else acct


def owner_filter(who, col: str = "owner_account"):
    """列表查询用的账号隔离条件 → (SQL 片段, 参数)"""
    if is_admin(who) or not TENANT_OK:
        return "", []
    acct = account_of(who)
    if acct is None:                       # 旧数据（人总 token 建的 Agent）：只看旧数据那一堆
        return " AND (%s IS NULL OR %s = ?)" % (col, col), [DEFAULT_ACCOUNT_ID]
    return " AND %s = ?" % col, [acct]


def can_see_owner(owner: Optional[str], who) -> bool:
    """owner_account = owner 的这一行，对该调用方可见吗"""
    if not who or is_admin(who) or not TENANT_OK:
        return True
    acct = account_of(who)
    if acct is None:
        return owner in LEGACY_OWNERS
    return owner == acct


def owner_of_row(r) -> Optional[str]:
    """取一行的 owner_account；没这列（accounts 没挂）时当 None"""
    try:
        return r["owner_account"]
    except (IndexError, KeyError, TypeError):
        return None


def has_col(tbl: str, col: str) -> bool:
    return col in [x[1] for x in q("PRAGMA table_info(%s)" % tbl)]


class QuotaError(Exception):
    """额度用尽 → HTTP 402，body 顶层就是 {"detail":"额度用尽", ...}"""

    def __init__(self, info=None) -> None:
        self.info = dict(info or {})
        super().__init__("额度用尽")


@app.exception_handler(QuotaError)
def quota_error_handler(request: Request, exc: QuotaError) -> JSONResponse:
    body = {"detail": "额度用尽"}
    body.update({k: v for k, v in exc.info.items() if k != "detail"})
    return JSONResponse(status_code=402, content=body)


def quota_guard(account_id: Optional[str], kind: str) -> None:
    """账号维度额度检查（billing.check_quota）：管理员 / 旧数据（account_id=None）不受限；
    不够就抛 QuotaError → 402。billing 没挂或自身出错时放行（先别把消息堵死）。"""
    if not account_id:
        return
    try:
        import billing as _b

        ok, info = _b.check_quota(_db, account_id, kind)
    except Exception as _e:
        print("check_quota 出错（放行）：", _e)
        return
    if not ok:
        raise QuotaError(info)


def record_usage(account_id: Optional[str], kind: str, n: int = 1) -> None:
    """按投递计费：投给 N 个成员就记 N 条（管理员 / 旧数据不记）"""
    if not account_id:
        return
    try:
        import billing as _b

        with _db_lock:
            _b.record_usage(_db, account_id, kind, n)
    except Exception as _e:
        print("record_usage 出错（忽略）：", _e)


def resolve_token(token: str):
    """账号 token → ("human", account_id)（accounts.resolve 要连接，这里包一层）；
    accounts 没挂上就认不出来（返回 None）"""
    if not TENANT_OK:
        return None
    return accounts.resolve(_db, token)


def tenancy_context() -> Dict[str, object]:
    """交给 api_v1 的那一套（api_v1.attach(tenancy=...)）"""
    return {
        "owner_filter": owner_filter,
        "can_see_owner": can_see_owner,
        "account_of": account_of,
        "billable_account": billable_account,
        "quota_guard": quota_guard,
        "record_usage": record_usage,
        "resolve_token": resolve_token,
    }


# api_v1 在上面就挂好了（那段不动），这里把账号/计费能力补进去：attach 是 dict update，可多次调
try:
    import api_v1 as _api_v1

    _api_v1.attach(tenancy=tenancy_context())
except Exception as _e:
    print("api_v1 多租户接线失败：", _e)

# billing 自带的鉴权只认"人总 token / agent token"（账号 token 会 401）；覆写成：
# 账号 token → 用账号 id 查自己的用量 / 兑自己的券；发券收紧到只有管理员能发
if TENANT_OK:
    try:
        import billing as _billing

        def _billing_who(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
            who = TENANT_AUTH(authorization)  # type: ignore[operator]
            acct = account_of(who)
            return {"kind": who["kind"], "id": (acct or ADMIN_ID), "name": who["name"]}

        def _billing_admin(who: Dict[str, str] = Depends(_billing_who)) -> Dict[str, str]:
            if who["id"] != ADMIN_ID:
                raise HTTPException(status_code=403, detail="发券只允许管理员（人总 token）")
            return who

        app.dependency_overrides[_billing._authenticate] = _billing_who
        app.dependency_overrides[_billing._require_human] = _billing_admin
    except Exception as _e:
        print("billing 鉴权接线失败：", _e)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 基础端点


@app.get("/health")
def health() -> Dict[str, object]:
    return {"ok": True, "service": SERVICE, "version": VERSION}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


# ---------------------------------------------------------------- Agents


@app.post("/api/agents")
def create_agent(body: AgentIn, who: Dict[str, str] = Depends(require_human)) -> Dict[str, str]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名字不能为空")
    acct = billable_account(who)
    quota_guard(acct, "agent")            # 账号维度卡口：超额 → 402（管理员不受限）
    aid = new_id("ag")
    token = secrets.token_urlsafe(24)
    if has_col("agents", "owner_account"):
        ex(
            "INSERT INTO agents (id, name, token, created_at, owner_account) VALUES (?,?,?,?,?)",
            (aid, name, token, now_iso(), account_of(who)),
        )
    else:
        ex(
            "INSERT INTO agents (id, name, token, created_at) VALUES (?,?,?,?)",
            (aid, name, token, now_iso()),
        )
    record_usage(acct, "agent", 1)
    return {"id": aid, "name": name, "token": token}


@app.get("/api/agents")
def list_agents(who: Dict[str, str] = Depends(authenticate)) -> Dict[str, object]:
    frag, fargs = owner_filter(who)       # 账号 token 只看自己名下的 Agent
    rows = q("SELECT id, name, created_at FROM agents WHERE 1=1%s ORDER BY created_at, id" % frag,
             tuple(fargs))
    return {"agents": [dict(r) for r in rows]}


# ---------------------------------------------------------------- Conversations


@app.post("/api/conversations")
def create_conversation(
    body: ConversationIn, who: Dict[str, str] = Depends(require_human)
) -> Dict[str, object]:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")

    members: List[str] = []
    for aid in body.members:
        aid = aid.strip()
        if aid and aid not in members:
            members.append(aid)
    for aid in members:
        row = q1("SELECT id, owner_account FROM agents WHERE id = ?", (aid,)) \
            if has_col("agents", "owner_account") else q1("SELECT id FROM agents WHERE id = ?", (aid,))
        if row is None:
            raise HTTPException(status_code=400, detail="成员不存在：%s" % aid)
        if not can_see_owner(owner_of_row(row), who):     # 多租户：不能把别人的 Agent 拉进自己的会话
            raise HTTPException(status_code=403, detail="成员不在你的账号里：%s" % aid)

    kind = (body.kind or "").strip().lower()
    if kind not in ("dm", "group"):
        kind = "dm" if len(members) == 1 else "group"

    cid = new_id("cv")
    created = now_iso()
    if has_col("conversations", "owner_account"):          # 会话归属调用方账号（管理员为 NULL）
        ex("INSERT INTO conversations (id, title, kind, created_at, owner_account) VALUES (?,?,?,?,?)",
           (cid, title, kind, created, account_of(who)))
    else:
        ex("INSERT INTO conversations (id, title, kind, created_at) VALUES (?,?,?,?)", (cid, title, kind, created))
    for aid in members:
        ex("INSERT OR IGNORE INTO members (conversation_id, agent_id) VALUES (?,?)", (cid, aid))
    return {"id": cid, "title": title, "kind": kind, "members": members}


@app.get("/api/conversations")
def list_conversations(who: Dict[str, str] = Depends(authenticate)) -> Dict[str, object]:
    frag, fargs = owner_filter(who, "c.owner_account")     # 账号隔离：只看自己账号名下的会话
    if who["kind"] == "human":
        rows = q("SELECT c.id, c.title, c.kind, c.created_at FROM conversations c WHERE 1=1%s "
                 "ORDER BY c.created_at, c.id" % frag, tuple(fargs))
    else:
        rows = q(
            "SELECT c.id, c.title, c.kind, c.created_at FROM conversations c "
            "JOIN members m ON m.conversation_id = c.id WHERE m.agent_id = ?%s "
            "ORDER BY c.created_at, c.id" % frag,
            (who["id"],) + tuple(fargs),
        )
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "created_at": r["created_at"],
                "members": conv_member_ids(r["id"]),
            }
        )
    return {"conversations": out}


# ---------------------------------------------------------------- Messages


def msg_out(r: sqlite3.Row) -> Dict[str, object]:
    return {
        "seq": r["seq"],
        "id": r["id"],
        "conversation_id": r["conversation_id"],
        "from_kind": r["from_kind"],
        "from_id": r["from_id"],
        "from_name": "人" if r["from_kind"] == "human" else (name_of(r["from_id"]) or r["from_id"]),
        "text": r["text"],
        "client_msg_id": r["client_msg_id"],
        "created_at": r["created_at"],
    }


_name_cache: Dict[str, str] = {}


def name_of(aid: str) -> str:
    if aid in _name_cache:
        return _name_cache[aid]
    row = q1("SELECT name FROM agents WHERE id = ?", (aid,))
    name = row["name"] if row else ""
    _name_cache[aid] = name
    return name


@app.post("/api/conversations/{cid}/messages")
def post_message(
    cid: str, body: MessageIn, who: Dict[str, str] = Depends(authenticate)
) -> Dict[str, object]:
    require_conv_access(cid, who)

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    acct = billable_account(who)
    quota_guard(acct, "msg")          # 账号维度卡口：超额 → 402（管理员 / 旧数据不受限）

    from_kind = who["kind"]
    from_id = who["id"]

    if body.from_agent_id:
        target = body.from_agent_id.strip()
        if not is_member(cid, target):
            raise HTTPException(status_code=400, detail="该 agent 不是这个会话的成员：%s" % target)
        if who["kind"] == "agent" and target != who["id"]:
            raise HTTPException(status_code=403, detail="agent 只能用自己身份发消息")
        from_kind, from_id = "agent", target

    # 幂等：同一会话内同一 client_msg_id 重复提交 → 返回第一次那条
    cmid = (body.client_msg_id or "").strip() or None
    if cmid:
        old = q1(
            "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
            (cid, cmid),
        )
        if old is not None:
            return {"id": old["id"], "seq": old["seq"], "duplicate": True}

    mid = new_id("msg")
    created = now_iso()
    try:
        cur = ex(
            "INSERT INTO messages (id, conversation_id, from_kind, from_id, text, client_msg_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (mid, cid, from_kind, from_id, text, cmid, created),
        )
        seq = cur.lastrowid
    except sqlite3.IntegrityError:
        old = q1(
            "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
            (cid, cmid),
        )
        if old is None:
            raise HTTPException(status_code=409, detail="消息写入冲突，请重试")
        return {"id": old["id"], "seq": old["seq"], "duplicate": True}
    recips = [a for a in conv_member_ids(cid) if not (from_kind == "agent" and a == from_id)]
    record_usage(acct, "msg", max(len(recips), 1))      # 按投递计量：投给几个成员就记几条
    return {"id": mid, "seq": seq, "duplicate": False}


@app.get("/api/conversations/{cid}/messages")
def get_messages(
    cid: str,
    since: int = Query(default=0),
    limit: int = Query(default=200, ge=1, le=1000),
    who: Dict[str, str] = Depends(authenticate),
) -> Dict[str, object]:
    require_conv_access(cid, who)
    rows = q(
        "SELECT * FROM messages WHERE conversation_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (cid, since, limit + 1),
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    if truncated and rows:
        latest = rows[-1]["seq"]
    else:
        head = q1("SELECT MAX(seq) AS m FROM messages WHERE conversation_id = ?", (cid,))
        latest = (head["m"] or 0) if head else 0
    return {"messages": [msg_out(r) for r in rows], "latest": latest}


@app.get("/api/agents/{aid}/inbox")
def agent_inbox(
    aid: str,
    since: int = Query(default=0),
    limit: int = Query(default=200, ge=1, le=1000),
    who: Dict[str, str] = Depends(authenticate),
) -> Dict[str, object]:
    if who["kind"] == "agent" and who["id"] != aid:
        raise HTTPException(status_code=403, detail="只能读自己的收件箱")
    arow = q1("SELECT id, owner_account FROM agents WHERE id = ?", (aid,)) \
        if has_col("agents", "owner_account") else q1("SELECT id FROM agents WHERE id = ?", (aid,))
    if arow is None:
        raise HTTPException(status_code=404, detail="agent 不存在")
    if not can_see_owner(owner_of_row(arow), who):      # 账号 token 只能读自己账号名下的 agent
        raise HTTPException(status_code=403, detail="这个 agent 不属于你的账号")

    cids = [r["conversation_id"] for r in q("SELECT conversation_id FROM members WHERE agent_id = ?", (aid,))]
    if not cids:
        return {"messages": [], "latest": 0}
    ph = ",".join("?" * len(cids))
    rows = q(
        "SELECT * FROM messages WHERE conversation_id IN (%s) AND seq > ? ORDER BY seq LIMIT ?" % ph,
        tuple(cids) + (since, limit + 1),
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    if truncated and rows:
        latest = rows[-1]["seq"]
    else:
        head = q1("SELECT MAX(seq) AS m FROM messages WHERE conversation_id IN (%s)" % ph, tuple(cids))
        latest = (head["m"] or 0) if head else 0
    return {"messages": [msg_out(r) for r in rows], "latest": latest}


# ---------------------------------------------------------------- 前端单页

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>温暖通信台</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", "Helvetica Neue", Arial, sans-serif;
         color: #1c2230; background: #f5f6f8; }
  header { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: #fff;
           border-bottom: 1px solid #e3e6ec; position: sticky; top: 0; z-index: 5; flex-wrap: wrap; }
  header h1 { font-size: 17px; margin: 0 8px 0 0; }
  input, select, textarea, button { font: inherit; }
  input[type=text], input[type=password], select, textarea {
    padding: 6px 8px; border: 1px solid #cfd5e0; border-radius: 6px; background: #fff; outline: none; }
  input:focus, select:focus, textarea:focus { border-color: #3b7ddd; }
  button { padding: 6px 12px; border: 1px solid #3b7ddd; border-radius: 6px; background: #3b7ddd;
           color: #fff; cursor: pointer; }
  button:hover { background: #3169be; }
  button.ghost { background: #fff; color: #3b7ddd; }
  button.ghost:hover { background: #eef4ff; }
  #wrap { display: flex; gap: 12px; padding: 12px; align-items: flex-start; }
  .col { background: #fff; border: 1px solid #e3e6ec; border-radius: 10px; padding: 10px; }
  #colLeft { width: 230px; flex: 0 0 230px; }
  #colMain { flex: 1 1 auto; min-width: 320px; display: flex; flex-direction: column; height: calc(100vh - 100px); }
  #colRight { width: 260px; flex: 0 0 260px; }
  .colhead { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
  .colhead b { font-size: 13px; color: #5a6376; letter-spacing: .5px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li.item { padding: 7px 9px; border-radius: 7px; cursor: pointer; display: flex; justify-content: space-between; gap: 6px; }
  li.item:hover { background: #f0f4fb; }
  li.item.active { background: #e6efff; }
  li.item small { color: #8b93a4; }
  li.agent { padding: 6px 4px; border-bottom: 1px dashed #eef0f4; }
  li.agent:last-child { border-bottom: 0; }
  .tag { font-size: 11px; color: #7b8496; }
  #stream { flex: 1 1 auto; overflow-y: auto; padding: 6px 4px 10px; }
  .msg { margin-bottom: 10px; }
  .msg .meta { font-size: 12px; color: #8b93a4; margin-bottom: 2px; }
  .msg .bubble { display: inline-block; padding: 7px 10px; border-radius: 9px; background: #f0f2f5;
                 white-space: pre-wrap; word-break: break-word; max-width: 80%; }
  .msg.agent .bubble { background: #e9f3ff; }
  .msg.mine .bubble { background: #e7f8ee; }
  #composer { border-top: 1px solid #eceff4; padding-top: 9px; display: flex; flex-direction: column; gap: 7px; }
  #composer .row { display: flex; gap: 8px; align-items: center; }
  #text { flex: 1 1 auto; resize: vertical; min-height: 40px; max-height: 140px; }
  #hint { color: #8b93a4; font-size: 12px; }
  #error { color: #c0392b; font-size: 12px; }
  #netStatus { font-size: 12px; color: #8b93a4; }
  .modal { position: fixed; inset: 0; background: rgba(20,25,35,.35); display: none; align-items: center; justify-content: center; z-index: 20; }
  .modal.on { display: flex; }
  .card { background: #fff; border-radius: 12px; padding: 16px; width: 380px; max-height: 82vh; overflow: auto; }
  .card h2 { font-size: 15px; margin: 0 0 10px; }
  .card label { display: block; margin: 8px 0 4px; font-size: 13px; color: #5a6376; }
  .card input[type=text], .card select { width: 100%; }
  .checks { border: 1px solid #e3e6ec; border-radius: 8px; padding: 8px; max-height: 190px; overflow: auto; }
  .checks label { display: flex; align-items: center; gap: 6px; margin: 3px 0; color: #1c2230; font-size: 13px; }
  .card .foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  code.token { display: block; background: #f6f8fb; border: 1px solid #e3e6ec; border-radius: 6px;
               padding: 8px; margin: 8px 0; word-break: break-all; font-size: 12px; }
  .warn { background: #fff8e5; border: 1px solid #f0dda0; padding: 8px; border-radius: 6px; font-size: 12px; }
  #empty { color: #9aa2b1; padding: 24px 8px; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>温暖通信台</h1>
  <span class="tag">我的 token</span>
  <input id="token" type="password" placeholder="人 token（默认 h-dev-token）" size="26">
  <button id="saveToken">保存</button>
  <span id="netStatus"></span>
  <span id="error"></span>
</header>

<div id="wrap">
  <div class="col" id="colLeft">
    <div class="colhead"><b>会话</b><button class="ghost" id="btnNewConv">新建会话</button></div>
    <ul id="convList"><li class="item"><small>加载中…</small></li></ul>
  </div>

  <div class="col" id="colMain">
    <div class="colhead">
      <b id="convTitle">未选择会话</b>
      <small class="tag" id="convMeta"></small>
    </div>
    <div id="stream"><div id="empty">从左边选一个会话开始</div></div>
    <div id="composer">
      <div class="row">
        <span class="tag">以谁的身份发：</span>
        <select id="fromSel"><option value="">人（我）</option></select>
      </div>
      <div class="row">
        <textarea id="text" placeholder="输入消息，Enter 发送，Shift+Enter 换行"></textarea>
        <button id="btnSend">发送</button>
      </div>
      <div id="hint">每 3 秒自动拉取新消息</div>
    </div>
  </div>

  <div class="col" id="colRight">
    <div class="colhead"><b>Agent</b><button class="ghost" id="btnRefreshAgents">刷新</button></div>
    <ul id="agentList"><li class="agent"><small>加载中…</small></li></ul>
    <div style="margin-top:10px">
      <label class="tag">新建 Agent</label>
      <div class="row" style="display:flex;gap:6px;margin-top:4px">
        <input type="text" id="newAgentName" placeholder="名字，如 研究助手" style="flex:1">
        <button id="btnNewAgent">新建</button>
      </div>
    </div>
  </div>
</div>

<!-- 新建会话弹窗 -->
<div class="modal" id="convModal">
  <div class="card">
    <h2>新建会话</h2>
    <label>标题</label>
    <input type="text" id="convTitleInput" placeholder="例如：策略讨论组">
    <label>类型</label>
    <select id="convKind">
      <option value="auto">自动（1 个成员=单聊，多个=群聊）</option>
      <option value="dm">单聊</option>
      <option value="group">群聊</option>
    </select>
    <label>成员（可多选）</label>
    <div class="checks" id="memberChecks"></div>
    <div class="foot">
      <button class="ghost" id="convCancel">取消</button>
      <button id="convCreate">创建</button>
    </div>
  </div>
</div>

<!-- 一次性 token 弹窗 -->
<div class="modal" id="tokenModal">
  <div class="card">
    <h2>Agent 创建成功</h2>
    <div class="warn">这个 token 只显示这一次，请现在复制保存。</div>
    <code class="token" id="newToken"></code>
    <div class="foot">
      <button class="ghost" id="btnCopyToken">复制</button>
      <button id="tokenClose">我已保存</button>
    </div>
  </div>
</div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var state = { convs: [], agents: [], cid: null, latest: 0, timer: null };

  function token() { return ($('token').value || '').trim(); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }
  function showErr(m) { $('error').textContent = m ? ('⚠ ' + m) : ''; }

  // 支持挂在子路径下（例如 https://hub.hvip.one/）：按当前页面路径前缀拼接口地址
  var BASE = (function () {
    var p = location.pathname;
    if (p.charAt(p.length - 1) !== '/') p = p.replace(/\/[^\/]*$/, '/');
    return p;
  })();

  function api(path, opts) {
    opts = opts || {};
    if (path.charAt(0) === '/') path = BASE.replace(/\/$/, '') + path;   // '/api/x' → '/hub/api/x'
    var h = { 'Authorization': 'Bearer ' + token() };
    if (opts.body) h['Content-Type'] = 'application/json';
    return fetch(path, {
      method: opts.method || 'GET',
      headers: h,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = t; }
        if (!r.ok) {
          var d = (data && data.detail) ? data.detail : ('HTTP ' + r.status);
          throw new Error(typeof d === 'string' ? d : JSON.stringify(d));
        }
        return data;
      });
    });
  }

  function randId() { return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  // ---------------------------------------------------------- 加载数据
  function loadAgents() {
    return api('/api/agents').then(function (d) {
      state.agents = d.agents || [];
      var list = $('agentList');
      if (!state.agents.length) { list.innerHTML = '<li class="agent"><small>还没有 Agent</small></li>'; }
      else {
        list.innerHTML = state.agents.map(function (a) {
          return '<li class="agent"><b>' + esc(a.name) + '</b><br><small class="tag">' + esc(a.id) + '</small></li>';
        }).join('');
      }
      var sel = $('fromSel');
      var cur = sel.value;
      sel.innerHTML = '<option value="">人（我）</option>' + state.agents.map(function (a) {
        return '<option value="' + esc(a.id) + '">' + esc(a.name) + '</option>';
      }).join('');
      sel.value = cur;
      var checks = $('memberChecks');
      if (!state.agents.length) { checks.innerHTML = '<small class="tag">先去右边建一个 Agent</small>'; }
      else {
        checks.innerHTML = state.agents.map(function (a) {
          return '<label><input type="checkbox" value="' + esc(a.id) + '"> ' + esc(a.name) +
                 ' <small class="tag">' + esc(a.id) + '</small></label>';
        }).join('');
      }
    }).catch(function (e) { showErr('读取 Agent 失败：' + e.message); });
  }

  function loadConvs() {
    return api('/api/conversations').then(function (d) {
      state.convs = d.conversations || [];
      var list = $('convList');
      if (!state.convs.length) { list.innerHTML = '<li class="item"><small>还没有会话</small></li>'; return; }
      list.innerHTML = state.convs.map(function (c) {
        return '<li class="item' + (c.id === state.cid ? ' active' : '') + '" data-cid="' + esc(c.id) + '">' +
               '<span>' + esc(c.title) + '</span><small>' + (c.kind === 'dm' ? '单聊' : '群聊') +
               ' · ' + (c.members || []).length + '人</small></li>';
      }).join('');
      Array.prototype.forEach.call(list.querySelectorAll('.item[data-cid]'), function (el) {
        el.onclick = function () { openConv(el.getAttribute('data-cid')); };
      });
      if (!state.cid && state.convs.length) { openConv(state.convs[0].id); }
    }).catch(function (e) { showErr('读取会话失败：' + e.message); });
  }

  // ---------------------------------------------------------- 会话与消息
  function openConv(cid) {
    state.cid = cid;
    state.latest = 0;
    $('stream').innerHTML = '<div id="empty">加载中…</div>';
    var c = state.convs.filter(function (x) { return x.id === cid; })[0];
    $('convTitle').textContent = c ? c.title : cid;
    $('convMeta').textContent = c ? ((c.kind === 'dm' ? '单聊' : '群聊') + ' · ' + cid) : cid;
    Array.prototype.forEach.call(document.querySelectorAll('#convList .item'), function (el) {
      el.classList.toggle('active', el.getAttribute('data-cid') === cid);
    });
    poll(true);
  }

  function render(msgs) {
    var s = $('stream');
    var empty = $('empty');
    if (empty) { empty.remove(); }
    var asWho = $('fromSel').value || 'human';
    msgs.forEach(function (m) {
      var div = document.createElement('div');
      div.className = 'msg ' + (m.from_kind === 'agent' ? 'agent' : 'human') +
                      (m.from_id === asWho ? ' mine' : '');
      div.innerHTML = '<div class="meta">' + esc(m.from_name || m.from_id) + ' · ' +
                      esc(m.created_at) + ' · #' + m.seq + '</div>' +
                      '<div class="bubble">' + esc(m.text) + '</div>';
      s.appendChild(div);
    });
    s.scrollTop = s.scrollHeight;
  }

  function poll(initial) {
    if (!state.cid) return;
    api('/api/conversations/' + encodeURIComponent(state.cid) + '/messages?since=' + state.latest + '&limit=200')
      .then(function (d) {
        var msgs = d.messages || [];
        if (msgs.length) { render(msgs); }
        state.latest = d.latest || state.latest;
        $('netStatus').textContent = '已连接 · ' + new Date().toLocaleTimeString('zh-CN');
        showErr('');
      })
      .catch(function (e) {
        $('netStatus').textContent = '连接异常';
        showErr(e.message);
      });
  }

  function send() {
    var text = $('text').value.trim();
    if (!text || !state.cid) { return; }
    var body = { text: text, client_msg_id: randId() };
    var from = $('fromSel').value;
    if (from) { body.from_agent_id = from; }
    $('btnSend').disabled = true;
    api('/api/conversations/' + encodeURIComponent(state.cid) + '/messages', { method: 'POST', body: body })
      .then(function () { $('text').value = ''; poll(); showErr(''); })
      .catch(function (e) { showErr('发送失败：' + e.message); })
      .then(function () { $('btnSend').disabled = false; $('text').focus(); });
  }

  // ---------------------------------------------------------- 事件绑定
  $('saveToken').onclick = function () {
    localStorage.setItem('hub_token', token());
    state.cid = null; state.latest = 0;
    $('stream').innerHTML = '<div id="empty">从左边选一个会话开始</div>';
    $('convTitle').textContent = '未选择会话';
    loadAgents().then(loadConvs);
  };

  $('btnRefreshAgents').onclick = function () { loadAgents(); };

  $('btnNewConv').onclick = function () { $('convModal').classList.add('on'); };
  $('convCancel').onclick = function () { $('convModal').classList.remove('on'); };
  $('convCreate').onclick = function () {
    var title = $('convTitleInput').value.trim();
    if (!title) { showErr('请填会话标题'); return; }
    var members = Array.prototype.filter.call(
      $('memberChecks').querySelectorAll('input:checked'), function (x) { return true; }
    ).map(function (x) { return x.value; });
    var kind = $('convKind').value;
    var body = { title: title, members: members };
    if (kind !== 'auto') { body.kind = kind; }
    api('/api/conversations', { method: 'POST', body: body }).then(function (c) {
      $('convModal').classList.remove('on');
      $('convTitleInput').value = '';
      loadConvs().then(function () { openConv(c.id); });
    }).catch(function (e) { showErr('创建会话失败：' + e.message); });
  };

  $('btnNewAgent').onclick = function () {
    var name = $('newAgentName').value.trim();
    if (!name) { showErr('请填 Agent 名字'); return; }
    api('/api/agents', { method: 'POST', body: { name: name } }).then(function (a) {
      $('newAgentName').value = '';
      $('newToken').textContent = a.token;
      $('tokenModal').classList.add('on');
      loadAgents();
    }).catch(function (e) { showErr('新建 Agent 失败：' + e.message); });
  };
  $('tokenClose').onclick = function () { $('tokenModal').classList.remove('on'); };
  $('btnCopyToken').onclick = function () {
    var t = $('newToken').textContent;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(t).then(function () { $('btnCopyToken').textContent = '已复制'; });
    } else {
      var r = document.createRange(); r.selectNode($('newToken'));
      window.getSelection().removeAllRanges(); window.getSelection().addRange(r);
      document.execCommand('copy');
      $('btnCopyToken').textContent = '已复制';
    }
  };

  $('btnSend').onclick = send;
  $('text').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // ---------------------------------------------------------- 启动
  var saved = localStorage.getItem('hub_token') || 'h-dev-token';
  $('token').value = saved;
  loadAgents().then(loadConvs);
  setInterval(function () { if (state.cid) { poll(); } }, 3000);
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(PAGE_HTML)


# ---------------------------------------------------------------- 入口

db_init()

if __name__ == "__main__":
    import uvicorn

    print("[温暖通信台] http://127.0.0.1:%d   DB=%s" % (PORT, DB_PATH))
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
