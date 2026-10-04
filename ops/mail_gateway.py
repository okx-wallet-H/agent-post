#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮件入口网关（秒级）：老板的邮件 → 邮局消息（收件人=温暖）

设计（2026-10-05 定）：
- 只认白名单发件人（MAIL_WHITELIST），其它邮件一律忽略（记录也不记，省事）
- 只有「主题或正文以 MAIL_PREFIXES 里任一个开头」才算指令（如 【任务】/【问】/【紧急】）
- 命中后 POST 到邮局 /v1/send：发件人=人，收件人=温暖；正文=主题+正文，末尾附 [mail:<Message-ID>]
- Message-ID 落 /var/lib/warm-mail/seen.txt 去重（一封只发一次）
- 免打扰：北京时间 01:00–07:00 只记不发（除非主题含【紧急】）
- 连接用 IMAP IDLE（准实时）；IDLE 不支持/超时则退回 60 秒轮询
- 任何异常只记日志，绝不退出（systemd 会重启，但重连更平滑）

凭据从 /etc/warm-mail.env 读（root 600），邮局管理员 token 从 /etc/warm-hub.env 读。
"""
import email
import email.header
import imaplib
import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ENV_MAIL = "/etc/warm-mail.env"
ENV_HUB = "/etc/warm-hub.env"
STATE_DIR = "/var/lib/warm-mail"
SEEN = os.path.join(STATE_DIR, "seen.txt")
HUB_URL = os.environ.get("MAIL_HUB_URL", "http://127.0.0.1:8795")
CN = timezone(timedelta(hours=8))  # 北京时间


def load_env(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:  # noqa: BLE001
        log("读 %s 失败：%s" % (path, e))
    return out


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now(CN).strftime("%F %T"), msg), flush=True)


def seen_ids() -> set:
    try:
        with open(SEEN, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(mid: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SEEN, "a", encoding="utf-8") as f:
        f.write(mid + "\n")


def decode_hdr(raw) -> str:
    if raw is None:
        return ""
    try:
        parts = email.header.decode_header(raw)
        out = ""
        for text, enc in parts:
            out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
        return out
    except Exception:  # noqa: BLE001
        return str(raw)


def body_text(msg) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            return ""
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def in_quiet_hours(subject: str, body: str) -> bool:
    now = datetime.now(CN)
    quiet = 1 <= now.hour < 7
    if not quiet:
        return False
    return "【紧急】" not in subject and "【紧急】" not in body


def send_ack(cfg: dict, to_addr: str, orig_subject: str) -> None:
    """收到即自动回执（老板 2026-10-05 定：每封邮件都要有回信说明）。"""
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText("已收到你的邮件，我开始办了。\n\n原主题：%s\n\n（做完或没做成，我都会再回一封说明原因）" % orig_subject, "plain", "utf-8")
        msg["Subject"] = "【已收到】" + orig_subject
        msg["From"] = cfg.get("MAIL_IMAP_USER", "")
        msg["To"] = to_addr
        s = smtplib.SMTP_SSL(cfg.get("MAIL_SMTP_HOST", "smtp.gmail.com"), 465, timeout=20)
        s.login(cfg["MAIL_IMAP_USER"], cfg["MAIL_IMAP_PASS"])
        s.sendmail(cfg["MAIL_IMAP_USER"], [to_addr], msg.as_string())
        s.quit()
        log("已回执：%s" % to_addr)
    except Exception as e:  # noqa: BLE001
        log("回执失败（不影响投递）：%s" % e)


def send_to_hub(text: str, admin_token: str, subject: str) -> bool:
    payload = json.dumps({"to": "温暖", "text": text, "client_msg_id": "mail-%s" % abs(hash(subject + text))}).encode()
    req = urllib.request.Request(HUB_URL + "/v1/send", data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + admin_token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log("已投递邮局：%s" % r.read().decode()[:160])
        return True
    except Exception as e:  # noqa: BLE001
        log("投递邮局失败：%s" % e)
        return False


def handle(raw_bytes: bytes, cfg: dict, admin_token: str) -> None:
    msg = email.message_from_bytes(raw_bytes)
    frm = decode_hdr(msg.get("From", "")).lower()
    subject = decode_hdr(msg.get("Subject", ""))
    mid = (msg.get("Message-ID") or "").strip() or ("nosubject-%s" % abs(hash(subject)))
    body = body_text(msg)
    whitelist = [w.strip().lower() for w in cfg.get("MAIL_WHITELIST", "").split(",") if w.strip()]
    if whitelist and not any(w in frm for w in whitelist):
        log("跳过（不在白名单）：%s | %s" % (frm[:48], subject[:40]))
        return
    prefixes = [p.strip() for p in cfg.get("MAIL_PREFIXES", "").split(",") if p.strip()]
    head = (subject + "\n" + body).lstrip()
    if prefixes and not any(head.startswith(p) for p in prefixes):
        log("白名单内但无前缀，忽略：%s" % subject[:60])
        return
    if mid in seen_ids():
        return
    who = decode_hdr(msg.get("From", ""))
    when = decode_hdr(msg.get("Date", ""))
    text = "【邮件入口】来自 %s（%s）\n主题：%s\n\n%s\n\n[mail:%s]" % (who, when, subject, body.strip()[:4000], mid)
    if in_quiet_hours(subject, body):
        log("免打扰时段，只记不发（%s 至 07:00）：%s" % (datetime.now(CN).strftime("%H:%M"), subject[:60]))
        mark_seen(mid)
        return
    if send_to_hub(text, admin_token, subject):
        mark_seen(mid)
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", who)
        if m:
            send_ack(cfg, m.group(0), subject)
        log("已处理：%s" % subject[:60])


def main() -> None:
    cfg = load_env(ENV_MAIL)
    hub = load_env(ENV_HUB)
    admin_token = hub.get("WARM_HUB_USER_TOKEN") or hub.get("HUB_USER_TOKEN") or os.environ.get("HUB_USER_TOKEN", "")
    if not cfg.get("MAIL_IMAP_PASS"):
        log("缺少 MAIL_IMAP_PASS，退出")
        sys.exit(2)
    log("启动：user=%s whitelist=%s prefixes=%s hub=%s" % (cfg.get("MAIL_IMAP_USER"), cfg.get("MAIL_WHITELIST"), cfg.get("MAIL_PREFIXES"), HUB_URL))
    idle_ok = "--poll" not in sys.argv
    while True:
        try:
            M = imaplib.IMAP4_SSL(cfg.get("MAIL_IMAP_HOST", "imap.gmail.com"), 993)
            M.login(cfg["MAIL_IMAP_USER"], cfg["MAIL_IMAP_PASS"])
            M.select("INBOX", readonly=True)
            log("IMAP 已连接（IDLE=%s）" % idle_ok)
            while True:
                typ, data = M.search(None, "UNSEEN")
                ids = data[0].split() if typ == "OK" else []
                for num in ids[-20:]:
                    typ, d = M.fetch(num, "(RFC822)")
                    if typ == "OK" and d and d[0]:
                        handle(d[0][1], cfg, admin_token)
                if not idle_ok:
                    time.sleep(60)
                    continue
                try:
                    M.select("INBOX", readonly=True)
                    tag = M._new_tag()
                    M.send(tag + b" IDLE\r\n")
                    line = M.readline()
                    if not line.startswith(b"+"):
                        raise RuntimeError("IDLE 未获准：%r" % line[:60])
                    sock = getattr(M, "sock", None)
                    sock.settimeout(25 * 60)
                    try:
                        sock.recv(1024)
                    except socket.timeout:
                        pass
                    M.send(b"DONE\r\n")
                    M.readline()
                except Exception as e:  # noqa: BLE001
                    log("IDLE 异常，转轮询 60s：%s" % e)
                    time.sleep(60)
            M.logout()
        except Exception as e:  # noqa: BLE001
            log("连接异常，10 秒后重连：%s" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
