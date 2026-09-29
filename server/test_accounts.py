#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accounts.py 自测（P1-A）：临时 DB 起真实 HTTP 服务，逐条断言多租户行为。
不改 main.py：同进程 import main，用 dependency_overrides + 函数替换接入 accounts 层。
只依赖标准库 + 已装的 fastapi/uvicorn。跑法：python3 test_accounts.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8796

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  <- {extra}" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def req(method: str, path: str, token: str | None = None, body: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=payload, headers=headers)
    r = c.getresponse()
    data = r.read().decode()
    c.close()
    try:
        j = json.loads(data)
    except Exception:
        j = data
    return r.status, j


def main() -> int:
    # ---- 临时 DB + 起服务（同进程，才能 override main 的鉴权）----
    tmp = tempfile.mkdtemp(prefix="hubtest_")
    db_path = os.path.join(tmp, "hub.db")
    os.environ["HUB_DB"] = db_path
    os.environ["HUB_USER_TOKEN"] = "t-human-token"
    sys.path.insert(0, SERVER_DIR)

    import main as hub          # 用临时 DB 建表（main 底部 db_init 只建表不起服务）
    import accounts

    accounts.ensure_schema(hub._db)               # 幂等建表 + 加列 + 默认账号归并
    hub.app.include_router(accounts.router)
    hub.app.dependency_overrides[hub.authenticate] = accounts.make_authenticate(
        hub._db, hub.USER_TOKEN
    )
    hub.require_conv_access = accounts.make_conv_access(hub._db)

    import uvicorn
    config = uvicorn.Config(hub.app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            s, _ = req("GET", "/health")
            if s == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        print("服务起不来"); return 1

    print("== 1. 注册 / 登录 ==")
    s, ja = req("POST", "/api/accounts/register", body={"email": "a@hub.local", "password": "aaaaaa"})
    check("注册 A 返回 200 且带 account_id/token", s == 200 and bool(ja.get("account_id")) and bool(ja.get("token")), f"status={s} body={ja}")
    tA, aidA = ja.get("token", ""), ja.get("account_id", "")

    s, jb = req("POST", "/api/accounts/register", body={"email": "b@hub.local", "password": "bbbbbb"})
    check("注册 B 返回 200", s == 200 and bool(jb.get("token")), f"status={s} body={jb}")
    tB, aidB = jb.get("token", ""), jb.get("account_id", "")

    s, dup = req("POST", "/api/accounts/register", body={"email": "a@hub.local", "password": "xxxxxx"})
    check("重复注册同一 email -> 400/409", s in (400, 409), f"status={s} body={dup}")

    s, bad = req("POST", "/api/accounts/login", body={"email": "a@hub.local", "password": "wrong!"})
    check("错误密码 login -> 401", s == 401, f"status={s}")
    s, ok = req("POST", "/api/accounts/login", body={"email": "a@hub.local", "password": "aaaaaa"})
    check("正确密码 login -> 200 带新 token", s == 200 and bool(ok.get("token")), f"status={s} body={ok}")

    print("== 2. 各自建 agent（走账号 token）==")
    s, ga = req("POST", "/api/accounts/agents", token=tA, body={"name": "A-助手"})
    check("A 建 agent 成功", s == 200 and bool(ga.get("token")), f"status={s} body={ga}")
    gA = ga.get("token", "")
    s, gb = req("POST", "/api/accounts/agents", token=tB, body={"name": "B-助手"})
    check("B 建 agent 成功", s == 200 and bool(gb.get("token")), f"status={s} body={gb}")
    gB = gb.get("token", "")

    print("== 3. /api/me 只看到自己的 agent ==")
    s, meA = req("GET", "/api/me", token=tA)
    namesA = [a["name"] for a in meA.get("agents", [])] if s == 200 else []
    check("A 的 /api/me 只含 A-助手", namesA == ["A-助手"], f"status={s} names={namesA}")
    s, meB = req("GET", "/api/me", token=tB)
    namesB = [a["name"] for a in meB.get("agents", [])] if s == 200 else []
    check("B 的 /api/me 只含 B-助手（看不到 A 的）", namesB == ["B-助手"], f"status={s} names={namesB}")

    print("== 4. 跨账号访问隔离（agent token 读对方会话 -> 403）==")
    # 直接造「已存在数据」：B 名下会话 + 一条消息 + 成员 b1（owner_account 列已由 ensure_schema 加好）
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    hub._db.execute(
        "INSERT INTO conversations (id, title, kind, created_at, owner_account) VALUES ('cv_b', 'B 的会话', 'dm', ?, ?)",
        (now, aidB),
    )
    hub._db.execute(
        "INSERT INTO members (conversation_id, agent_id) VALUES ('cv_b', ?)", (gb["id"],)
    )
    hub._db.execute(
        "INSERT INTO messages (id, conversation_id, from_kind, from_id, text, created_at) "
        "VALUES ('msg_b1', 'cv_b', 'agent', ?, 'B 的消息', ?)",
        (gb["id"], now),
    )
    hub._db.commit()

    s, _ = req("GET", "/api/conversations/cv_b/messages", token=gB)
    check("B 自己的 agent 读 B 会话 -> 200", s == 200, f"status={s}")
    s, body = req("GET", "/api/conversations/cv_b/messages", token=gA)
    check("A 的 agent token 读 B 名下会话 -> 403", s == 403, f"status={s} body={body}")
    s, body = req("GET", "/api/conversations/cv_b/messages", token=tB)
    check("B 的 account token 读自己会话 -> 200", s == 200, f"status={s}")
    s, body = req("GET", "/api/conversations/cv_b/messages", token=tA)
    check("A 的 account token 读 B 会话 -> 403", s == 403, f"status={s}")

    print("== 5. 幂等加列抽查 ==")
    cols_agents = [r[1] for r in hub._db.execute("PRAGMA table_info(agents)")]
    cols_convs = [r[1] for r in hub._db.execute("PRAGMA table_info(conversations)")]
    check("agents 有 owner_account 列", "owner_account" in cols_agents)
    check("conversations 有 owner_account 列", "owner_account" in cols_convs)
    accounts.ensure_schema(hub._db)   # 再跑一次不炸 = 幂等
    check("ensure_schema 二次执行不炸（幂等）", True)

    server.should_exit = True
    if FAILS:
        print("\nTEST: FAIL -> " + "; ".join(FAILS))
        return 1
    print("\nTEST: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
