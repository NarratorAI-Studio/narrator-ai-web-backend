# narrator-ai-web-backend

[中文](./README_CN.md)

`narrator-ai-web-backend` is a self-hostable Flask backend for NarratorAI, a template-driven workflow that helps users turn movies, short dramas, raw clips, and other long-form video material into commentary videos.

The companion frontend lets users upload source videos or import video URLs, choose a commentary template, review the task price, and submit the job. This backend provides the API layer for template pricing, quotes, wallet transactions, user-scoped task storage, cloud-drive proxying, task orchestration, and upstream commentary-service proxy routes.

Each commentary template can carry its own price. Before a task is submitted, the frontend asks this backend for a quote so the user can confirm the charge first. The connected upstream commentary service can then generate commentary scripts, editing instructions, and a condensed commentary video suitable for publishing on short-video platforms.

This project is useful for movie commentary, short-drama commentary, plot recaps, content-studio batch production, and teams that want to package commentary templates as sellable products.

Companion frontend repository: [NarratorAI-Studio/narrator-ai-web](https://github.com/NarratorAI-Studio/narrator-ai-web).

## Demo

A hosted demo of the full web experience is available at **https://app.jieshuo.cn/**.

## Screenshots

**Custom material upload**

<img src="./docs/assets/screenshot-custom-material.png" alt="Custom material upload screen" width="800">

**Template selection**

<img src="./docs/assets/screenshot-template-selection.png" alt="Template selection screen" width="800">

## Features

- Flask API server with a static OpenAPI contract at [`openapi.json`](./openapi.json)
- Postgres-backed wallet lifecycle: quote, freeze, confirm, refund
- Pricing catalog and quote APIs for template-based workflows
- User-scoped task storage and orchestration for long-running commentary jobs
- Cloud-drive upload, download, transfer, listing, and deletion proxy routes
- Backend-for-frontend bearer-token checks for wallet, pricing, cloud-drive, and proxy APIs
- Alembic database migrations and a pytest test suite

## Requirements

- Python 3.11+
- Postgres 14+
- An upstream commentary API compatible with the configured proxy routes
- Optional: Docker 24+ for containerized local runs

## Documentation

| Document | Purpose |
|---|---|
| [Deployment Guide](./docs/DEPLOYMENT.md) | Local, Docker, and Fly.io deployment steps |
| [Architecture Overview](./docs/ARCHITECTURE.md) | Main modules, request flow, and data boundaries |
| [CI and Quality Guide](./docs/CI.md) | Tests, linting, and release checks |
| [Security Policy](./SECURITY.md) | Private vulnerability reporting process |
| [Contributing Guide](./CONTRIBUTING.md) | Branch, commit, PR, and local quality conventions |

## Configuration

Copy `.env.example` to `.env` and set values for your environment.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN. Takes precedence when set. |
| `NARRATOR_DB_HOST` / `NARRATOR_DB_PORT` / `NARRATOR_DB_USER` / `NARRATOR_DB_PASSWORD` / `NARRATOR_DB_NAME` | Fallback Postgres connection settings when `DATABASE_URL` is unset. |
| `OPEN_FASTAPI_BASE` | Base URL for your upstream commentary API. |
| `OPEN_FASTAPI_APP_KEY` | Server-side upstream API key used by backend proxy calls. |
| `WALLET_BFF_AUTH_TOKEN` | Bearer token required by `/wallet/*` routes. |
| `PRICING_BFF_AUTH_TOKEN` | Bearer token required by pricing, cloud-drive, narrator-metadata, narrator-proxy, and admin routes. |
| `PORT` | HTTP port. Defaults to `8080`. |

The full list of supported variables is documented inline in [`.env.example`](./.env.example).

## Quick Start

```bash
git clone <repo-url> narrator-ai-web-backend
cd narrator-ai-web-backend

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your local Postgres connection, BFF tokens, and upstream API settings.

alembic upgrade head
python server.py
```

The server listens on `PORT`, which defaults to `8080`.

Check the service:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
curl http://localhost:8080/openapi.json
```

## Docker

Published Linux images are available from Alibaba Cloud Container Registry:

| Tag | Purpose |
|---|---|
| `registry.cn-hangzhou.aliyuncs.com/narrator-ai/public:backend-latest` | Latest public backend image |
| `registry.cn-hangzhou.aliyuncs.com/narrator-ai/public:backend-0.1.0-eae5acf` | Versioned backend image built from `docs/public` commit `eae5acf` |

Both tags are built for `linux/amd64`. Because the frontend and backend share the same registry repository, the project uses component-specific tags instead of a single `latest` tag.

Pull the image:

```bash
docker pull --platform linux/amd64 registry.cn-hangzhou.aliyuncs.com/narrator-ai/public:backend-latest
```

Run it against a reachable Postgres database and upstream commentary API:

```bash
docker run --rm --platform linux/amd64 -p 8080:8080 \
  -e DATABASE_URL='postgres://<username>:<password>@<host>:5432/<database>' \
  -e OPEN_FASTAPI_BASE='https://api.example.com' \
  -e OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  -e WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  -e PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  registry.cn-hangzhou.aliyuncs.com/narrator-ai/public:backend-latest
```

Check the running container:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/openapi.json
```

After the backend is running, start the frontend image with `NARRATOR_PRICING_API_URL=http://host.docker.internal:8080` so its BFF routes can reach this service from another Docker container on the same machine.

To pin the tested version instead of the moving tag:

```bash
docker run --rm --platform linux/amd64 -p 8080:8080 \
  -e DATABASE_URL='postgres://<username>:<password>@<host>:5432/<database>' \
  -e OPEN_FASTAPI_BASE='https://api.example.com' \
  -e OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  -e WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  -e PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  registry.cn-hangzhou.aliyuncs.com/narrator-ai/public:backend-0.1.0-eae5acf
```

Build a local image when you want to modify the source:

```bash
docker build -t narrator-ai-web-backend:local .
```

Run the locally built image against a reachable Postgres database:

```bash
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgres://<username>:<password>@<host>:5432/<database>' \
  -e OPEN_FASTAPI_BASE='https://api.example.com' \
  -e OPEN_FASTAPI_APP_KEY='<upstream-api-key>' \
  -e WALLET_BFF_AUTH_TOKEN='<wallet-bff-token>' \
  -e PRICING_BFF_AUTH_TOKEN='<pricing-bff-token>' \
  narrator-ai-web-backend:local
```

The included `Dockerfile` starts the Flask app with `python server.py` and exposes port `8080`.

## Database Migrations

Apply all migrations before starting the app:

```bash
alembic upgrade head
```

Rollback one migration when needed:

```bash
alembic downgrade -1
```

For production deployments, run migrations as a release step before routing traffic to the new application version.

## Testing

```bash
pytest
```

Optional lint command, when `ruff` is installed in your environment:

```bash
ruff check .
```

## Core API Endpoints

For the complete API surface, see [`openapi.json`](./openapi.json).

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Database-backed readiness check |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/openapi.json` | OpenAPI contract |
| `POST` | `/pricing/hard-price` | Query a template price by `template_id` and `combo_key` |
| `GET` | `/pricing/hard-price/all?template_id=N` | Query all template-price tiers for a template |
| `POST` | `/pricing/quote` | Create a server-side quote |
| `POST` | `/wallet/quotes` | Pin a server-approved quote |
| `POST` | `/wallet/freezes` | Freeze a quoted amount |
| `POST` | `/wallet/confirms` | Confirm a frozen transaction |
| `POST` | `/wallet/refunds` | Refund or release a frozen transaction |
| `GET` | `/wallet/transactions/{wallet_transaction_id}` | Query a wallet transaction |
| `GET` | `/narrator/tasks` | List authenticated user's stored tasks |
| `POST` | `/narrator/tasks` | Create or upsert a stored task |
| `POST` | `/cloud-drive/files/upload/presigned-url` | Create a cloud-drive upload URL |

The `hard-price` path fragment is a historical route identifier retained for API compatibility. In documentation it means "template price."

## Typical Flow

1. The frontend authenticates to the backend and supplies an end-user app key where required.
2. The user uploads or references source material through the cloud-drive proxy.
3. The frontend asks the backend to quote the selected template workflow.
4. The backend validates the request, persists the quote, and returns a server-approved amount.
5. The frontend freezes wallet balance before starting the long-running task.
6. The backend stores task state and can advance eligible tasks through the orchestrator.
7. The wallet transaction is confirmed on success or refunded on failure.

## Project Layout

```text
server.py                  Flask app, route wiring, health checks, OpenAPI serving
account/                   Account profile formatting
cloud_drive/               Cloud-drive proxy schema, upstream client, and local store
db/                        Shared SQLAlchemy table metadata
finance/                   Reconciliation helpers
migrations/                Alembic migrations
narrator_metadata/         Read-only upstream metadata proxy routes
narrator_proxy/            Commentary-task upstream proxy routes
narrator_tasks/            User-scoped task schema and persistence
orchestrator/              Background task advancement
pricing/                   Template-price v1 endpoints and helpers
pricing_catalog_v2/        Pricing catalog persistence and validation
pricing_quote_v2/          Quote generation and snapshot logic
scripts/                   Local maintenance and migration helper scripts
tests/                     Pytest suite
users/                     End-user key and profile management
wallet/                    Wallet lifecycle implementation
```

## Contact

For project questions, use this repository's GitHub issues or discussions. A project QR code is also available for community contact.

Tip: For large-volume needs or technical support requests, contact us through the same channel.

<img src="./docs/assets/project-contact-qr.png" alt="Project contact QR code" width="240">

## License

Apache License 2.0. See [LICENSE](./LICENSE).

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

Do not report vulnerabilities through public issues. Report privately at security@gridltd.com. See [SECURITY.md](./SECURITY.md).
