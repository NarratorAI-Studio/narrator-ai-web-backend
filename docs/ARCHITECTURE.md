# Architecture Overview

[中文](./ARCHITECTURE_CN.md)

`narrator-ai-web-backend` is a synchronous Flask application backed by Postgres. It is designed to sit between a web frontend and an upstream commentary API.

## Main Responsibilities

- Authenticate backend-for-frontend requests with server-side bearer tokens.
- Store end-user profiles and wallet balances.
- Compute and persist quotes for template-based commentary workflows.
- Manage wallet transaction state from quote to freeze, confirm, or refund.
- Store user-scoped long-running task state in Postgres.
- Proxy selected cloud-drive, metadata, and commentary-task calls to an upstream API.
- Run an optional background orchestrator for eligible tasks.

## Runtime Components

| Component | Files | Responsibility |
|---|---|---|
| Flask app | `server.py` | Route registration, health checks, OpenAPI serving, request-level orchestration |
| Users | `users/`, `account/` | End-user app keys, profile fields, account response formatting |
| Wallet | `wallet/` | Quote pinning, freezes, confirms, refunds, idempotency |
| Pricing | `pricing/`, `pricing_catalog_v2/`, `pricing_quote_v2/` | Template prices, catalog validation, quote snapshots |
| Cloud drive | `cloud_drive/` | File ownership, quota accounting, upstream file proxying |
| Narrator proxy | `narrator_metadata/`, `narrator_proxy/` | Metadata and task API proxy routes |
| Tasks | `narrator_tasks/` | User-scoped persisted task body and hot columns |
| Orchestrator | `orchestrator/` | Optional background task advancement |
| Database | `db/`, `migrations/` | Table metadata and Alembic migrations |

## Request Boundaries

The backend uses two separate authentication concepts:

- BFF bearer tokens, configured with `WALLET_BFF_AUTH_TOKEN` and `PRICING_BFF_AUTH_TOKEN`, prove that a request came from a trusted server-side frontend or integration.
- `X-Web-App-Key` identifies the end user for routes that read or write user-owned data, consume quota, or affect wallet balance.

Browser-visible code should not contain BFF bearer tokens or upstream service keys.

## Data Boundaries

- Postgres is the source of truth for users, wallet state, quotes, cloud-drive ownership, and task storage.
- Upstream API responses are proxied only through explicit route allowlists.
- Task bodies are stored as JSON so the frontend task shape can evolve without a migration for every non-query field.
- Hot columns such as status, current step, user id, and timestamps are stored separately for efficient filtering and tenant checks.

## Orchestration

The orchestrator is disabled by default. Enable it with:

```bash
ORCHESTRATOR_ENABLED=1
```

When enabled, it polls eligible task rows, claims work through database coordination, triggers the next upstream step, and persists the resulting task state.

## API Contract

The static OpenAPI contract is served from:

```text
/openapi.json
```

The file is committed as [`openapi.json`](../openapi.json). Keep it aligned with route behavior when changing public APIs.
