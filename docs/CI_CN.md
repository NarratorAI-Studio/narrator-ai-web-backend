# CI 与质量指南

[英文](./CI.md)

本仓使用轻量级本地质量门禁。打开 PR 前请先执行。

## 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 测试

运行完整测试：

```bash
pytest
```

运行单个文件：

```bash
pytest tests/test_wallet_api.py
```

运行单个测试：

```bash
pytest tests/test_wallet_api.py::test_quote_requires_idempotency_key
```

## Lint

如果当前环境安装了 `ruff`：

```bash
ruff check .
```

仓库可能已有历史 lint findings。新改动不应引入新的 findings。

## 数据库检查

修改迁移时执行：

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

请使用一次性的本地数据库测试迁移。

## API 合同检查

路由行为变化时，检查：

- [`openapi.json`](../openapi.json)
- 覆盖该路由的测试
- README 和 docs 中对该 endpoint 的引用

## Secret Hygiene

发布分支前，确认没有真实凭证或环境文件被 stage：

```bash
git status --short
git diff --cached --name-only
```

建议额外执行：

```bash
gitleaks detect --source . --redact --no-banner
```

如果当前环境没有安装该 scanner，请使用可用的其他 secret scanner，并在 PR 中说明结果。
