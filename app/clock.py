"""A single place to ask for the current time.

MySQL's DATETIME carries no timezone, so everything here is naive UTC and
never local time - mixing the two is how you end up with "can't compare
offset-naive and offset-aware datetimes".
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
