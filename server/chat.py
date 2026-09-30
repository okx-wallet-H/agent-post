"""chat —— 给人用的群聊界面（派 #12，2026-09-29）

约定同 api_v1.py：main.py 启动时调用 ``chat.attach(**state)`` 注入
db / lock / user_token / q / q1 / ex / new_id / now_iso / new_token，
然后 ``app.include_router(chat.router)``。本文件不改 main.py，挂载由使用方完成
（冒烟脚本 smoke_chat.sh 里就是这么挂的）。

页面与接口：
  GET  /chat        单页群聊界面（三栏 260 / 1fr / 240；<860px 收成单栏）
  GET  /chat/state  页面数据：会话列表 + 当前会话消息流 + 成员 + 在线状态
  POST /chat/send   以「我」或会话里某个 Agent 的身份发消息

收消息：页面 JS 用长轮询 GET /v1/inbox?since=N&wait=55（等 55 秒，不每秒轮），
被新消息唤醒后再刷 /chat/state 全量渲染。

送达口径（messages 表没有送达列，按在线状态现算）：
  Agent 发的 → 已送达；人发的 → 会话里有 Agent 离线（presence 超 120 秒）就
  「排队中 · Agent 离线」，全在线（或会话里没有 Agent）才「已送达」。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}


def attach(**state) -> None:
    """main.py 启动时调用（与 api_v1.attach 同签名，可重复调用）"""
    _S.update(state)


def _q(sql: str, args=()) -> List[sqlite3.Row]:
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()) -> sqlite3.Cursor:
    return _S["ex"](sql, args)  # type: ignore[operator]


# 鉴权与多租户复用 api_v1 的一套（api_v1 的 me 顺带给 agent token 打心跳）
from api_v1 import me, _t  # noqa: E402

ONLINE_TTL = 120  # 与 console 一致：presence.last_seen ≤ 120 秒算在线


# ---------------------------------------------------------------- 在线状态

def _ensure_presence() -> None:
    _ex("CREATE TABLE IF NOT EXISTS presence (agent_id TEXT PRIMARY KEY, last_seen TEXT)")


def _online_map() -> Dict[str, bool]:
    """agent_id → 是否在线（没有 presence 记录 = 离线）"""
    _ensure_presence()
    now = datetime.now(timezone.utc)
    out: Dict[str, bool] = {}
    for r in _q("SELECT agent_id, last_seen FROM presence"):
        try:
            seen = datetime.fromisoformat(r["last_seen"])
            out[r["agent_id"]] = (now - seen).total_seconds() <= ONLINE_TTL
        except (ValueError, TypeError):
            out[r["agent_id"]] = False
    return out


def _members_of(cid: str) -> List[str]:
    return [r["agent_id"] for r in _q("SELECT agent_id FROM members WHERE conversation_id = ?", (cid,))]


def _msg_status(conv_id: str, from_kind: str, from_id: str, online: Dict[str, bool]) -> str:
    """delivered | queued。Agent 发的算已送达；人发的看会话里有没有 Agent 离线。"""
    if from_kind == "agent":
        return "delivered"
    for aid in _members_of(conv_id):
        if aid == from_id:
            continue
        if not online.get(aid, False):
            return "queued"
    return "delivered"


# ---------------------------------------------------------------- 数据接口

def _conv_out(r: sqlite3.Row) -> Dict[str, object]:
    last = _q1("SELECT m.text, m.created_at, m.from_kind, m.from_id FROM messages m"
               " WHERE m.conversation_id = ? ORDER BY m.seq DESC LIMIT 1", (r["id"],))
    last_from = ""
    if last is not None:
        last_from = "人" if last["from_kind"] == "human" else (
            _q1("SELECT name FROM agents WHERE id = ?", (last["from_id"],)) or {"name": last["from_id"]})["name"]
    return {
        "id": r["id"],
        "title": r["title"],
        "kind": r["kind"],
        "members": _members_of(r["id"]),
        "last": {"text": last["text"], "from": last_from, "ts": last["created_at"]} if last is not None else None,
    }


@router.get("/chat/state")
def chat_state(cid: Optional[str] = None,
               who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """页面数据：会话列表 + 当前会话消息流 + 全部 Agent（含在线）+ 成员。"""
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="群聊界面给人用，要人 token")

    frag, fargs = _t()["owner_filter"](who, "c.owner_account")  # type: ignore[operator]
    convs = [_conv_out(r) for r in
             _q("SELECT c.* FROM conversations c WHERE 1=1%s ORDER BY c.created_at, c.id" % frag, tuple(fargs))]

    online = _online_map()
    agents = []
    for r in _q("SELECT id, name FROM agents ORDER BY created_at"):
        agents.append({"id": r["id"], "name": r["name"], "online": online.get(r["id"], False)})

    msgs: List[Dict[str, object]] = []
    if cid:
        for r in _q("SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq LIMIT 500", (cid,)):
            name = "人" if r["from_kind"] == "human" else (
                _q1("SELECT name FROM agents WHERE id = ?", (r["from_id"],)) or {"name": r["from_id"]})["name"]
            msgs.append({
                "seq": r["seq"], "from_kind": r["from_kind"], "from_id": r["from_id"], "from_name": name,
                "text": r["text"], "ts": r["created_at"],
                "status": _msg_status(r["conversation_id"], r["from_kind"], r["from_id"], online),
            })

    return {"conversations": convs, "agents": agents, "messages": msgs, "server_time": _S["now_iso"]()}  # type: ignore[index]


class ChatSendIn(BaseModel):
    conversation_id: str
    text: str
    as_agent_id: Optional[str] = None
    client_msg_id: Optional[str] = None


@router.post("/chat/send")
def chat_send(body: ChatSendIn, who: Dict[str, str] = Depends(me)) -> Dict[str, object]:  # type: ignore[assignment]
    """以「我」或会话里某个 Agent 的身份发消息。幂等：同 client_msg_id 不重复投。"""
    if who["kind"] != "human":
        raise HTTPException(status_code=403, detail="群聊界面给人用，要人 token")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容不能空")
    conv = _q1("SELECT * FROM conversations WHERE id = ?", (body.conversation_id,))
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在：%s" % body.conversation_id)

    from_kind, from_id = "human", "human"
    if body.as_agent_id:
        mids = _members_of(conv["id"])
        if body.as_agent_id not in mids:
            raise HTTPException(status_code=400, detail="这个 Agent 不在会话里：%s" % body.as_agent_id)
        from_kind, from_id = "agent", body.as_agent_id

    acct = _t()["billable_account"](who)  # type: ignore[operator]
    _t()["quota_guard"](acct, "msg")  # type: ignore[operator]  额度不够 → 402

    cmid = (body.client_msg_id or "").strip() or None
    if cmid:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is not None:
            return {"ok": True, "duplicate": True, "seq": old["seq"], "conversation_id": conv["id"]}
    mid = _S["new_id"]("msg")  # type: ignore[operator]
    try:
        cur = _ex("INSERT INTO messages (id, conversation_id, from_kind, from_id, text, client_msg_id, created_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (mid, conv["id"], from_kind, from_id, text, cmid, _S["now_iso"]()))  # type: ignore[index]
        seq = cur.lastrowid
    except sqlite3.IntegrityError:
        old = _q1("SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?", (conv["id"], cmid))
        if old is None:
            raise HTTPException(status_code=409, detail="写入冲突，请重试")
        return {"ok": True, "duplicate": True, "seq": old["seq"], "conversation_id": conv["id"]}
    _t()["record_usage"](acct, "msg", 1)  # type: ignore[operator]
    return {"ok": True, "duplicate": False, "seq": seq, "conversation_id": conv["id"],
            "status": _msg_status(conv["id"], from_kind, from_id, _online_map())}


# ---------------------------------------------------------------- /chat 单页

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentPost · 智能体邮局 —— 群聊</title>
<style>
:root{
  --canvas:#FAFAF8; --panel:#FFFFFF; --line:#E8E6E1;
  --ink:#1C1B1A; --muted:#5C5A57; --faint:#8C8985;
  --primary:#1E3A5F; --accent:#2D6A4F;
  --sans:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
}
*,*::before,*::after{box-sizing:border-box;min-width:0}
html,body{margin:0;padding:0;height:100%}
body{background:var(--canvas);color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.6}
a{color:var(--primary);text-decoration:none}

/* 顶栏 */
.top{display:flex;align-items:center;gap:16px;justify-content:space-between;flex-wrap:wrap;
  padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.top h1{font-size:20px;line-height:1.4;margin:0;color:var(--primary);font-weight:600}
.tokbox{display:flex;align-items:center;gap:8px}
.tokbox label{font-size:13px;line-height:1.5;color:var(--faint)}
.tokbox input{border:1px solid var(--line);border-radius:6px;background:var(--canvas);color:var(--ink);
  font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.5;padding:6px 10px;min-width:200px}
.tokbox input:focus{outline:none;border-color:var(--primary)}

/* 三栏：会话 260 / 消息流 1fr / 成员 240 */
.layout{display:grid;grid-template-columns:260px minmax(0,1fr) 240px;height:calc(100vh - 57px)}
.col{min-height:0;display:flex;flex-direction:column;background:var(--panel)}
.side{background:var(--canvas);border-right:1px solid var(--line)}
.panel{border-left:1px solid var(--line)}
.side h2,.panel h2{font-size:13px;line-height:1.5;color:var(--muted);font-weight:600;margin:0;
  padding:12px 16px 8px;text-transform:none}

/* 会话列表 */
.convs{overflow-y:auto;flex:1}
.conv{padding:10px 16px;cursor:pointer;border-bottom:1px solid var(--line)}
.conv:hover{background:rgba(30,58,95,.04)}
.conv.on{background:rgba(30,58,95,.08);box-shadow:inset 3px 0 0 var(--primary)}
.conv .t{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv .p{font-size:13px;line-height:1.5;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv .k{font-size:12px;color:var(--faint);border:1px solid var(--line);border-radius:6px;padding:0 6px;margin-left:6px}
.empty{font-size:13px;line-height:1.5;color:var(--faint);padding:16px}

/* 消息流 */
.stream{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:6px}
.msg{display:flex;flex-direction:column;max-width:78%}
.msg.me{align-self:flex-end;align-items:flex-end}
.msg.ag{align-self:flex-start;align-items:flex-start}
.bubble{border-radius:12px;padding:8px 14px;font-size:16px;line-height:1.6;word-break:break-word;
  white-space:pre-wrap;box-shadow:0 1px 2px rgba(28,27,26,.05)}
.msg.me .bubble{background:var(--primary);color:#fff}
.msg.ag .bubble{background:var(--panel);border:1px solid var(--line);color:var(--ink)}
.meta{font-size:13px;line-height:1.5;color:var(--muted);padding:2px 4px 0}
.meta .who{font-weight:600}
.meta.queued{color:var(--faint)}
.waiting{font-size:13px;line-height:1.5;color:var(--faint);font-style:italic;padding:8px 4px}

/* 成员面板 */
.members{overflow-y:auto;flex:1;list-style:none;margin:0;padding:0 0 12px}
.member{display:flex;align-items:center;gap:10px;padding:8px 16px}
.member:hover{background:rgba(30,58,95,.04)}
.ava{flex:0 0 auto;width:30px;height:30px;border-radius:50%;background:var(--primary-soft,#E8EDF3);
  color:var(--primary);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600}
.ava.human{background:var(--accent-soft,#E8F0EB);color:var(--accent)}
.member .nm{flex:1;font-size:14.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{flex:0 0 auto;width:8px;height:8px;border-radius:50%;background:var(--faint)}
.dot.on{background:var(--accent)}
.as{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 8px;
  cursor:pointer;background:var(--panel)}
.as:hover{border-color:var(--primary);color:var(--primary)}

/* 底部输入 */
.inputbar{display:flex;gap:10px;align-items:center;padding:10px 16px;border-top:1px solid var(--line);
  background:var(--panel)}
.identity{display:flex;align-items:center;gap:6px;font-size:13px;line-height:1.5;color:var(--muted);white-space:nowrap}
.identity select{border:1px solid var(--line);border-radius:6px;background:var(--canvas);color:var(--ink);
  font-family:var(--sans);font-size:14px;padding:7px 8px}
.identity select:focus{outline:none;border-color:var(--primary)}
.inputbar input[type=text]{flex:1;border:1px solid var(--line);border-radius:6px;background:var(--canvas);
  color:var(--ink);font-family:var(--sans);font-size:16px;line-height:1.6;padding:8px 12px;min-width:0}
.inputbar input[type=text]:focus{outline:none;border-color:var(--primary)}
.inputbar button{border:0;border-radius:6px;background:var(--primary);color:#fff;font-size:16px;
  font-weight:600;padding:9px 22px;cursor:pointer}
.inputbar button:disabled{background:var(--faint)}

/* 移动端 <860px：侧栏和成员面板隐藏，单栏（ui-notes 实现注意 2） */
@media (max-width:860px){
  .side,.panel{display:none}
  .layout{grid-template-columns:minmax(0,1fr)}
}
</style>
</head>
<body>

<div class="top">
  <h1>群聊</h1>
  <div class="tokbox">
    <label for="tok">token</label>
    <input id="tok" type="text" autocomplete="off" spellcheck="false" placeholder="粘贴人的 token">
    <span id="tokstate" style="font-size:13px;color:var(--faint)">未连接</span>
  </div>
</div>

<div class="layout">
  <aside class="col side">
    <h2>会话</h2>
    <div class="convs" id="convs"><div class="empty">还没有会话。去主控制台建一个。</div></div>
  </aside>

  <section class="col main">
    <div class="stream" id="stream"><div class="empty">选一个会话开始聊。</div></div>
    <div class="inputbar">
      <span class="identity">以：<select id="identity"><option value="">我</option></select></span>
      <input id="text" type="text" placeholder="输入消息，回车发送" autocomplete="off">
      <button id="send">发送</button>
    </div>
  </section>

  <aside class="col panel">
    <h2>成员</h2>
    <ul class="members" id="members"><li class="empty">选一个会话看成员。</li></ul>
  </aside>
</div>

<script>
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var hhmm = function (ts) {
    var d = new Date(ts);
    if (isNaN(d.getTime())) { return String(ts || '').slice(11, 16); }
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  };
  var KEY = 'hub_token';
  var SINCE = 0;        // /v1/inbox 长轮询游标
  var CUR = null;       // 当前会话 id
  // 支持挂在子路径下（例如 https://hub.hvip.one/chat 或 /hub/chat）：接口地址按当前页面路径前缀拼
  var BASE = (function () {
    var p = location.pathname;
    if (p.charAt(p.length - 1) !== '/') p = p.replace(/\/[^\/]*$/, '/');
    return p;
  })();
  var ST = null;        // 最近一次 /chat/state

  var tok = function () { return ($('tok').value || '').trim(); };
  var auth = function () { return { 'Authorization': 'Bearer ' + tok() }; };

  function renderConvs() {
    var list = ST.conversations || [], h = '', i, c;
    for (i = 0; i < list.length; i++) {
      c = list[i];
      h += '<div class="conv' + (c.id === CUR ? ' on' : '') + '" data-cid="' + esc(c.id) + '">' +
        '<div class="t">' + esc(c.title) + '<span class="k">' + (c.kind === 'dm' ? '单聊' : '群聊') + '</span></div>' +
        '<div class="p">' + (c.last ? esc(c.last.from) + '：' + esc(c.last.text) : '还没有消息') + '</div></div>';
    }
    $('convs').innerHTML = h || '<div class="empty">还没有会话。去主控制台建一个。</div>';
    var els = document.querySelectorAll('.conv');
    for (i = 0; i < els.length; i++) {
      (function (el) {
        el.addEventListener('click', function () { load(el.getAttribute('data-cid')); });
      })(els[i]);
    }
  }

  function renderMsgs() {
    var msgs = ST.messages || [], h = '', i, m, prev = null, sameMin;
    for (i = 0; i < msgs.length; i++) {
      m = msgs[i];
      sameMin = prev !== null && prev.from_id === m.from_id && prev.from_kind === m.from_kind &&
                String(prev.ts).slice(0, 16) === String(m.ts).slice(0, 16);   // 同一分钟合并
      if (sameMin) {
        h += '<div class="msg ' + (m.from_kind === 'human' ? 'me' : 'ag') + '"><div class="bubble">' + esc(m.text) + '</div></div>';
      } else {
        h += '<div class="msg ' + (m.from_kind === 'human' ? 'me' : 'ag') + '">' +
             '<div class="meta' + (m.status === 'queued' ? ' queued' : '') + '"><span class="who">' + esc(m.from_name) + '</span> · ' +
             (m.status === 'queued' ? '排队中 · Agent 离线' : '已送达 · ' + hhmm(m.ts)) + '</div>' +
             '<div class="bubble">' + esc(m.text) + '</div></div>';
      }
      prev = m;
    }
    var tail = msgs.length ? msgs[msgs.length - 1] : null;
    if (tail && tail.from_kind === 'human' && tail.status === 'queued') {
      h += '<div class="waiting">等待 Agent 回复…</div>';
    }
    $('stream').innerHTML = h || '<div class="empty">还没有消息。发一句试试。</div>';
    $('stream').scrollTop = $('stream').scrollHeight;
  }

  function renderMembers() {
    var conv = null, i, c;
    for (i = 0; i < (ST.conversations || []).length; i++) { if (ST.conversations[i].id === CUR) { conv = ST.conversations[i]; } }
    if (!conv) { $('members').innerHTML = '<li class="empty">选一个会话看成员。</li>'; return; }
    var agents = ST.agents || [], byId = {}, h = '';
    for (i = 0; i < agents.length; i++) { byId[agents[i].id] = agents[i]; }
    h += '<li class="member"><span class="ava human">我</span><span class="nm">我（人）</span>' +
         '<span class="dot on"></span></li>';
    for (i = 0; i < conv.members.length; i++) {
      var a = byId[conv.members[i]];
      if (!a) { continue; }
      h += '<li class="member"><span class="ava">' + esc(a.name.charAt(0)) + '</span>' +
           '<span class="nm">' + esc(a.name) + '</span>' +
           '<span class="dot' + (a.online ? ' on' : '') + '"></span>' +
           '<span class="as" data-aid="' + esc(a.id) + '">以此身份发</span></li>';
    }
    $('members').innerHTML = h || '<li class="empty">这个会话还没有成员。</li>';
    var els = document.querySelectorAll('.as');
    for (i = 0; i < els.length; i++) {
      (function (el) {
        el.addEventListener('click', function () { $('identity').value = el.getAttribute('data-aid'); });
      })(els[i]);
    }
    // 身份选择器：我 + 会话成员
    var oh = '<option value="">我</option>';
    for (i = 0; i < conv.members.length; i++) {
      var ag = byId[conv.members[i]];
      if (ag) { oh += '<option value="' + esc(ag.id) + '">' + esc(ag.name) + '</option>'; }
    }
    $('identity').innerHTML = oh;
  }

  function render() {
    renderConvs();
    renderMsgs();
    renderMembers();
    $('tokstate').textContent = '已连接';
    var max = 0, msgs = ST.messages || [], i;
    for (i = 0; i < msgs.length; i++) { if (msgs[i].seq > max) { max = msgs[i].seq; } }
    if (max > SINCE) { SINCE = max; }
  }

  function load(cid) {
    CUR = cid;
    var q = cid ? '?cid=' + encodeURIComponent(cid) : '';
    fetch(BASE + 'chat/state' + q, { headers: auth() })
      .then(function (r) { if (!r.ok) { throw new Error('state ' + r.status); } return r.json(); })
      .then(function (b) { ST = b; render(); })
      .catch(function (e) { $('tokstate').textContent = '连不上：' + e.message; });
  }

  function sendMsg() {
    var text = $('text').value.trim();
    if (!text || !CUR) { return; }
    var body = { conversation_id: CUR, text: text };
    if ($('identity').value) { body.as_agent_id = $('identity').value; }
    body.client_msg_id = 'chat-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    $('send').disabled = true;
    fetch(BASE + 'chat/send', { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, auth()),
                          body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) { throw new Error('send ' + r.status); } return r.json(); })
      .then(function () { $('text').value = ''; load(CUR); })
      .catch(function (e) { $('tokstate').textContent = '发送失败：' + e.message; })
      .then(function () { $('send').disabled = false; });
  }

  // 长轮询：GET /v1/inbox?since=N&wait=55，有新消息就醒，不每秒轮
  function poll() {
    var base = '/v1/inbox?since=' + SINCE + '&wait=55';
    fetch(base, { headers: auth() })
      .then(function (r) { if (!r.ok) { throw new Error('inbox ' + r.status); } return r.json(); })
      .then(function (b) {
        if (b.latest > SINCE) { SINCE = b.latest; load(CUR); }   // 有消息：全量刷新当前会话
        poll();                                                    // 立即挂下一轮长轮询
      })
      .catch(function (e) { setTimeout(poll, 3000); });            // 出错退避 3 秒再挂
  }

  $('send').addEventListener('click', sendMsg);
  $('text').addEventListener('keydown', function (e) { if (e.key === 'Enter') { sendMsg(); } });
  $('tok').addEventListener('change', function () {
    try { localStorage.setItem(KEY, tok()); } catch (e) {}
    load(CUR);
  });

  var saved = '';
  try { saved = localStorage.getItem(KEY) || ''; } catch (e) {}
  if (!saved) { saved = 'h-dev-token'; }
  $('tok').value = saved;
  load(null);
  poll();
})();
</script>
</body>
</html>
"""


@router.get("/chat", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    return HTMLResponse(PAGE_HTML)
