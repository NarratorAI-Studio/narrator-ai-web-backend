# 部署指南

[英文](./DEPLOYMENT.md)

本文说明如何在本地、Docker 和 Fly.io 类平台部署 `narrator-ai-web-backend`。

## 前置条件

- Python 3.11+
- Postgres 14+
- 已配置的上游解说 API
- 钱包路由使用的 `WALLET_BFF_AUTH_TOKEN`
- 定价、云盘、admin 和 narrator proxy 路由使用的 `PRICING_BFF_AUTH_TOKEN`

完整环境变量列表见 [`.env.example`](../.env.example)。

## 本地开发

```bash
git clone <repo-url> narrator-ai-web-backend
cd narrator-ai-web-backend

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入本地数据库、BFF token 和上游 API 配置。

alembic upgrade head
python server.py
```

开发服务监听 `PORT`，默认 `8080`。

### 用 Docker 启动本地 Postgres

```bash
docker run --name narrator-backend-pg -d \
  -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=narrator_ai_web_backend \
  -p 5432:5432 \
  postgres:14
```

然后在 `.env` 中配置：

```bash
NARRATOR_DB_HOST=localhost
NARRATOR_DB_PORT=5432
NARRATOR_DB_USER=postgres
NARRATOR_DB_PASSWORD=dev
NARRATOR_DB_NAME=narrator_ai_web_backend
```

### 常用命令

```bash
pytest
pytest tests/test_wallet_api.py
ruff check .
alembic upgrade head
alembic downgrade -1
python scripts/apply_wallet_schema.py
python scripts/cleanup_wallet_idempotency.py
```

## Docker

构建镜像：

```bash
docker build -t narrator-ai-web-backend:local .
```

运行容器：

```bash
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgres://<username>:<password>@<host>:5432/<database>' \
  -e OPEN_FASTAPI_BASE='https://api.example.com' \
  -e OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  -e WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  -e PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  narrator-ai-web-backend:local
```

容器暴露 `8080` 端口，并用 `python server.py` 启动应用。

## Fly.io 类平台部署

仓库包含 `fly.toml` 和 `fly-test.toml` 作为部署模板。它们已经中性化，使用前必须按你的环境调整。

部署前：

1. 把 `app` 改成你自己的应用名。
2. 替换数据库 host、user、database 的占位值。
3. 用平台 secret manager 管理密钥，不要写入 TOML 文件。
4. 把 `OPEN_FASTAPI_BASE` 设为你的上游解说 API 基础 URL。

示例：

```bash
fly auth login
fly apps create <your-app-name>
fly postgres create --name <your-db-name>
fly postgres attach --app <your-app-name> <your-db-name>

fly secrets set \
  OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  -a <your-app-name>
```

部署：

```bash
fly deploy -a <your-app-name>
```

使用测试模板部署：

```bash
fly deploy -c fly-test.toml -a <your-test-app-name>
```

## 发布时执行迁移

Fly 模板把下面的命令配置为 `release_command`：

```bash
alembic upgrade head
```

这会在新应用版本接入流量前应用待执行迁移。如果某个迁移需要人工检查或数据准备，请把迁移和应用发布拆成两个步骤。

## 环境变量参考

### 大多数部署都需要

| 变量 | 用途 |
|---|---|
| `DATABASE_URL` | Postgres DSN。部署环境推荐使用。 |
| `OPEN_FASTAPI_BASE` | 上游解说 API 基础 URL。 |
| `OPEN_FASTAPI_APP_KEY` | 服务端代理上游请求使用的 API key。 |
| `WALLET_BFF_AUTH_TOKEN` | 钱包路由所需 token。 |
| `PRICING_BFF_AUTH_TOKEN` | 定价、云盘、admin 和 narrator proxy 路由所需 token。 |

### 数据库 fallback 变量

仅在未设置 `DATABASE_URL` 时使用：

| 变量 | 默认值 |
|---|---|
| `NARRATOR_DB_HOST` | `localhost` |
| `NARRATOR_DB_PORT` | `5432` |
| `NARRATOR_DB_USER` | `postgres` |
| `NARRATOR_DB_PASSWORD` | 空 |
| `NARRATOR_DB_NAME` | `postgres` |
| `NARRATOR_DB_CONNECT_TIMEOUT_SECONDS` | `10` |
| `NARRATOR_DB_STATEMENT_TIMEOUT_MS` | `15000` |
| `NARRATOR_DB_POOL_SIZE` | `10` |
| `NARRATOR_DB_POOL_MAX_OVERFLOW` | `0` |
| `NARRATOR_DB_POOL_TIMEOUT_SECONDS` | `5` |
| `NARRATOR_DB_POOL_RECYCLE_SECONDS` | `1800` |

### 运行时和钱包

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PORT` | `8080` | HTTP 端口。 |
| `QUOTE_TTL_SECONDS` | `900` | quote 有效期。 |
| `WALLET_IDEMPOTENCY_TTL_HOURS` | `168` | 幂等记录清理保留窗口。 |
| `WALLET_IDEMPOTENCY_CLEANUP_BATCH_SIZE` | `1000` | 清理批大小。 |
| `ORCHESTRATOR_ENABLED` | `0` | 设置为 `1` 或 `true` 时启用后台 orchestrator。 |
| `ORCHESTRATOR_INTERVAL_SECONDS` | `10` | orchestrator 轮询间隔。 |
| `ORCHESTRATOR_BATCH_SIZE` | `20` | 每批最多处理任务数。 |

## 健康检查

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
```

`/health` 检查进程是否存活，`/ready` 检查数据库是否就绪。

## 排障

### 钱包路由返回 503

设置 `WALLET_BFF_AUTH_TOKEN`。钱包鉴权未配置时，后端会 fail closed。

### 定价、云盘或代理路由返回 503

设置 `PRICING_BFF_AUTH_TOKEN`、`OPEN_FASTAPI_BASE` 和 `OPEN_FASTAPI_APP_KEY`。

### `alembic upgrade head` 失败

检查数据库连接、权限，以及当前数据库 schema 是否符合迁移预期。只有在理解失败迁移副作用后，才执行回滚。

### 上游代理请求超时

确认后端运行环境能访问 `OPEN_FASTAPI_BASE`。只有在确认上游服务健康后，再考虑调大 `OPEN_FASTAPI_TIMEOUT_SECONDS` 或 `OPEN_FASTAPI_TRANSFER_TIMEOUT_SECONDS`。
