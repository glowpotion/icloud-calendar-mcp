"""ISO 8601 parsing and normalisation.

iCloud is strict about how time values reach it, so everything written to the
server is converted to UTC and serialised in the ``...Z`` form. That sidesteps
``TZID=`` parameters entirely, which would otherwise require shipping a matching
``VTIMEZONE`` component in the same VCALENDAR or risk iCloud rejecting or
silently misplacing the event.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo


class TimeParseError(ValueError):
    """Raised when an input string is not a usable ISO 8601 timestamp."""


def parse_datetime(value: str, default_tz: tzinfo, *, field: str) -> datetime:
    """Parse an ISO 8601 date or datetime into an aware datetime.

    A bare date (``2026-09-24``) is read as midnight. A naive datetime is
    assumed to be in ``default_tz`` rather than silently treated as UTC.
    """
    raw = (value or "").strip()
    if not raw:
        raise TimeParseError(f"{field} is required and must be an ISO 8601 timestamp.")

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw), time.min)
        except ValueError as exc:
            raise TimeParseError(
                f"{field}={raw!r} is not valid ISO 8601. Expected e.g. "
                "'2026-09-24T14:00:00', '2026-09-24T14:00:00+01:00' or '2026-09-24'."
            ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC."""
    if value.tzinfo is None:
        raise TimeParseError("Refusing to convert a naive datetime to UTC.")
    return value.astimezone(timezone.utc)


def normalise(value: date | datetime | None, display_tz: tzinfo) -> str | None:
    """Render a VEVENT date/datetime as an ISO 8601 string for the caller.

    All-day values stay as plain dates; timed values are rendered in
    ``display_tz`` with an explicit offset so the caller never has to guess.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=display_tz)
        return value.astimezone(display_tz).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def default_window(default_tz: tzinfo, days: int = 30) -> tuple[datetime, datetime]:
    """The now → now + ``days`` window used when the caller gives no range."""
    now = datetime.now(tz=default_tz)
    return now, now + timedelta(days=days)
