#!/usr/bin/env bash
# 站内帮助页（/help）自测：起临时库，断言页面 200 + 关键串
# 用法：bash smoke_help.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8796}"; BASE="http://127.0.0.1:$PORT"
PY="${PYTHON:-python3}"
TMPD="$DIR/.smoke-help-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

# 临时启动器：main 的 app + help_page.router（不改 main.py）
cat > "$TMPD/launcher.py" <<EOF
import os, sys
sys.path.insert(0, "$DIR")
import main
import help_page
main.app.include_router(help_page.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
EOF

HUB_PORT="$PORT" HUB_DB="$TMPD/help.db" "$PY" "$TMPD/launcher.py" >"$TMPD/srv.log" 2>&1 &
SRV=$!; sleep 4
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "[1] GET /help 单页 200 + 关键串"
CODE=$(curl -s -o "$TMPD/page.html" -w '%{http_code}' "$BASE/help")
[ "$CODE" = "200" ] && ok "GET /help → 200" || bad "GET /help → $CODE"
for kw in "三步" "第一步" "第二步" "第三步" "/v1/send" "/v1/inbox" "/v1/agents" "/v1/usage" "/mcp" \
          "暂未开放收费" "免费试用" "token 在哪" "收不到消息怎么查" "改名" "能发群吗" "怎么收费" \
          "GitHub" "注册" "建 Agent" "发第一条"; do
  grep -q -- "$kw" "$TMPD/page.html" && ok "页面含「$kw」" || bad "页面缺「$kw」"
done

echo "[2] 命令与入口"
grep -q -- "$BASE/api/accounts/register" "$TMPD/page.html" && ok "注册命令用本站地址（$BASE）" || bad "注册命令地址不对"
grep -q "__HUB__" "$TMPD/page.html" && bad "HUB 占位符没替换干净" || ok "HUB 已按部署地址替换（无 __HUB__ 残留）"
for kw in 'href="./"' 'href="./chat"' 'href="./connect"' 'https://github.com/okx-wallet-H/agent-post'; do
  grep -q -- "$kw" "$TMPD/page.html" && ok "页脚入口「$kw」" || bad "页脚缺「$kw」"
done

echo "[3] 无资源类外链（GitHub 文字链接是派活要求，不算）"
grep -qE '<(link|script)[^>]+(src|href)="https?://' "$TMPD/page.html" && bad "有 CDN/资源外链" || ok "无资源类外链"

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; tail -20 "$TMPD/srv.log"; exit 1; }
