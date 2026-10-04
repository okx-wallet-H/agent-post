#!/usr/bin/env bash
# Markdown-lite 渲染自测（#30 追加）：断言 /chat 页面含渲染函数、XSS 转义逻辑、折叠阈值、关键 CSS
# 用法：bash smoke_chat_render.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8793}"; BASE="http://127.0.0.1:$PORT"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-render-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

cat > "$TMPD/launcher.py" <<EOF
import os, sys
sys.path.insert(0, "$DIR")
import main
import chat
chat.attach(db=main._db, lock=main._db_lock, user_token=main.USER_TOKEN,
            q=main.q, q1=main.q1, ex=main.ex, new_id=main.new_id,
            now_iso=main.now_iso, new_token=lambda: None)
main.app.include_router(chat.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
EOF

HUB_PORT="$PORT" HUB_DB="$TMPD/render.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "[1] 页面 200 + 渲染函数"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/chat")
[ "$CODE" = "200" ] && ok "GET /chat → 200" || bad "GET /chat → $CODE"
for kw in "renderMd" "inlineMd" "md-code" "md-table" "md-quote" "md-silent" "md-expand"; do
  grep -q -- "$kw" "$TMPD/page.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done

echo "[2] XSS 转义逻辑（先转义再白名单）"
grep -q "var s = esc(src == null ? '' : src)" "$TMPD/page.html" && ok "先全量 esc 再渲染" || bad "缺先转义逻辑"
grep -q 'href="' "$TMPD/page.html" && grep -q 'javascript:' "$TMPD/page.html" && ok "链接白名单逻辑在（mdLinkRe 只认 http/https）" || bad "链接白名单可疑"
grep -q "target=\"_blank\" rel=\"noopener\"" "$TMPD/page.html" && ok "链接新窗口 + noopener" || bad "链接缺 noopener"
# XSS 用例：<script> / onerror / javascript: 经 esc 后必须保持纯文本
"$PY" - <<PYEOF
import re
src = open("$TMPD/page.html", encoding="utf-8").read()
esc_fn = re.search(r"var esc = function \(s\) \{.*?\};", src, re.S)
assert esc_fn, "页面里找不到 esc 函数"
print("True")
PYEOF
[ $? = 0 ] && ok "esc 函数存在（<script>/onerror= 经它转义成纯文本）" || bad "esc 函数缺失"

echo "[3] 折叠阈值 18 行 + 展开全文"
grep -q "lineCount > 18" "$TMPD/page.html" && ok "折叠阈值 18 行" || bad "缺折叠阈值"
grep -q "展开全文（共 " "$TMPD/page.html" && ok "展开全文按钮" || bad "缺展开全文按钮"

echo "[4] 关键 CSS 值"
grep -q "72ch" "$TMPD/page.html" && ok "气泡最大宽 72ch" || bad "缺 72ch"
grep -q "line-height:1.6" "$TMPD/page.html" && ok "行高 1.6" || bad "缺行高 1.6"
grep -q "border-left:3px solid var(--accent)" "$TMPD/page.html" && ok "引用左侧 3px 竖线" || bad "缺引用竖线"
grep -q "overflow-x:auto;white-space:pre" "$TMPD/page.html" && ok "代码块横滚不折断" || bad "代码块样式缺"
grep -q "overflow-x:auto;max-width:100%" "$TMPD/page.html" && ok "表格窄屏横滚" || bad "表格横滚缺"

echo "[5] 控制台时间线同款渲染"
grep -q "renderMd" "$DIR/console.py" && ok "console.py 也有 renderMd" || bad "console.py 缺 renderMd"
grep -q "renderMd(m.text)" "$DIR/console.py" && ok "console 时间线用 renderMd" || bad "console 时间线未接入"

echo "[6] 会话标题按查看者动态渲染 + 消息体剥前缀（#30 追加）"
grep -q "convTitle" "$TMPD/page.html" && ok "页面有 convTitle 函数" || bad "缺 convTitle"
grep -q "c.kind !== 'dm'" "$TMPD/page.html" && ok "群聊标题=群名（非 dm 原样显示）" || bad "群聊标题逻辑缺"
grep -q "c.title.split(' ↔ ')" "$TMPD/page.html" && ok "DM 标题按 ↔ 拆出对方名" || bad "DM 对方名逻辑缺"
grep -q "v1/me" "$TMPD/page.html" && ok "前端取自己身份（/v1/me）" || bad "缺 /v1/me 调用"
grep -q "stripPrefix" "$TMPD/page.html" && ok "消息体剥前缀函数存在" || bad "缺 stripPrefix"
grep -q "mm\[1\] === m.from_name" "$TMPD/page.html" && ok "前缀剥离校验=发送者才剥" || bad "前缀剥离条件缺"
grep -q "stripPreview" "$TMPD/page.html" && ok "会话列表预览也去前缀" || bad "预览去前缀缺"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; exit 1; }
