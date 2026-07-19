"""
SQLite has no native datetime type -- it stores ISO strings, and reads them
back as naive datetimes regardless of what was written. To keep comparisons
safe across both SQLite (local dev) and Postgres (production), we adopt one
convention everywhere in this codebase: all datetimes are UTC and naive
(no tzinfo attached) once they cross into application/DB logic.
"""

from datetime import datetime, timezone


def to_naive_utc(dt: datetime) -> datetime:
    if dt is None:
        return dt
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(tzinfo=None)
    return dt


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
