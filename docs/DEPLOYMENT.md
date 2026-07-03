# Deployment Guide

[中文](./DEPLOYMENT_CN.md)

This guide covers local development, Docker, and Fly.io-style platform deployment for `narrator-ai-web-backend`.

## Prerequisites

- Python 3.11+
- Postgres 14+
- A configured upstream commentary API
- `WALLET_BFF_AUTH_TOKEN` for wallet routes
- `PRICING_BFF_AUTH_TOKEN` for pricing, cloud-drive, admin, and narrator proxy routes

All supported environment variables are listed in [`.env.example`](../.env.example).

## Local Development

```bash
git clone <repo-url> narrator-ai-web-backend
cd narrator-ai-web-backend

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env for your local database, BFF tokens, and upstream API.

alembic upgrade head
python server.py
```

The development server listens on `PORT`, defaulting to `8080`.

### Local Postgres with Docker

```bash
docker run --name narrator-backend-pg -d \
  -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=narrator_ai_web_backend \
  -p 5432:5432 \
  postgres:14
```

Then configure `.env`:

```bash
NARRATOR_DB_HOST=localhost
NARRATOR_DB_PORT=5432
NARRATOR_DB_USER=postgres
NARRATOR_DB_PASSWORD=dev
NARRATOR_DB_NAME=narrator_ai_web_backend
```

### Useful Commands

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

Build the image:

```bash
docker build -t narrator-ai-web-backend:local .
```

Run the container:

```bash
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgres://<username>:<password>@<host>:5432/<database>' \
  -e OPEN_FASTAPI_BASE='https://api.example.com' \
  -e OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  -e WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  -e PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  narrator-ai-web-backend:local
```

The container exposes port `8080` and starts the app with `python server.py`.

## Fly.io-Style Deployment

The repository includes `fly.toml` and `fly-test.toml` as deployment templates. They are intentionally generic and must be customized before use.

Before deploying:

1. Replace the `app` value with your own application name.
2. Replace placeholder database host, user, and database values.
3. Set secrets with your platform's secret manager, not in the TOML files.
4. Set `OPEN_FASTAPI_BASE` to your upstream commentary API base URL.

Example setup:

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

Deploy:

```bash
fly deploy -a <your-app-name>
```

Deploy with the test template:

```bash
fly deploy -c fly-test.toml -a <your-test-app-name>
```

## Migrations During Deploy

The Fly templates run:

```bash
alembic upgrade head
```

as `release_command`. This applies pending migrations before the new app version receives traffic. If a migration needs manual review or data preparation, split the release into separate migration and app rollout steps.

## Environment Reference

### Required in Most Deployments

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN. Preferred for deployed environments. |
| `OPEN_FASTAPI_BASE` | Base URL of the upstream commentary API. |
| `OPEN_FASTAPI_APP_KEY` | Server-side API key for upstream proxy requests. |
| `WALLET_BFF_AUTH_TOKEN` | Required for wallet routes. |
| `PRICING_BFF_AUTH_TOKEN` | Required for pricing, cloud-drive, admin, and narrator proxy routes. |

### Database Fallback Variables

These are used only when `DATABASE_URL` is unset:

| Variable | Default |
|---|---|
| `NARRATOR_DB_HOST` | `localhost` |
| `NARRATOR_DB_PORT` | `5432` |
| `NARRATOR_DB_USER` | `postgres` |
| `NARRATOR_DB_PASSWORD` | empty |
| `NARRATOR_DB_NAME` | `postgres` |
| `NARRATOR_DB_CONNECT_TIMEOUT_SECONDS` | `10` |
| `NARRATOR_DB_STATEMENT_TIMEOUT_MS` | `15000` |
| `NARRATOR_DB_POOL_SIZE` | `10` |
| `NARRATOR_DB_POOL_MAX_OVERFLOW` | `0` |
| `NARRATOR_DB_POOL_TIMEOUT_SECONDS` | `5` |
| `NARRATOR_DB_POOL_RECYCLE_SECONDS` | `1800` |

### Runtime and Wallet

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | HTTP port. |
| `QUOTE_TTL_SECONDS` | `900` | Quote validity window. |
| `WALLET_IDEMPOTENCY_TTL_HOURS` | `168` | Retention window for idempotency cleanup. |
| `WALLET_IDEMPOTENCY_CLEANUP_BATCH_SIZE` | `1000` | Cleanup batch size. |
| `ORCHESTRATOR_ENABLED` | `0` | Enables the background orchestrator when set to `1` or `true`. |
| `ORCHESTRATOR_INTERVAL_SECONDS` | `10` | Orchestrator polling interval. |
| `ORCHESTRATOR_BATCH_SIZE` | `20` | Maximum tasks per orchestrator batch. |

## Health Checks

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
```

`/health` checks that the process is alive. `/ready` checks database readiness.

## Troubleshooting

### Wallet Routes Return 503

Set `WALLET_BFF_AUTH_TOKEN`. The backend fails closed when wallet auth is not configured.

### Pricing, Cloud-Drive, or Proxy Routes Return 503

Set `PRICING_BFF_AUTH_TOKEN`, `OPEN_FASTAPI_BASE`, and `OPEN_FASTAPI_APP_KEY`.

### `alembic upgrade head` Fails

Check database connectivity, permissions, and whether the migration expects an existing schema state that differs from your database. Roll back only when you understand the failed migration's side effects.

### Upstream Proxy Calls Time Out

Verify that `OPEN_FASTAPI_BASE` is reachable from the backend environment. Increase `OPEN_FASTAPI_TIMEOUT_SECONDS` or `OPEN_FASTAPI_TRANSFER_TIMEOUT_SECONDS` only after confirming the upstream service is healthy.
