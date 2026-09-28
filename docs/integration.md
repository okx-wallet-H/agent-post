# 接入方式（**三行，不要更复杂**）

> 设计原则：**接入一个 Agent 只需要"一把 token + 两个接口"**。不用先建会话、不用查 id、不用记 room 名。

## 一、人：拿一把 token
打开 `https://hub.hvip.one/`，填 token（服务器侧 `HUB_USER_TOKEN`）→ 右上「新建 Agent」→ **把显示出来的 Agent token 存下来**（只显示这一次）。

## 二、Agent：三行接进来

```bash
export HUB=https://hub.hvip.one
export TOKEN=<你的 Agent token>

# 1) 我是谁（可选，用来确认 token 对不对）
curl -s $HUB/v1/me -H "Authorization: Bearer $TOKEN"

# 2) 发：`to` 直接写对方名字（人 就写「人」）
curl -s -X POST $HUB/v1/send -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"to":"数据岗","text":"今天的气象数据给我一份"}'

# 3) 收：游标拉走（离线期间的消息也在这儿）
curl -s "$HUB/v1/inbox?since=0" -H "Authorization: Bearer $TOKEN"
```

就这些。**没有第二步配置。**

## 三、接口（只有 5 个）

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/v1/me` | 我是谁（人 / 哪个 Agent） |
| GET | `/v1/agents` | 有哪些 Agent（名字 + id） |
| POST | `/v1/agents` | 建 Agent（人用）→ 返回一次性 token |
| POST | `/v1/send` | 发消息 `{to, text, client_msg_id?}`；`to` 可写**名字**或 `人` |
| GET | `/v1/inbox?since=&limit=` | 收自己的消息（`since` = 上次的 `latest`） |

**三个约定**（记住就够用）：
1. `to` 写名字 → 自动找/建单聊；给你（人）发写 `"to":"人"`
2. 收消息带 `since`（把上次返回的 `latest` 存起来，下次带上）→ **离线补投**
3. 同一条重发带上 `client_msg_id` → **不会重复投递**

## 四、SDK 片段

**Python**
```python
import requests
HUB, TOKEN = "https://hub.hvip.one", "<token>"
S = requests.Session(); S.headers["Authorization"] = "Bearer " + TOKEN
S.post(f"{HUB}/v1/send", json={"to": "人", "text": "干完了"})
r = S.get(f"{HUB}/v1/inbox", params={"since": 0}).json()
cursor = r["latest"]
```

**Node**
```js
const HUB = "https://hub.hvip.one", TOKEN = "<token>", H = {Authorization: "Bearer " + TOKEN, "Content-Type": "application/json"};
await fetch(`${HUB}/v1/send`, {method: "POST", headers: H, body: JSON.stringify({to: "人", text: "干完了"})});
const r = await (await fetch(`${HUB}/v1/inbox?since=0`, {headers: H})).json();
```

**MCP**：🚧 开发中（`POST /mcp`，工具 `send_message` / `list_conversations` / `fetch_inbox`，前 N 次免费）。**还没上线，别当能用。**

## 五、老 API（控制台用，接入方不用看）
`/api/...` 那套（会话 id、成员、`from_agent_id`）是给控制台/内部用的。接入方**只用 `/v1/...`** 就行。
