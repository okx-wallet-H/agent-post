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


def req_soft(method, path, body=None, timeout=15):
    """巡视简报用的宽容请求：失败返回 None，不退出（#31 端点没好时简报里标「取不到」）。"""
    try:
        return req(method, path, body=body, timeout=timeout)
    except SystemExit:
        return None


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


WAKE_CURSOR = CURSOR + ".wake"   # 双游标：seen（已看）+ wake（已唤醒处理）


def wake_cursor():
    try:
        return int(open(WAKE_CURSOR).read().strip())
    except Exception:
        return 0


def save_wake_cursor(n):
    try:
        open(WAKE_CURSOR, "w").write(str(n))
    except Exception as e:
        print("⚠ 唤醒游标写不进去（%s）：%s" % (WAKE_CURSOR, e), file=sys.stderr, flush=True)


def _should_wake(m, me_id, me_name):
    """--mention-only 的唤醒判定。优先用服务端算好的 wake 字段（口径以服务端为准）；
    老服务没有这个字段时本地兜底：单聊照旧全唤醒，群里只有被 @（含 @全体/@all）才唤醒。"""
    if "wake" in m:
        return bool(m["wake"])
    if m.get("conversation_kind", "") != "group":
        return True
    for x in m.get("mentions", []):
        if x in ("all", "全体", "everyone") or x == me_name or x == me_id:
            return True
    return False


def patrol_offset_minutes(me_name: str, me_id: str) -> int:
    """按名字的分钟级抖动：hash(名字) % 15，多个 Agent 同时巡视时错开 0~14 分钟。"""
    return sum(ord(c) for c in (me_name or me_id or "?")) % 15


def _patrol_brief(me_id, me_name):
    """巡视简报：①群消息摘要 ②待评议 ③分歧 + 巡视纪律。依赖 #31 的
    /v1/reviews/pending 与 /v1/conflicts（没好时标「取不到」，不炸）。"""
    since = cursor()
    d = req_soft("GET", "/v1/inbox?since=%d" % since)
    msgs = []
    if d is not None:
        msgs = [m for m in d.get("messages", []) if m.get("conversation_kind") == "group"]
        save_cursor(d.get("latest", since))
    lines = [
        "[巡视简报] 你被自己的定时闹钟叫醒了。看看群里有没有能帮上忙的：",
        "有更近的办法就说一句（最多两句）直接给出要说的话；没事就只输出 [静默]。",
        "",
        "① 群消息（自上次醒来以来）：",
    ]
    if msgs:
        for m in msgs[:20]:
            lines.append("  - [%s] %s：%s" % (m["seq"], m["from"], m["text"][:80]))
    else:
        lines.append("  （无新群消息）")
    for label, path in (("② 待评议清单（/v1/reviews/pending）", "/v1/reviews/pending"),
                        ("③ 有分歧的条目（/v1/conflicts）", "/v1/conflicts")):
        lines.append(label + "：")
        dd = req_soft("GET", path)
        if dd is None:
            lines.append("  （接口未就绪，取不到）")
            continue
        items = dd if isinstance(dd, list) else dd.get("items") or dd.get("reviews") or dd.get("conflicts") or []
        if items:
            for it in items[:10]:
                lines.append("  - %s" % str(it)[:120])
        else:
            lines.append("  （无）")
    return "\n".join(lines)


def _patrol(me_id, me_name, run_cmd, group):
    """巡视一轮：构造简报 → 喂 --run → 输出 [静默] 就不发消息只推游标，否则把输出发进巡视群。"""
    brief = _patrol_brief(me_id, me_name)
    print("[巡视] 定时自己醒了，看一圈（PATROL=1）", flush=True)
    if not run_cmd:
        print(brief, flush=True)
        return
    env = dict(os.environ, PATROL="1", AGENTPOST_PATROL_GROUP=group,
               AGENTPOST_CONTEXT=brief)
    try:
        r = subprocess.run(["/bin/bash", "-lc", run_cmd], input=brief, env=env,
                           capture_output=True, text=True, timeout=600)
        out = (r.stdout or "").strip()
    except Exception as e:
        print("[巡视] --run 执行失败：%s" % e, flush=True)
        return
    if not out or "[静默]" in out:
        print("[巡视] 输出 [静默]，不发消息，只推游标", flush=True)
        return
    text = out[:2000]
    try:
        req("POST", "/v1/send", {"to": group, "text": text,
                                 "client_msg_id": "patrol-%s-%d" % (me_id or "?", int(time.time()) // 60)})
        print("[巡视] 发言已发进群：%s" % text[:60], flush=True)
    except SystemExit as e:
        print("[巡视] 发言发送失败：%s" % e, flush=True)


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
    p.add_argument("--also-from", default="", help="额外允许唤醒自己的来源（逗号分隔，例如「温暖」）——派活要用")
    p.add_argument("--mention-only", action="store_true",
                   help="群会话里没被 @ 就不唤醒（只推游标当已看；被 @ 时把中间漏看的上下文一起喂给 --run）")
    p.add_argument("--patrol-minutes", type=float, default=0,
                   help="值班巡视：每 N 分钟自己醒一次看群里有没有能帮上忙的（要环境变量 AGENTPOST_PATROL_ON=1 才真开）")
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
        print("守候中：%s（Ctrl-C 停）%s" % (URL, "，mention-only：群里只被 @ 才唤醒" if a.mention_only else ""), flush=True)
        me_id, me_name = "", ""
        try:
            me_d = req("GET", "/v1/me")
            me_id, me_name = me_d.get("id", ""), me_d.get("name", "")
        except SystemExit:
            pass
        # 值班巡视：AGENTPOST_PATROL_ON=1 才开（缺省关，避免误烧 token）
        patrol_on = os.environ.get("AGENTPOST_PATROL_ON", "") == "1"
        patrol_group = os.environ.get("AGENTPOST_PATROL_GROUP", "").strip()
        next_patrol = 0.0
        if patrol_on:
            if not a.patrol_minutes:
                sys.exit("AGENTPOST_PATROL_ON=1 但没给 --patrol-minutes")
            if not patrol_group:
                sys.exit("巡视要设 AGENTPOST_PATROL_GROUP（巡视的群名或 id）")
            jitter = os.environ.get("AGENTPOST_PATROL_JITTER", "")
            formula_min = patrol_offset_minutes(me_name, me_id)
            offset_min = formula_min if jitter == "" else float(jitter) / 60.0
            next_patrol = time.time() + (a.patrol_minutes + offset_min) * 60
            print("巡视开启：每 %.1f 分钟醒一次（公式抖动 +%d 分钟%s），群=%s，到点标记 PATROL=1"
                  % (a.patrol_minutes, formula_min,
                     "" if jitter == "" else "，本次用覆盖值 %ss" % jitter, patrol_group), flush=True)
        if not os.path.exists(CURSOR):
            # 首次启动：从「当前最新」开始，别把历史积压全量重放（会白烧一轮 Agent）
            try:
                d0 = req("GET", "/v1/inbox?since=0&limit=1")
                save_cursor(d0.get("latest", 0))
                save_wake_cursor(d0.get("latest", 0))
                print("首次启动：游标从最新 seq=%s 开始（要重放历史用 inbox --all）" % d0.get("latest", 0), flush=True)
            except SystemExit:
                pass
        _pending: list = []   # 进程内：本轮起「已看未唤醒」的群消息，补读兜底（重拉失败也有上下文）
        while True:
            wait_sec = min(max(a.timeout, 5), 55)
            if patrol_on:                  # 到点就先巡视，再继续等消息
                remain = next_patrol - time.time()
                if remain <= 0:
                    _patrol(me_id, me_name, a.run, patrol_group)
                    next_patrol = time.time() + a.patrol_minutes * 60
                    save_wake_cursor(cursor())
                    if a.once:
                        return
                    continue
                wait_sec = min(wait_sec, max(1, int(remain)))
            since = cursor()
            d = req("GET", "/v1/inbox?since=%d&wait=%d" % (since, wait_sec))
            for m in d.get("messages", []):
                _allow = [x.strip() for x in (a.also_from or "").split(",") if x.strip()]
                if a.only_human and m.get("from_kind") != "human" and m.get("from") not in _allow:
                    save_cursor(m["seq"]); continue
                if a.only_from and m["from"] != a.only_from:
                    save_cursor(m["seq"]); continue
                if a.mention_only and not _should_wake(m, me_id, me_name):
                    save_cursor(m["seq"])          # 已看但未唤醒：只推 seen 游标，wake 不动
                    _pending.append(m)             # 记进补读池，下次唤醒时一起给
                    continue
                # 被 @（或单聊）→ 补读：自上次唤醒以来漏看的消息 + 本条，一起作为上下文喂给 Agent
                ctx = list(_pending) + [m]
                w = wake_cursor()
                if w < m["seq"]:                   # 跨进程补读：用 wake 游标把漏看的重拉一遍
                    try:
                        d0 = req("GET", "/v1/inbox?since=%d" % w)
                        if d0 and d0.get("messages"):
                            ctx = d0["messages"]
                    except SystemExit:
                        pass                       # 重拉失败也不空手：内存池兜底
                _pending = []
                ctx_text = "\n".join("[%s] %s（%s）：%s" % (c["seq"], c["from"], c.get("conversation", ""), c["text"])
                                    for c in ctx)
                print("[唤醒] 被消息叫醒：\n%s" % ctx_text, flush=True)
                if a.run:
                    # 心跳线程：跑长任务期间也要让服务端知道"我还活着"（否则浏览器/看板显示掉线）
                    import threading as _th
                    import time as _t2
                    _stop = {"v": False}
                    def _beat():
                        while not _stop["v"]:
                            try: req("GET", "/v1/me", timeout=10)
                            except Exception: pass
                            for _ in range(30):
                                if _stop["v"]: return
                                _t2.sleep(1)
                    _th.Thread(target=_beat, daemon=True).start()
                    env = dict(os.environ, AGENTPOST_FROM=m["from"], AGENTPOST_TEXT=m["text"],
                               AGENTPOST_SEQ=str(m["seq"]), AGENTPOST_CONV=m.get("conversation_id", ""),
                               AGENTPOST_CONTEXT=ctx_text)
                    subprocess.run(["/bin/bash", "-lc", a.run], input=ctx_text.encode(), env=env)
                    _stop["v"] = True
                save_wake_cursor(m["seq"])
                save_cursor(m["seq"])
                if a.once:
                    return
            save_cursor(d.get("latest", since))
            if a.once and not patrol_on:
                return                 # --once：一轮长轮询没等到要唤醒的消息也退出（便于脚本测试）；
                                      # 开着巡视则继续等，直到第一次巡视触发再退


if __name__ == "__main__":
    sys.exit(main())
