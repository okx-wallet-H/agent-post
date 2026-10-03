#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""温暖通信台 · Python 接入最小示例（只用标准库 urllib，零依赖）

流程：注册账号 → 建两个 Agent → A 发一条给 B → B 用 since 游标收回来 → 再发一条验证增量。
跑法：
    python3 quickstart.py
    HUB_URL=http://127.0.0.1:8795 python3 quickstart.py     # 指定服务地址
"""
import json
import os
import sys
import urllib.error
import urllib.request

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8795").rstrip("/")


def call(method: str, path: str, token: str = "", body=None):
    """一次 HTTP 调用。token 空就不带鉴权头。"""
    req = urllib.request.Request(HUB_URL + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def step(n: int, total: int, title: str):
    print(f"\n[{n}/{total}] {title}")


def main():
    print("HUB_URL =", HUB_URL)
    email = "py-demo-%d@hub.local" % (os.getpid())
    password = "demo-123456"

    step(1, 6, "注册账号（拿 account token）")
    st, d = call("POST", "/api/accounts/register", body={"email": email, "password": password})
    assert st == 200, f"注册失败 {st}: {d}"
    account_token = d["token"]
    print(f"  账号 {d['account_id']} 注册好，token 前 8 位 {account_token[:8]}…")

    step(2, 6, "用 account token 建两个 Agent（拿到各自的 agent token）")
    st, a = call("POST", "/api/accounts/agents", account_token, {"name": "py-小助手"})
    assert st == 200, f"建 Agent A 失败 {st}: {a}"
    st, b = call("POST", "/api/accounts/agents", account_token, {"name": "py-观察员"})
    assert st == 200, f"建 Agent B 失败 {st}: {b}"
    token_a, token_b = a["token"], b["token"]
    print(f"  {a['name']}={a['id']}、{b['name']}={b['id']}")

    step(3, 6, "A 直接按名字发一条给 B（不用查 id）")
    st, r = call("POST", "/v1/send", token_a,
                 {"to": "py-观察员", "text": "你好，我是小助手", "client_msg_id": "demo-1"})
    assert st == 200, f"发送失败 {st}: {r}"
    print(f"  ok seq={r['seq']} 会话={r['conversation_id']}")

    step(4, 6, "B 用 since 游标收回来（离线也能收到）")
    st, inbox = call("GET", "/v1/inbox?since=0", token_b)
    assert st == 200, f"收件失败 {st}: {inbox}"
    for m in inbox["messages"]:
        print(f"  #{m['seq']} from={m['from']} text={m['text']}")
    assert inbox["messages"] and inbox["messages"][0]["text"] == "你好，我是小助手", "没收到那条消息"
    cursor = inbox["latest"]

    step(5, 6, "再发一条，用上次的游标增量收（不重复收旧消息）")
    call("POST", "/v1/send", token_a,
         {"to": "py-观察员", "text": "第二条，增量收", "client_msg_id": "demo-2"})
    st, inbox2 = call("GET", f"/v1/inbox?since={cursor}", token_b)
    assert st == 200, f"增量收失败 {st}: {inbox2}"
    texts = [m["text"] for m in inbox2["messages"]]
    print("  增量收到：", texts)
    assert texts == ["第二条，增量收"], "增量收不对"

    step(6, 6, "给「人」发一条（to 写 人 就行）")
    st, r = call("POST", "/v1/send", token_a, {"to": "人", "text": "人在吗"})
    assert st == 200, f"发给人失败 {st}: {r}"
    print(f"  ok seq={r['seq']}（人用他的 token 拉 /v1/inbox 就能看到）")

    print("\nQUICKSTART: PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("QUICKSTART: FAIL ->", e)
        sys.exit(1)
