"""Formatting helpers for the /account/me response.

Pulled out of server.py so they can be unit-tested in isolation without
having to spin up the Flask app + DB engine.
"""

from __future__ import annotations

from decimal import Decimal


def mask_mobile(mobile: str | None) -> str | None:
    """Mask a phone number for client display: middle chars → `*`.

    `13912345678` → `139*****678` (matches the example: first 3 + middle 5
    asterisks + last 3, total 11 chars). For values too short to keep the
    head/tail readable (< 7 chars) we mask the entire string rather than
    leak it raw — `mobile` is `TEXT` and `scripts/create_user.py --mobile`
    does no format check, so a test / mistakenly-entered short value
    would otherwise pass through and violate the "raw value never leaves
    backend" API boundary (contract compatibility on regression coverage).

    Returns `None` when input is `None` (so the JSON encoder renders `null`,
    not `""`).
    """
    if mobile is None:
        return None
    if len(mobile) < 7:
        return "*" * len(mobile)
    head, tail = mobile[:3], mobile[-3:]
    middle = "*" * (len(mobile) - 6)
    return f"{head}{middle}{tail}"


def format_balance(points: object) -> str | None:
    """Render `users.balance_points` (NUMERIC(18,2)) as a 2-decimal string.

    The /account/me example pins the contract to a string (`"2340.02"`),
    not a float, to avoid the usual JSON-float rounding surprises on the
    client.

    Input may arrive as Decimal (real PG NUMERIC), int, float (sqlite test
    REAL coercion), or str — coerce via `str()` so a float `2340.02` doesn't
    decompose into `Decimal('2340.019999999…')`.

    Returns `None` if `points` is `None` (defensive — `balance_points` is
    `NOT NULL` in schema so this should not happen at runtime).
    """
    if points is None:
        return None
    return f"{Decimal(str(points)):.2f}"
