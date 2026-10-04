"""session —— 一次性登录码 + 会话 cookie（#35，2026-10-05，手机免粘 token）

老板场景：在外面用手机收发消息，现在要粘 43 位 token 很烦。本模块提供：
- POST /login/code（管理员 token 调）→ 6 位数字码，5 分钟有效、一次性
- POST /login/redeem {"code": ...} → 校验通过种 HttpOnly+Secure+SameSite=Lax 的
  session cookie（30 天），服务端存 session→account_id
- resolve_token(request) 辅助：其它模块把「读 Bearer」换成它，就能同时接受
  cookie 登录（Bearer 路径的解析与 api_v1._who 完全一致，返回 who dict；
  cookie 路径查 sessions 表，返回 {"kind":"human","id":account_id,"name":...}）
- POST /logout（登录态）→ 删 session、清 cookie

存储（attach 时幂等建表，不动 main.py SCHEMA）：
- login_codes(code PRIMARY KEY, account_id, created_at, used_at NULL=未用)
- sessions(token PRIMARY KEY, account_id, created_at, expires_at)

注入约定同其它模块：session.attach(db=, lock=, user_token=, q=, q1=, ex=,
now_iso=, tenancy=可选)。main.py 挂载由 H 加。
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}

SESSION_COOKIE = "warm_session"
CODE_TTL_S = 300          # 登录码 5 分钟有效
SESSION_DAYS = 30         # cookie 30 天


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）；幂等建表。"""
    _S.update(state)
    _ex(
        "CREATE TABLE IF NOT EXISTS login_codes ("
        " code TEXT PRIMARY KEY,"
        " account_id TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " used_at TEXT)"
    )
    _ex(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " token TEXT PRIMARY KEY,"
        " account_id TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL)"
    )


def _q(sql: str, args=()):
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()):
    return _S["ex"](sql, args)  # type: ignore[operator]


def _now_iso() -> str:
    return _S["now_iso"]() if "now_iso" in _S else datetime.now(timezone.utc).isoformat(timespec="seconds")  # type: ignore[operator]


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- 身份解析（对齐 api_v1._who）

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
            row = _q1("SELECT email FROM accounts WHERE id = ?", (r[1],))
            return {"kind": "human", "id": r[1], "name": (row["email"] if row else r[1])}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is None:
        return None
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


def _account_of(who: Dict[str, str]) -> str:
    """who → account_id（登录码/会话绑定的账号维度）。"""
    if who.get("kind") == "human":
        ten = _S.get("tenancy")
        account_of = ten.get("account_of") if isinstance(ten, dict) else None
        if account_of is not None:
            try:
                return str(account_of(who))
            except Exception:
                pass
        return who.get("id") or "human"
    return who.get("id", "")


def resolve_token(request: Request) -> Optional[Dict[str, str]]:
    """其它模块接 cookie 的入口：Bearer 优先（解析同 api_v1._who），
    没有 Bearer 时读 warm_session cookie 查 sessions 表（未过期才认）。
    返回 who dict 或 None。"""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return _who(auth[7:].strip())
    sess = request.cookies.get(SESSION_COOKIE)
    if sess:
        row = _q1("SELECT account_id, expires_at FROM sessions WHERE token = ?", (sess,))
        if row is not None:
            exp = _parse_ts(row["expires_at"])
            if exp is not None and exp > datetime.now(timezone.utc):
                email = _q1("SELECT email FROM accounts WHERE id = ?", (row["account_id"],))
                return {"kind": "human", "id": row["account_id"],
                        "name": (email["email"] if email else row["account_id"])}
    return None


def require_login(request: Request) -> Dict[str, str]:
    who = resolve_token(request)
    if who is None:
        raise HTTPException(status_code=401, detail="未登录：Bearer token 或会话 cookie 都没有效")
    return who


# ---------------------------------------------------------------- 路由

@router.post("/login/code")
def login_code(request: Request) -> Dict[str, object]:
    """管理员（Bearer 管理员 token）生成一次性 6 位登录码，5 分钟有效。"""
    auth = request.headers.get("Authorization") or ""
    who = _who(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if who is None or who.get("kind") != "human":
        raise HTTPException(status_code=401, detail="只有管理员 token 能生成登录码")
    code = f"{secrets.randbelow(1000000):06d}"
    _ex("INSERT INTO login_codes (code, account_id, created_at, used_at) VALUES (?,?,?,NULL)",
        (code, _account_of(who), _now_iso()))
    return {"code": code, "expires_in_s": CODE_TTL_S}


class RedeemIn(BaseModel):
    code: str


@router.post("/login/redeem")
def login_redeem(body: RedeemIn, request: Request) -> JSONResponse:
    """用一次性码换 30 天 session cookie（HttpOnly+Secure+SameSite=Lax）。
    码过期/已用/不存在 → 401。"""
    code = (body.code or "").strip()
    row = _q1("SELECT * FROM login_codes WHERE code = ?", (code,))
    if row is None or row["used_at"] is not None:
        raise HTTPException(status_code=401, detail="码无效或已用过")
    created = _parse_ts(row["created_at"])
    if created is None or (datetime.now(timezone.utc) - created).total_seconds() > CODE_TTL_S:
        raise HTTPException(status_code=401, detail="码已过期（5 分钟有效）")
    _ex("UPDATE login_codes SET used_at = ? WHERE code = ?", (_now_iso(), code))
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    _ex("INSERT INTO sessions (token, account_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, row["account_id"], _now_iso(), expires.isoformat(timespec="seconds")))
    resp = JSONResponse({"ok": True, "account_id": row["account_id"], "expires_in_days": SESSION_DAYS})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=True, samesite="lax")
    return resp


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    """删掉当前会话并清 cookie（幂等）。"""
    sess = request.cookies.get(SESSION_COOKIE)
    if sess:
        _ex("DELETE FROM sessions WHERE token = ?", (sess,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/session/me")
def session_me(who: Dict[str, str] = Depends(require_login)) -> Dict[str, str]:  # type: ignore[assignment]
    """登录态自检（cookie 或 Bearer 都认）。"""
    return who
