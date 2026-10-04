#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发邮件（带截图附件）给老板 —— 用于"拿不准就截图发邮件问"

用法：
    python3 mail_send.py --to hwalletapp@icloud.com --subject "【问】…" --body "……" --attach /tmp/x.png

凭据读 /etc/warm-mail.env（MAIL_IMAP_USER / MAIL_IMAP_PASS / MAIL_SMTP_HOST）。
只发给老板本人（白名单），别的不许发。
"""
import argparse
import mimetypes
import os
import smtplib
import sys
from email.message import EmailMessage

ENV = "/etc/warm-mail.env"


def load_env(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--attach", action="append", default=[])
    a = ap.parse_args()

    env = load_env(ENV)
    sender = env.get("MAIL_IMAP_USER")
    allowed = [w.strip().lower() for w in env.get("MAIL_WHITELIST", "").split(",") if w.strip()]
    if allowed and a.to.strip().lower() not in allowed:
        print("拒绝：收件人不在白名单（只允许发给 %s）" % ",".join(allowed))
        sys.exit(3)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = a.to
    msg["Subject"] = a.subject
    msg.set_content(a.body or "")
    for path in a.attach:
        if not os.path.exists(path):
            print("跳过不存在的附件：%s" % path)
            continue
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))

    s = smtplib.SMTP_SSL(env.get("MAIL_SMTP_HOST", "smtp.gmail.com"), 465, timeout=30)
    s.login(sender, env["MAIL_IMAP_PASS"])
    s.send_message(msg)
    s.quit()
    print("已发送给 %s（附件 %d 个）" % (a.to, len(a.attach)))


if __name__ == "__main__":
    main()
