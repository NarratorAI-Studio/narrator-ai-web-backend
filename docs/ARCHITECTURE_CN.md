# 架构概览

[英文](./ARCHITECTURE.md)

`narrator-ai-web-backend` 是一个基于 Postgres 的同步 Flask 应用，位于 Web 前端和上游解说 API 之间。

## 主要职责

- 用服务端 Bearer token 鉴权 BFF 请求。
- 存储终端用户资料和钱包余额。
- 为模板化解说工作流计算并持久化 quote。
- 管理钱包交易状态：quote、freeze、confirm、refund。
- 在 Postgres 中存储用户级长耗时任务状态。
- 代理选定的云盘、元数据和解说任务上游 API。
- 可选运行后台 orchestrator 推进符合条件的任务。

## 运行时组件

| 组件 | 文件 | 职责 |
|---|---|---|
| Flask app | `server.py` | 路由注册、健康检查、OpenAPI 输出、请求级编排 |
| Users | `users/`, `account/` | 终端用户 app key、资料字段、账户响应格式化 |
| Wallet | `wallet/` | quote 固化、冻结、确认、退款、幂等 |
| Pricing | `pricing/`, `pricing_catalog_v2/`, `pricing_quote_v2/` | 模板价格、目录校验、quote 快照 |
| Cloud drive | `cloud_drive/` | 文件归属、配额计算、上游文件代理 |
| Narrator proxy | `narrator_metadata/`, `narrator_proxy/` | 元数据和任务 API 代理路由 |
| Tasks | `narrator_tasks/` | 用户级任务 body 和查询热字段 |
| Orchestrator | `orchestrator/` | 可选后台任务推进 |
| Database | `db/`, `migrations/` | 表元数据和 Alembic 迁移 |

## 请求边界

后端使用两类鉴权概念：

- BFF bearer token，通过 `WALLET_BFF_AUTH_TOKEN` 和 `PRICING_BFF_AUTH_TOKEN` 配置，用于证明请求来自可信的服务端前端或集成服务。
- `X-Web-App-Key` 标识终端用户。读取或写入用户数据、消耗配额、影响钱包余额的路由需要它。

浏览器可见代码不应包含 BFF bearer token 或上游服务密钥。

## 数据边界

- Postgres 是 users、wallet state、quotes、cloud-drive ownership 和 task storage 的事实源。
- 上游 API 响应只通过明确 allowlist 的路由代理。
- 任务 body 以 JSON 存储，便于前端任务结构演进，避免每个非查询字段都需要迁移。
- status、current step、user id、timestamps 等热字段单独存储，便于高效过滤和租户校验。

## Orchestrator

orchestrator 默认关闭。通过下面的变量启用：

```bash
ORCHESTRATOR_ENABLED=1
```

启用后，它会轮询符合条件的任务行，通过数据库协调领取任务，触发下一个上游步骤，并持久化结果状态。

## API 合同

静态 OpenAPI 合同通过下面的路径提供：

```text
/openapi.json
```

文件提交在 [`openapi.json`](../openapi.json)。修改公开 API 行为时，需要同步更新它。
