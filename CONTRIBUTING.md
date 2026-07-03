# Contributing to NarratorAI Web

Thanks for your interest in contributing. This document describes the conventions we expect from PRs against this repository.

## Code of Conduct

This project follows the [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md). Please read it before contributing — by participating, you agree to abide by it.

## Getting set up

See [`README.md`](./README.md) for prerequisites and the quick start. The TL;DR:

```bash
python -m venv .venv && source .venv/bin/activate   # or your preferred venv tool
pip install -r requirements.txt
cp .env.example .env
# edit .env with your local database + secrets
alembic upgrade head
python server.py
```

For deployment-related setup, see [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md).

## Branching model

We use a simplified Git Flow:

| Branch prefix | Target | When to use |
|---|---|---|
| `feature/<issue-id>-<short-desc>` | `develop` | New user-facing features |
| `bugfix/<issue-id>-<short-desc>` | `develop` | Non-urgent bug fixes |
| `chore/<issue-id>-<short-desc>` | `develop` | Refactors, cleanups, dependency bumps, docs |
| `hotfix/<issue-id>-<short-desc>` | `main` | Urgent production fixes only |
| `release/<version>` | `main` | Release preparation |

Rules enforced by CI:

- Only `release/*` and `hotfix/*` may target `main` directly. Everything else targets `develop`.
- Branch names use lowercase + hyphens (e.g. `feature/42-add-export`).
- Delete branches after merge.

If you're not sure which branch type to use, ask in the issue before opening the PR.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer — Closes #N, BREAKING CHANGE: …, etc.]
```

Valid types: `feat`, `fix`, `chore`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `revert`.

Examples:

```
feat(narrator): add SRT preview on confirm page
fix(cloud-drive): poll inflight transfers and refresh files on completion
docs(deployment): document NEXT_DEV_BUNDLER workaround for Turbopack crash
```

Breaking changes go in the footer (`BREAKING CHANGE: …`) or as `!` after the type (`feat(api)!: remove v1 endpoints`).

## Pull requests

PR title follows the same Conventional Commits format. Body must contain three sections in order — CI enforces this:

```markdown
## Related Issue

Closes #<N>

## Summary

- <what changed and why>

## Test Plan

- [ ] <how to verify>
```

Other expectations:

- Each PR references and closes a single issue (`Closes #N`, `Fixes #N`, or `Resolves #N`). Use `Refs #N` only when the PR is one of several closing a larger planning record.
- Keep PRs focused: one concern per PR. If you find yourself touching unrelated areas, split into separate PRs.
- Surface tradeoffs in the PR description, especially for non-obvious choices.
- All PRs need at least one approval before merge.
- Default merge strategy: **Squash Merge** for feature → develop; **Merge Commit** for release / hotfix → main.

If your issue lists explicit Acceptance Criteria, add an `## AC Verification` section to the PR body mapping each AC to evidence (file + line, command output, screenshot, etc.). CI's `ac-verify-gate` requires this when AC items exist in the linked issue.

## Local quality gates

Before opening a PR, please run:

```bash
make lint       # ruff check — should not introduce new errors
make typecheck  # if configured
make test       # pytest — should pass
```

Or directly:

```bash
pytest                                # run tests
ruff check .                          # lint
```

The repo carries a baseline of existing lint warnings/errors. **Don't introduce new ones**; if you can opportunistically reduce the count, even better, but it's not required.

There is an opt-in pre-commit hook at `.git-hooks/pre-commit`. Enable it with:

```bash
git config core.hooksPath .git-hooks
```

## Reporting bugs / requesting features

Use the issue templates:

- Bug report — when something doesn't work as expected
- Feature request — when you want to propose new functionality

Please search existing issues before opening a new one.

## Security

Do not report security issues through public GitHub issues. See [`SECURITY.md`](./SECURITY.md) for the disclosure process.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](./LICENSE) — the same license under which the project is distributed.
