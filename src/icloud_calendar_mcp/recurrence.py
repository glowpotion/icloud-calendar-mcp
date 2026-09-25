"""Recurrence rules: validating caller input and testing occurrences.

Recurring timed events are the one place this server writes ``TZID=`` times
instead of UTC. A weekly 09:00 meeting stored as ``08:00Z`` would drift to
10:00 local time once daylight saving ends, because an RRULE repeats the
stored wall-clock time. The matching ``VTIMEZONE`` is added at save time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable

from dateutil.rrule import rruleset, rrulestr
from icalendar.prop import vRecur

ALLOWED_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")


class RecurrenceError(ValueError):
    """Raised when a recurrence rule is unusable."""


def parse_rule(value: str, start: date | datetime, tz: tzinfo) -> vRecur:
    """Parse an RRULE string and check it agrees with the series start.

    ``start`` must already be in the form it will be written as: a date for
    all-day series, a datetime in the series timezone otherwise.
    """
    raw = (value or "").strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[len("RRULE:"):]
    if not raw:
        raise RecurrenceError("recurrence is empty.")

    try:
        rule = vRecur.from_ical(raw)
    except (ValueError, TypeError) as exc:
        raise RecurrenceError(
            f"recurrence={value!r} is not a valid RRULE. Expected e.g. "
            "'FREQ=WEEKLY;BYDAY=MO,WE', 'FREQ=DAILY;COUNT=5' or "
            "'FREQ=MONTHLY;BYMONTHDAY=1;UNTIL=20261231'."
        ) from exc

    freq = [str(f).upper() for f in rule.get("FREQ", [])]
    if len(freq) != 1 or freq[0] not in ALLOWED_FREQS:
        raise RecurrenceError(
            f"recurrence={value!r} needs FREQ set to one of {', '.join(ALLOWED_FREQS)}."
        )
    if "COUNT" in rule and "UNTIL" in rule:
        raise RecurrenceError("recurrence may set COUNT or UNTIL, not both.")

    rule = with_normalised_until(rule, start, tz)
    until = rule.get("UNTIL")
    if until and _as_dt(until[0]) < _as_dt(_until_comparable(start)):
        raise RecurrenceError(
            f"recurrence UNTIL ({until[0].isoformat()}) is before the event starts."
        )

    check_start(rule, start, tz)
    return rule


def check_start(rule: vRecur, start: date | datetime, tz: tzinfo) -> None:
    """Refuse a DTSTART that is not itself an occurrence of ``rule``.

    RFC 5545 leaves such a series undefined, and clients disagree about it.
    """
    series = Series(start, rule, (), tz, include_start=False)
    first = series.after(start, inclusive=True)
    if first is None:
        raise RecurrenceError(
            f"recurrence {rule_text(rule)!r} produces no occurrences from {_iso(start)}."
        )
    if first != start:
        raise RecurrenceError(
            f"The event starts {_iso(start)}, which is not an occurrence of "
            f"{rule_text(rule)!r}; the first one is {_iso(first)}. Move the start "
            "to that date or adjust the rule."
        )


def rule_text(rule: vRecur) -> str:
    return rule.to_ical().decode("utf-8")


def truncated_before(rule: vRecur, occurrence: date | datetime) -> vRecur:
    """A copy of ``rule`` that ends just before ``occurrence``."""
    cut = vRecur(dict(rule))
    cut.pop("COUNT", None)
    if isinstance(occurrence, datetime):
        until: date | datetime = occurrence.astimezone(timezone.utc) - timedelta(seconds=1)
    else:
        until = occurrence - timedelta(days=1)
    cut["UNTIL"] = [until]
    return cut


def with_normalised_until(rule: vRecur, start: date | datetime, tz: tzinfo) -> vRecur:
    """RFC 5545 wants UNTIL as a DATE for all-day series and UTC otherwise."""
    until = rule.get("UNTIL")
    if not until:
        return rule
    value = until[0]
    if isinstance(start, datetime):
        if not isinstance(value, datetime):
            # A date bound on a timed series means "through the end of that day".
            value = datetime.combine(value, time(23, 59, 59), tzinfo=tz)
        elif value.tzinfo is None:
            value = value.replace(tzinfo=start.tzinfo or tz)
        if start.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        else:
            value = value.astimezone(tz).replace(tzinfo=None)
    elif isinstance(value, datetime):
        value = (value if value.tzinfo else value.replace(tzinfo=tz)).astimezone(tz).date()
    fixed = vRecur(dict(rule))
    fixed["UNTIL"] = [value]
    return fixed


class Series:
    """The occurrence set of a recurring VEVENT, ignoring EXDATEs."""

    def __init__(
        self,
        start: date | datetime,
        rule: vRecur | None,
        rdates: Iterable[date | datetime],
        tz: tzinfo,
        *,
        include_start: bool = True,
    ):
        self.all_day = not isinstance(start, datetime)
        self._tz = tz
        dtstart = _as_dt(start)
        self._set = rruleset()
        if rule is not None:
            rule = with_normalised_until(rule, start, tz)
            self._set.rrule(rrulestr(rule_text(rule), dtstart=dtstart))
        if include_start:
            self._set.rdate(dtstart)
        for value in rdates:
            self._set.rdate(self._align(value, dtstart))

    def contains(self, when: date | datetime) -> bool:
        dt = _as_dt(when)
        return bool(self._set.between(dt, dt, inc=True))

    def after(self, when: date | datetime, *, inclusive: bool = False) -> date | datetime | None:
        found = self._set.after(_as_dt(when), inc=inclusive)
        return self._back(found) if found is not None else None

    def between(self, lo: date | datetime, hi: date | datetime) -> list[date | datetime]:
        """Occurrences in the half-open range ``[lo, hi)``."""
        dt_lo, dt_hi = _as_dt(lo), _as_dt(hi)
        return [
            self._back(v)
            for v in self._set.between(dt_lo, dt_hi, inc=True)
            if v < dt_hi
        ]

    def _back(self, value: datetime) -> date | datetime:
        return value.date() if self.all_day else value

    def _align(self, value: date | datetime, dtstart: datetime) -> datetime:
        value = _as_dt(value)
        if dtstart.tzinfo is None and value.tzinfo is not None:
            return value.astimezone(self._tz).replace(tzinfo=None)
        if dtstart.tzinfo is not None and value.tzinfo is None:
            return value.replace(tzinfo=dtstart.tzinfo)
        return value


def dates_of(prop: Any) -> list[date | datetime]:
    """Flatten an EXDATE/RDATE property (one line or several) into values."""
    if prop is None:
        return []
    lines = prop if isinstance(prop, list) else [prop]
    values: list[date | datetime] = []
    for line in lines:
        for item in getattr(line, "dts", []):
            if isinstance(item.dt, (date, datetime)):
                values.append(item.dt)
    return values


def _as_dt(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


def _until_comparable(start: date | datetime) -> date | datetime:
    if isinstance(start, datetime) and start.tzinfo is not None:
        return start.astimezone(timezone.utc)
    return start


def _iso(value: date | datetime) -> str:
    return value.isoformat()
