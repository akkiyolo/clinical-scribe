"""Small time helpers shared across services."""

from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime | None:
    """Treat naive datetimes (SQLite returns them) as UTC, which is what the app stores."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
