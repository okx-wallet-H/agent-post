"""review —— 群里评议机制（#31，2026-10-04，再补一层：评议=让人更近，不是挑毛病）

群聊三级机制的第 3 级：结论型消息（交付/结论/方案）进「待评议」，由相关岗位的
Agent 评议——评议的目的是互相进步，三种立场（不做打分）：
  ① 附议 stance=agree：认同，可加一句补充
  ② 建议 stance=improve（最重要）：我有个更近/更好的办法 + 具体做法（方案片段、
     参数、实现路径）
  ③ 异议 stance=disagree：我认为这不对 + 理由（要具体，不说「不认同」）
只有 disagree 算分歧（进 /v1/conflicts 需裁决）；improve 单独汇总到
GET /v1/reviews/improvements（协作最主要的产出，供集成者并进方案）。

════════════════════════════════════════════ 口径（写死在代码里的定义）════════════════════════════════════════════

存储（本模块 attach 时自动迁移，不改 main.py 的 SCHEMA）：
- messages.review  TEXT NULL —— 待评议标记。NULL = 普通消息；非 NULL = 显式请求
  评议（review_requested）。可以是简单标记（如 "1"）或 JSON {"by": ["乙", ...]}
  （显式 review_by 指定评议人，守候侧 #29 按 by 唤醒）。
- messages.reviews TEXT NULL —— 评议记录 JSON 数组：
  [{"who": "乙", "stance": "agree"|"improve"|"disagree", "reason": "理由/建议正文", "at": ISO时间}]

结论型消息判定（GET pending 用）：
  ① text 以「交付：」「结论：」「方案：」开头；或 ② review 列非 NULL（显式请求）。
  日常通知（巡检、测试等）不带前缀也不显式请求 → 不进待评议。

GET /v1/reviews/pending?agent=名字 —— 列出「我需要评议的消息」：
  我 = 该 agent（agent token 查自己；人 token 可查任意 agent）。
  规则：我在消息所在会话的 members 里、不是我发的、我还没评过（reviews 里没有
  我的名字或 id）、且是结论型消息。每条返回带 wake 字段（这条消息 @ 了谁 +
  review.by 点名谁，供 #29 守候侧唤醒用），@全体 也标。

POST /v1/messages/{id}/review —— 给一条消息评议：body {"stance": "agree|improve|disagree",
  "reason": "..."}（reason 对三种立场分别是补充/更好做法/具体理由）。要求：消息存在；
  agent 必须是该会话成员（人全权）；不能评自己发的。同一人重复评 → 覆盖旧记录。

GET /v1/conflicts —— 分歧检测：只列 reviews 里有 stance=disagree 的消息（供集成者/
  人类裁决），按最新 disagree 时间倒序。

GET /v1/reviews/improvements —— 汇总所有 stance=improve 的评议（大家提的更好做法，
  按最新 improve 时间倒序），供集成者并进方案；这是协作最主要的产出。

GET /v1/group-digest?since=&agent= —— 值班巡视用：自 since（seq 游标）以来该 Agent
  所在群的新消息摘要（seq/谁发的/正文前 200 字/mentions），鉴权/越权同 /v1/inbox，
  不含自己发的；latest 供下次 since 接着游。

注入约定同 alerts.py：main.py 调 review.attach(db=, lock=, user_token=, q=, q1=, ex=,
now_iso=...)，attach 时自动给 messages 表补 review/reviews 两列（幂等）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}

PREFIXES = ("交付：", "结论：", "方案：")   # 结论型消息前缀


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）；自动迁移 messages 表两列（幂等）。"""
    _S.update(state)
    # 迁移：messages 加 review / reviews 列（没有才加；已有数据行新列为 NULL，不影响旧消息）
    cols = [r[1] for r in _q("PRAGMA table_info(messages)")]
    if "review" not in cols:
        _ex("ALTER TABLE messages ADD COLUMN review TEXT")
    if "reviews" not in cols:
        _ex("ALTER TABLE messages ADD COLUMN reviews TEXT")


def _q(sql: str, args=()):
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()):
    return _S["ex"](sql, args)  # type: ignore[operator]


def _now_iso() -> str:
    return _S["now_iso"]() if "now_iso" in _S else datetime.now(timezone.utc).isoformat(timespec="seconds")  # type: ignore[operator]


# ---------------------------------------------------------------- 鉴权（同 api_v1 的 _who/me）

def _who(token: str) -> Optional[Dict[str, str]]:
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


def _find_agent(name: str) -> Optional[sqlite3.Row]:
    """按名字（或 id）找 Agent；重名时报错提示用 id。"""
    row = _q1("SELECT id, name FROM agents WHERE id = ?", (name,))
    if row is not None:
        return row
    rows = _q("SELECT id, name FROM agents WHERE name = ?", (name,))
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(status_code=409, detail=f"名字 {name} 有多个，请改用 id")
    return rows[0]


# ---------------------------------------------------------------- 判定工具

def _is_conclusion(m: sqlite3.Row) -> bool:
    """结论型消息：带前缀 或 显式 review 标记。"""
    if m["review"]:
        return True
    t = (m["text"] or "").strip()
    return any(t.startswith(p) for p in PREFIXES)


def _reviews_of(m: sqlite3.Row) -> List[dict]:
    try:
        revs = json.loads(m["reviews"]) if m["reviews"] else []
        return revs if isinstance(revs, list) else []
    except Exception:
        return []


def _mentions_of(m: sqlite3.Row) -> List[str]:
    """这条消息点名了谁（含 @全体）。优先读 messages.mentions 列（api_v1 #29 的
    发送路径/触发器维护）；列值为 NULL（临时库直插、无触发器）时退回正文 @ 解析。"""
    try:
        raw = m["mentions"]
    except (IndexError, KeyError):
        raw = None
    if raw:
        out: List[str] = []
        for x in raw.split(","):
            x = x.strip()
            if x in ("all", "everyone"):
                x = "全体"
            if x and x not in out:
                out.append(x)
        return out
    if raw == "":
        return []  # 列存在且空：确实没人被点名
    # fallback：正文解析（@名字 或 @全体）
    text = m["text"] or ""
    out = []
    for r in _q("SELECT name FROM agents"):
        if f"@{r['name']}" in text and r["name"] not in out:
            out.append(r["name"])
    if "@全体" in text and "全体" not in out:
        out.append("全体")
    return out


def _wake_of(m: sqlite3.Row) -> List[str]:
    """这条消息 @ 了谁 / review.by 点名了谁（#29 守候侧按此唤醒）。"""
    out = _mentions_of(m)
    if m["review"]:
        try:
            rv = json.loads(m["review"])
            if isinstance(rv, dict):
                for b in rv.get("by") or []:
                    if b not in out:
                        out.append(b)
        except Exception:
            pass
    return out


def _conv_title(cid: str) -> str:
    row = _q1("SELECT title FROM conversations WHERE id = ?", (cid,))
    return row["title"] if row else cid


def _msg_out(m: sqlite3.Row) -> Dict[str, object]:
    return {
        "id": m["id"], "seq": m["seq"], "conversation": _conv_title(m["conversation_id"]),
        "conversation_id": m["conversation_id"],
        "from_kind": m["from_kind"], "from_id": m["from_id"],
        "text": m["text"], "created_at": m["created_at"],
        "review": m["review"], "wake": _wake_of(m), "reviews": _reviews_of(m),
    }


# ---------------------------------------------------------------- 路由

@router.get("/v1/reviews/pending")
def v1_reviews_pending(agent: str = "", who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """列出「我需要评议的消息」：结论型、在我在的会话里、不是我发的、我还没评过。"""
    if who["kind"] == "human":
        if not agent:
            raise HTTPException(status_code=400, detail="人查 pending 要带 agent=名字")
        target = _find_agent(agent)
        if target is None:
            raise HTTPException(status_code=404, detail=f"没有这个 Agent：{agent}")
    else:
        if agent and agent not in (who["name"], who["id"]):
            raise HTTPException(status_code=403, detail="只能查自己的 pending")
        target = _q1("SELECT id, name FROM agents WHERE id = ?", (who["id"],))

    rows = _q(
        "SELECT m.* FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id "
        "WHERE mb.agent_id = ? AND NOT (m.from_kind = 'agent' AND m.from_id = ?) "
        "ORDER BY m.seq DESC LIMIT 200",
        (target["id"], target["id"]),
    )
    out: List[Dict[str, object]] = []
    for m in rows:
        if not _is_conclusion(m):
            continue
        mine = (who["name"], who["id"])
        if any(r.get("who") in mine for r in _reviews_of(m)):
            continue  # 我评过了
        out.append(_msg_out(m))
    return {"agent": target["name"], "pending": out}


class ReviewIn(BaseModel):
    stance: str
    reason: Optional[str] = None


@router.post("/v1/messages/{mid}/review")
def v1_message_review(mid: str, body: ReviewIn, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """给一条消息评议：stance=agree|improve|disagree + reason（补充/更好做法/具体理由）；
    同人重复评覆盖旧记录。"""
    stance = (body.stance or "").strip()
    if stance not in ("agree", "improve", "disagree"):
        raise HTTPException(status_code=400, detail="stance 只能是 agree / improve / disagree")
    reason = (body.reason or "").strip()
    m = _q1("SELECT * FROM messages WHERE id = ?", (mid,))
    if m is None:
        raise HTTPException(status_code=404, detail=f"没有这条消息：{mid}")
    if who["kind"] == "agent":
        if m["from_kind"] == "agent" and m["from_id"] == who["id"]:
            raise HTTPException(status_code=400, detail="别评自己发的消息")
        ok = _q1(
            "SELECT 1 FROM members WHERE conversation_id = ? AND agent_id = ? LIMIT 1",
            (m["conversation_id"], who["id"]),
        )
        if ok is None:
            raise HTTPException(status_code=403, detail="你不是这个会话的成员，不能评议")
    revs = [r for r in _reviews_of(m) if r.get("who") not in (who["name"], who["id"])]
    revs.append({"who": who["name"], "stance": stance, "reason": reason, "at": _now_iso()})
    _ex("UPDATE messages SET reviews = ? WHERE id = ?",
        (json.dumps(revs, ensure_ascii=False), mid))
    m2 = _q1("SELECT * FROM messages WHERE id = ?", (mid,))
    if m2 is None:
        raise HTTPException(status_code=500, detail="评议写入后读不回，请重试")
    return {"ok": True, "message": _msg_out(m2)}


@router.get("/v1/conflicts")
def v1_conflicts(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """分歧检测：只列 reviews 里有 stance=disagree 的消息（agree/improve 不算分歧），
    供集成者/人类裁决。"""
    rows = _q("SELECT * FROM messages WHERE reviews IS NOT NULL AND reviews != '' AND reviews != '[]' ORDER BY seq DESC LIMIT 500")
    out: List[Dict[str, object]] = []
    for m in rows:
        revs = _reviews_of(m)
        dis = [r for r in revs if r.get("stance") == "disagree"]
        if not dis:
            continue
        agrs = [r for r in revs if r.get("stance") == "agree"]
        out.append({
            "id": m["id"], "seq": m["seq"], "conversation": _conv_title(m["conversation_id"]),
            "conversation_id": m["conversation_id"], "text": m["text"],
            "created_at": m["created_at"],
            "disagrees": dis, "agree_count": len(agrs), "reviews": revs,
        })
    out.sort(key=lambda x: max((r.get("at") or "") for r in x["disagrees"]), reverse=True)
    return {"conflicts": out}


@router.get("/v1/reviews/improvements")
def v1_reviews_improvements(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """汇总所有 stance=improve 的评议（大家提的更好做法），按最新 improve 时间倒序，
    供集成者并进方案——协作最主要的产出。"""
    rows = _q("SELECT * FROM messages WHERE reviews IS NOT NULL AND reviews != '' AND reviews != '[]' ORDER BY seq DESC LIMIT 500")
    out: List[Dict[str, object]] = []
    for m in rows:
        revs = _reviews_of(m)
        imps = [r for r in revs if r.get("stance") == "improve"]
        if not imps:
            continue
        out.append({
            "id": m["id"], "seq": m["seq"], "conversation": _conv_title(m["conversation_id"]),
            "conversation_id": m["conversation_id"], "text": m["text"],
            "created_at": m["created_at"], "improvements": imps,
        })
    out.sort(key=lambda x: max((r.get("at") or "") for r in x["improvements"]), reverse=True)
    return {"improvements": out}


@router.get("/v1/group-digest")
def v1_group_digest(since: int = 0, agent: str = "",
                    who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """值班巡视用：自 since（seq 游标）以来该 Agent 所在群（kind=group）的新消息摘要。
    每条给 seq / 谁发的 / 正文前 200 字（截断标 …，full_len 给原文长度）/ mentions
    （@ 了谁，含 @全体）。鉴权/越权同 /v1/inbox：agent token 查自己（agent 参数空或
    等于自己），人 token 可查任意 agent；不含自己发的。latest = 本次最大 seq，供下次
    since 接着游。"""
    if who["kind"] == "human":
        if not agent:
            raise HTTPException(status_code=400, detail="人查 digest 要带 agent=名字")
        target = _find_agent(agent)
        if target is None:
            raise HTTPException(status_code=404, detail=f"没有这个 Agent：{agent}")
    else:
        if agent and agent not in (who["name"], who["id"]):
            raise HTTPException(status_code=403, detail="只能查自己的 digest")
        target = _q1("SELECT id, name FROM agents WHERE id = ?", (who["id"],))
    if not isinstance(since, int) or since < 0:
        raise HTTPException(status_code=400, detail="since 要非负整数（seq 游标）")
    rows = _q(
        "SELECT m.* FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE mb.agent_id = ? AND c.kind = 'group' AND m.seq > ? "
        "AND NOT (m.from_kind = 'agent' AND m.from_id = ?) "
        "ORDER BY m.seq LIMIT 200",
        (target["id"], since, target["id"]),
    )
    msgs: List[Dict[str, object]] = []
    for m in rows:
        text = m["text"] or ""
        from_name = "人" if m["from_kind"] == "human" else (
            _q1("SELECT name FROM agents WHERE id = ?", (m["from_id"],)) or {"name": m["from_id"]}
        )["name"]
        msgs.append({
            "seq": m["seq"], "conversation": _conv_title(m["conversation_id"]),
            "conversation_id": m["conversation_id"],
            "from": from_name, "from_kind": m["from_kind"],
            "text": text[:200] + ("…" if len(text) > 200 else ""),
            "full_len": len(text),
            "mentions": _mentions_of(m),
            "created_at": m["created_at"],
        })
    latest = max([r["seq"] for r in rows], default=0)
    return {"agent": target["name"], "since": since, "latest": latest, "messages": msgs}
