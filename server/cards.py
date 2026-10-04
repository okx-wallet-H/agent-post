"""cards —— 岗位 Agent 能力卡（#33，2026-10-05，第一步：服务端只读接口）

老板要「打开控制台就能看到这 7 个家伙各自能干什么、现在在忙什么」。
本模块读 data/agent-cards.json（静态能力）+ 查库合成实时状态，只读不写。

════════════════════════════════════════════ 口径（写死在代码里的定义）════════════════════════════════════════════

静态能力（data/agent-cards.json 的 cards 数组，H 维护）：name / role / machine /
can[] / how_to_call / notes。json 文件每次请求按 mtime 变化重读（改文件即生效）。

实时状态（每张卡按 name 对齐 agents 表 + presence + messages）：
- online：presence.last_seen 距现在 ≤ 120 秒（和「控制台在线」同一个心跳口径；
  没有 presence 行 = 从未上线 = offline）
- last_seen：presence 里的原值（ISO 时间，无则 null）
- msgs_in：近 24h 该 agent 参与会话里别人发的消息数
  （JOIN members 且排除它自己发的）
- msgs_out：近 24h 该 agent 自己发的消息数
- last_message_at / last_message_preview：该 agent 最近一条**自己发的**消息
  时间 + 正文前 80 字（「现在在忙什么」看它最后说了什么；没发过则 null）

鉴权：GET /v1/cards 与 /v1/cards/{name} 都要 Authorization（管理员 token 或
账号 token，同 api_v1._who 的 tenancy 逻辑；agent token 也可读）。

挂载方式（main.py 由 H 加，本模块只管导出）：
    import cards
    cards.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                 tenancy=_t(), now_iso=now_iso)   # 可选：tenancy / now_iso
    app.include_router(cards.router)
数据源路径可用环境变量 CARDS_JSON 覆盖（默认仓库根 data/agent-cards.json）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

router = APIRouter()

_S: Dict[str, object] = {}

ONLINE_MAX_S = 120          # 心跳 ≤120s 算在线（对齐控制台口径）
PREVIEW_LEN = 80            # 最近一条消息正文预览字数

CARDS_FILE = os.environ.get(
    "CARDS_JSON", str(Path(__file__).resolve().parent.parent / "data" / "agent-cards.json")
)

_CACHE: Dict[str, object] = {"mtime": None, "cards": []}   # json 缓存（mtime 变了重读）


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）。"""
    _S.update(state)


def _q(sql: str, args=()):
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _S["q1"](sql, args)  # type: ignore[operator]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- 静态卡

def _load_cards() -> List[dict]:
    """读 data/agent-cards.json；按 mtime 变化重读（改文件即生效）。"""
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
            return _CACHE["cards"]  # type: ignore[return-value]  读坏了用旧缓存兜底
    return _CACHE["cards"]  # type: ignore[return-value]


# ---------------------------------------------------------------- 鉴权（同 api_v1._who）

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
    if row is None:
        # 账号 token（人在网页上用的那把）也要能看能力卡
        acc = _q1("SELECT account_id FROM account_tokens WHERE token = ?", (token,))
        if acc is not None:
            return {"kind": "account", "id": acc["account_id"], "name": "人"}
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


# ---------------------------------------------------------------- 实时状态合成

def _live_state(name: str, now: datetime) -> Dict[str, object]:
    """按卡名对齐 agents/presence/messages，合成一张卡的实时状态。"""
    since_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    a = _q1("SELECT id FROM agents WHERE name = ?", (name,))
    if a is None:
        return {"online": False, "last_seen": None, "msgs_in": 0, "msgs_out": 0,
                "last_message_at": None, "last_message_preview": None}
    aid = a["id"]
    p = _q1("SELECT last_seen FROM presence WHERE agent_id = ?", (aid,))
    seen = _parse_ts(p["last_seen"] if p else None)
    online = seen is not None and (now - seen).total_seconds() <= ONLINE_MAX_S
    msgs_out = int(_q1(
        "SELECT count(*) AS c FROM messages WHERE from_kind='agent' AND from_id = ? AND created_at >= ?",
        (aid, since_24h),
    )["c"])
    msgs_in = int(_q1(
        "SELECT count(*) AS c FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id "
        "WHERE mb.agent_id = ? AND NOT (m.from_kind='agent' AND m.from_id = ?) AND m.created_at >= ?",
        (aid, aid, since_24h),
    )["c"])
    last_out = _q1(
        "SELECT created_at, text FROM messages WHERE from_kind='agent' AND from_id = ? ORDER BY seq DESC LIMIT 1",
        (aid,),
    )
    preview = None
    if last_out is not None:
        t = (last_out["text"] or "").replace("\n", " ").strip()
        preview = t[:PREVIEW_LEN] + ("…" if len(t) > PREVIEW_LEN else "")
    return {
        "online": online,
        "last_seen": p["last_seen"] if p else None,
        "msgs_in": msgs_in,
        "msgs_out": msgs_out,
        "last_message_at": last_out["created_at"] if last_out else None,
        "last_message_preview": preview,
    }


def _card_out(c: dict, now: datetime) -> Dict[str, object]:
    out = dict(c)
    out.update(_live_state(c.get("name", ""), now))
    return out


# ---------------------------------------------------------------- 路由

@router.get("/v1/cards")
def v1_cards(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """全部能力卡（静态 + 实时状态）。"""
    now = _now()
    cards = [_card_out(c, now) for c in _load_cards()]
    return {"at": now.isoformat(timespec="seconds"), "cards": cards}


@router.get("/v1/cards/{name}")
def v1_card(name: str, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """单张能力卡；名字不存在 404。"""
    for c in _load_cards():
        if c.get("name") == name:
            return _card_out(c, _now())
    raise HTTPException(status_code=404, detail=f"没有这张能力卡：{name}")
