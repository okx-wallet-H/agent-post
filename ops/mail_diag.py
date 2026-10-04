#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：把邮箱里最近的邮件（含「全部邮件」文件夹）列出来，确认老板的信到底有没有到。"""
import email
import imaplib
from email.header import decode_header

env = dict(l.split("=", 1) for l in open("/etc/warm-mail.env").read().strip().split("\n") if "=" in l)
M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
M.login(env["MAIL_IMAP_USER"], env["MAIL_IMAP_PASS"])


def dec(s):
    try:
        return "".join(t.decode(e or "utf-8", "replace") if isinstance(t, bytes) else t for t, e in decode_header(s or ""))
    except Exception:
        return str(s)


raw_boxes = [b.decode() for b in M.list()[1]]
targets = []
for line in raw_boxes:
    parts = line.split(' "')
    name = line.rsplit('"', 2)[-2] if line.endswith('"') else None
    if name and (name.upper() == "INBOX" or "YkBnCZCuTvY" in name or "All" in line):
        targets.append(name)
print("要看的文件夹：", targets)

for box in targets:
    st, d = M.select(box, readonly=True)
    if st != "OK":
        print(box, "→ 打不开", d)
        continue
    st, ids = M.search(None, "ALL")
    ids = ids[0].split()[-6:]
    print("--- %s（最近 %d 封）---" % (box, len(ids)))
    for i in ids:
        typ, data = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        blob = data[0][1].decode("utf-8", "replace") if data and len(data[0]) > 1 else ""
        m = email.message_from_string(blob)
        print("   ", dec(m.get("From"))[:32], "|", dec(m.get("Subject"))[:44], "|", m.get("Date"))
M.logout()
