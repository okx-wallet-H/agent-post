#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
温暖通信台 · 多租户账号层（P1-A）

交付约束：本文件是唯一新文件，不改 main.py、不引入新依赖（仅 fastapi/pydantic/标准库）。

提供：
- router = APIRouter()：
    POST /api/accounts/register  {email, password} -> {account_id, token}
    POST /api/accounts/login     {email, password} -> {token}
    POST /api/accounts/agents    {name}             -> {id, name, token}（account token，agent 归该账号）
    GET  /api/me                 -> {account_id, email, agents:[{id,name}]}
- def resolve(conn, token) -> ("human", account_id) | ("agent", agent_id) | None
- ensure_schema(conn)：幂等建表 accounts / account_tokens，幂等给 agents、conversations
  加列 owner_account（PRAGMA table_info 判存在再 ALTER），已存在的行归固定默认账号。
- make_authenticate / make_conv_access：集成辅助，供 main.py 三行接入（见文件尾注释）。

集成方式（改 main.py 时照 api_v1 的位置加三行，本文件不动 main.py）：
    import accounts
    accounts.ensure_schema(_db)
    app.include_router(accounts.router)
并把依赖覆写/替换成 make_authenticate、make_conv_access（见 test_accounts.py 的用法）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

# ---------------------------------------------------------------- 注册防刷（#17）
# 最小两道防线：注册限流（每 IP 每小时 ≤3 / 每天 ≤10）+ 邮箱校验（格式 + 一次性邮箱黑名单）。
# 可选邀请码：环境变量 HUB_INVITE_CODE 设了就必须带对，不设则开放。
# 所有拦截写一行日志（stdout；另配 HUB_GUARD_EVENTS_URL/TOKEN 时顺手上报事件流）。

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "trashmail.com", "yopmail.com", "tempmail.ninja", "throwawaymail.com",
    "sharklasers.com", "grr.la", "mailnesia.com", "dispostable.com",
    "temp-mail.org", "emailondeck.com", "mohmal.com", "maildrop.cc",
    "getnada.com", "nada.email", "mail.tm", "0wnd.net", "crazymailing.com",
    "wegwerfmail.de", "temporary-mail.net", "spambox.us", "mailcatch.com",
}
HUB_INVITE_CODE = os.environ.get("HUB_INVITE_CODE", "").strip()

_RATE_LOCK = threading.Lock()
_RATE: Dict[str, List[float]] = {}   # ip -> 最近 24h 的注册尝试时刻（含被拒的）


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _log_block(ip: str, reason: str, email: str = "") -> None:
    """拦截日志：一行 stdout；配了 HUB_GUARD_EVENTS_URL/TOKEN 再顺手 POST 事件（失败静默）。"""
    line = "[guard] 拦截注册 ip=%s reason=%s email=%s" % (ip, reason, email)
    print(line, flush=True)
    url = os.environ.get("HUB_GUARD_EVENTS_URL", "").strip()
    tok = os.environ.get("HUB_GUARD_EVENTS_TOKEN", "").strip()
    if url and tok:
        try:
            subprocess.run(
                ["curl", "-s", "-m", "2", "-o", "/dev/null", "-X", "POST", url,
                 "-H", "Authorization: Bearer " + tok, "-H", "Content-Type: application/json",
                 "--data-binary", json.dumps({"agent": "warm-hub", "kind": "告警",
                                              "text": line}, ensure_ascii=False)],
                timeout=3,
            )
        except Exception:
            pass


def _check_rate(ip: str) -> None:
    """同一来源 IP：每小时 ≤3 次、每天 ≤10 次注册；超限 429 + Retry-After。"""
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(ip, []) if now - t < 3600 * 24]
        hour = [t for t in hits if now - t < 3600]
        if len(hour) >= 3:
            retry = int(3600 - (now - hour[0])) + 1
            _log_block(ip, "注册限流:每小时超3次")
            raise HTTPException(status_code=429, detail="注册太频繁，稍后再试",
                                headers={"Retry-After": str(retry)})
        if len(hits) >= 10:
            retry = int(3600 * 24 - (now - hits[0])) + 1
            _log_block(ip, "注册限流:每天超10次")
            raise HTTPException(status_code=429, detail="今天注册次数用完了，明天再试",
                                headers={"Retry-After": str(retry)})
        hits.append(now)
        _RATE[ip] = hits


def _email_error(email: str) -> Optional[str]:
    """返回错误文案；None = 通过。"""
    email = email.strip().lower()
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        return "邮箱格式不对"
    if email.rsplit("@", 1)[-1] in _DISPOSABLE_DOMAINS:
        return "一次性邮箱不支持，请用常用邮箱"
    return None

# 固定默认账号：加列后已存在的 agents/conversations 都归到它名下（人 token 的老数据不丢）
DEFAULT_ACCOUNT_ID = "acct_default"
DEFAULT_ACCOUNT_EMAIL = "default@hub.local"

_PBKDF2_ROUNDS = 200_000


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    ).hex()


def verify_password(password: str, salt: str, pw_hash: str) -> bool:
    return secrets.compare_digest(hash_password(password, salt), pw_hash)


# ---------------------------------------------------------------- 表结构（幂等）


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建 accounts/account_tokens（幂等）；给 agents、conversations 幂等加列
    owner_account；已存在的行归到固定默认账号。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id         TEXT PRIMARY KEY,
            email      TEXT UNIQUE NOT NULL,
            pw_hash    TEXT NOT NULL,
            salt       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_tokens (
            token      TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # 幂等加列：PRAGMA table_info 判存在再 ALTER（ALTER ADD COLUMN 重复执行会报错）
    for tbl in ("agents", "conversations"):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % tbl)]
        if "owner_account" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN owner_account TEXT" % tbl)
    # 固定默认账号（老数据归它）
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, email, pw_hash, salt, created_at) VALUES (?,?,?,?,?)",
        (DEFAULT_ACCOUNT_ID, DEFAULT_ACCOUNT_EMAIL, "x", "00", now_iso()),
    )
    conn.execute(
        "UPDATE agents SET owner_account = ? WHERE owner_account IS NULL",
        (DEFAULT_ACCOUNT_ID,),
    )
    conn.execute(
        "UPDATE conversations SET owner_account = ? WHERE owner_account IS NULL",
        (DEFAULT_ACCOUNT_ID,),
    )
    conn.commit()


# ---------------------------------------------------------------- token 解析


def resolve(conn: sqlite3.Connection, token: str) -> Optional[tuple[str, str]]:
    """token -> ("human", account_id) / ("agent", agent_id)；找不到返回 None。"""
    if not token:
        return None
    row = conn.execute(
        "SELECT account_id FROM account_tokens WHERE token = ?", (token,)
    ).fetchone()
    if row is not None:
        return ("human", row[0])
    row = conn.execute("SELECT id FROM agents WHERE token = ?", (token,)).fetchone()
    if row is not None:
        return ("agent", row[0])
    return None


def _owner_account_of_agent(conn: sqlite3.Connection, agent_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT owner_account FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- 集成辅助


def make_authenticate(conn: sqlite3.Connection, user_token: str):
    """返回与 main.authenticate 同签名的鉴权函数（Header 可选），
    依次认：人总 token → account token（human）→ agent token。
    集成时用 app.dependency_overrides[main.authenticate] = make_authenticate(...) 覆写。"""

    def authenticate(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="token 为空")
        if secrets.compare_digest(token, user_token):
            return {"kind": "human", "id": "human", "name": "人"}
        r = resolve(conn, token)
        if r is None:
            raise HTTPException(status_code=401, detail="token 无效")
        kind, ident = r
        if kind == "human":
            email = conn.execute(
                "SELECT email FROM accounts WHERE id = ?", (ident,)
            ).fetchone()
            return {"kind": "human", "id": ident, "name": (email[0] if email else ident)}
        name = conn.execute("SELECT name FROM agents WHERE id = ?", (ident,)).fetchone()
        return {"kind": "agent", "id": ident, "name": (name[0] if name else ident)}

    return authenticate


def make_conv_access(conn: sqlite3.Connection):
    """返回与 main.require_conv_access 同签名的访问判定（多租户版）：
    人总 token 全可见；account token 只见自己名下的会话；agent token 只见
    所属账号名下的会话且必须是成员。集成时 main.require_conv_access = make_conv_access(...)。"""

    def conv_access(cid: str, who: Dict[str, str]):
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (cid,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conv_owner = row["owner_account"] if "owner_account" in row.keys() else None
        if who["kind"] == "human":
            if who["id"] != "human" and conv_owner != who["id"]:
                raise HTTPException(status_code=403, detail="这个会话不属于你的账号")
            return row
        # agent
        owner = _owner_account_of_agent(conn, who["id"])
        if conv_owner != owner:
            raise HTTPException(status_code=403, detail="这个会话不属于你所属的账号")
        member = conn.execute(
            "SELECT 1 FROM members WHERE conversation_id = ? AND agent_id = ?",
            (cid, who["id"]),
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=403, detail="你不在这个会话里")
        return row

    return conv_access


# ---------------------------------------------------------------- 请求体


class RegisterIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=6, max_length=128)
    invite_code: Optional[str] = None


class LoginIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class AgentNameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")
    return token


def _require_account(conn, authorization: Optional[str]) -> str:
    """从 Header 解出 account token 并返回 account_id；不是账号 token 一律 401。"""
    r = resolve(conn, _bearer(authorization))
    if r is None or r[0] != "human":
        raise HTTPException(status_code=401, detail="需要账号 token")
    return r[1]


# ---------------------------------------------------------------- 端点


@router.post("/api/accounts/register")
def register(
    body: RegisterIn,
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, str]:
    import main as _m  # 延迟 import，避免模块加载顺序问题

    conn = _m._db
    ip = _client_ip(request)
    email = body.email.strip().lower()
    err = _email_error(email)           # ① 邮箱：格式 + 一次性邮箱黑名单（不计限流——刷不出账号）
    if err:
        _log_block(ip, err, email)
        raise HTTPException(status_code=400, detail=err)
    if HUB_INVITE_CODE:                 # ② 邀请码：设了就必带，带错 403（不计限流）
        if (body.invite_code or "").strip() != HUB_INVITE_CODE:
            _log_block(ip, "邀请码不对", email)
            raise HTTPException(status_code=403, detail="邀请码不对")
    _check_rate(ip)                     # ③ 限流：每 IP 每小时 ≤3 / 每天 ≤10（只卡能产出账号的尝试）
    if conn.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
        raise HTTPException(status_code=409, detail="这个邮箱已注册")
    account_id = "acct_%s" % secrets.token_hex(6)
    salt = secrets.token_hex(16)
    pw_hash = hash_password(body.password, salt)
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO accounts (id, email, pw_hash, salt, created_at) VALUES (?,?,?,?,?)",
        (account_id, email, pw_hash, salt, now_iso()),
    )
    conn.execute(
        "INSERT INTO account_tokens (token, account_id, created_at) VALUES (?,?,?)",
        (token, account_id, now_iso()),
    )
    # 新账号自动领试用券（14 天 / 2000 条）——否则新用户一进来就撞「额度用尽」
    try:
        import billing as _b
        _code = _b.create_coupon(conn, None, 14, 2000)
        _b.redeem_coupon(conn, _code, account_id)
    except Exception as _e:
        print("试用券发放失败（不影响注册）：", _e)
    conn.commit()
    return {"account_id": account_id, "token": token}


@router.post("/api/accounts/login")
def login(body: LoginIn, authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    import main as _m

    conn = _m._db
    email = body.email.strip().lower()
    row = conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
    if row is None or not verify_password(body.password, row["salt"], row["pw_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不对")
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO account_tokens (token, account_id, created_at) VALUES (?,?,?)",
        (token, row["id"], now_iso()),
    )
    conn.commit()
    return {"token": token}


@router.post("/api/accounts/agents")
def create_agent_for_account(
    body: AgentNameIn, authorization: Optional[str] = Header(default=None)
) -> Dict[str, str]:
    """account token 建 agent，agent 归该账号名下（多租户版建 agent 入口）。"""
    import main as _m

    conn = _m._db
    account_id = _require_account(conn, authorization)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名字不能为空")
    aid = "ag_%s" % secrets.token_hex(6)
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO agents (id, name, token, created_at, owner_account) VALUES (?,?,?,?,?)",
        (aid, name, token, now_iso(), account_id),
    )
    conn.commit()
    return {"id": aid, "name": name, "token": token}


@router.get("/api/me")
def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, object]:
    import main as _m

    conn = _m._db
    account_id = _require_account(conn, authorization)
    row = conn.execute("SELECT email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    email = row["email"] if row else ""
    agents = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            "SELECT id, name FROM agents WHERE owner_account = ? ORDER BY created_at, id",
            (account_id,),
        )
    ]
    return {"account_id": account_id, "email": email, "agents": agents}
