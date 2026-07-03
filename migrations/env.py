from __future__ import annotations

from pathlib import Path
import sys

from alembic import context
from sqlalchemy import create_engine


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server import db_url, positive_int_env  # noqa: E402


target_metadata = None


def migration_url() -> str:
    url = db_url()
    render_as_string = getattr(url, "render_as_string", None)
    if render_as_string:
        return render_as_string(hide_password=False)
    return str(url)


def run_migrations_offline() -> None:
    context.configure(
        url=migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        db_url(),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": positive_int_env(
                "NARRATOR_DB_CONNECT_TIMEOUT_SECONDS", 10
            ),
            "options": "-c statement_timeout="
            f"{positive_int_env('NARRATOR_DB_STATEMENT_TIMEOUT_MS', 15000)}",
        },
    )
    with connectable.begin() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
