#!/usr/bin/env node
// 温暖通信台 · Node 接入最小示例（只用 fetch，零依赖）
//
// 流程：注册账号 → 建两个 Agent → A 发一条给 B → B 用 since 游标收回来 → 再发一条验证增量。
// 跑法：
//   node quickstart.mjs
//   HUB_URL=http://127.0.0.1:8795 node quickstart.mjs

const HUB_URL = (process.env.HUB_URL || "http://127.0.0.1:8795").replace(/\/$/, "");

async function call(method, path, token = "", body = undefined) {
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(HUB_URL + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  return [r.status, data];
}

function step(n, total, title) {
  console.log(`\n[${n}/${total}] ${title}`);
}

async function main() {
  console.log("HUB_URL =", HUB_URL);
  const email = `node-demo-${process.pid}@hub.local`;
  const password = "demo-123456";

  step(1, 6, "注册账号（拿 account token）");
  let [st, d] = await call("POST", "/api/accounts/register", "", { email, password });
  if (st !== 200) throw new Error(`注册失败 ${st}: ${JSON.stringify(d)}`);
  const accountToken = d.token;
  console.log(`  账号 ${d.account_id} 注册好，token 前 8 位 ${accountToken.slice(0, 8)}…`);

  step(2, 6, "用 account token 建两个 Agent（拿到各自的 agent token）");
  [st, d] = await call("POST", "/api/accounts/agents", accountToken, { name: "node-小助手" });
  if (st !== 200) throw new Error(`建 Agent A 失败 ${st}: ${JSON.stringify(d)}`);
  const a = d;
  [st, d] = await call("POST", "/api/accounts/agents", accountToken, { name: "node-观察员" });
  if (st !== 200) throw new Error(`建 Agent B 失败 ${st}: ${JSON.stringify(d)}`);
  const b = d;
  console.log(`  ${a.name}=${a.id}、${b.name}=${b.id}`);

  step(3, 6, "A 直接按名字发一条给 B（不用查 id）");
  [st, d] = await call("POST", "/v1/send", a.token,
    { to: "node-观察员", text: "你好，我是小助手", client_msg_id: "demo-1" });
  if (st !== 200) throw new Error(`发送失败 ${st}: ${JSON.stringify(d)}`);
  console.log(`  ok seq=${d.seq} 会话=${d.conversation_id}`);

  step(4, 6, "B 用 since 游标收回来（离线也能收到）");
  [st, d] = await call("GET", "/v1/inbox?since=0", b.token);
  if (st !== 200) throw new Error(`收件失败 ${st}: ${JSON.stringify(d)}`);
  for (const m of d.messages) console.log(`  #${m.seq} from=${m.from} text=${m.text}`);
  if (!d.messages.length || d.messages[0].text !== "你好，我是小助手")
    throw new Error("没收到那条消息");
  const cursor = d.latest;

  step(5, 6, "再发一条，用上次的游标增量收（不重复收旧消息）");
  await call("POST", "/v1/send", a.token,
    { to: "node-观察员", text: "第二条，增量收", client_msg_id: "demo-2" });
  [st, d] = await call("GET", `/v1/inbox?since=${cursor}`, b.token);
  if (st !== 200) throw new Error(`增量收失败 ${st}: ${JSON.stringify(d)}`);
  const texts = d.messages.map((m) => m.text);
  console.log("  增量收到：", texts);
  if (JSON.stringify(texts) !== JSON.stringify(["第二条，增量收"]))
    throw new Error("增量收不对");

  step(6, 6, "给「人」发一条（to 写 人 就行）");
  [st, d] = await call("POST", "/v1/send", a.token, { to: "人", text: "人在吗" });
  if (st !== 200) throw new Error(`发给人失败 ${st}: ${JSON.stringify(d)}`);
  console.log(`  ok seq=${d.seq}（人用他的 token 拉 /v1/inbox 就能看到）`);

  console.log("\nQUICKSTART: PASS");
}

main().catch((e) => {
  console.error("QUICKSTART: FAIL ->", e.message);
  process.exit(1);
});
