# A2A（Agent2Agent）协议要点（可直接引用的中文版）

> 核对基准：官方规范原文 `docs/specification.md` + `specification/a2a.proto`（a2aproject/A2A，main 分支，2026-09-25 最后一次推送），以及 a2a-protocol.org 官方文档与 AAIF 官方公告。
> 标注约定：**【规范】= 规范里明确写了（附章节号）**；**【推断】= 我基于检索/原文引申的判断**。

---

## 1. 治理与现状

- **谁提出**：Google。2025 年 4 月发布，同时捐给 Linux Foundation，创始组织含 AWS、Cisco、Google、Microsoft、Salesforce、SAP、ServiceNow。**【AAIF 官方博客原文】**："Google launched A2A in April 2025 and donated it to the Linux Foundation with founding organizations like AWS, Cisco, Google, Microsoft, Salesforce, SAP, and ServiceNow."
- **现在归谁**：**Agentic AI Foundation（AAIF）**，由 Linux Foundation 托管。2026 年 8 月 A2A 被接纳为 AAIF 的 **Growth Stage project**。AAIF 项目族里还有 MCP、goose、AGENTS.md、agentgateway。
  - 注意日期差异：AAIF 博客标注 **2026-08-17**，A2A 官方博客标注 **2026-08-27**（应为「宣布/正式接纳」两个时点，两处公告都在）。
- **当前规范版本与日期**：**v1.0.0，2026-03-12**（GitHub release tag `v1.0.0`）。仓库最新 release 是 **v1.0.1，2026-05-28**，仅 3 个 bugfix（HTTP binding 优先用 `application/a2a+json`、transcoding 相关错误、TaskStatus 值订正）。文档站头部标 "Latest Released Version 1.0.0"。
- **版本号规则**：协议版本用 `Major.Minor`（如 `1.0`）；patch 不参与兼容性协商，**SHOULD NOT** 出现在请求/响应/Agent Card 里。空 `A2A-Version` 按 0.3 处理。
- **历史版本**：0.1.0 / 0.2.6 / 0.3.0（2025-07-30）/ 1.0.0 / 1.0.1。
- **v1.0 的主要破坏性变化**（写预研要提，否则代码会写错）：
  - 方法名从斜杠式改 PascalCase：`message/send` → `SendMessage`、`message/stream` → `SendStreamingMessage`、`tasks/get` → `GetTask`、`tasks/cancel` → `CancelTask`、`tasks/resubscribe` → `SubscribeToTask`、`agent/getAuthenticatedExtendedCard` → `GetExtendedAgentCard`。
  - 流事件去掉 `kind` 判别字段，改用 JSON 成员名判别；`TaskStatusUpdateEvent.final` 字段删除。
  - `TaskState` 枚举值从 kebab-case 改为 **SCREAMING_SNAKE_CASE**（ProtoJSON 合规）。
  - `protocolVersion` 从 AgentCard 顶层下移到每个 `AgentInterface`；`preferredTransport` + `additionalInterfaces` 合并为 `supportedInterfaces[]`。
  - `supportsAuthenticatedExtendedCard` → `capabilities.extendedAgentCard`。
  - ID 简化为纯 UUID（去掉 `tasks/{id}` 这种复合 ID）。
  - 新增 `ListTasks`、多租户 `tenant`、OAuth Device Code flow、`pkceRequired`。
- **生态规模**：
  - **150+ 组织** backing（AAIF 博客原文 "backed by over 150 organizations"，Linux Foundation 亦有 150+ 组织的新闻稿）。
  - **云平台原生支持**：Google Cloud（ADK、Agent Engine、Cloud Run、GKE）、Microsoft Azure AI Foundry、AWS Bedrock AgentCore Runtime。
  - **企业 SaaS**：ServiceNow、Salesforce、Atlassian、SAP（AAIF 转引 aaif/project-proposals#37）。
  - **框架**：LangGraph、CrewAI、Pydantic AI、AG2、IBM BeeAI（同上来源）。
  - **终端/OS 级落地**：华为把 A2A 定为 Celia（HarmonyOS 的系统级 AI 助手）与 App Agent 之间的协议；腾讯微信是首批通过 A2A 与华为及其他 Android OEM 助手集成的应用之一；Google Cloud 与 PayPal 用 A2A 做 AP2（Agent Payments Protocol）的购物/商户 Agent 通信。
- **官方链接**：站点 <https://a2a-protocol.org/latest/specification/>；仓库 <https://github.com/a2aproject/A2A>；SDK 组织 <https://github.com/a2aproject>。

**出处链接**
- 官方规范：<https://a2a-protocol.org/latest/specification/>｜规范源码：<https://github.com/a2aproject/A2A/blob/main/docs/specification.md>｜proto：<https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto>
- v1.0 变化说明：<https://a2a-protocol.org/latest/whats-new-v1/>
- 加入 AAIF（A2A 官方博客，2026-08-27）：<https://a2a-protocol.org/latest/blog/2026/08/27/a-new-chapter-for-a2a-joining-the-agentic-ai-foundation/>
- AAIF 官方博客（2026-08-17，含 150+ 组织与云厂商落地）：<https://aaif.io/blog/a2a-joins-aaif>
- Release 列表：<https://github.com/a2aproject/A2A/releases>

---

## 2. 核心对象与数据模型

> 规范第 4 节的原话：「The A2A protocol defines a canonical data model using **Protocol Buffers**. All protocol bindings **MUST** provide functionally equivalent representations of these data structures.」——即以 `specification/a2a.proto` 为准，各绑定只做等价映射。JSON 序列化 **MUST** 用 **camelCase**，枚举值按 ProtoJSON 序列化为字符串（SCREAMING_SNAKE_CASE）。**【规范 §4 / §5.5】**

### 2.1 AgentCard（发现对象）

| 字段（JSON 名） | 必填 | 说明 |
|---|---|---|
| `name` | REQUIRED | 人类可读名 |
| `description` | REQUIRED | 用途说明 |
| `supportedInterfaces[]` | REQUIRED | **有序**，第一项为首选。每项是 `AgentInterface` |
| `provider` | 可选 | `{organization, url}` |
| `version` | REQUIRED | Agent 自身版本，如 "1.0.0" |
| `documentationUrl` | 可选 | |
| `capabilities` | REQUIRED | `{streaming, pushNotifications, extensions[], extendedAgentCard}` |
| `securitySchemes` | 可选 | `map<string, SecurityScheme>` |
| `securityRequirements[]` | 可选 | `[{schemes: {schemeName: {list: [scopes]}}}]` |
| `defaultInputModes[]` / `defaultOutputModes[]` | REQUIRED | 媒体类型（MIME） |
| `skills[]` | REQUIRED | `AgentSkill` 数组 |
| `signatures[]` | 可选 | `AgentCardSignature`，JWS |
| `iconUrl` | 可选 | |

- `AgentInterface`：`url`(REQUIRED)、`protocolBinding`(REQUIRED，官方取值为 `JSONRPC` / `GRPC` / `HTTP+JSON`，自定义绑定 **SHOULD** 用 URI)、`tenant`(可选，opaque)、`protocolVersion`(REQUIRED，如 "1.0")。
- `AgentCapabilities`：`streaming`、`pushNotifications`、`extensions[]`、`extendedAgentCard`（都是 optional bool）。
- `AgentExtension`：`uri`、`description`、`required`、`params`。
- `AgentSkill`：`id`、`name`、`description`、`tags[]`、`examples[]`、`inputModes[]`、`outputModes[]`、`securityRequirements[]`。
- `AgentCardSignature`：`protected`（base64url 的 JWS header）、`signature`（base64url）、`header`。签名前 **MUST** 用 **RFC 8785 (JCS)** 规范化，`signatures` 字段自身从被签内容中排除；`signatures` 字段不参与 canonical form。**【规范 §8.4】**

### 2.2 Task / TaskStatus / TaskState

- `Task`：`id`(REQUIRED，服务端生成 UUID)、`contextId`、`status`(REQUIRED)、`artifacts[]`、`history[]`(Message 数组)、`metadata`。
- `TaskStatus`：`state`(REQUIRED)、`message`(可选 Message)、`timestamp`（ISO 8601 UTC，毫秒精度）。
- **`TaskState` 枚举共 9 个（准确英文名，SCREAMING_SNAKE_CASE）**：
  1. `TASK_STATE_UNSPECIFIED`（未知/不确定）
  2. `TASK_STATE_SUBMITTED`（已提交确认）
  3. `TASK_STATE_WORKING`（处理中）
  4. `TASK_STATE_COMPLETED`（**终态**，成功）
  5. `TASK_STATE_FAILED`（**终态**，出错）
  6. `TASK_STATE_CANCELED`（**终态**，被取消）
  7. `TASK_STATE_INPUT_REQUIRED`（**中断态**，需额外输入）
  8. `TASK_STATE_REJECTED`（**终态**，Agent 决定不做）
  9. `TASK_STATE_AUTH_REQUIRED`（**中断态**，需授权）
  - 终态集合 = {COMPLETED, FAILED, CANCELED, REJECTED}；中断态集合 = {INPUT_REQUIRED, AUTH_REQUIRED}。规范用这两个集合定义「能否再发消息」「流是否关闭」「阻塞式调用何时返回」。

### 2.3 Message / Role / Part / Artifact

- `Message`：`messageId`(REQUIRED，**由消息创建方生成**)、`contextId`、`taskId`、`role`(REQUIRED)、`parts[]`(REQUIRED，至少一个)、`metadata`、`extensions[]`(扩展 URI)、`referenceTaskIds[]`。
- `Role`：`ROLE_UNSPECIFIED` / `ROLE_USER`（客户端→服务端）/ `ROLE_AGENT`（服务端→客户端）。
- `Part`（oneof `content`，同时只能有一种）：
  - `text`（string）
  - `raw`（bytes，JSON 里 base64）
  - `url`（指向文件内容的链接）
  - `data`（任意 JSON 值：object/array/string/number/bool/null）
  - 附加：`metadata`、`filename`、`mediaType`（对所有 part 类型都可用）
  - **v1.0 破坏性变化**：旧的 `text`/`file`/`data` 分类型 Part 统一成单 `Part`，用 oneof 成员名判别。
- `Artifact`：`artifactId`(REQUIRED，task 内唯一)、`name`、`description`、`parts[]`(REQUIRED，至少一个)、`metadata`、`extensions[]`。

### 2.4 流式事件与推送对象

- `TaskStatusUpdateEvent`：`taskId`、`contextId`、`status`、`metadata`。
- `TaskArtifactUpdateEvent`：`taskId`、`contextId`、`artifact`、`append`（追加到同 ID 的既有 artifact）、`lastChunk`、`metadata`。
- `StreamResponse`（oneof）：`task` | `message` | `statusUpdate` | `artifactUpdate`。这就是 SSE 与 webhook 的统一载荷。
- `SendMessageResponse`（oneof）：`task` | `message`。
- `TaskPushNotificationConfig`：`tenant`、`id`、`taskId`、`url`(REQUIRED)、`token`、`authentication{scheme, credentials}`。

### 2.5 contextId / taskId 的作用（规范原文级）

- `contextId`：**逻辑分组**多个相关 Task 与 Message，提供跨交互的连续性。Agent **MAY** 生成、**MAY** 接受并保存客户端提供的值；若无法接受客户端提供的 `contextId`，**MUST** 报错且 **MUST NOT** 自行生成新的。客户端 **SHOULD NOT** 自己造 `contextId` 发给服务端；服务端生成的值客户端 **SHOULD** 当作 opaque。服务端 **MAY** 实现 context 过期/清理并被建议文档化。**【规范 §3.4.1】**
- `taskId`：Task 的唯一标识，代表一个有生命周期、有状态的工作单元。**服务端生成**；Agent **MUST** 为每个新 Task 生成唯一 `taskId` 并在返回的 Task 中带上；客户端带 `taskId` 时 **MUST** 指代已存在的 Task，否则 Agent **MUST** 返回 `TaskNotFoundError`；**不支持客户端提供 `taskId` 来创建新 Task**。**【规范 §3.4.2】**
- 组合规则：只给 `taskId` 时 Agent **MUST** 从 Task 推断 `contextId`；`contextId` 与 `taskId` 不匹配（contextId 与所引用 Task 的不同）时 Agent **MUST** 拒绝。仅用 `contextId`（不带 `taskId`）可以在既有会话里开一个**新** Task。**【规范 §3.4.3】**
- 客户端 **SHOULD** 用 `Message.referenceTaskIds` 显式引用相关 Task。

**出处链接**
- 数据模型：<https://a2a-protocol.org/latest/specification/#4-protocol-data-model>
- 精确字段定义（proto）：<https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto>
- JSON 命名与枚举序列化：<https://a2a-protocol.org/latest/specification/#55-json-field-naming-convention>
- 多轮交互与 ID 语义：<https://a2a-protocol.org/latest/specification/#34-multi-turn-interactions>
- Agent Card 签名：<https://a2a-protocol.org/latest/specification/#84-agent-card-signing>

---

## 3. 方法与传输

### 3.1 核心操作 × 三种官方绑定（规范 §5.3 映射表原文）

| 功能 | JSON-RPC 方法 | gRPC 方法 | REST 端点 |
|---|---|---|---|
| 发消息 | `SendMessage` | `SendMessage` | `POST /message:send` |
| 流式发消息 | `SendStreamingMessage` | `SendStreamingMessage` | `POST /message:stream` |
| 取任务 | `GetTask` | `GetTask` | `GET /tasks/{id}` |
| 列任务 | `ListTasks` | `ListTasks` | `GET /tasks` |
| 取消任务 | `CancelTask` | `CancelTask` | `POST /tasks/{id}:cancel` |
| 订阅任务 | `SubscribeToTask` | `SubscribeToTask` | `POST /tasks/{id}:subscribe` |
| 建推送配置 | `CreateTaskPushNotificationConfig` | 同名 | `POST /tasks/{id}/pushNotificationConfigs` |
| 取推送配置 | `GetTaskPushNotificationConfig` | 同名 | `GET /tasks/{id}/pushNotificationConfigs/{configId}` |
| 列推送配置 | `ListTaskPushNotificationConfigs` | 同名 | `GET /tasks/{id}/pushNotificationConfigs` |
| 删推送配置 | `DeleteTaskPushNotificationConfig` | 同名 | `DELETE /tasks/{id}/pushNotificationConfigs/{configId}` |
| 取扩展 Agent Card | `GetExtendedAgentCard` | 同名 | `GET /extendedAgentCard` |

> 注意：**v1.0 的 JSON-RPC 方法名就是 PascalCase 的 `SendMessage` / `GetTask`…，不再有 `message/send`、`tasks/get` 这类字符串。**（§9.1 原文："Method Naming: PascalCase method names matching gRPC conventions"）

### 3.2 三种传输绑定

- **JSON-RPC 绑定（§9）**：JSON-RPC 2.0 over HTTP(S)；`Content-Type: application/json`；流式用 **SSE（`text/event-stream`）**，每行 `data: {"jsonrpc":"2.0","id":...,"result":{StreamResponse}}`。
- **gRPC 绑定（§10）**：proto 里是 `service A2AService`，11 个 RPC，其中 `SendStreamingMessage`、`SubscribeToTask` 是 `returns (stream StreamResponse)` 服务端流。服务参数走 gRPC metadata。
- **HTTP+JSON / REST 绑定（§11）**：HTTP(S) + JSON；`Content-Type: application/a2a+json`（**SHOULD**，v1.0.1 起优先）；流式同样 SSE；GET/DELETE 的参数用 camelCase 查询串（如 `?contextId=uuid&status=TASK_STATE_WORKING&pageSize=50`）。
- 三绑定 **MUST** 功能等价（同样的操作集、语义等同的返回、一致的错误映射、一致的鉴权 scheme）。**【§5.1】**
- 服务参数以 `a2a-` 前缀，HTTP 下是请求头：`A2A-Version`、`A2A-Extensions`（逗号分隔的扩展 URI）。**【§3.2.6 / §14.2】** 客户端 **MUST** 每个请求都带 `A2A-Version`。
- 自定义绑定 **MAY** 存在，**MUST** 遵守 §5，**SHOULD** 用 URI 标识（`protocolBinding: "https://example.com/bindings/websocket/v1"`）。仓库里有实验性的 `SLIMRPC` 绑定。

### 3.3 返回语义（容易踩坑，规范写得很硬）

- `SendMessage` **MUST** 立即返回（Task 或 Message 二者之一）。返回 Task 时处理 **MAY** 在响应之后继续异步进行。
- `SendMessageConfiguration.returnImmediately` 控制阻塞与否：**默认 `false` = 阻塞**，**MUST** 等到终态或中断态才返回；`true` = 建完 Task 立刻返回，之后由调用方轮询 / 订阅 / 走推送。
- 向终态 Task 再发消息要返回 `UnsupportedOperationError`。
- `CancelTask`：服务端**尝试**取消，**不保证成功**；不可取消状态返回 `TaskNotCancelableError`。
- `ListTasks` **MUST** 只返回认证调用方可见的 Task，**MUST** 用游标分页（`pageToken`/`nextPageToken`，无更多时分页 token 必须是**空字符串""**），**MUST** 按状态时间倒序。`pageSize` 默认最多 50，范围 1–100。**【§3.1.4】**
- 能力校验：`capabilities.streaming=false` 时调流式方法 **MUST** 返回 `UnsupportedOperationError`；`capabilities.pushNotifications=false` 时调推送配置类方法 **MUST** 返回 `PushNotificationNotSupportedError`。**【§3.3.4】**

### 3.4 流式怎么工作

- 两种流形态（`SendStreamingMessage`）：① 只返回一个 `Message` → 流里**恰好一个** Message 然后立即关闭；② 返回 `Task` → 流以 Task 开头，随后是零或多个 `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`，Task 进入**终态**时 **MUST** 关闭流。
- `SubscribeToTask`：对非终态 Task 建流；**流的第一条必须是当前 Task 对象**（防止 GetTask 与订阅之间的信息丢失）；Task 到终态时流 **MUST** 终止；对终态 Task 调用返回 `UnsupportedOperationError`。
- 事件顺序：所有实现 **MUST** 按生成顺序投递，**MUST NOT** 重排。
- 同一 Task 支持**多个并发流**；事件 **MUST** 广播给该 Task 的所有活跃流，每条流收到同样顺序的事件；关掉一条流 **MUST NOT** 影响其他流；Task 生命周期独立于任何单条流。
- REST 侧补充：流是「先是 Task（或单个 Message），随后若干状态/产物事件，直到终态或中断态关闭」；实现 **SHOULD** 避免重排，**MAY** 在关闭前重发一次最终 Task 快照。

### 3.5 推送回调（webhook）怎么工作

- 机制：客户端用 `CreateTaskPushNotificationConfig` 注册 webhook URL（+ 出站鉴权信息），Agent 在 Task 更新时**主动 HTTP POST** 到该 URL。推送载荷就是 `StreamResponse`，与实时流**同一套事件类型**：`{"task"|"message"|"statusUpdate"|"artifactUpdate"}`。`Content-Type: application/a2a+json`，并带 `Authorization: {scheme} {credentials}`（格式由 `authentication.scheme` 决定，如 Bearer/Basic）。
- **无论 Agent 自己用哪种绑定，webhook 一律走纯 HTTP + HTTP 绑定的 JSON 载荷。**【§3.5.1】
- 服务端保证（原话级）：Agents **MUST** attempt delivery **at least once** per configured webhook；**MAY** 用指数退避重试；**SHOULD** 设合理超时（建议 **10–30 秒**）；**MAY** 连续失败 N 次后停止投递。
- 客户端责任：**MUST** 回 2xx 确认；**SHOULD** 幂等处理（「duplicate deliveries may occur」）；**MUST** 校验 taskId 符合预期 Task；**SHOULD** 做来源校验。
- 配置生命周期：「The configuration **MUST** persist until task completion or explicit deletion.」删除 **MUST** 幂等。
- 安全要求：Agent **SHOULD** 校验 webhook URL 防 SSRF（拒私网段、localhost、link-local）；客户端 **SHOULD** 限流防 webhook 洪水；文档给了 JWT + JWKS 的非对称密钥流程示例。

### 3.6 错误模型

- 通用错误载荷 **MUST** 含：错误码、人类可读错误消息、可选 details 数组（每项 **MUST** 带 `@type`，**SHOULD** 用 `google.rpc` 错误模型如 `ErrorInfo`）。
- 9 个 A2A 专属错误及映射（§5.4）：`TaskNotFoundError`(-32001 / NOT_FOUND / 404)、`TaskNotCancelableError`(-32002 / FAILED_PRECONDITION / 400)、`PushNotificationNotSupportedError`(-32003 / 400)、`UnsupportedOperationError`(-32004 / 400)、`ContentTypeNotSupportedError`(-32005 / INVALID_ARGUMENT / 400)、`InvalidAgentResponseError`(-32006 / INTERNAL / 500)、`ExtendedAgentCardNotConfiguredError`(-32007 / 400)、`ExtensionSupportRequiredError`(-32008 / 400)、`VersionNotSupportedError`(-32009 / 400)。
- REST 下 HTTP 状态码不足以区分（多个错误都映射到 400），**MUST** 在 details 里放 `google.rpc.ErrorInfo`：`reason` 为 UPPER_SNAKE_CASE 的错误名（如 `TASK_NOT_FOUND`），`domain` 为 `a2a-protocol.org`。**【§11.6】**

**出处链接**
- 方法映射表：<https://a2a-protocol.org/latest/specification/#53-method-mapping-reference>
- JSON-RPC 绑定：<https://a2a-protocol.org/latest/specification/#9-json-rpc-protocol-binding>
- gRPC 绑定：<https://a2a-protocol.org/latest/specification/#10-grpc-protocol-binding>
- REST 绑定：<https://a2a-protocol.org/latest/specification/#11-httpjsonrest-protocol-binding>
- 流式与异步：<https://a2a-protocol.org/latest/topics/streaming-and-async/>
- 推送载荷与投递语义：<https://a2a-protocol.org/latest/specification/#433-push-notification-payload>
- 错误码映射：<https://a2a-protocol.org/latest/specification/#54-error-code-mappings>

---

## 4. 鉴权

- **声明位置**：Agent Card 的 `securitySchemes`（`map<string, SecurityScheme>`）+ `securityRequirements`（`map<schemeName, {list: [scopes]}>`）。**【规范 §4.4.1 / §4.5】**
- **`SecurityScheme` 是 oneof 判别联合，基于 OpenAPI 3.2 Security Scheme Object，共 5 种类型（准确英文名）**：
  1. `APIKeySecurityScheme`：`location`（`query` / `header` / `cookie`）、`name`、`description`
  2. `HTTPAuthSecurityScheme`：`scheme`（IANA HTTP 认证方案名，如 `Bearer`）、`bearerFormat`（提示用）
  3. `OAuth2SecurityScheme`：`flows`（必填）、`oauth2MetadataUrl`（RFC 8414 授权服务器元数据，要求 TLS）
  4. `OpenIdConnectSecurityScheme`：`openIdConnectUrl`（OIDC Discovery URL）
  5. `MutualTlsSecurityScheme`：只有 `description`（即 mTLS 靠传输层）
- **OAuth 流程**：`authorizationCode`（新加 `pkceRequired`，RFC 7636）、`clientCredentials`、`deviceCode`（RFC 8628，v1.0 新增）；`implicit` 与 `password` 已标 **deprecated**。**【§4.5.7 / What's New v1.0】**
- **认证流程三步（规范 §7.3 原文）**：① 客户端从 Agent Card 的 `securitySchemes` 发现所需认证方案；② **带外**（out-of-band）获取凭据；③ 在每个 A2A 请求里按协议的 header/metadata 传凭据。
- **传输层硬要求**：生产部署 **MUST** 用加密通信（HTTP 系用 HTTPS，gRPC 用 TLS）；**SHOULD** 用 TLS 1.3+。客户端 **SHOULD** 校验服务端 TLS 证书。**【§7.1 / §7.2】**
- **服务端责任**：**MUST** 对每个入站请求做认证；**SHOULD** 用绑定对应的错误码返回认证挑战信息。**【§7.4】**
- **授权是实现的自由**（原话）："Once authenticated, the A2A Server authorizes requests based on the authenticated identity and its own policies. **Authorization logic is implementation-specific**"。**【§7.5】**
- **任务内授权（in-task authorization）**：Agent 需要授权时把 Task 置为 `TASK_STATE_AUTH_REQUIRED`，并在 TaskStatus 里说明所需授权（除非已带外约定或用扩展协商）；凭据 **SHOULD** 带外交付。客户端可以发消息协商/纠正/拒绝，也可以把请求**继续向上委派**给自己的客户端（形成 `TASK_STATE_AUTH_REQUIRED` 的任务链）。**【§7.6.1–7.6.2】**
- **范围界定（原话）**：「The A2A protocol **does not define** the scope, representation, validity, or revocation semantics of the authorization decision or credential obtained in response to this state.」且 **MUST NOT** 仅凭进入 `TASK_STATE_AUTH_REQUIRED` 就视作对某操作的授权。**【§7.6.4】**
- 数据访问范围：服务端 **MUST** 对每个请求做授权检查，**MUST** 把结果限定在调用方的授权边界内；授权模型由各 Agent 自定，协议不规定。**【§13.1】**

**出处链接**
- 认证与授权：<https://a2a-protocol.org/latest/specification/#7-authentication-and-authorization>
- 安全对象：<https://a2a-protocol.org/latest/specification/#45-security-objects>
- 企业特性（含示例）：<https://a2a-protocol.org/latest/topics/enterprise-ready/>
- 安全考量汇总：<https://a2a-protocol.org/latest/specification/#13-security-considerations>

---

## 5. 协议明确不负责什么（重要）

> 说明：规范**没有**一个叫 "out of scope" 的专门章节。下面每条都给了原文/章节；凡是我靠「检索不到 → 判定没规定」得出的，标 **【推断】**。

### 5.1 投递保证 —— 只承诺「至少尝试一次」
- 原文（§4.3.3 Server Guarantees）："Agents **MUST attempt delivery at least once** for each configured webhook." 仅此一句是 MUST，且只是 **attempt**。
- 没有端到端确认（客户端回 2xx 只是 ack 收到，Agent 侧无「必须重发至成功」的 MUST）；没有 exactly-once；没有顺序保证（顺序 MUST 只针对**流内**事件，§3.5.2）。

### 5.2 重试 —— 只是 MAY
- 原文（§4.3.3）："Agents **MAY** implement retry logic with exponential backoff for failed deliveries"；"Agents **MAY** stop attempting delivery after a configured number of consecutive failures"。超时也只是 **SHOULD**（建议 10–30 秒）。
- 请求侧（SendMessage 本身）的重试语义规范未作任何规定。**【推断】**

### 5.3 幂等 —— 只有 Cancel / Delete 是硬要求
- 原文（§3.3.1 Idempotency）：
  - Get 类（GetTask、ListTasks、GetExtendedAgentCard）天然幂等；
  - **"Send Message operations MAY be idempotent. Agents may utilize the messageId to detect duplicate messages."**——即去重靠实现自己拿 `messageId` 做，协议不强制；
  - CancelTask 幂等（重复取消效果相同，但任务已取消并被清理时重复请求 **MAY** 返回 `TaskNotFoundError`）。
- 删除推送配置 **MUST** 幂等；webhook 接收方 **SHOULD** 幂等（因为「duplicate deliveries may occur」）。**【§3.1.10 / §4.3.3】**

### 5.4 离线队列 —— 规范毫无条文 **【推断：完全未规定】**
- 对 `queue` / `offline` / `durable` / `store-and-forward` 等词在规范全文检索，无任何规范性条文。规范只在「流式 vs 推送 vs 轮询」三种更新获取方式之间做选择，没有为「客户端不在线」提供服务端排队/补投机制。
- 唯一相关的是反向警告（§3.7 原文）："Clients using streaming to retrieve task updates **MAY not receive all status update messages if the client is disconnected and then reconnects**. **Messages MUST NOT be considered a reliable delivery mechanism for critical information.**"
- 断线重连后的 backfill 行为：v1.0 文档明说 "Backfill behavior implementation-dependent"。**【What's New v1.0 / §3.1.6】**

### 5.5 持久化与保留期 —— 交给实现
- 原文（§3.7）："not all Messages are guaranteed to be persisted in the Task history; for example, transient informational messages may not be stored. Messages exchanged prior to task creation may not be stored in Task history. **The agent is responsible to determine which Messages are persisted in the Task History.**"
- 同段："Agents **MAY** choose to persist all Messages… However, **clients MUST NOT rely on this behavior** unless negotiated out-of-band."
- 保留期无规定，只能从 `TaskNotFoundError` 的描述反推由实现决定："It might be invalid, **expired**, or already completed and **purged**."（§3.2.2 错误表）
- 没有关于存储后端、数据库 schema、历史裁剪策略的任何要求。

### 5.6 多租户与配额 —— 只留一个 opaque 字段
- `tenant` 字段的 proto 注释原话："…The server is responsible for interpreting the value and routing requests accordingly; **the protocol does not define its format or semantics**."
- 多租户指南原话："**The A2A protocol does not prescribe a specific routing implementation** — operators are free to choose the approach that best fits their infrastructure."
- 客户端侧的硬要求只有一条：如果所选 `AgentInterface` 声明了 `tenant`，客户端 **MUST** 在每个请求里原样回填；没声明则 **MUST** 省略。
- **配额（quota）**：规范里唯一出现 "quotas" 的地方是说**扩展 Agent Card 可以额外暴露** rate limits / quotas 这类信息（§13.3），也就是说配额本身不是协议概念。限速只有 SHOULD/MAY（§13.4：「Agents **SHOULD** implement rate limiting on all operations」「**MAY** implement different rate limits for different operations or user tiers」）。**【规范 + 推断混合：限速是 SHOULD，配额非协议概念】**

### 5.7 注册中心 / 服务目录 —— 明确声明「没有标准 API」
- 规范发现机制只列了三种（§8.2）：Well-Known URI（`https://{domain}/.well-known/agent-card.json`）、**Registries/Catalogs**、直接配置。
- 发现指南原话："**The current A2A specification does not prescribe a standard API for curated registries.**" 该页 Future Considerations 只写「社区在探索标准化注册中心交互」，即**尚未标准化**。
- `.well-known/agent-card.json` 有 IANA 注册模板（URI suffix = `agent-card.json`，Status: Permanent），这部分是规范的。

### 5.8 其它规范明确交给实现/不涉及的
- **授权模型**：「Authorization boundaries are defined by each agent's authorization model, **not prescribed by the protocol**」（§13.1）。
- **任务内授权的凭据语义**：范围/表示/有效期/吊销语义协议不定义（§7.6.4，见第 4 节）。
- **Agent 内部**：核心原则是 "**Opaque Execution**"——协作只基于声明的能力和交换的信息，不需要共享各自的内部思考、计划或工具实现（§1.2）。
- **取消成功性**：「The server will attempt to cancel the task, but **success is not guaranteed**」（§3.1.5）。
- **计费/结算/支付**：规范全文没有支付、计费、结算相关的规范性条文。**【推断：检索无命中】**（生态里有独立的 AP2 支付层，见第 1 节。）
- **未识别字段**：实现 **SHOULD** 忽略，以便向前兼容（§5.7）。

**出处链接**
- 操作语义（幂等/错误/异步/能力校验）：<https://a2a-protocol.org/latest/specification/#33-operation-semantics>
- 消息与产物（不可靠投递原文）：<https://a2a-protocol.org/latest/specification/#37-messages-and-artifacts>
- 推送投递语义：<https://a2a-protocol.org/latest/specification/#433-push-notification-payload>
- 多租户：<https://a2a-protocol.org/latest/topics/multi-tenancy/>
- 发现与「无注册中心标准 API」：<https://a2a-protocol.org/latest/topics/agent-discovery/>
- 数据访问与授权范围：<https://a2a-protocol.org/latest/specification/#131-data-access-and-authorization-scoping>

---

## 6. 与 MCP / ACP / ANP 的区别（各一句话）

- **MCP（Model Context Protocol）**：垂直集成层——标准化 Agent 如何连接和调用**工具、API、数据源等外部资源**（「how-to 使用某个能力」）。规范附录 B 原文对比表在此。
- **A2A**：水平编排层——标准化**独立、彼此不透明的 Agent 之间作为对等方**互相发现、协商交互模态、共享任务状态、交换上下文与结果（「Agent 之间如何合作/委派」）。
- **ACP（Agent Communication Protocol）**：IBM 提出的通用 Agent 通信协议，RESTful HTTP + MIME-typed 多部件消息、同步/异步交互，**已于 2025 年 8 月并入 A2A**（Linux Foundation LF AI & Data 社区博客宣布，AAIF 博客亦确认「IBM's Agent Communication Protocol merged into A2A」）。
- **ANP（Agent Network Protocol）**：面向**开放网络**的去中心化路线——用 **W3C 去中心化标识符（DID）** 与 **JSON-LD 图** 做 Agent 发现与安全协作，目标是大规模 Agent 市场/网络。

**权威综述**：Ehtesham, Singh, Gupta, Kumar，*A survey of agent interoperability protocols: MCP, ACP, A2A, and ANP*，arXiv:2505.02279（v1 2025-05-04，v2 2025-05-23）。该文四协议逐一比较交互模式、发现机制、通信模式、安全模型，并提出「MCP → ACP → A2A → ANP」的分阶段采用路线图。

**出处链接**
- 规范附录 B（A2A 与 MCP 官方定位）：<https://a2a-protocol.org/latest/specification/#appendix-b-relationship-to-mcp-model-context-protocol>
- A2A and MCP 指南：<https://a2a-protocol.org/latest/topics/a2a-and-mcp/>
- 综述（arXiv 摘要页）：<https://arxiv.org/abs/2505.02279>｜全文：<https://arxiv.org/html/2505.02279v2>
- ACP 并入 A2A（LF AI & Data）：<https://lfaidata.foundation/communityblog/2025/08/29/acp-joins-forces-with-a2a-under-the-linux-foundations-lf-ai-data/>
- ANP 仓库：<https://github.com/agent-network-protocol/anp>

---

## 7. 实现门槛

### 7.1 官方 SDK（6 种语言）
| 语言 | 仓库 |
|---|---|
| Python | <https://github.com/a2aproject/a2a-python> |
| Go | <https://github.com/a2aproject/a2a-go> |
| Java | <https://github.com/a2aproject/a2a-java> |
| JavaScript | <https://github.com/a2aproject/a2a-js> |
| C#/.NET | <https://github.com/a2aproject/a2a-dotnet> |
| Rust | <https://github.com/a2aproject/a2a-rs> |

（官方 SDK 页原文表的六行，无其它「official」语言。）

### 7.2 参考实现 / 样例 / 工具
- **样例**：<https://github.com/a2aproject/a2a-samples>（覆盖各语言的示例 Agent 与服务端）。
- **`a2a-cli`**：官方命令行客户端（<https://github.com/a2aproject/a2a-cli>）。
- **`a2a-inspector`**：Agent 校验工具（<https://github.com/a2aproject/a2a-inspector>）。
- **`a2a-gateway`**：把 A2A Agent 接到不同通信渠道的网关（<https://github.com/a2aproject/a2a-gateway>）。
- **实验性**：`experimental-cpb-slimrpc`（SLIMRPC 自定义绑定）、`experimental-ext-oid4vp-auth`（OID4VP 任务内授权扩展）。
- **教程**：官方 Python Quickstart 八节 + DeepLearning.AI 合作课程。

### 7.3 测试套件
- **TCK（Technology Compatibility Kit）**：<https://github.com/a2aproject/a2a-tck>。Python 3.11+ / uv 安装；`./run_tck.py --sut-host http://localhost:9999`；`--transport grpc,jsonrpc,http_json` 过滤传输；`--level must|should|may` 按 RFC 2119 级别跑；跑完在 `reports/` 生成兼容性报告。**三传输（gRPC / JSON-RPC / HTTP+JSON）全覆盖，是「能不能算合规实现」的判据。**
- **ITK（Integration Test Kit）**：<https://github.com/a2aproject/a2a-itk>。多跳（multi-hop）Agent 链式遍历，验证跨 SDK/跨传输的互操作，含流式与推送通知（配 Mock Notification Server）两种验证路径。
- 规范层面只有 **SHOULD** 级的互操作测试建议（§12.8）：对照参考实现测试、记录差异、提供样例、测边界（错误、大载荷、长任务）。——即「自定义绑定没有强制认证测试」。

### 7.4 注册中心 / 服务目录
- **没有规范性注册中心协议**（见 §5.7 引文）。官方只给三种发现方式；注册中心是「社区在探索」。
- 唯一有规范落点的是 **Well-Known URI**：`https://{domain}/.well-known/agent-card.json` **MUST** 返回 AgentCard 对象，已提交 IANA 注册模板（URI suffix `agent-card.json`，Status: Permanent）。
- Agent Card 的**缓存**有规范：服务端 **SHOULD** 带 `Cache-Control: max-age` 与 `ETag`；客户端 **SHOULD** 遵守 RFC 9111 并用条件请求。**【§8.6】**
- 协议栈注册：媒体类型 `application/a2a+json`（文件扩展名 `.a2a.json`）、HTTP 头 `A2A-Version` / `A2A-Extensions` 均给了 IANA 注册模板（§14）。

**出处链接**
- SDK 一览：<https://a2a-protocol.org/latest/sdk/>
- 组织仓库列表：<https://github.com/a2aproject>
- TCK：<https://github.com/a2aproject/a2a-tck>｜ITK：<https://github.com/a2aproject/a2a-itk>
- 发现与 well-known：<https://a2a-protocol.org/latest/specification/#8-agent-discovery-the-agent-card>
- IANA 注册模板：<https://a2a-protocol.org/latest/specification/#14-iana-considerations>

---

## 8. 不确定 / 没查到的点

1. **AAIF 公告日期不一致**：AAIF 博客标 2026-08-17，A2A 官方博客标 2026-08-27。两处都对得上「加入 AAIF」这件事，但哪个是「正式接纳日」我没核实。
2. **「当前规范版本」有两个候选**：文档站头部写 **1.0.0**（指向 `/v1.0.0/specification`），GitHub 最新 release 是 **v1.0.1**。v1.0.1 只是 3 个 bugfix，但「对外该说哪个版本」建议由你们按引用场景定。
3. **150+ 组织这个数字**来自 AAIF 博客与 Linux Foundation 新闻稿，我**没有**去核原始成员名单；云厂商/企业 SaaS/框架那串名字是 AAIF 博客转引 `aaif/project-proposals#37`，**未逐条一手核实**（该 issue 我没打开）。
4. **「协议不负责什么」的结论性质**：规范没有专门的 out-of-scope 章节。除我逐条引用的原文外（投递=MUST attempt、重试=MAY、幂等=MAY、持久化=agent responsible、tenant=不定义、注册中心=不规定），其余判断（如「完全没有离线队列条文」「没有计费条文」）来自对 queue/offline/durable/at-least-once/guarantee/persist/retention/purge/quota 等词的全文检索**无命中**，属**负向证据 + 推断**，不是规范的原话声明。
5. **ACP 的当前状态未一手核实**：我没有打开 `i-am-bee` 的仓库或讨论确认 ACP 是否已 archive/冻结，只依据 LF AI & Data 社区博客与 AAIF 博客的「merged into A2A」表述。
6. **ANP 只看了仓库描述层面**（W3C DID + JSON-LD），未核对其当前规范版本与实现成熟度。
7. **IANA 是否已实际登记**：规范给的是「registration templates, intended for submission to IANA」，我没有去 IANA 注册表核对 `application/a2a+json`、`A2A-Version`、`.well-known/agent-card.json` 是否已正式登记生效。
8. **未查**：a2a-java 的 `compat03` 包（v0.3 兼容层）的具体兼容范围；各 SDK 对 v1.0 的支持完成度；`/latest/definitions/` 生成的 JSON Schema 与 proto 是否有偏差。
9. **未查**：社区通行的 A2A 服务目录实现（如 Google 的 Agent Space、第三方 registry 产品）——规范没规定，产品侧有哪些既成事实我没调研。
