"""
Timestamp normalisation, shared by everything that reads a stored instant.

The same helper exists in the OVMS server as app/utils/timestamps.py, deliberately:
these two services read each other's rows, and a rule that holds in one of them and
not the other is worth less than no rule at all.

Karto's own database is PostgreSQL, so its `timestamptz` columns read back tz-aware.
The OVMS database it also reads is whatever that deployment chose, and the default is
SQLite — which has no timezone type at all and hands back naive values. Both shapes
therefore arrive here, from the same code, and the difference is invisible until a
comparison or a format string silently means something else.
"""

import datetime
from typing import Annotated, Optional

from pydantic import PlainSerializer


def as_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """The instant this value names, in UTC.

    Two cases, and telling them apart is the whole point:

    * **Naive** — a SQLite column, or a parsed protocol timestamp. Every timestamp
      either server writes is UTC, so the missing tzinfo is a missing *label*, not an
      unknown zone. Attaching it is correct.
    * **Aware** — a PostgreSQL `timestamptz`, returned in the session's TimeZone.
      Overwriting its tzinfo would move the instant by that offset; converting keeps
      it. This is why `replace(tzinfo=utc)` is wrong on its own and appears nowhere
      outside this function: applied to an aware value it accepts an API key hours
      after it expired, or attributes a trip to the wrong registration.

    None passes through: an absent timestamp is not a time, and must not become the
    epoch by way of a default.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


UtcDatetime = Annotated[
    datetime.datetime,
    PlainSerializer(as_utc, return_type=datetime.datetime),
]
