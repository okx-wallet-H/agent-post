#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
温暖通信台 · 计费出账 + 账单页（#13，2026-09-29）

口径：
- 标准套餐 ¥49/月：5 Agent + 2 万条；团队 ¥199/月：20 Agent + 10 万条；超额 ¥0.01/条（按消息条数）
- 无套餐：plan="试用"，included 按 billing 免费券当前有效额度（billing.get_usage 的 quota_msgs），
  超额部分计 0 元（试用期不收费，但要如实标出超了多少条）
- 金额内部全部按「分」整数计算，输出时转元，避免浮点误差（1000 × 0.01 恰好 10.00）
- 出账只读 billing 的 usage_counters 表，不写、不改 billing 的任何表和行为

挂载约定同 api_v1.py（main.py 启动时 attach(**state) 注入）：
    import invoice
    invoice.attach(db=_db, lock=_db_lock, user_token=USER_TOKEN, q1=q1)
    app.include_router(invoice.router)
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

import billing

router = APIRouter()

# ---------------------------------------------------------------- 套餐口径（金额单位：分）

PLANS = {
    "标准": {"fee_fen": 4900, "agents": 5, "msgs": 20000},
    "团队": {"fee_fen": 19900, "agents": 20, "msgs": 100000},
}
OVERAGE_FEN_PER_MSG = 1  # ¥0.01/条

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

_S: Dict[str, object] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    account_id TEXT PRIMARY KEY,
    plan       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def attach(**state) -> None:
    """main.py 启动时调用：db / lock / user_token / q1（约定同 api_v1.py）。"""
    _S.update(state)
    init_db(_S["db"])


def init_db(conn) -> None:
    """幂等自建出账用表（subscriptions）。"""
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 核心逻辑（函数层，自测直调）

def set_plan(conn, account_id: str, plan: str) -> None:
    """订阅/换套餐。plan ∈ 标准 / 团队。"""
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="套餐不存在：%s（标准 / 团队）" % plan)
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions (account_id, plan, created_at) VALUES (?,?,?)",
        (account_id, plan, billing.now_iso()),
    )
    conn.commit()


def compute_invoice(conn, account_id: str, period: str) -> Dict[str, object]:
    """出账：读 usage_counters 当月用量 + subscriptions 套餐，算本期账单。"""
    row = conn.execute(
        "SELECT msgs, agents FROM usage_counters WHERE account_id = ? AND period = ?",
        (account_id, period),
    ).fetchone()
    used_msgs = row["msgs"] if row else 0
    used_agents = row["agents"] if row else 0

    sub = conn.execute("SELECT plan FROM subscriptions WHERE account_id = ?", (account_id,)).fetchone()
    if sub is not None:
        plan = sub["plan"]
        spec = PLANS[plan]
        included_msgs = spec["msgs"]
        overage_msgs = max(used_msgs - included_msgs, 0)
        plan_fee_fen = spec["fee_fen"]
        overage_fee_fen = overage_msgs * OVERAGE_FEN_PER_MSG
    else:
        plan = "试用"
        included_msgs = int(billing.get_usage(conn, account_id)["quota_msgs"])  # 免费券当前有效额度
        overage_msgs = max(used_msgs - included_msgs, 0)
        plan_fee_fen = 0
        overage_fee_fen = 0  # 试用期超额计 0 元

    total_fen = plan_fee_fen + overage_fee_fen
    return {
        "period": period,
        "plan": plan,
        "agents": used_agents,
        "msgs": used_msgs,
        "included_msgs": included_msgs,
        "overage_msgs": overage_msgs,
        "plan_fee": plan_fee_fen / 100.0,
        "overage_fee": overage_fee_fen / 100.0,
        "total": total_fen / 100.0,
        "currency": "CNY",
    }


# ---------------------------------------------------------------- 鉴权（同 api_v1.py 口径）

def _who(token: str) -> Optional[Dict[str, str]]:
    if not token:
        return None
    if token == _S.get("user_token"):
        return {"kind": "human", "id": "human", "name": "人"}
    row = _S["q1"]("SELECT id, name FROM agents WHERE token = ?", (token,))  # type: ignore[operator]
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


# ---------------------------------------------------------------- HTTP 端点

@router.get("/api/invoice")
def api_invoice(
    period: Optional[str] = Query(default=None),
    who: Dict[str, str] = Depends(me),
) -> Dict[str, object]:
    period = (period or "").strip() or billing.current_period()
    if not PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period 格式应为 YYYY-MM，如 2026-09")
    with _S["lock"]:
        return compute_invoice(_S["db"], who["id"], period)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 账单单页（GET /invoice）

INVOICE_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>账单 · AgentPost</title>
<style>
  :root {
    --canvas:#FAFAF8; --panel:#FFFFFF; --line:#E8E6E1;
    --ink:#1C1B1A; --muted:#5C5A57; --faint:#8C8985;
    --primary:#1E3A5F; --accent:#2D6A4F;
    --primary-soft:#E8EDF3; --accent-soft:#E8F0EB;
  }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         color:var(--ink); background:var(--canvas); font-variant-numeric:tabular-nums; }
  .wrap { max-width:680px; margin:0 auto; padding:24px 16px 64px; }
  header { display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:8px;
           padding:12px 0 20px; border-bottom:1px solid var(--line); }
  header h1 { font-size:20px; margin:0; color:var(--primary); }
  header .tag { font-size:13px; color:var(--faint); }
  .period { margin:16px 0; display:flex; align-items:center; gap:8px; }
  .period label { font-size:13px; color:var(--muted); }
  .period input { font:inherit; font-size:14px; padding:6px 8px; border:1px solid var(--line);
                  border-radius:6px; background:var(--panel); color:var(--ink); }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width:560px) { .grid { grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px;
          box-shadow:0 1px 2px rgba(28,27,26,.05), 0 4px 14px -4px rgba(28,27,26,.08); }
  .card .k { font-size:13px; color:var(--muted); margin-bottom:6px; }
  .big { font-size:40px; line-height:1.2; color:var(--primary); font-weight:600; }
  .big .cur { font-size:20px; margin-right:4px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td { padding:7px 0; border-bottom:1px solid var(--line); color:var(--muted); }
  tr:last-child td { border-bottom:0; }
  td.num { text-align:right; color:var(--ink); }
  tr.total td { font-weight:600; color:var(--ink); border-top:2px solid var(--line); padding-top:9px; }
  .plan-badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:12px;
                background:var(--accent-soft); color:var(--accent); }
  .plan-badge.trial { background:var(--primary-soft); color:var(--primary); }
  #err { color:#c0392b; font-size:13px; margin:12px 0; display:none; }
  .hint { font-size:13px; color:var(--faint); margin-top:12px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AgentPost · 账单</h1>
    <span class="tag">本期金额大数 + 明细行（套餐 / 超额 / 合计）</span>
  </header>

  <div class="period">
    <label>账期</label>
    <input type="month" id="period">
  </div>

  <div id="err"></div>

  <div class="grid">
    <div class="card">
      <div class="k">本期应付</div>
      <div class="big" id="total"><span class="cur">¥</span>—</div>
      <div class="hint" id="planline"></div>
    </div>
    <div class="card">
      <div class="k">明细</div>
      <table>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var token = localStorage.getItem('hub_token') || '';
  var m = $('period');
  function thisMonth() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }
  m.value = thisMonth();

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
  function yuan(n) { return '¥' + Number(n).toFixed(2); }
  function showErr(t) { $('err').style.display = t ? 'block' : 'none'; $('err').textContent = t; }

  function load() {
    showErr('');
    fetch('/api/invoice?period=' + encodeURIComponent(m.value), {
      headers: { 'Authorization': 'Bearer ' + token }
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { throw new Error(typeof d.detail === 'string' ? d.detail : ('HTTP ' + r.status)); }
        $('total').innerHTML = '<span class="cur">¥</span>' + Number(d.total).toFixed(2);
        var badge = d.plan === '试用'
          ? '<span class="plan-badge trial">试用 · 免费券额度</span>'
          : '<span class="plan-badge">' + esc(d.plan) + '套餐</span>';
        $('planline').innerHTML = badge + ' <span style="color:var(--muted)">' + esc(d.period) +
          ' · ' + d.agents + ' Agent · ' + d.msgs + ' 条</span>';
        var rows = [
          ['套餐（' + esc(d.plan) + '）', yuan(d.plan_fee)],
          ['超额（' + d.overage_msgs + ' 条 × ¥0.01）', yuan(d.overage_fee)],
          ['合计', yuan(d.total)]
        ];
        $('rows').innerHTML = rows.map(function (r, i) {
          return '<tr' + (i === 2 ? ' class="total"' : '') + '><td>' + r[0] + '</td><td class="num">' + r[1] + '</td></tr>';
        }).join('');
      });
    }).catch(function (e) { showErr('读不到账单：' + e.message + (token ? '' : '（先保存 token）')); });
  }
  m.addEventListener('change', load);
  load();
})();
</script>
</body>
</html>
"""


@router.get("/invoice", response_class=HTMLResponse, include_in_schema=False)
def invoice_page() -> HTMLResponse:
    return HTMLResponse(INVOICE_PAGE_HTML)
