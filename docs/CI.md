# CI and Quality Guide

[中文](./CI_CN.md)

This repository uses lightweight local quality gates. Run them before opening a pull request.

## Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Tests

Run the full suite:

```bash
pytest
```

Run one file:

```bash
pytest tests/test_wallet_api.py
```

Run one test:

```bash
pytest tests/test_wallet_api.py::test_quote_requires_idempotency_key
```

## Lint

If `ruff` is installed:

```bash
ruff check .
```

The repository may carry existing lint findings. New changes should not introduce new findings.

## Database Checks

For migration changes:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Use a disposable local database for migration testing.

## API Contract Checks

When route behavior changes, review:

- [`openapi.json`](../openapi.json)
- Tests that exercise the changed route
- README and docs references to the changed endpoint

## Secret Hygiene

Before publishing a branch, check that no real credentials or environment files are staged:

```bash
git status --short
git diff --cached --name-only
```

Recommended additional scan:

```bash
gitleaks detect --source . --redact --no-banner
```

If the scanner is not installed, use another secret scanner available in your environment and document the result in the PR.
