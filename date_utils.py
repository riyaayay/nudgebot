"""Date and time utilities: UTC normalization, robust portal date parsing,
and user timezone conversions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateutil.parser

logger = logging.getLogger(__name__)


def parse_to_utc(raw: str | None, default_tz_name: str = "Asia/Kolkata") -> datetime | None:
    """Parses a raw date string from a portal into an aware UTC datetime object."""
    if not raw or not isinstance(raw, str):
        return None

    cleaned = raw.strip()
    if not cleaned:
        return None

    try:
        default_tz = ZoneInfo(default_tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        default_tz = ZoneInfo("UTC")

    # Fast path: fromisoformat
    try:
        # replace Z with +00:00 for older python compatibility if needed
        iso_str = cleaned.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt.astimezone(UTC)
    except Exception:
        pass

    # dateutil fallback
    try:
        dt = dateutil.parser.parse(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt.astimezone(UTC)
    except Exception:
        logger.debug("Could not parse date string '%s'", cleaned)
        return None


def normalize_due_date(raw: str | None, default_tz_name: str = "Asia/Kolkata") -> str | None:
    """Normalizes any valid date string into a canonical UTC ISO-8601 string.
    If parsing fails, returns the stripped raw string so we don't lose information."""
    if not raw:
        return None
    dt = parse_to_utc(raw, default_tz_name)
    if dt is not None:
        return dt.isoformat()
    return raw.strip()


def format_in_timezone(
    iso_utc_or_raw: str | None,
    tz_name: str = "Asia/Kolkata",
    fmt: str = "%a, %b %d at %I:%M %p",
) -> str:
    """Formats an ISO UTC date string into a user-friendly string in the user's timezone."""
    if not iso_utc_or_raw:
        return "No due date"

    try:
        user_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        user_tz = ZoneInfo("UTC")

    dt = parse_to_utc(iso_utc_or_raw, tz_name)
    if dt is not None:
        local_dt = dt.astimezone(user_tz)
        return local_dt.strftime(fmt)

    # Return raw text if not parseable
    return iso_utc_or_raw


def hours_until_due(iso_utc: str | None) -> float | None:
    """Returns hours remaining until due date (positive if in future, negative if past)."""
    if not iso_utc:
        return None
    dt = parse_to_utc(iso_utc)
    if dt is None:
        return None
    now = datetime.now(UTC)
    return (dt - now).total_seconds() / 3600.0
