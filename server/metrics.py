"""metrics —— 邮局运行指标（2026-09-29）

目标：把「不丢」量化。GET /v1/metrics（需 Authorization: Bearer <human 或任一 agent token>）。

════════════════════════════════════════════ 口径（写死在代码里的定义）════════════════════════════════════════════

1. backlog（积压，每条：窗口 3600 秒）
   每个 agent：最近 1 小时（按消息 created_at）内写入、且投递给它的消息条数——
   它是消息所在会话的成员、且发送者不是它。
   背景：服务端不记录取走游标（/v1/inbox 的 since 是客户端传入的），presence 表只有
   心跳 last_seen、没有 cursor，精确未读算不出来。所以按 H 给的降级口径，把
   「最近 1 小时投递的新消息」当作未取走上界。字段 window_s 注明窗口。
   返回：{"<agent_name>": {"count": int, "window_s": 3600}, ...}（用名字做键，方便人看）。

2. delivery_latency_s（投递延迟，只统计已被取走的消息）
   样本：最近 100 条消息（按 seq 倒序）。对每条消息的「该收的人」（会话中除发送者外的
   每个成员）取一个样本：
       延迟 = 接收方 presence.last_seen − 消息 created_at（秒）
   背景：服务端不记录取走时刻，只能用接收方最后一次心跳作为「已经取走」的时间上界。
   判定「已被取走」：心跳晚于消息写入，且 心跳 − 写入 ≤ PICKUP_MAX_S（3600 秒）。
   写入后超过 1 小时才有心跳的，视为离线补投/一直没取走，不计入延迟样本——
   它们由第 5 条 stale_delivery 指标统计（#15 拆开两条口径，防止 p95 被无人取走的消息污染）。
   样本为 0 个 → median/p95 都是 null。
   返回：{"median_s": float|null, "p95_s": float|null, "n": int, "pickup_max_s": 3600}。

3. failures（失败/异常，最近 24 小时）
   两个来源求和：
   a) messages 里最近 24 小时 from_kind 不在 {human, agent} 的脏数据条数（写入侧异常）；
   b) healthcheck 日志里最近 24 小时的 FAIL 行数——日志路径取环境变量
      WARM_HUB_HC_LOG，缺省 /var/log/warm-hub-healthcheck.log（与 healthcheck.sh 一致）；
      日志文件不存在或读不了时该来源记 0。
   返回：{"count": int, "from_kind_bad": int, "hc_fail": int}。

4. throughput_hourly（吞吐）
   最近 24 个完整 UTC 小时（含当前小时）每小时 messages 写入条数，缺失小时补 0。
   返回：{"YYYY-MM-DDTHH": int, ...} 恰好 24 个键（键是 UTC 小时起点）。

5. stale_delivery（超时未取走，#15 新增）
   写入超过 stale_hours 小时（默认 6）、且没有任何接收方在写入后活跃过
   （presence.last_seen ≥ created_at）的消息——一直没人取走。
   只对「会话里有 agent 接收方」的消息判（human 发的一对一会话若只有人收，不算）。
   返回：{"count": int, "oldest_created_at": str|null, "stale_hours": number}。

注入约定同 api_v1.py：main.py 调 attach(db=, lock=, user_token=, q=, q1=, ex=, ...)。
本模块只读（q / q1），不写库、不加锁。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

router = APIRouter()

_S: Dict[str, object] = {}


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）。"""
    _S.update(state)


def _q(sql: str, args=()) -> List[sqlite3.Row]:
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    return _S["q1"](sql, args)  # type: ignore[operator]


# ---------------------------------------------------------------- 鉴权（同 api_v1 的 me()）

def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <token>")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is None:
        raise HTTPException(status_code=401, detail="token 无效")
    return {"kind": "agent", "id": row["id"], "name": row["name"]}


# ---------------------------------------------------------------- 工具

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str) -> Optional[datetime]:
    """'2026-09-29T01:10:51+00:00' → aware datetime；解析不了返回 None。"""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _median(v: List[float]) -> Optional[float]:
    if not v:
        return None
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _p95(v: List[float]) -> Optional[float]:
    if not v:
        return None
    s = sorted(v)
    # 最近邻排序法：第 ceil(0.95*n) 个（1 基）
    idx = max(0, min(len(s) - 1, int(0.95 * len(s) + 0.999999) - 1))
    return s[idx]


# ---------------------------------------------------------------- 指标

def _backlog(now: datetime) -> Dict[str, Dict[str, object]]:
    """每个 agent 最近 1 小时投递给它的消息条数（未取走上界，见文件头口径 1）。"""
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    rows = _q(
        """SELECT m.from_id, m.conversation_id, me.agent_id
           FROM messages m
           JOIN members me ON me.conversation_id = m.conversation_id
           WHERE m.created_at >= ? AND me.agent_id != m.from_id""",
        (since,),
    )
    names = {r["id"]: r["name"] for r in _q("SELECT id, name FROM agents")}
    out: Dict[str, Dict[str, object]] = {}
    for r in rows:
        aid = r["agent_id"]
        name = names.get(aid, aid)
        slot = out.setdefault(name, {"count": 0, "window_s": 3600})
        slot["count"] = int(slot["count"]) + 1
    return out


PICKUP_MAX_S = 3600  # 「已被取走」判定窗口（秒）：写入 → 心跳超过它，视为一直没取走（#15）


def _latency(now: datetime) -> Dict[str, object]:
    """最近 100 条消息的投递延迟中位数/p95（只统计已被取走的，见文件头口径 2）。"""
    rows = _q(
        """SELECT m.seq, m.conversation_id, m.from_id, m.created_at
           FROM messages m ORDER BY m.seq DESC LIMIT 100"""
    )
    pres = {r["agent_id"]: r["last_seen"] for r in _q("SELECT agent_id, last_seen FROM presence")}
    samples: List[float] = []
    for r in rows:
        created = _parse_ts(r["created_at"])
        if created is None:
            continue
        recvs = _q("SELECT agent_id FROM members WHERE conversation_id = ? AND agent_id != ?",
                   (r["conversation_id"], r["from_id"]))
        for rec in recvs:
            seen = pres.get(rec["agent_id"])
            if not seen:
                continue
            seen_dt = _parse_ts(seen)
            if seen_dt is None or seen_dt < created:
                continue  # 心跳早于写入：还没取走，跳过
            lag = (seen_dt - created).total_seconds()
            if lag > PICKUP_MAX_S:
                continue  # 写入后超窗口才有心跳：离线补投/没取走，不计延迟（#15）
            samples.append(lag)
    return {"median_s": _median(samples), "p95_s": _p95(samples), "n": len(samples),
            "pickup_max_s": PICKUP_MAX_S}


def _stale_delivery(now: datetime, stale_hours: float = 6) -> Dict[str, object]:
    """超时未取走的消息（#15，见文件头口径 5）。"""
    cutoff = (now - timedelta(hours=stale_hours)).isoformat(timespec="seconds")
    rows = _q(
        """SELECT m.seq, m.conversation_id, m.from_id, m.created_at
           FROM messages m WHERE m.created_at < ? ORDER BY m.seq ASC""",
        (cutoff,),
    )
    pres = {r["agent_id"]: r["last_seen"] for r in _q("SELECT agent_id, last_seen FROM presence")}
    stale: List[str] = []
    for r in rows:
        recvs = _q("SELECT agent_id FROM members WHERE conversation_id = ? AND agent_id != ?",
                   (r["conversation_id"], r["from_id"]))
        if not recvs:
            continue  # 没有 agent 接收方的会话（纯人）不判未取走
        taken = False
        for rec in recvs:
            seen = pres.get(rec["agent_id"])
            seen_dt = _parse_ts(seen) if seen else None
            created = _parse_ts(r["created_at"])
            if seen_dt is not None and created is not None and seen_dt >= created:
                taken = True
                break
        if not taken:
            stale.append(r["created_at"])
    return {"count": len(stale), "oldest_created_at": stale[0] if stale else None,
            "stale_hours": stale_hours}


def _failures(now: datetime) -> Dict[str, int]:
    """最近 24 小时失败/异常计数（见文件头口径 3）。"""
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    bad_kind = _q1(
        "SELECT COUNT(*) AS c FROM messages WHERE created_at >= ? AND from_kind NOT IN ('human','agent')",
        (since,),
    )
    from_kind_bad = int(bad_kind["c"]) if bad_kind else 0
    hc_fail = 0
    log_path = os.environ.get("WARM_HUB_HC_LOG", "/var/log/warm-hub-healthcheck.log")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if "FAIL" not in line:
                    continue
                ts = _parse_ts(line[:19].replace(" ", "T"))  # 'YYYY-MM-DD HH:MM:SS' → T
                if ts is not None and ts >= since_dt(now):
                    hc_fail += 1
    except OSError:
        hc_fail = 0  # 日志不存在/读不了：该来源记 0（文件头口径已注明）
    return {"count": from_kind_bad + hc_fail, "from_kind_bad": from_kind_bad, "hc_fail": hc_fail}


def since_dt(now: datetime) -> datetime:
    return now - timedelta(hours=24)


def _throughput(now: datetime) -> Dict[str, int]:
    """最近 24 个完整 UTC 小时每小时消息数，缺失小时补 0（见文件头口径 4）。"""
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    cnt: Dict[str, int] = {}
    for r in _q(
        "SELECT substr(created_at, 1, 13) AS h, COUNT(*) AS c FROM messages WHERE created_at >= ? GROUP BY h",
        (since,),
    ):
        cnt[r["h"]] = int(r["c"])
    out: Dict[str, int] = {}
    base = now.replace(minute=0, second=0, microsecond=0)
    for i in range(23, -1, -1):
        h = (base - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        out[h] = cnt.get(h, 0)
    return out


# ---------------------------------------------------------------- 路由

@router.get("/v1/metrics")
def v1_metrics(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    now = _now()
    return {
        "at": now.isoformat(timespec="seconds"),
        "backlog": _backlog(now),
        "delivery_latency_s": _latency(now),
        "stale_delivery": _stale_delivery(now),
        "failures": _failures(now),
        "throughput_hourly": _throughput(now),
    }
