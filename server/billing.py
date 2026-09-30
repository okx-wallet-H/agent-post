#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
温暖通信台 · 计费与免费券（P1-B）+ 账号限速（P2 #20）

独立模块，自包含。挂载方式（main.py 不动，由平台侧加三行）：

    import billing
    billing.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q1=q1)
    app.include_router(billing.router)

计量口径（按投递计）：
  一条消息投给 N 个成员，由调用方在投递后调
  record_usage(conn, account_id, "msg", n=N) 记 N 条；
  周期按自然月记账（本地时间），usage_counters 主键 (account_id, period)，
  跨月后当月计数归零、额度不结转。
  发消息前调 check_quota(conn, account_id, "msg")，返回 False 时调用方回 HTTP 402；
  平台侧接线用 guard_quota(conn, account_id, kind)：超限直接抛 HTTP 402。

配额口径：
  券给 msgs 条消息额度 + 固定 AGENTS_QUOTA_DEFAULT 个 agent 名额；
  一个账号兑换多张券：msgs 额度求和、有效期取最晚那张；
  券过期（redeemed_at + days 天 < 现在）后不再贡献额度。

账号限速（P2 #20）：
  每分钟 ≤ 60 条消息，超额 HTTP 429 + Retry-After 头；按账号（agent）维度计数，
  管理员（人 token）不限。实现为内存滑动窗口（单进程 uvicorn 够用；多 worker 需换存储）。
  接入：app.add_middleware(billing.RateLimitMiddleware)，只对 POST /v1/send 计数；
  函数层可直调 rate_limit_guard(account_id)。

/v1/send 的 402 行为实测（2026-09-30，smoke_quota.sh 跑出）：
  账号 ag_c 兑换 1 条额度券后打 POST /v1/send：第 1 条 200 ok；
  第 2 条 HTTP 402，响应体 {"detail":{"detail":"额度用尽","period":"2026-09","remaining":0}}。
  即：quota_guard 在消息落库前拦截，detail 是 check_quota 返回的原样 dict。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

router = APIRouter()

AGENTS_QUOTA_DEFAULT = 10
PERIOD_FMT = "%Y-%m"
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

RATE_LIMIT_MAX = 60      # 每分钟最多消息条数
RATE_WINDOW_SEC = 60     # 滑动窗口（秒）
RATE_LIMITED_PATHS = ("/v1/send",)  # 只对发消息口计数；要加别的路径在这里扩

# ---------------------------------------------------------------- 上下文（attach 注入）

_CTX: Dict[str, object] = {"db": None, "lock": None, "user_token": None, "q1": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS coupons (
    code        TEXT PRIMARY KEY,
    days        INTEGER NOT NULL,
    msgs        INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    redeemed_by TEXT,
    redeemed_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_counters (
    account_id TEXT NOT NULL,
    period     TEXT NOT NULL,
    msgs       INTEGER NOT NULL DEFAULT 0,
    agents     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, period)
);
"""


def attach(db, lock, user_token, q1) -> None:
    _CTX.update(db=db, lock=lock, user_token=user_token, q1=q1)
    init_db(db)


def init_db(conn) -> None:
    """幂等自建 billing 两张表（agents 等基础表由 main.py 建）。"""
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 时间

def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def current_period() -> str:
    return _now().strftime(PERIOD_FMT)


def _expiry_of(redeemed_at_iso: str, days: int) -> datetime:
    return datetime.fromisoformat(redeemed_at_iso) + timedelta(days=days)


# ---------------------------------------------------------------- 核心逻辑（函数层，自测直调）

def create_coupon(conn, code: Optional[str] = None, days: int = 14, msgs: int = 2000) -> str:
    if days < 1 or msgs < 1:
        raise HTTPException(status_code=400, detail="days/msgs 必须 ≥ 1")
    if code is None:
        code = "cp_" + secrets.token_urlsafe(8)
    code = code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")
    try:
        conn.execute(
            "INSERT INTO coupons (code, days, msgs, created_at) VALUES (?,?,?,?)",
            (code, days, msgs, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="券码已存在")
    return code


def redeem_coupon(conn, code: str, account_id: str) -> Dict[str, object]:
    code = code.strip()
    row = conn.execute(
        "SELECT days, msgs, redeemed_by FROM coupons WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="券不存在")
    if row["redeemed_by"] is not None:
        raise HTTPException(status_code=409, detail="券已被兑换")
    # 条件更新：并发下也保证一券只能被兑换一次
    redeemed_at = now_iso()
    cur = conn.execute(
        "UPDATE coupons SET redeemed_by = ?, redeemed_at = ? WHERE code = ? AND redeemed_by IS NULL",
        (account_id, redeemed_at, code),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="券已被兑换")
    expires_at = _expiry_of(redeemed_at, row["days"])
    return {
        "ok": True,
        "days": row["days"],
        "msgs": row["msgs"],
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }


def _active_quota(conn, account_id: str) -> Tuple[int, Optional[str]]:
    """该账号未过期券的 msgs 额度总和 + 最晚有效期（ISO 或 None）。"""
    now = _now()
    rows = conn.execute(
        "SELECT days, msgs, redeemed_at FROM coupons WHERE redeemed_by = ? AND redeemed_at IS NOT NULL",
        (account_id,),
    ).fetchall()
    total = 0
    latest_exp: Optional[datetime] = None
    for r in rows:
        exp = _expiry_of(r["redeemed_at"], r["days"])
        if exp > now:
            total += r["msgs"]
        if latest_exp is None or exp > latest_exp:
            latest_exp = exp
    return total, (latest_exp.isoformat(timespec="seconds") if latest_exp else None)


def get_usage(conn, account_id: str) -> Dict[str, object]:
    period = current_period()
    row = conn.execute(
        "SELECT msgs, agents FROM usage_counters WHERE account_id = ? AND period = ?",
        (account_id, period),
    ).fetchone()
    used_msgs = row["msgs"] if row else 0
    used_agents = row["agents"] if row else 0
    quota_msgs, expires_at = _active_quota(conn, account_id)
    quota_agents = AGENTS_QUOTA_DEFAULT if quota_msgs > 0 else 0
    return {
        "period": period,
        "agents": used_agents,
        "msgs": used_msgs,
        "quota_msgs": quota_msgs,
        "quota_agents": quota_agents,
        "remaining_msgs": max(quota_msgs - used_msgs, 0),
        "remaining_agents": max(quota_agents - used_agents, 0),
        "expires_at": expires_at,
    }


def check_quota(conn, account_id: str, kind: str) -> Tuple[bool, dict]:
    """kind: "msg" | "agent"。超了返回 (False, {"detail":"额度用尽", ...})，调用方回 HTTP 402。"""
    if kind not in ("msg", "agent"):
        raise ValueError("kind 只能是 msg 或 agent")
    u = get_usage(conn, account_id)
    remaining = u["remaining_msgs"] if kind == "msg" else u["remaining_agents"]
    if remaining <= 0:
        return False, {"detail": "额度用尽", "period": u["period"], "remaining": 0}
    quota = u["quota_msgs"] if kind == "msg" else u["quota_agents"]
    return True, {"period": u["period"], "remaining": remaining, "quota": quota}


def record_usage(conn, account_id: str, kind: str, n: int = 1) -> None:
    """按投递计量：一条消息投给 N 个成员就记 N 条。kind: "msg" | "agent"。"""
    if kind not in ("msg", "agent"):
        raise ValueError("kind 只能是 msg 或 agent")
    if n < 1:
        return
    period = current_period()
    conn.execute(
        "INSERT OR IGNORE INTO usage_counters (account_id, period, msgs, agents) VALUES (?,?,0,0)",
        (account_id, period),
    )
    col = "msgs" if kind == "msg" else "agents"  # 白名单，无注入风险
    conn.execute(
        "UPDATE usage_counters SET %s = %s + ? WHERE account_id = ? AND period = ?" % (col, col),
        (n, account_id, period),
    )
    conn.commit()


def guard_quota(conn, account_id: Optional[str], kind: str) -> None:
    """平台接线的 402 卡口：超限直接抛 HTTP 402（detail = check_quota 的 dict）。
    account_id 为 None（管理员/旧数据）不受限。"""
    if account_id is None:
        return
    ok, info = check_quota(conn, account_id, kind)
    if not ok:
        raise HTTPException(status_code=402, detail=info)


# ---------------------------------------------------------------- 账号限速（每分钟 ≤60 条）

_rate_lock = threading.Lock()
_rate_hits: Dict[str, List[float]] = {}  # account_id -> 窗口内命中时间戳（内存滑动窗口）


def rate_limit_guard(account_id: Optional[str], limit: int = RATE_LIMIT_MAX,
                     window: float = RATE_WINDOW_SEC) -> None:
    """账号维度限速：窗口内 ≥ limit 次就抛 429 + Retry-After。account_id 为 None 不限。"""
    if account_id is None:
        return
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(account_id, ()) if now - t < window]
        if len(hits) >= limit:
            retry_after = max(int(hits[0] + window - now) + 1, 1)
            raise HTTPException(
                status_code=429,
                detail="消息发送太频繁（每分钟 ≤ %d 条）" % limit,
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)
        _rate_hits[account_id] = hits


class RateLimitMiddleware(BaseHTTPMiddleware):
    """对 RATE_LIMITED_PATHS 的 POST 按账号限速；管理员（人 token）不限。
    挂载：app.add_middleware(billing.RateLimitMiddleware)"""

    async def dispatch(self, request, call_next):
        if request.method == "POST" and request.url.path in RATE_LIMITED_PATHS:
            token = _token_of(request)
            who = _who_token(token) if token else None
            if who is not None and who["kind"] == "agent":
                try:
                    rate_limit_guard(who["id"])
                except HTTPException as e:
                    return JSONResponse(
                        {"detail": e.detail, "retry_after": e.headers.get("Retry-After", "1")},
                        status_code=429,
                        headers={"Retry-After": e.headers.get("Retry-After", "1")},
                    )
        return await call_next(request)


def _token_of(request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ---------------------------------------------------------------- 鉴权（与 main.py 同口径）

def _who_token(token: str) -> Optional[Dict[str, str]]:
    """token → {"kind": "human"|"agent", "id", "name"}；无效返回 None。"""
    if not token:
        return None
    if secrets.compare_digest(token, _CTX["user_token"] or ""):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _CTX["q1"]("SELECT id, name FROM agents WHERE token = ?", (token,)) if _CTX["q1"] else None
    if row is None:
        return None
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


def _authenticate(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    who = _who_token(authorization[7:].strip())
    if who is None:
        raise HTTPException(status_code=401, detail="token 无效")
    return who


def _require_human(who: Dict[str, str] = Depends(_authenticate)) -> Dict[str, str]:
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="此操作只允许人（管理员）执行")
    return who


def _conn():
    if _CTX["db"] is None:
        raise HTTPException(status_code=503, detail="billing 未挂载（attach 未调用）")
    return _CTX["db"]


# ---------------------------------------------------------------- HTTP 端点

class CouponIn(BaseModel):
    code: Optional[str] = None
    days: int = Field(14, ge=1)
    msgs: int = Field(2000, ge=1)


class RedeemIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


@router.post("/api/coupons")
def post_coupons(body: CouponIn, who: Dict[str, str] = Depends(_require_human)) -> Dict[str, str]:
    with _CTX["lock"]:
        code = create_coupon(_conn(), code=body.code, days=body.days, msgs=body.msgs)
    return {"code": code}


@router.post("/api/redeem")
def post_redeem(body: RedeemIn, who: Dict[str, str] = Depends(_authenticate)) -> Dict[str, object]:
    with _CTX["lock"]:
        return redeem_coupon(_conn(), body.code, who["id"])


@router.get("/api/usage")
def get_usage_api(who: Dict[str, str] = Depends(_authenticate)) -> Dict[str, object]:
    with _CTX["lock"]:
        return get_usage(_conn(), who["id"])


@router.get("/v1/usage")
def v1_usage(
    period: Optional[str] = Query(default=None),
    who: Dict[str, str] = Depends(_authenticate),
) -> Dict[str, object]:
    """简化接入版用量（账号或 agent token），口径与 /api/usage 完全一致。"""
    period = (period or "").strip() or current_period()
    if not PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period 格式应为 YYYY-MM，如 2026-09")
    with _CTX["lock"]:
        return get_usage(_conn(), who["id"])
