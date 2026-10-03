"""alerts —— 指标告警（#14，2026-09-30）

把 metrics 里的异常主动喊出来。GET /v1/alerts（需 Authorization: Bearer <human 或任一 agent token>）
返回告警列表；POST /v1/alerts/rule（仅人 token）改阈值。

════════════════════════════════════════════ 口径（写死在代码里的定义）════════════════════════════════════════════

四类告警（类型名 type）：

① heartbeat_stale —— 某 Agent 超阈值时间没有心跳（守候挂了）
   判定：presence 表里存在该 agent 的行，且 last_seen 早于 now − heartbeat_max_min 分钟。
   注意：没有 presence 行的 agent（从未上过线）不判挂，只在有历史心跳后消失才报。
   since = 它最后一次心跳 last_seen。value = 距最后心跳的分钟数。
   action：检查 agentpost@<agent_name> 是否 active，必要时重启它的守候进程。

② latency_p95_high —— 投递延迟 p95 超阈值
   判定：metrics 口径 2 的 delivery_latency_s.p95_s（最近 100 条消息，写入 → 接收方心跳
   的上界估计）大于 latency_p95_max_s 秒；p95 为 null（没有样本）不报。
   since = 现在（该值本身是滚动估计，无单一事发时刻）。value = p95 秒。
   action：投递变慢，检查接收方是否在正常拉 inbox、积压（GET /v1/metrics）是否在涨。

③ throughput_zero —— 连续 zero_hours 小时（默认 3）无新消息才报（告警降噪改）
   判定：过去 24 小时内有消息，但最近 zero_hours 小时一条都没有。
   since = 24h 窗口内最后一条消息的 created_at。value = 距最后一条消息的小时数。
   action：连续 N 小时无消息但 24h 内有流量，检查对接方发送链路是否停摆。
   阈值理由：产品现在没人用的时候多，单个 1 小时空窗很常见（深夜、周末），
   报出来是噪声；连续 3 小时全空才算「链路可能停摆」，值得看一眼。

④ failures_positive —— 失败计数 > 0
   判定：metrics 口径 3 的 failures.count（24h 内 from_kind 脏数据 + healthcheck FAIL 行）> failures_max。
   since = 现在（无单一事发时刻）。value = count。
   action：有写入异常/健康检查失败，查 GET /v1/metrics 的 failures 分项与 healthcheck 日志。

⑤ stale_delivery —— 超时未取走积压（#15 新增）
   判定：metrics 口径 5 的 stale_delivery.count（写入超过 stale_hours 小时且无接收方
   在写入后活跃过的消息数）> stale_delivery_max。
   since = 最老一条未取走消息的 created_at。value = count。
   action：检查收件方是否接入/守候是否活着。

各阈值默认值及理由：
- heartbeat_max_min = 30：守候进程有 3s 自拉兜底，30 分钟没心跳基本就是挂了；
  比 30 小（如 10 分钟）会把网络抖动当故障。
- latency_p95_max_s = 1800：延迟用接收方心跳做取走上界估计，30 分钟是「投了
  半小时还没被取走」的线，再慢就该查接收方拉取节奏；阈值下限受心跳粒度限制。
- zero_hours = 720：见 ③。产品当前没有真实用户，"连续 N 小时没消息"是常态；先静音到 30 天，有真实用户后调回 12。
- failures_max = 0：failures 只计脏数据与 healthcheck FAIL 行，正常时就是 0，
  任何非零都值得查，不降噪。
- stale_delivery_max = 0 / stale_hours = 24（告警降噪改）：原 6 小时对低频使用者
  太紧——晚上或周末发的消息隔天取很常见，6 小时未取走报出来是噪声；放宽到
  24 小时没人取走才像「接收方没接入/守候挂了」。且守候挂掉本身 30 分钟就会被
  ① 抓到，这条只兜「人不在、消息没人取」的积压，24 小时起报不丢事。

阈值规则（POST /v1/alerts/rule，人 token）：heartbeat_max_min / latency_p95_max_s /
zero_hours / failures_max / stale_delivery_max / stale_hours，可只传要改的字段。
规则存内存（_S["rules"]），服务重启回默认值——持久化等有需要再接库。

注入约定同 api_v1.py / metrics.py：main.py 调 attach(db=, lock=, user_token=, q=, q1=, ex=, ...)。
本模块只读（q / q1），并把 attach 转发给 metrics（复用它口径 2/3 的计算函数）。
"""

from __future__ import annotations
import os

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from metrics import _failures, _latency, _parse_ts, _q, _q1, _stale_delivery  # noqa: E402

router = APIRouter()

_S: Dict[str, object] = {}
DEFAULT_RULES = {
    "heartbeat_max_min": 30,     # ① 心跳超时（分钟）
    "latency_p95_max_s": 1800,   # ② 延迟 p95 阈值（秒）
    "zero_hours": int(os.environ.get("ALERTS_ZERO_HOURS", 3)),  # 生产用 ALERTS_ZERO_HOURS 覆盖（无用户阶段静音）   # 无用户阶段静音（有真实用户后调回 12 或 3，见 devlog 2026-10-03）             # ③ 连续 N 小时全 0 才报吞吐停摆（小时，降噪改）
    "failures_max": 0,           # ④ 失败计数上限
    "stale_delivery_max": 0,     # ⑤ 超时未取走条数上限（#15）
    "stale_hours": 24,           # ⑤ 未取走判定的小时数（#15；6→24 降噪改，理由见文件头）
}


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）；转发给 metrics 复用其口径函数。"""
    import metrics
    metrics.attach(**state)
    _S.update(state)
    _S.setdefault("rules", dict(DEFAULT_RULES))


def _rules() -> Dict[str, object]:
    r = _S.get("rules")
    return r if isinstance(r, dict) else DEFAULT_RULES  # type: ignore[return-value]


# ---------------------------------------------------------------- 鉴权（同 api_v1 / metrics 的 me()）

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


def require_human(who: Dict[str, str] = Depends(me)) -> Dict[str, str]:
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="此操作只允许人（人 token）执行")
    return who


# ---------------------------------------------------------------- 四类判定

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_alerts(now: datetime, rules: Dict[str, object]) -> List[Dict[str, object]]:
    cutoff = (now - timedelta(minutes=float(rules["heartbeat_max_min"]))).isoformat(timespec="seconds")
    rows = _q("SELECT agent_id, last_seen FROM presence WHERE last_seen < ?", (cutoff,))
    names = {r["id"]: r["name"] for r in _q("SELECT id, name FROM agents")}
    out: List[Dict[str, object]] = []
    for r in rows:
        seen = _parse_ts(r["last_seen"])
        minutes = round((now - seen).total_seconds() / 60, 1) if seen else None
        out.append({
            "type": "heartbeat_stale",
            "subject": names.get(r["agent_id"], r["agent_id"]),
            "value": minutes,
            "threshold": float(rules["heartbeat_max_min"]),
            "since": r["last_seen"],
            "action": f"检查 agentpost@{names.get(r['agent_id'], r['agent_id'])} 是否 active，必要时重启它的守候进程",
        })
    return out


def _latency_alerts(now: datetime, rules: Dict[str, object]) -> List[Dict[str, object]]:
    lat = _latency(now)
    p95 = lat.get("p95_s")
    if not isinstance(p95, (int, float)):
        return []  # 没有样本（null）不报
    thr = float(rules["latency_p95_max_s"])
    if p95 <= thr:
        return []
    return [{
        "type": "latency_p95_high",
        "subject": "全站",
        "value": round(float(p95), 1),
        "threshold": thr,
        "since": now.isoformat(timespec="seconds"),
        "action": "投递变慢，检查接收方是否在正常拉 inbox、积压（GET /v1/metrics）是否在涨",
    }]


def _throughput_alerts(now: datetime, rules: Dict[str, object]) -> List[Dict[str, object]]:
    hours = int(rules["zero_hours"])
    since_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    since_win = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    last24 = _q1("SELECT created_at FROM messages WHERE created_at >= ? ORDER BY seq DESC LIMIT 1", (since_24h,))
    if last24 is None:
        return []  # 24h 内没消息：不是停摆，是没接入（口径见文件头）
    recent = _q1("SELECT 1 FROM messages WHERE created_at >= ? LIMIT 1", (since_win,))
    if recent is not None:
        return []  # 最近 N 小时内有消息：不报（1 小时空窗是正常没人用）
    last = _parse_ts(last24["created_at"])
    idle_h = round((now - last).total_seconds() / 3600, 1) if last else None
    return [{
        "type": "throughput_zero",
        "subject": "全站",
        "value": idle_h,
        "threshold": float(hours),
        "since": last24["created_at"],
        "action": f"连续 {hours} 小时无消息但 24h 内有流量，检查对接方发送链路是否停摆",
    }]


def _failure_alerts(now: datetime, rules: Dict[str, object]) -> List[Dict[str, object]]:
    f = _failures(now)
    count = int(f.get("count", 0))
    if count <= int(rules["failures_max"]):
        return []
    return [{
        "type": "failures_positive",
        "subject": "全站",
        "value": count,
        "threshold": float(rules["failures_max"]),
        "since": now.isoformat(timespec="seconds"),
        "action": "有写入异常/健康检查失败，查 GET /v1/metrics 的 failures 分项与 healthcheck 日志",
    }]


def _stale_alerts(now: datetime, rules: Dict[str, object]) -> List[Dict[str, object]]:
    sd = _stale_delivery(now, float(rules["stale_hours"]))
    count = int(sd.get("count", 0))
    if count <= int(rules["stale_delivery_max"]):
        return []
    return [{
        "type": "stale_delivery",
        "subject": "全站",
        "value": count,
        "threshold": float(rules["stale_delivery_max"]),
        "since": sd.get("oldest_created_at") or now.isoformat(timespec="seconds"),
        "action": f"有 {count} 条消息超过 {rules['stale_hours']} 小时没人取走，检查收件方是否接入/守候是否活着",
    }]


# ---------------------------------------------------------------- 路由

class RuleIn(BaseModel):
    heartbeat_max_min: Optional[float] = None
    latency_p95_max_s: Optional[float] = None
    zero_hours: Optional[int] = None
    failures_max: Optional[int] = None
    stale_delivery_max: Optional[int] = None
    stale_hours: Optional[float] = None


@router.get("/v1/alerts")
def v1_alerts(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    now = _now()
    rules = _rules()
    alerts = (_heartbeat_alerts(now, rules) + _latency_alerts(now, rules)
              + _throughput_alerts(now, rules) + _failure_alerts(now, rules)
              + _stale_alerts(now, rules))
    return {"at": now.isoformat(timespec="seconds"), "rules": rules, "alerts": alerts}


@router.post("/v1/alerts/rule")
def v1_alerts_rule(body: RuleIn, who: Dict[str, str] = Depends(require_human)) -> Dict[str, object]:  # type: ignore[assignment]
    rules = dict(_rules())
    for k in ("heartbeat_max_min", "latency_p95_max_s", "zero_hours", "failures_max",
              "stale_delivery_max", "stale_hours"):
        v = getattr(body, k)
        if v is not None:
            rules[k] = v
    _S["rules"] = rules
    return {"rules": rules}
