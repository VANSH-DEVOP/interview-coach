"""Time helpers.

One function, because getting it wrong is silent. Every timestamp column in
this schema is `TIMESTAMP WITHOUT TIME ZONE` holding UTC, so anything handed to
asyncpg must be naive or the driver rejects it -- and anything compared against
a stored value must be naive too, or the comparison is between an aware and a
naive datetime and raises at runtime.
"""

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Current UTC time, naive, matching the schema's datetime columns.

    Computed in UTC and stripped rather than via `datetime.utcnow()`, which is
    deprecated in 3.12.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_plus(delta: timedelta) -> datetime:
    """`utcnow()` offset by `delta`. For expiry timestamps."""
    return utcnow() + delta
