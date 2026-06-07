"""Shared parsing helpers used by the export scripts.

Extracted from :mod:`firefly_iii_utils.guess_categories` so multiple
scripts can lean on the same Firefly III date / amount parsing
without copy-paste drift.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation


def parse_date(value: str) -> datetime:
    """Parse Firefly III's ISO 8601 date-time, tolerating a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_date(value: str) -> str:
    """Return the ``YYYY-MM-DD`` date portion of a Firefly III date-time."""
    return parse_date(value).date().isoformat()


def parse_amount(value: str) -> Decimal:
    """Parse a Firefly III amount string into a :class:`Decimal`."""
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {value!r}") from exc
