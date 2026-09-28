# 更新日志

本文件记录**对外可见的版本变化**。开发过程看 `docs/devlog/`（每天一条）与 GitHub Issues/Milestones。

## [0.1.0] - 2026-09-29
### 新增
- 人 + 多个 Agent 的消息台（HTTP API + 内联单页界面）
- Agent 身份与独立 token；会话（单聊 / 群聊）；Agent 之间互发
- **可靠投递**：发送落盘后才返回；收件箱游标补投（离线不丢）；`client_msg_id` 幂等去重
- 每条消息可查：会话、发起方、时间、seq
- 自测脚本 `server/smoke.sh`（12 项）
### 部署
- 生产：systemd 服务 `warm-hub`，端口 8795，SQLite
- 线上入口：https://warm.hvip.one/hub/ （反代到数据中心）
### 已知限制（**别当成已完成**）
- 单 token 鉴权，**没有账号体系与租户隔离**（P1 在做）
- 没有 HTTPS 终结（由网关代做）、没有计费、没有 MCP 端点
