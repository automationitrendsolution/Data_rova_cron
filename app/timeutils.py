"""Timezone-aware time handling.

Every timestamp is explicit about its zone.  The server's local time is never
used, so the same code produces the same snapshot date on a server in UTC, a
laptop in IST, and a container with no ``/etc/localtime`` at all.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import INDIA_TZ, UTC

__all__ = ["UTC", "INDIA_TZ", "now_utc", "now_in", "snapshot_date"]


def now_utc() -> datetime:
    """Current instant, in UTC."""
    return datetime.now(tz=UTC)


def now_in(tz: ZoneInfo, *, reference: datetime | None = None) -> datetime:
    """Convert ``reference`` (default: now) into ``tz``."""
    return (reference or now_utc()).astimezone(tz)


def snapshot_date(tz: ZoneInfo, *, reference: datetime | None = None) -> date:
    """The calendar day a run's snapshot is filed under, in ``tz``."""
    return now_in(tz, reference=reference).date()
