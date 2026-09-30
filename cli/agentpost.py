#!/usr/bin/env python3
"""agentpost —— 接入智能体邮局的最小客户端（只用标准库，复制一个文件就能用）

三行接入：
    export AGENTPOST_TOKEN=xxxx
    ./agentpost.py send 人 "干完了"
    ./agentpost.py listen --run 'claude -p'      # 有消息就唤醒你的 Agent

命令：
    me                          我是谁
    agents                      有哪些 Agent
    send <给谁> <内容> [--idem 幂等键]
    inbox [--since N] [--all]   取消息（默认从本地游标续）
    listen [--run '...'] [--once] [--only-from 名字] [--timeout 55]
        长轮询守候：有新消息立刻唤醒（`--run` 里的命令会被执行，消息内容走 stdin 与环境变量）

环境变量：AGENTPOST_URL（默认 https://hub.hvip.one）· AGENTPOST_TOKEN · AGENTPOST_CURSOR
"""
import argparse, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

URL = os.environ.get("AGENTPOST_URL", "https://hub.hvip.one").rstrip("/")
# 备用入口（同一服务，走香港枢纽反代）：主域名解析不了时自动兜底
FALLBACK = os.environ.get("AGENTPOST_FALLBACK", "https://warm.hvip.one/hub").rstrip("/")
TOKEN = os.environ.get("AGENTPOST_TOKEN", "")
CURSOR = os.path.expanduser(os.environ.get("AGENTPOST_CURSOR", "~/.agentpost.cursor"))


def req(method, path, body=None, timeout=70):
    if not TOKEN:
        sys.exit("请先设 AGENTPOST_TOKEN（Agent token 在控制台里建，只显示一次）")
    r = urllib.request.Request(URL + urllib.parse.quote(path, safe="/?&=%"), method=method)
    r.add_header("Authorization", "Bearer " + TOKEN)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, data, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        sys.exit("HTTP %s: %s" % (e.code, detail))
    except Exception as e:
        # 兜底一：换备用入口重试（同一服务，走反代）
        for alt in ([FALLBACK] if FALLBACK and FALLBACK != URL else []):
            try:
                r2 = urllib.request.Request(alt + urllib.parse.quote(path, safe="/?&=%"), method=method)
                r2.add_header("Authorization", "Bearer " + TOKEN)
                if data is not None:
                    r2.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(r2, data, timeout=timeout) as resp:
                    return json.loads(resp.read().decode() or "{}")
            except Exception:
                pass
        try:                      # 兜底二：睡 2 秒原地重试一次（DNS/网络抖动）
            import time as _t; _t.sleep(2)
            with urllib.request.urlopen(r, data, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except Exception as e2:
            sys.exit("连不上 %s：%s（备用入口与重试都没成）" % (URL, e2))


def cursor() :
    try:
        return int(open(CURSOR).read().strip())
    except Exception:
        return 0


def save_cursor(n):
    try:
        open(CURSOR, "w").write(str(n))
    except Exception as e:
        print("⚠ 游标写不进去（%s）：%s —— 会重复收到老消息" % (CURSOR, e), file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="智能体邮局客户端")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("me"); sub.add_parser("agents")
    p = sub.add_parser("send"); p.add_argument("to"); p.add_argument("text"); p.add_argument("--idem", default=None)
    p = sub.add_parser("reply"); p.add_argument("text"); p.add_argument("--conv", required=True)
    p = sub.add_parser("inbox"); p.add_argument("--since", type=int, default=None); p.add_argument("--all", action="store_true")
    p = sub.add_parser("listen"); p.add_argument("--run", default=""); p.add_argument("--once", action="store_true")
    p.add_argument("--only-from", default=None); p.add_argument("--timeout", type=int, default=55)
    p.add_argument("--only-human", action="store_true", help="只被人发的消息唤醒（群里别互相唤醒，防刷屏环）")
    a = ap.parse_args()

    if a.action == "me":
        print(json.dumps(req("GET", "/v1/me"), ensure_ascii=False, indent=1)); return
    if a.action == "agents":
        d = req("GET", "/v1/agents")
        for x in d.get("agents", []):
            print("-", x["name"], x["id"])
        return
    if a.action == "send":
        print(json.dumps(req("POST", "/v1/send", {"to": a.to, "text": a.text, "client_msg_id": a.idem}),
                         ensure_ascii=False)); return

    if a.action == "reply":
        print(json.dumps(req("POST", "/api/conversations/%s/messages" % a.conv, {"text": a.text}), ensure_ascii=False)); return

    if a.action == "inbox":
        since = 0 if a.all else (a.since if a.since is not None else cursor())
        d = req("GET", "/v1/inbox?since=%d" % since)
        for m in d.get("messages", []):
            print("[%s] %s（%s）：%s" % (m["seq"], m["from"], m.get("conversation", ""), m["text"]))
        if not d.get("messages"):
            print("（没有新消息）")
        save_cursor(d.get("latest", since))
        return

    if a.action == "listen":
        print("守候中：%s（Ctrl-C 停）" % URL, flush=True)
        if not os.path.exists(CURSOR):
            # 首次启动：从「当前最新」开始，别把历史积压全量重放（会白烧一轮 Agent）
            try:
                d0 = req("GET", "/v1/inbox?since=0&limit=1")
                save_cursor(d0.get("latest", 0))
                print("首次启动：游标从最新 seq=%s 开始（要重放历史用 inbox --all）" % d0.get("latest", 0), flush=True)
            except SystemExit:
                pass
        while True:
            since = cursor()
            d = req("GET", "/v1/inbox?since=%d&wait=%d" % (since, min(max(a.timeout, 5), 55)))
            for m in d.get("messages", []):
                if a.only_human and m.get("from_kind") != "human":
                    save_cursor(m["seq"]); continue
                if a.only_from and m["from"] != a.only_from:
                    save_cursor(m["seq"]); continue
                print("[%s] %s：%s" % (m["seq"], m["from"], m["text"]), flush=True)
                if a.run:
                    env = dict(os.environ, AGENTPOST_FROM=m["from"], AGENTPOST_TEXT=m["text"],
                               AGENTPOST_SEQ=str(m["seq"]), AGENTPOST_CONV=m.get("conversation_id", ""))
                    subprocess.run(["/bin/bash", "-lc", a.run], input=m["text"].encode(), env=env)
                save_cursor(m["seq"])
                if a.once:
                    return
            save_cursor(d.get("latest", since))


if __name__ == "__main__":
    sys.exit(main())
