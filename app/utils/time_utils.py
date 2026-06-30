from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return current UTC time as a timezone-naive datetime (compatible with MariaDB DATETIME columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
