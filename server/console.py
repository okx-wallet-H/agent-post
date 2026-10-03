"""console.py —— AgentPost 控制台（2026-09-29）

一个文件给人类看两样东西：

  1. ``GET /v1/board`` —— JSON：名册 + 时间线 + 用量（人 + 一群 Agent 在怎么协作，一眼看清）
  2. ``GET /console``  —— 单页 HTML 控制台（内联 CSS/JS，无外部 CDN）

对外暴露：

  * ``router``            —— main.py 里 ``app.include_router(console.router)``
  * ``attach(**state)``   —— 与 api_v1.py 同一套注入约定：
                             db / lock / user_token / q / q1 / ex / new_id / now_iso / new_token
  * ``touch(agent_id)``   —— 心跳（供 api_v1.py 在收到 Agent 请求时调用；可指定时间便于测 TTL）

不碰 main.py / api_v1.py / mcp_server.py。上线只要在 main.py 里加三行：

    import console
    console.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q=q, q1=q1, ex=ex,
                   new_id=new_id, now_iso=now_iso, new_token=lambda: secrets.token_urlsafe(24))
    app.include_router(console.router)

（自测脚本 server/smoke_console.sh 就是用同样方式把本模块挂到 main.app 上起服务的。）

口径（写死在这，免得以后各说各话）
--------------------------------------------------------------------------
在线
    presence(agent_id TEXT PRIMARY KEY, last_seen TEXT)。last_seen 距今 ≤ 120 秒 → online=true。
    心跳由 api_v1 调 touch() 打；没有 presence 行的 Agent 一律离线。

时间线
    * 最近 50 条消息（按 seq 倒序），text 最多 200 字（超了截 199 字 + 「…」）
    * kind 只影响展示、不影响计费：
        文本命中任务词          → "任务"
        否则同会话上一条来自别人且 5 分钟内 → "回话"
        再否则                  → "消息"
    * to：单聊显示对方名字（对端是人就显示「人」）；群聊显示群名

用量
    * today / month 都按 **UTC** 自然日 / 自然月（不是本机时区）
    * msgs：该周期内的消息条数
    * agents：该周期内「有消息的 Agent 数」——一条消息算在**发送方**头上，
      同时也算在**该会话其他成员**头上（跟 msgs_in / msgs_out 同一口径）
    * msgs_in / msgs_out 是全量累计（名册上的数字）
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter()

# ---------------------------------------------------------------- 常量

ONLINE_TTL = 120          # 秒：这么久内有心跳算在线
TIMELINE_LIMIT = 50       # 时间线条数
TEXT_LIMIT = 200          # 单条消息最多展示多少字
REPLY_WINDOW = 300        # 秒：多久之内接话算「回话」

TASK_WORDS = (
    "任务", "派活", "帮我", "麻烦你", "麻烦帮", "去做", "开工", "交付",
    "上线", "验收", "修一下", "排一下", "钉一下", "回我", "请把", "请帮",
)

_S: Dict[str, object] = {}


# ---------------------------------------------------------------- 注入

def attach(**state) -> None:
    """main.py 启动时调用（与 api_v1.attach 同签名，可重复调用）"""
    _S.update(state)
    _ensure_presence()


def _q(sql: str, args=()) -> List[sqlite3.Row]:
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    return _S["q1"](sql, args)  # type: ignore[operator]


def _db_path() -> str:
    return os.environ.get("HUB_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub.db"))


def _ex(sql: str, args=()) -> None:
    """已 attach 就走 main.py 的连接（带锁）；没 attach 就自己开一条（touch 要能单独跑）"""
    fn = _S.get("ex")
    if fn:
        fn(sql, args)  # type: ignore[operator]
        return
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    fn = _S.get("now_iso")
    if fn:
        return str(fn())  # type: ignore[operator]
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 在线（presence）

def _ensure_presence() -> None:
    _ex("CREATE TABLE IF NOT EXISTS presence (agent_id TEXT PRIMARY KEY, last_seen TEXT)")


def touch(agent_id: str, at: Optional[str] = None) -> None:
    """给某个 Agent 记一次心跳。api_v1 每收到一次该 Agent 的请求调一次。

    `at` 是 ISO 时间串（缺省=现在）。测试里用它模拟「10 分钟没动静」来验在线判定。
    """
    if not agent_id:
        return
    _ensure_presence()
    _ex("INSERT OR REPLACE INTO presence (agent_id, last_seen) VALUES (?,?)",
        (agent_id, at or _now_iso()))


def _presence_map() -> Dict[str, str]:
    _ensure_presence()
    return {r["agent_id"]: r["last_seen"] for r in _q("SELECT agent_id, last_seen FROM presence")}


# ---------------------------------------------------------------- 时间工具

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """ISO 串 → UTC aware datetime。解析不了就 None（脏数据不炸接口）"""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _local_iso(dt_utc: datetime) -> str:
    """按 now_iso() 同一时区格式化成串，用来做 SQL 粗筛（字符串可比）"""
    return dt_utc.astimezone().isoformat(timespec="seconds")


def _clip(text: str) -> str:
    t = text or ""
    return t if len(t) <= TEXT_LIMIT else t[:TEXT_LIMIT - 1] + "…"


def _looks_like_task(text: str) -> bool:
    return any(w in (text or "") for w in TASK_WORDS)


# ---------------------------------------------------------------- 鉴权（与 api_v1 同规则）

def _who(token: str) -> Optional[Dict[str, str]]:
    if not token:
        return None
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _q1("SELECT id, name FROM agents WHERE token = ?", (token,))
    if row is not None:
        return {"kind": "agent", "id": row["id"], "name": row["name"]}
    # 账号 token（人在网页上用的那把）：只看得见自己名下的 Agent
    acc = _q1("SELECT account_id FROM account_tokens WHERE token = ?", (token,))
    if acc is not None:
        return {"kind": "account", "id": acc["account_id"], "name": "人"}
    return None


def me(authorization: Optional[str] = Header(default=None)) -> Dict[str, str]:
    tok = ""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    who = _who(tok)
    if who is None:
        raise HTTPException(status_code=401, detail="token 不对：请在请求头带上 Authorization: Bearer <你的 token>")
    return who


# ---------------------------------------------------------------- 一次请求内的查名缓存

class _View:
    """一个请求一份缓存：Agent 名、会话、会话成员（都是会变的，不做跨请求缓存）"""

    def __init__(self) -> None:
        self._names: Dict[str, str] = {}
        self._convs: Dict[str, Optional[sqlite3.Row]] = {}
        self._members: Dict[str, List[str]] = {}

    def name(self, aid: str) -> str:
        if aid not in self._names:
            row = _q1("SELECT name FROM agents WHERE id = ?", (aid,))
            self._names[aid] = row["name"] if row else aid
        return self._names[aid]

    def conv(self, cid: str) -> Optional[sqlite3.Row]:
        if cid not in self._convs:
            self._convs[cid] = _q1("SELECT * FROM conversations WHERE id = ?", (cid,))
        return self._convs[cid]

    def members_of(self, cid: str) -> List[str]:
        if cid not in self._members:
            self._members[cid] = [r["agent_id"] for r in
                                  _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))]
        return self._members[cid]

    def participants(self, conv_id: str, from_kind: str, from_id: str) -> set:
        """一条消息牵涉到的 Agent：发送方（若是 Agent）+ 该会话其他成员"""
        ids = set()
        if from_kind == "agent":
            ids.add(from_id)
        for m in self.members_of(conv_id):
            ids.add(m)
        ids.discard("")
        return ids

    def sender_name(self, from_kind: str, from_id: str) -> str:
        return "人" if from_kind == "human" else self.name(from_id)

    def peer_label(self, row: sqlite3.Row, v_conv: Optional[sqlite3.Row], members: List[str]) -> str:
        """「谁 → 谁」里的那个「谁」"""
        if v_conv is not None and v_conv["kind"] == "group":
            return v_conv["title"] or "群聊"
        others = [m for m in members if m != row["from_id"]]
        if not others:
            return "人"
        names = [self.name(m) for m in others]
        if len(names) <= 2:
            return "、".join(names)
        return "%s 等 %d 个" % (names[0], len(names))


# ---------------------------------------------------------------- /v1/board

def _agents_block(v: _View, now: datetime, owner: Optional[str] = None) -> List[Dict[str, object]]:
    presence = _presence_map()
    if owner:
        rows = _q("SELECT id, name, created_at FROM agents WHERE owner_account = ? ORDER BY created_at", (owner,))
    else:
        rows = _q("SELECT id, name, created_at FROM agents ORDER BY created_at")
    out: List[Dict[str, object]] = []
    for r in rows:
        aid = r["id"]
        raw_last_seen = presence.get(aid)
        seen_dt = _parse_ts(raw_last_seen)
        online = bool(seen_dt is not None and (now - seen_dt).total_seconds() <= ONLINE_TTL)

        n_in = _q1("SELECT COUNT(*) AS n FROM messages m JOIN members mb ON mb.conversation_id = m.conversation_id"
                   " WHERE mb.agent_id = ? AND NOT (m.from_kind = 'agent' AND m.from_id = ?)", (aid, aid))
        n_out = _q1("SELECT COUNT(*) AS n FROM messages WHERE from_kind = 'agent' AND from_id = ?", (aid,))

        last = _q1("SELECT text, created_at FROM messages WHERE from_kind = 'agent' AND from_id = ?"
                   " ORDER BY seq DESC LIMIT 1", (aid,))
        if last is None:      # 自己还没说过话 → 退一步显示它参与会话里最近的一条
            last = _q1("SELECT m.text AS text, m.created_at AS created_at FROM messages m"
                       " JOIN members mb ON mb.conversation_id = m.conversation_id"
                       " WHERE mb.agent_id = ? ORDER BY m.seq DESC LIMIT 1", (aid,))
        last_line = _clip(last["text"]) if last is not None else "还没说过话"
        last_at = _parse_ts(last["created_at"]) if last is not None else None

        out.append({
            "id": aid,
            "name": r["name"],
            "kind": "agent",
            "online": online,
            "last_seen": raw_last_seen,
            "msgs_in": int(n_in["n"]) if n_in else 0,
            "msgs_out": int(n_out["n"]) if n_out else 0,
            "last_line": last_line,
            "_sort": max([d for d in (seen_dt, last_at) if d is not None], default=None),
        })

    # 在线优先，其次最近有动静的优先，最后按名字
    out.sort(key=lambda a: (not a["online"],
                            -((a["_sort"] or datetime(1970, 1, 1, tzinfo=timezone.utc)).timestamp()),
                            str(a["name"])))
    for a in out:
        a.pop("_sort", None)
    return out


def _timeline_block(v: _View) -> List[Dict[str, object]]:
    rows = _q("SELECT * FROM messages ORDER BY seq DESC LIMIT ?", (TIMELINE_LIMIT,))
    out: List[Dict[str, object]] = []
    for r in rows:
        conv = v.conv(r["conversation_id"])
        members = v.members_of(r["conversation_id"])
        text = r["text"] or ""

        prev = _q1("SELECT from_kind, from_id, created_at FROM messages"
                   " WHERE conversation_id = ? AND seq < ? ORDER BY seq DESC LIMIT 1",
                   (r["conversation_id"], r["seq"]))
        kind = "消息"
        if _looks_like_task(text):
            kind = "任务"
        elif prev is not None and (prev["from_kind"], prev["from_id"]) != (r["from_kind"], r["from_id"]):
            t_now, t_prev = _parse_ts(r["created_at"]), _parse_ts(prev["created_at"])
            if t_now and t_prev and 0 <= (t_now - t_prev).total_seconds() <= REPLY_WINDOW:
                kind = "回话"

        out.append({
            "ts": r["created_at"],
            "from": v.sender_name(r["from_kind"], r["from_id"]),
            "to": v.peer_label(r, conv, members),
            "kind": kind,
            "text": _clip(text),
            "seq": r["seq"],
        })
    return out


def _usage_block(v: _View, now: datetime) -> Dict[str, object]:
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # 粗筛（留 2 天余量，防时区偏移），再在 Python 里按 UTC 精确分桶
    coarse = _local_iso(month0 - timedelta(days=2))
    rows = _q("SELECT from_kind, from_id, conversation_id, created_at FROM messages WHERE created_at >= ?", (coarse,))

    t_msgs = m_msgs = 0
    t_agents: set = set()
    m_agents: set = set()
    for r in rows:
        dt = _parse_ts(r["created_at"])
        if dt is None:
            continue
        if dt < month0:
            continue
        parts = v.participants(r["conversation_id"], r["from_kind"], r["from_id"])
        m_msgs += 1
        m_agents |= parts
        if dt >= today0:
            t_msgs += 1
            t_agents |= parts
    return {
        "today": {"msgs": t_msgs, "agents": len(t_agents)},
        "month": {"msgs": m_msgs, "agents": len(m_agents)},
    }


@router.get("/v1/board")
def v1_board(who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """名册 + 协作时间线 + 用量（人机协作一眼看清）"""
    now = datetime.now(timezone.utc)
    v = _View()
    return {
        "agents": _agents_block(v, now, owner=(who.get("id") if who.get("kind") == "account" else None)),
        "timeline": _timeline_block(v),
        "usage": _usage_block(v, now),
        "server_time": _now_iso(),
    }


# ---------------------------------------------------------------- /console 单页

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentPost · 智能体邮局 —— 控制台</title>
<style>
:root{
  --brand:#1E3A5F; --online:#2D6A4F;
  --bg:#FAFAF8; --card:#FFFFFF; --line:#E8E6E1;
  --ink:#1C1B1A; --sub:#5C5A57; --weak:#8C8985;
  --sans:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,monospace;
}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.6;
  -webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.page{max-width:1080px;margin:0 auto;padding:0 24px 64px}
a{color:var(--brand);text-decoration:none;border-bottom:1px solid rgba(30,58,95,.28)}
a:hover{border-bottom-color:var(--brand)}

/* 顶栏 */
.top{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-end;justify-content:space-between;
  padding:64px 0 24px;border-bottom:1px solid var(--line)}
.top>*{min-width:0}   /* flex 子项默认 min-width:auto，会把窄屏撑出横向滚动 */
h1{font-size:40px;line-height:1.2;font-weight:600;margin:0;letter-spacing:-.01em}
.tagline{margin:8px 0 0;color:var(--sub);font-size:16px;line-height:1.6}
.tokbox{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tokbox label{font-size:13px;line-height:1.5;color:var(--weak)}
input#tok{border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);
  font-family:var(--mono);font-size:13px;line-height:1.5;padding:8px 12px;min-width:240px}
input#tok:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(30,58,95,.10)}
.hint{font-size:13px;line-height:1.5;color:var(--weak)}
.hint.live{color:var(--online)}

/* 汇总行 */
.sum{padding:24px 0 16px;font-size:16px;line-height:1.6;color:var(--sub)}
.sum b{color:var(--ink);font-weight:600}
.sum .mono{color:var(--ink)}

/* 三栏 */
.grid{display:grid;grid-template-columns:240px minmax(0,1fr) 220px;gap:24px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px}
.card h2{font-size:20px;line-height:1.4;font-weight:600;margin:0 0 12px}
.card h2 .cnt{font-family:var(--mono);font-size:13px;color:var(--weak);font-weight:400;margin-left:4px}

/* 左：名册 */
.roster{list-style:none;margin:0;padding:0}
.row{display:flex;gap:8px;align-items:flex-start;padding:8px;margin:0 -8px;border-radius:6px;
  border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row:hover{background:rgba(30,58,95,.04)}
.dot{flex:0 0 auto;width:8px;height:8px;margin-top:9px;border-radius:50%;background:var(--weak)}
.dot.on{background:var(--online);box-shadow:0 0 0 3px rgba(45,106,79,.14)}
.who{min-width:0;flex:1 1 auto}
.nm{display:block;font-weight:600;font-size:16px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ll{display:block;font-size:13px;line-height:1.5;color:var(--weak);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{font-family:var(--mono);font-size:13px;color:var(--weak)}

/* 中：时间线 */
.tl{list-style:none;margin:0;padding:0}
.item{border:1px solid var(--line);border-left:3px solid transparent;border-radius:12px;
  background:var(--card);padding:12px 16px;margin-bottom:8px}
.item:hover{border-color:rgba(30,58,95,.22);border-left-color:rgba(30,58,95,.22)}
.item.task{border-left-color:var(--brand)}
.item.task:hover{border-left-color:var(--brand)}
.item .head{font-size:13px;line-height:1.5;color:var(--weak);display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.item .head .t{font-family:var(--mono);font-variant-numeric:tabular-nums}
.item .head .fr,.item .head .to{font-weight:600;font-size:13px;color:var(--sub)}
.item .head .fr.human,.item .head .to.human{color:var(--brand)}
.item .txt{margin-top:4px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;font-size:16px;line-height:1.6}
.tag{display:inline-block;border-radius:6px;background:#F0EEEA;color:#6B6864;font-size:13px;
  line-height:1.5;padding:0 8px}
.tl .empty,.roster .empty{color:var(--weak);font-size:13px;line-height:1.5;padding:8px 0}

/* 右：用量 */
.ubox{border-top:1px solid var(--line);padding:12px 0}
.ubox:first-of-type{border-top:0;padding-top:0}
.ubox .ut{font-size:13px;line-height:1.5;color:var(--weak);margin-bottom:4px}
.uline{display:flex;align-items:baseline;gap:8px}
.unum{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:20px;line-height:1.4;font-weight:600}
.usub{font-size:13px;line-height:1.5;color:var(--sub)}
.ucap{font-size:13px;line-height:1.5;color:var(--weak);margin:12px 0 0}
footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;line-height:1.5;color:var(--weak)}

@keyframes fi{from{opacity:0;transform:translateY(2px)}to{opacity:1;transform:none}}
.item.fresh{animation:fi .18s ease-out}

@media (max-width:760px){
  .page{padding:0 24px 48px}
  .top{padding:48px 0 24px}
  h1{font-size:32px}
  .grid{grid-template-columns:minmax(0,1fr);gap:16px}   /* 必须 minmax(0,…)：1fr=minmax(auto,1fr) 会被 nowrap 文字撑破 */
  input#tok{min-width:0;width:100%;flex:1 1 100%}
  .tokbox{width:100%}
}
</style>
</head>
<body>
<div class="page">

  <header class="top">
    <div>
      <h1>AgentPost · 智能体邮局</h1>
      <p class="tagline">发出去，就一定到。</p>
    </div>
    <div class="tokbox">
      <label for="tok">你的 token</label>
      <input id="tok" type="text" autocomplete="off" spellcheck="false" placeholder="粘贴 token">
      <span id="tokstate" class="hint">未连接</span>
    </div>
  </header>

  <div class="sum" id="sum">正在连接…</div>

  <main class="grid">
    <section class="card">
      <h2>Agent 名册<span class="cnt" id="rcnt"></span></h2>
      <ul class="roster" id="roster"></ul>
    </section>

    <section class="card">
      <h2>协作时间线<span class="cnt" id="tcnt"></span></h2>
      <ol class="tl" id="tl"></ol>
    </section>

    <aside class="card">
      <h2>用量</h2>
      <div class="ubox">
        <div class="ut">今日（UTC）</div>
        <div class="uline"><span class="unum" id="tm">–</span><span class="usub">条消息</span>
          <span class="unum" id="ta">–</span><span class="usub">个 Agent</span></div>
      </div>
      <div class="ubox">
        <div class="ut">本月（UTC）</div>
        <div class="uline"><span class="unum" id="mm">–</span><span class="usub">条消息</span>
          <span class="unum" id="ma">–</span><span class="usub">个 Agent</span></div>
      </div>
      <p class="ucap">按 Agent 数 × 消息条数计费。</p>
    </aside>
  </main>

  <footer>
    每 5 秒自动刷新 · <a href="/">主控制台</a> · 数据来自这台机器上的消息台账
  </footer>
</div>

<script>
(function () {
  var KEY = 'hub_token';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  };
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  var hhmmss = function (ts) {
    var d = new Date(ts);
    if (isNaN(d.getTime())) { return String(ts || '').slice(11, 19); }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  };
  var fresh = 0;      // 上次渲染到的最大 seq：只给新到的消息加淡入
  var busy = false;

  function render(b) {
    var agents = b.agents || [], line = b.timeline || [], u = b.usage || {};
    var today = u.today || {}, month = u.month || {};
    var on = 0, i;
    for (i = 0; i < agents.length; i++) { if (agents[i].online) { on++; } }

    $('sum').innerHTML = '<b>' + agents.length + '</b> 个 Agent · 在线 <b>' + on + '</b> · 今日 ' +
      '<span class="mono">' + (today.msgs || 0) + '</span> 条消息';
    $('tokstate').className = 'hint live';
    $('tokstate').textContent = '已连接';
    $('rcnt').textContent = agents.length ? agents.length + ' 个' : '';
    $('tcnt').textContent = line.length ? '最近 ' + line.length + ' 条' : '';
    $('tm').textContent = today.msgs || 0;
    $('ta').textContent = today.agents || 0;
    $('mm').textContent = month.msgs || 0;
    $('ma').textContent = month.agents || 0;

    if (!agents.length) {
      $('roster').innerHTML = '<li class="empty">还没有 Agent。去<a href="/">主控制台</a>建一个，它一说话就出现在这。</li>';
    } else {
      var rh = '';
      for (i = 0; i < agents.length; i++) {
        var a = agents[i];
        rh += '<li class="row" title="' + esc(a.last_seen ? '最后出现 ' + a.last_seen : '还没出现过') + '">' +
              '<span class="dot' + (a.online ? ' on' : '') + '"></span>' +
              '<span class="who"><span class="nm">' + esc(a.name) + '</span>' +
              '<span class="ll">' + esc(a.last_line || '') + '</span></span>' +
              '<span class="meta">' + (a.msgs_out || 0) + '↑' + (a.msgs_in || 0) + '↓</span></li>';
      }
      $('roster').innerHTML = rh;
    }

    if (!line.length) {
      $('tl').innerHTML = '<li class="empty">还没有消息。人在主控制台发一句，或让 Agent 回一句。</li>';
    } else {
      var th = '';
      for (i = 0; i < line.length; i++) {
        var m = line[i], isNew = m.seq > fresh;
        th += '<li class="item' + (m.kind === '任务' ? ' task' : '') + (isNew ? ' fresh' : '') + '">' +
              '<div class="head"><span class="t">' + esc(hhmmss(m.ts)) + '</span>' +
              '<span class="fr' + (m.from === '人' ? ' human' : '') + '">' + esc(m.from) + '</span>' +
              '<span>→</span>' +
              '<span class="to' + (m.to === '人' ? ' human' : '') + '">' + esc(m.to) + '</span>' +
              (m.kind !== '消息' ? '<span class="tag">' + esc(m.kind) + '</span>' : '') +
              '</div><div class="txt">' + esc(m.text) + '</div></li>';
      }
      $('tl').innerHTML = th;
    }
    if (line.length) { fresh = Math.max(fresh, line[0].seq); }
  }

  function fail(msg) {
    $('tokstate').className = 'hint';
    $('tokstate').textContent = '未连接';
    $('sum').textContent = msg;
  }

  function load() {
    if (busy) { return; }
    busy = true;
    var tok = ($('tok').value || '').trim();
    fetch('/v1/board', { headers: { 'Authorization': 'Bearer ' + tok } })
      .then(function (r) {
        if (r.status === 401) { throw new Error('token 不对（401）——填好上面的 token 就会自动连上'); }
        if (!r.ok) { throw new Error('服务返回 ' + r.status); }
        return r.json();
      })
      .then(render)
      .catch(function (e) { fail('连不上：' + e.message); })
      .then(function () { busy = false; });
  }

  var saved = '';
  try { saved = localStorage.getItem(KEY) || ''; } catch (e) { saved = ''; }
  if (!saved) { saved = 'h-dev-token'; }
  $('tok').value = saved;
  $('tok').addEventListener('change', function () {
    try { localStorage.setItem(KEY, $('tok').value.trim()); } catch (e) {}
    load();
  });
  load();
  setInterval(load, 5000);
})();
</script>
</body>
</html>
"""


@router.get("/console", response_class=HTMLResponse)
def console_page() -> HTMLResponse:
    """给人看的单页控制台（内联 CSS/JS，无外部 CDN）"""
    return HTMLResponse(PAGE_HTML)
