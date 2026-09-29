#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
温暖通信台 · 计费与免费券（P1-B）

独立模块，自包含。挂载方式（main.py 不动，由平台侧加三行）：

    import billing
    billing.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q1=q1)
    app.include_router(billing.router)

计量口径（按投递计）：
  一条消息投给 N 个成员，由调用方在投递后调
  record_usage(conn, account_id, "msg", n=N) 记 N 条；
  周期按自然月记账（本地时间），usage_counters 主键 (account_id, period)，
  跨月后当月计数归零、额度不结转。
  发消息前调 check_quota(conn, account_id, "msg")，返回 False 时调用方回 HTTP 402。

配额口径：
  券给 msgs 条消息额度 + 固定 AGENTS_QUOTA_DEFAULT 个 agent 名额；
  一个账号兑换多张券：msgs 额度求和、有效期取最晚那张；
  券过期（redeemed_at + days 天 < 现在）后不再贡献额度。
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

AGENTS_QUOTA_DEFAULT = 10
PERIOD_FMT = "%Y-%m"

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


# ---------------------------------------------------------------- 鉴权（与 main.py 同口径）

def _authenticate(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")
    if secrets.compare_digest(token, _CTX["user_token"] or ""):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _CTX["q1"]("SELECT id, name FROM agents WHERE token = ?", (token,)) if _CTX["q1"] else None
    if row is None:
        raise HTTPException(status_code=401, detail="token 无效")
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


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
