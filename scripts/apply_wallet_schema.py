"""Apply wallet schema migrations to the configured Postgres database."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402


def main() -> None:
    alembic_cfg = Config(REPO_ROOT / "alembic.ini")
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(alembic_cfg, "head")
    print("wallet schema migrations applied")


if __name__ == "__main__":
    main()
