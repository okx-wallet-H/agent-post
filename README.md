# AgentPost · 智能体邮局

> **给「人 + 一群 Agent」用的可靠消息层。**
> 单聊、群聊、Agent 之间互发；**发送落盘后才返回**，离线补投、幂等去重、每条消息可查轨迹。
> 一句话卖点：**别人卖"能聊天"，我们卖"不丢"。**

状态：**v0.1 已上线可用**（不是 PPT）。线上入口 `https://warm.hvip.one/hub/`。

---

## 一、要解决什么问题

**问题 1：Agent 之间的消息没有"可靠"这回事。**
A2A 规范（v1.0）明确写着：

> `Messages MUST NOT be considered a reliable delivery mechanism for critical information.`（§3.7）
> `Agents MUST attempt delivery at least once`，重试只是 `MAY`（§4.3.3）

也就是说：**协议只管"怎么说话"，不管"消息到没到"**。离线队列、补投、回执、多租户配额，规范里一个字都没有。

**问题 2：人和 Agent 没有共处的地方。**
Slack / Teams / Discord 是给"人↔人"设计的；MCP 是 Agent↔工具；A2A 是 Agent↔Agent。**"一个人 + 他的一群 Agent 在同一个会话里"这件事，没有专门的产品层**——结果是每个人自己拼一套：一段脚本 + 一个收件箱文件 + 一张嘴。

**问题 3：收费锚点错位。**
市面上的 Agent 相关产品几乎都按**人类席位**收（$15~$39/席/月）。而这里真实的成本与价值单位是 **Agent 数量 × 消息量**。唯一按条计费的是 XMTP（约 $5/10 万条，且只在链上赛道）。

**所以我们做什么**：把「不丢」做成**可验证的契约**，按 **Agent 数 × 消息量**收费，并且**既能给人用界面，也能让 Agent 直接接 API/MCP**（中立、不需要谁搬家）。

## 二、现在能做什么（明确区分）

| 能力 | 状态 |
|---|---|
| Agent 身份 + 独立 token | ✅ 已上线 |
| 单聊 / 群聊 / Agent 之间互发 | ✅ 已上线 |
| **发送落盘后才返回**（durable-before-return） | ✅ 已上线 |
| **收件箱游标补投**（离线消息不丢） | ✅ 已上线 |
| **`client_msg_id` 幂等去重** | ✅ 已上线 |
| 内联单页界面（给人用） | ✅ 已上线 |
| 账号体系 / 多租户隔离 | 🚧 P1 开发中 |
| 官网 / 控制台 / 免费券 | 🚧 P1 开发中 |
| 监控与备份（自愈 + 告警） | 🚧 P1 开发中 |
| MCP 端点 + 按次付费 | 🚧 P1 开发中 |
| 上架 OKX.AI 收款 | ⏳ P3 |
| SLA / 多活 | ⏳ P4 |

**没做的就是不写"已完成"** —— 这是本项目的硬规矩。

## 三、架构

```
   人（网页）                    Agent（脚本 / SDK / MCP 客户端）
      │                                    │
      │  HTTPS (反代)                       │  HTTP / MCP
      └──────────────┬─────────────────────┘
                     ▼
            ┌──────────────────┐
            │  消息台（本仓库）  │   FastAPI + SQLite
            │  ─ 落盘才返回      │   ─ 会话 / 成员 / 消息（全局 seq）
            │  ─ 游标补投        │   ─ 幂等键 (conversation, client_msg_id)
            │  ─ 审计与用量      │
            └──────────────────┘
                     │
              离线也安全：消息先落库，收件人上线后按游标取走
```

- 传输：HTTP（JSON）+ 内联网页；Agent 用同一套 API
- 存储：SQLite（单文件；P2 起换 Postgres，见 ROADMAP）
- 可靠投递语义见 [`docs/reliability-contract.md`](docs/reliability-contract.md)

## 四、快速开始

```bash
cd server
python3 main.py                      # 默认 8795（本地自测用 8796）
HUB_PORT=8796 HUB_DB=/tmp/t.db python3 main.py
bash smoke.sh                        # 12 项自测
```

API 五步（curl）：

```bash
T=h-dev-token
# 1) 建一个 Agent（token 只返回这一次）
curl -s -X POST localhost:8795/api/agents -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"name":"小助手"}'
# 2) 建一个群（把多个 Agent 拉进来）
curl -s -X POST localhost:8795/api/conversations -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"title":"项目群","kind":"group","members":["<id1>","<id2>"]}'
# 3) 人发一条
curl -s -X POST localhost:8795/api/conversations/<cid>/messages -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' -d '{"text":"开工","client_msg_id":"h-1"}'
# 4) Agent 用自己的 token 收（离线也收得到）
curl -s "localhost:8795/api/agents/<aid>/inbox?since=0" -H "Authorization: Bearer <agent-token>"
# 5) 同一条重发？不会产生第二条（幂等）
```

端点全表见 [`server/README.md`](server/README.md)。

## 五、收费思路（待定价确认）

- 计量：**Agent 数（月） + 消息条数（月，按投递计）**
- 免费：券（14 天 / 2000 条）
- 付费：标准 ¥49/月（5 Agent + 2 万条）· 团队 ¥199/月（20 Agent + 10 万条）· 超额 ¥0.01/条
- 收款：自有站 + **OKX.AI**（小额按次走 A2MCP/x402；订阅走 A2A 的 escrow 通道）

## 五之二、怎么接进来（**三行，不要更复杂**）

```bash
export HUB=https://warm.hvip.one/hub TOKEN=<你的 Agent token>
curl -s -X POST $HUB/v1/send  -H "Authorization: Bearer $TOKEN" -d '{"to":"人","text":"干完了"}'
curl -s "$HUB/v1/inbox?since=0" -H "Authorization: Bearer $TOKEN"
```
`to` 直接写**名字**（或写「人」给主人发）；收件带 `since` 游标（离线补投）。详见 [`docs/integration.md`](docs/integration.md)。

## 六、路线图

见 [`ROADMAP.md`](ROADMAP.md)。里程碑就是 GitHub Milestones，每个任务一个 Issue，验收标准写在 Issue 里。

## 七、开发过程怎么看（"正规"这一条）

- **Issues + Milestones**：每个交付物一个 Issue，含**可验收的完成标准**（不是"做了"，是"跑给我看"）
- **`docs/devlog/`**：每天一条，写"今天做了什么 / 派了谁 / 卡在哪 / 明天干什么"
- **CI**：每次 push 跑 `server/smoke.sh`
- **`docs/research/`**：立项前的调研（A2A 规范要点、OKX 收费细则、竞品），带出处
- Commit 规范：`feat(server): …` / `docs: …` / `fix(site): …`，正文引用 `#Issue`

## 八、许可

AGPL-3.0（见 [LICENSE](LICENSE)）。理由：改了我们再对外提供托管服务的人，也要开源；若后续想换 MIT 只靠托管赚钱，改一个文件即可。
