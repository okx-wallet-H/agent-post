#!/usr/bin/env bash
# 群聊移动端/PWA 自测（#36）：manifest 链接、SW 注册、登录码入口、开启通知、375px 关键样式
# 用法：bash smoke_chat_mobile.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8792}"; BASE="http://127.0.0.1:$PORT"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-mobile-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

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

HUB_PORT="$PORT" HUB_DB="$TMPD/mobile.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "[1] PWA 资源路由"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/chat")
[ "$CODE" = "200" ] && ok "GET /chat → 200" || bad "GET /chat → $CODE"
for p in manifest.webmanifest sw.js icon.svg; do
  C=$(curl -s -o "$TMPD/$p" -w '%{http_code}' "$BASE/$p")
  [ "$C" = "200" ] && ok "GET /$p → 200" || bad "GET /$p → $C"
done
grep -q '"display": "standalone"' "$TMPD/manifest.webmanifest" && ok "manifest display=standalone" || bad "manifest 缺 standalone"
grep -q '"name": "AgentPost"' "$TMPD/manifest.webmanifest" && ok "manifest 名称 AgentPost" || bad "manifest 名称缺"
grep -q '"theme_color": "#1E3A5F"' "$TMPD/manifest.webmanifest" && ok "manifest 主题色 #1E3A5F" || bad "manifest 主题色缺"
grep -q "addEventListener('fetch'" "$TMPD/sw.js" && ok "SW 有 fetch 缓存壳" || bad "SW 缺 fetch"
grep -q "caches" "$TMPD/sw.js" && ok "SW 有缓存逻辑" || bad "SW 缺缓存"

echo "[2] 页面接线"
grep -q 'rel="manifest"' "$TMPD/page.html" && ok "页面链了 manifest" || bad "缺 manifest link"
grep -q "serviceWorker.register" "$TMPD/page.html" && ok "页面注册 Service Worker" || bad "缺 SW 注册"
grep -q "离线，恢复后自动同步" "$TMPD/page.html" && ok "离线提示文案" || bad "缺离线提示"
grep -q "用登录码进入" "$TMPD/page.html" && ok "登录码输入框" || bad "缺登录码入口"
grep -q "api/login-code" "$TMPD/page.html" && ok "登录码换 token 调用点" || bad "缺登录码调用"
grep -q "开启通知" "$TMPD/page.html" && ok "开启通知按钮" || bad "缺通知按钮"
grep -q "v1/push/subscribe" "$TMPD/page.html" && ok "推送订阅调用点" || bad "缺推送订阅调用"
grep -q "'已开'" "$TMPD/page.html" && grep -q "'未开'" "$TMPD/page.html" && grep -q "'不支持'" "$TMPD/page.html" && ok "通知状态三态（已开/未开/不支持）" || bad "通知状态三态缺"

echo "[3] 375px 移动端关键样式"
grep -q "drawer-btn" "$TMPD/page.html" && ok "抽屉按钮存在" || bad "缺抽屉按钮"
grep -q "transform:translateX(-100%)" "$TMPD/page.html" && ok "侧栏抽屉收起动画" || bad "缺抽屉动画"
grep -q "drawer-backdrop" "$TMPD/page.html" && ok "抽屉遮罩存在" || bad "缺遮罩"
grep -q "100dvh" "$TMPD/page.html" && ok "高度用 100dvh（键盘不遮）" || bad "缺 100dvh"
grep -q "env(safe-area-inset-bottom" "$TMPD/page.html" && ok "底部 safe-area 适配" || bad "缺 safe-area"
grep -q "viewport-fit=cover" "$TMPD/page.html" && ok "viewport-fit=cover" || bad "缺 viewport-fit"
grep -q ".bubble{max-width:92%}" "$TMPD/page.html" && ok "移动端气泡宽 92%" || bad "移动端气泡宽度缺"
grep -q "user-select:text" "$TMPD/page.html" && ok "长按可复制（user-select）" || bad "缺长按复制"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
