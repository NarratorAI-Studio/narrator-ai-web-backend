"""Delete wallet idempotency records older than configured retention."""

from pathlib import Path
import os
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server import get_wallet_service  # noqa: E402


def main() -> None:
    batch_size = int(os.environ.get("WALLET_IDEMPOTENCY_CLEANUP_BATCH_SIZE", "1000"))
    deleted = get_wallet_service().cleanup_expired_idempotency_records(
        batch_size=batch_size
    )
    print(f"wallet idempotency records deleted: {deleted}")


if __name__ == "__main__":
    main()
