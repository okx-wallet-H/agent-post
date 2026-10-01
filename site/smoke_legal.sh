#!/usr/bin/env bash
# 条款/隐私/退款三页自测：起本地站，断言三页 200 + 关键段落 + 无外链 + 承诺句留占位符
# 用法：bash smoke_legal.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HUB_PORT:-8803}"; BASE="http://127.0.0.1:$PORT"
TMPD="$DIR/.smoke-legal-tmp"; rm -rf "$TMPD"; mkdir -p "$TMPD"

python3 -m http.server $PORT --bind 127.0.0.1 --directory "$DIR" >"$TMPD/srv.log" 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null' EXIT
sleep 1

PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }
check(){ # check <页面> <描述> <grep 模式>
  if grep -q -- "$3" "$TMPD/$1"; then ok "$2"; else bad "$2（$1 里找不到「$3」）"; fi
}

echo "[1] 三页 200"
for p in terms privacy refund; do
  CODE=$(curl -s -o "$TMPD/$p.html" -w '%{http_code}' "$BASE/$p.html")
  [ "$CODE" = "200" ] && ok "$p.html → 200" || bad "$p.html → $CODE"
done

echo "[2] 服务条款关键段落"
check terms.html "含「服务条款」" "服务条款"
check terms.html "不承诺 SLA" "不承诺"
check terms.html "SLA 开发中" "开发中"
check terms.html "含「禁止用途」" "禁止用途"
check terms.html "含「终止条件」" "终止条件"

echo "[3] 隐私说明关键段落"
check privacy.html "含「隐私说明」" "隐私说明"
check privacy.html "存什么（消息内容）" "消息内容"
check privacy.html "存什么（账号邮箱）" "账号邮箱"
check privacy.html "存什么（用量）" "用量"
check privacy.html "谁能看" "谁能看"
check privacy.html "导出与删除" "导出"
check privacy.html "删除申请" "删除"

echo "[4] 退款说明关键段落"
check refund.html "含「退款说明」" "退款说明"
check refund.html "按比例退" "按比例"
check refund.html "A2MCP 不退款" "A2MCP"
check refund.html "按次调用不退款" "不退款"

echo "[5] 页脚互相链接（三页 × 三链接）"
for p in terms privacy refund; do
  N=$(grep -oE 'href="(terms|privacy|refund)\.html"' "$TMPD/$p.html" | sort -u | wc -l)
  [ "$N" -ge 3 ] && ok "$p 页脚互相链接（$N 个）" || bad "$p 页脚链接不足：$N 个"
done

echo "[6] 无外链"
for p in terms privacy refund; do
  grep -qE '(href|src)="https?://' "$TMPD/$p.html" && bad "$p 有外链" || ok "$p 无外链"
done

echo "[7] 对外承诺句留占位符（纪律）"
for p in terms privacy refund; do
  # HTML 里尖括号按实体 &lt;/&gt; 写（渲染出来就是 <待老板确认>），断言核心词
  grep -q '待老板确认' "$TMPD/$p.html" && ok "$p 承诺句已留「<待老板确认>」占位符" || bad "$p 没有留占位符"
done

echo; echo "=== 结果：通过 $PASS 项，失败 $FAIL 项 ==="
[ "$FAIL" = "0" ] && echo "TEST: PASS" || { echo "TEST: FAIL"; exit 1; }
