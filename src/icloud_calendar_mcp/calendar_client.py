"""CalDAV client for iCloud calendars.

Apple publishes no REST API for iCloud Calendar, so this talks CalDAV against
https://caldav.icloud.com using an app-specific password.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable
from urllib.parse import quote

import caldav
from caldav.lib import error as caldav_error
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from .config import Config, looks_like_app_specific_password
from .recurrence import (
    RecurrenceError,
    Series,
    check_start,
    dates_of,
    parse_rule,
    rule_text,
    truncated_before,
)
from .timeutil import (
    TimeParseError,
    default_window,
    normalise,
    parse_datetime,
    to_utc,
)

PRODID = "-//icloud-calendar-mcp//EN"
MAX_WINDOW_DAYS = 400


class CalendarToolError(RuntimeError):
    """Any failure the caller can act on, phrased for an agent to read."""


class CalendarNotFoundError(CalendarToolError):
    pass


class EventNotFoundError(CalendarToolError):
    pass


@dataclass
class EventRecord:
    """A single occurrence of an event, normalised for the caller."""

    title: str
    start_time: str | None
    end_time: str | None
    event_id: str
    location: str | None = None
    description: str | None = None
    all_day: bool = False
    recurrence_id: str | None = None
    calendar_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class _Stored:
    """A calendar object resource and the VEVENTs sharing one UID inside it."""

    resource: Any
    vcal: ICalendar
    uid: str
    master: Any | None
    overrides: list[Any]


@dataclass
class ICloudCalendarClient:
    """Thin, lazily-connected wrapper over :mod:`caldav`."""

    config: Config
    _principal: Any = field(default=None, init=False, repr=False)
    _calendars: list[Any] | None = field(default=None, init=False, repr=False)

    # -- connection ---------------------------------------------------------

    @property
    def _is_icloud(self) -> bool:
        return "icloud.com" in self.config.url

    @property
    def _server_name(self) -> str:
        """How errors refer to the server, since CALDAV_URL may not be iCloud."""
        return "iCloud" if self._is_icloud else "The CalDAV server"

    def principal(self) -> Any:
        if self._principal is None:
            try:
                client = caldav.DAVClient(
                    url=self.config.url,
                    username=self.config.username,
                    password=self.config.password,
                )
                self._principal = client.principal()
            except caldav_error.AuthorizationError as exc:
                hint = ""
                if self._is_icloud and not looks_like_app_specific_password(
                    self.config.password
                ):
                    hint = (
                        " The supplied password does not look like an app-specific "
                        "password (Apple issues them as 'xxxx-xxxx-xxxx-xxxx'). "
                        "Generate one at https://appleid.apple.com — a normal Apple "
                        "ID password cannot authenticate against iCloud CalDAV while "
                        "two-factor authentication is on."
                    )
                raise CalendarToolError(
                    f"{self._server_name} rejected the credentials for {self.config.username}.{hint}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the agent
                raise CalendarToolError(
                    f"Could not connect to {self.config.url}: {exc}"
                ) from exc
        return self._principal

    def calendars(self, *, refresh: bool = False) -> list[Any]:
        if self._calendars is None or refresh:
            try:
                self._calendars = list(self.principal().calendars())
            except CalendarToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CalendarToolError(f"Could not list calendars: {exc}") from exc
        return self._calendars

    # -- calendar lookup ----------------------------------------------------

    @staticmethod
    def _display_name(cal: Any) -> str:
        try:
            name = cal.get_display_name() if hasattr(cal, "get_display_name") else cal.name
        except Exception:  # noqa: BLE001
            name = None
        return str(name) if name else str(cal.url).rstrip("/").rsplit("/", 1)[-1]

    def list_calendars(self) -> list[dict[str, str]]:
        return [
            {"name": self._display_name(c), "url": str(c.url)}
            for c in self.calendars()
        ]

    def find_calendar(self, calendar_name: str) -> Any:
        """Resolve a calendar by name, falling back to a unique partial match."""
        wanted = (calendar_name or "").strip()
        if not wanted:
            raise CalendarToolError("calendar_name is required.")

        cals = self.calendars()
        names = [self._display_name(c) for c in cals]

        exact = [c for c, n in zip(cals, names) if n.casefold() == wanted.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise CalendarToolError(
                f"More than one calendar is named {wanted!r}. Rename one in the "
                "Calendar app, or address it by URL."
            )

        partial = [c for c, n in zip(cals, names) if wanted.casefold() in n.casefold()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            matched = ", ".join(sorted(self._display_name(c) for c in partial))
            raise CalendarNotFoundError(
                f"{wanted!r} matches more than one calendar ({matched}). "
                "Use the exact name."
            )

        available = ", ".join(repr(n) for n in sorted(names)) or "(none)"
        raise CalendarNotFoundError(
            f"No calendar named {wanted!r}. Available calendars: {available}."
        )

    # -- event lookup -------------------------------------------------------

    def _load(self, calendar: Any, event_id: str) -> _Stored:
        """Fetch the stored object holding ``event_id``.

        iCloud answers a UID ``calendar-query`` with 412, so this first tries
        the ``<uid>.ics`` path that iCloud and caldav both use when creating
        events, then falls back to scanning the calendar.
        """
        uid = (event_id or "").strip()
        if not uid:
            raise CalendarToolError("event_id is required. Get it from list_events.")

        for url in _candidate_urls(calendar, uid):
            try:
                resource = calendar.event_by_url(url)
                stored = _unpack(resource, uid)
            except Exception:  # noqa: BLE001 - a miss here just means "try the next way"
                continue
            if stored is not None:
                return stored

        try:
            resources = calendar.events()
        except Exception as exc:  # noqa: BLE001
            raise CalendarToolError(f"Could not fetch events: {exc}") from exc
        for resource in resources:
            try:
                stored = _unpack(resource, uid)
            except Exception:  # noqa: BLE001 - skip objects we cannot parse
                continue
            if stored is not None:
                return stored

        raise EventNotFoundError(
            f"No event with event_id {uid!r} in calendar "
            f"{self._display_name(calendar)!r}. Use list_events to look it up."
        )

    def _save(self, stored: _Stored, calendar_name: str) -> None:
        _add_timezones(stored.vcal)
        stored.resource.data = stored.vcal.to_ical().decode("utf-8")
        try:
            # SEQUENCE is managed here; and caldav must not try to merge an
            # override into its master, which needs the UID search iCloud refuses.
            stored.resource.save(increase_seqno=False, only_this_recurrence=False)
        except caldav_error.AuthorizationError as exc:
            raise CalendarToolError(
                f"Not permitted to change events in calendar {calendar_name!r}. "
                "Subscribed and shared read-only calendars cannot be edited."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CalendarToolError(f"{self._server_name} rejected the change: {exc}") from exc

    # -- writing ------------------------------------------------------------

    def create_event(
        self,
        calendar_name: str,
        event_title: str,
        start_time: str,
        end_time: str,
        description: str | None = None,
        location: str | None = None,
        recurrence: str | None = None,
    ) -> dict[str, Any]:
        title = (event_title or "").strip()
        if not title:
            raise CalendarToolError("event_title is required and cannot be blank.")

        calendar = self.find_calendar(calendar_name)
        tz = self.config.default_timezone

        start, end, all_day = _parse_span(start_time, end_time, tz)

        rule = None
        zone = None
        warnings: list[str] = []
        if recurrence and recurrence.strip():
            zone = tz
            try:
                rule = parse_rule(recurrence, _in_zone(start, zone), tz)
            except RecurrenceError as exc:
                raise CalendarToolError(str(exc)) from exc
            note = _anchor_warning(start_time, start, zone)
            if note:
                warnings.append(note)

        uid = f"{uuid.uuid4()}@icloud-calendar-mcp"
        vevent = IEvent()
        vevent.add("uid", uid)
        vevent.add("dtstamp", datetime.now(tz=timezone.utc))
        vevent.add("summary", title)
        _set_times(vevent, start, end, zone)
        if rule is not None:
            vevent.add("rrule", rule)
        if description:
            vevent.add("description", description)
        if location:
            vevent.add("location", location)

        vcalendar = ICalendar()
        vcalendar.add("prodid", PRODID)
        vcalendar.add("version", "2.0")
        vcalendar.add_component(vevent)
        _add_timezones(vcalendar)
        ical = vcalendar.to_ical().decode("utf-8")

        try:
            saved = calendar.save_event(ical)
        except caldav_error.AuthorizationError as exc:
            raise CalendarToolError(
                f"Not permitted to write to calendar {calendar_name!r}. Subscribed "
                "and shared read-only calendars cannot accept new events."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CalendarToolError(
                f"{self._server_name} rejected the event: {exc}"
            ) from exc

        result: dict[str, Any] = {
            "status": "success",
            "event_id": uid,
            "calendar_name": self._display_name(calendar),
            "title": title,
            "start_time": normalise(start, tz),
            "end_time": normalise(end - timedelta(days=1) if all_day else end, tz),
            "all_day": all_day,
            "url": str(getattr(saved, "url", "")) or None,
        }
        if rule is not None:
            result["recurrence"] = rule_text(rule)
        return _with_warnings(result, warnings)

    def update_event(
        self,
        calendar_name: str,
        event_id: str,
        event_title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        recurrence: str | None = None,
        occurrence: str | None = None,
    ) -> dict[str, Any]:
        """Change an event, a whole recurring series, or one occurrence of it.

        ``None`` leaves a field alone; an empty string clears ``description``,
        ``location`` or ``recurrence``.
        """
        changes = (event_title, start_time, end_time, description, location, recurrence)
        if all(v is None for v in changes):
            raise CalendarToolError(
                "Nothing to change: pass at least one of event_title, start_time, "
                "end_time, description, location or recurrence."
            )

        calendar = self.find_calendar(calendar_name)
        name = self._display_name(calendar)
        tz = self.config.default_timezone
        stored = self._load(calendar, event_id)

        if occurrence is not None and occurrence.strip():
            if recurrence is not None:
                raise CalendarToolError(
                    "recurrence applies to the whole series; omit occurrence to change it."
                )
            return self._update_occurrence(
                stored, name, occurrence, event_title, start_time, end_time,
                description, location,
            )

        master = _require_master(stored)
        cur_start, cur_end = _span_of(master)
        span = _resolve_span(start_time, end_time, cur_start, cur_end, tz)
        new_start, new_end = span or (_aware(cur_start, tz), _aware(cur_end, tz))

        old_rule = master.get("rrule")
        rdates = dates_of(master.get("rdate"))
        removing = recurrence is not None and not recurrence.strip()
        recurring = (not removing) and (
            bool(recurrence and recurrence.strip()) or old_rule is not None or bool(rdates)
        )
        zone = _write_zone(cur_start, recurring, tz)
        start_w = _in_zone(new_start, zone)

        rule = None if removing else old_rule
        try:
            if recurrence and recurrence.strip():
                rule = parse_rule(recurrence, start_w, tz)
            elif rule is not None and span is not None:
                check_start(rule, start_w, tz)
        except RecurrenceError as exc:
            if recurrence is None:
                raise CalendarToolError(
                    f"{exc} Pass `recurrence` as well to change the rule to match."
                ) from exc
            raise CalendarToolError(str(exc)) from exc

        warnings: list[str] = []
        if recurring and span is not None:
            note = _anchor_warning(start_time or end_time, new_start, zone)
            if note:
                warnings.append(note)

        if span is not None or recurrence is not None:
            _set_times(master, new_start, new_end, zone)
        master.pop("RRULE", None)
        if rule is not None:
            master.add("rrule", rule)

        removed = 0
        own_time: list[str] = []
        if removing:
            removed = len(stored.overrides) + len(dates_of(master.get("exdate")))
            for key in ("EXDATE", "RDATE"):
                master.pop(key, None)
            _drop(stored, stored.overrides)
            if removed:
                warnings.append(
                    f"The event no longer repeats, so its {removed} exception(s) "
                    "(individually changed or deleted occurrences) were discarded."
                )
        elif recurring and (span is not None or recurrence is not None):
            shift_from = cur_start if recurrence is None else None
            removed, own_time = _realign_exceptions(
                stored, master, rule, shift_from, cur_end - cur_start,
                start_w, new_end - new_start, tz,
            )
            if removed:
                warnings.append(
                    f"{removed} exception(s) to the old schedule (individually "
                    "changed or deleted occurrences) do not fall on the new "
                    "schedule and were discarded."
                )
            if own_time:
                warnings.append(
                    "These occurrences had been individually rescheduled and kept "
                    f"their own times: {', '.join(own_time)}. Update them with "
                    "`occurrence` if they should move too."
                )

        old_text = _text_of(master)
        dropped_pin = _apply_text(master, event_title, description, location)
        pin_in_overrides, kept_text = _propagate_text(
            stored.overrides, old_text,
            {"event_title": event_title, "description": description, "location": location},
            tz,
        )
        for arg, rids in kept_text.items():
            label = "title" if arg == "event_title" else arg
            warnings.append(
                f"These occurrences have their own {label} and were left unchanged: "
                f"{', '.join(rids)}. Update them with `occurrence` if they should match."
            )
        if dropped_pin or pin_in_overrides:
            warnings.append(PIN_WARNING)
        if _has_attendees(stored):
            warnings.append(ATTENDEE_WARNING)
        _touch(master)
        self._save(stored, name)

        result = self._describe(stored, master, name)
        result["scope"] = "series" if rule is not None or rdates else "event"
        if removed:
            result["removed_exceptions"] = removed
        return _with_warnings(result, warnings)

    def _update_occurrence(
        self,
        stored: _Stored,
        calendar_name: str,
        occurrence: str,
        event_title: str | None,
        start_time: str | None,
        end_time: str | None,
        description: str | None,
        location: str | None,
    ) -> dict[str, Any]:
        tz = self.config.default_timezone
        master = _require_master(stored)
        when = _resolve_occurrence(master, occurrence, tz)
        key = _key(when, tz)

        comp = next(
            (o for o in stored.overrides if _key(o["RECURRENCE-ID"].dt, tz) == key),
            None,
        )
        if comp is None:
            comp = copy.deepcopy(master)
            for prop in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                comp.pop(prop, None)
            m_start, m_end = _span_of(master)
            for prop in ("DTSTART", "DTEND", "DURATION"):
                comp.pop(prop, None)
            comp.add("recurrence-id", when)
            comp.add("dtstart", when)
            comp.add("dtend", when + (m_end - m_start))
            stored.vcal.add_component(comp)
            stored.overrides.append(comp)

        cur_start, cur_end = _span_of(comp)
        span = _resolve_span(start_time, end_time, cur_start, cur_end, tz)
        if span is not None:
            _set_times(comp, span[0], span[1], _write_zone(cur_start, True, tz))
        warnings: list[str] = []
        if _apply_text(comp, event_title, description, location):
            warnings.append(PIN_WARNING)
        if _has_attendees(stored):
            warnings.append(ATTENDEE_WARNING)
        _touch(comp)
        self._save(stored, calendar_name)

        result = self._describe(stored, comp, calendar_name)
        result["scope"] = "occurrence"
        return _with_warnings(result, warnings)

    def delete_event(
        self,
        calendar_name: str,
        event_id: str,
        occurrence: str | None = None,
        and_following: bool = False,
    ) -> dict[str, Any]:
        """Delete an event or series, one occurrence, or an occurrence onwards."""
        calendar = self.find_calendar(calendar_name)
        name = self._display_name(calendar)
        tz = self.config.default_timezone
        stored = self._load(calendar, event_id)
        title = _title_of(stored)
        warnings = [ATTENDEE_WARNING] if _has_attendees(stored) else []

        if not (occurrence and occurrence.strip()):
            if and_following:
                raise CalendarToolError("and_following needs an occurrence to start from.")
            self._delete_resource(stored, name)
            return _with_warnings({
                "status": "success",
                "deleted": "event",
                "event_id": stored.uid,
                "title": title,
                "calendar_name": name,
            }, warnings)

        master = _require_master(stored)
        when = _resolve_occurrence(master, occurrence, tz)
        key = _key(when, tz)

        if and_following:
            if key == _key(_span_of(master)[0], tz):
                self._delete_resource(stored, name)
                return _with_warnings({
                    "status": "success",
                    "deleted": "series",
                    "event_id": stored.uid,
                    "title": title,
                    "calendar_name": name,
                }, warnings)
            rule = master.get("rrule")
            if rule is not None:
                master.pop("RRULE")
                master.add("rrule", truncated_before(rule, when))
            _replace_dates(master, "EXDATE", lambda v: _key(v, tz) < key)
            _replace_dates(master, "RDATE", lambda v: _key(v, tz) < key)
            _drop(
                stored,
                [o for o in stored.overrides if _key(o["RECURRENCE-ID"].dt, tz) >= key],
            )
            deleted = "occurrence_and_following"
        else:
            master.add("exdate", when)
            _drop(
                stored,
                [o for o in stored.overrides if _key(o["RECURRENCE-ID"].dt, tz) == key],
            )
            deleted = "occurrence"

        _touch(master)
        self._save(stored, name)
        result = {
            "status": "success",
            "deleted": deleted,
            "event_id": stored.uid,
            "title": title,
            "calendar_name": name,
            "occurrence": normalise(when, tz),
        }
        if master.get("rrule") is not None:
            result["recurrence"] = rule_text(master["RRULE"])
        return _with_warnings(result, warnings)

    def _delete_resource(self, stored: _Stored, calendar_name: str) -> None:
        try:
            stored.resource.delete()
        except caldav_error.AuthorizationError as exc:
            raise CalendarToolError(
                f"Not permitted to delete events in calendar {calendar_name!r}."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CalendarToolError(f"{self._server_name} rejected the delete: {exc}") from exc

    # -- reading ------------------------------------------------------------

    def get_event(self, calendar_name: str, event_id: str) -> dict[str, Any]:
        """One event with its recurrence rule and exceptions spelled out."""
        calendar = self.find_calendar(calendar_name)
        stored = self._load(calendar, event_id)
        base = stored.master if stored.master is not None else stored.overrides[0]
        result = self._describe(stored, base, self._display_name(calendar))
        result["url"] = str(getattr(stored.resource, "url", "")) or None
        warnings: list[str] = []
        if stored.master is None:
            warnings.append(
                "Only individual occurrences of this event are in this calendar "
                "(usually an invitation to a single occurrence), so it cannot be "
                "edited or partly deleted as a series."
            )
        elif stored.master.get("EXRULE") is not None:
            warnings.append(
                "This series uses EXRULE, which this tool does not evaluate, so "
                "it may accept an `occurrence` that the calendar app hides."
            )
        if _has_attendees(stored):
            warnings.append(
                "This event has attendees. Changes made with this tool are not "
                "sent as invitations or updates."
            )
        return _with_warnings(result, warnings)

    def _describe(self, stored: _Stored, comp: Any, calendar_name: str) -> dict[str, Any]:
        tz = self.config.default_timezone
        result: dict[str, Any] = {
            "status": "success",
            **_record_of(comp, tz, calendar_name).to_dict(),
        }
        master = stored.master
        if master is None:
            return result
        rule = master.get("rrule")
        if rule is not None:
            result["recurrence"] = rule_text(rule)
        if comp is master and (rule is not None or master.get("rdate") is not None):
            excluded = sorted(dates_of(master.get("exdate")), key=lambda v: _key(v, tz))
            result["excluded_occurrences"] = [normalise(v, tz) for v in excluded]
            overrides = sorted(
                stored.overrides, key=lambda o: _key(o["RECURRENCE-ID"].dt, tz)
            )
            result["modified_occurrences"] = [
                _record_of(o, tz, calendar_name).to_dict() for o in overrides
            ]
        return result

    def list_events(
        self,
        calendar_name: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        calendar = self.find_calendar(calendar_name)
        tz = self.config.default_timezone

        window_start, window_end = default_window(tz)
        try:
            if start_date:
                window_start = parse_datetime(start_date, tz, field="start_date")
            if end_date:
                window_end = parse_datetime(end_date, tz, field="end_date")
        except TimeParseError as exc:
            raise CalendarToolError(str(exc)) from exc

        if window_end <= window_start:
            raise CalendarToolError(
                f"end_date ({window_end.isoformat()}) must be after start_date "
                f"({window_start.isoformat()})."
            )
        span_days = (window_end - window_start).days
        if span_days > MAX_WINDOW_DAYS:
            raise CalendarToolError(
                f"Requested range spans {span_days} days. Keep it to "
                f"{MAX_WINDOW_DAYS} days or fewer so the server is not asked to "
                "expand an unbounded number of recurrences."
            )

        found, expanded = self._search(calendar, window_start, window_end)
        name = self._display_name(calendar)
        records = [
            r
            for r in (self._to_record(obj, tz, name) for obj in found)
            if r is not None
        ]
        records.sort(key=lambda r: (r.start_time or "", r.title))

        result = {
            "status": "success",
            "calendar_name": name,
            "start_date": window_start.isoformat(),
            "end_date": window_end.isoformat(),
            "count": len(records),
            "events": [r.to_dict() for r in records],
        }
        warnings = []
        if not expanded:
            warnings.append(
                "The server refused to expand repeating events, so each series "
                "appears once with its first occurrence's time (which may be "
                "outside the requested range) instead of once per occurrence, "
                "and without recurrence_id. Use get_event to see a series' rule."
            )
        return _with_warnings(result, warnings)

    @staticmethod
    def _search(calendar: Any, start: datetime, end: datetime) -> tuple[Iterable[Any], bool]:
        """Time-range search, expanding recurrences where the server allows it.

        Returns the results and whether they were expanded.
        """
        try:
            return calendar.search(start=start, end=end, event=True, expand=True), True
        except Exception:  # noqa: BLE001 - expansion is best-effort
            try:
                return calendar.search(start=start, end=end, event=True), False
            except Exception as exc:  # noqa: BLE001
                raise CalendarToolError(f"Could not fetch events: {exc}") from exc

    @staticmethod
    def _to_record(obj: Any, tz: Any, calendar_name: str) -> EventRecord | None:
        try:
            comp = obj.icalendar_component
        except Exception:  # noqa: BLE001
            return None
        if comp is None:
            return None
        return _record_of(comp, tz, calendar_name)


# -- VEVENT helpers ---------------------------------------------------------


def _record_of(comp: Any, tz: tzinfo, calendar_name: str) -> EventRecord:
    start_val, end_val = _span_of(comp)
    all_day = start_val is not None and not isinstance(start_val, datetime)
    if all_day and isinstance(end_val, date):
        # Undo iCalendar's exclusive DTEND so the caller sees the last day
        # the event actually covers.
        end_val = end_val - timedelta(days=1)

    recurrence = comp.get("recurrence-id")

    return EventRecord(
        title=str(comp.get("summary") or "(no title)"),
        start_time=normalise(start_val, tz),
        end_time=normalise(end_val, tz),
        event_id=str(comp.get("uid") or ""),
        location=_opt_str(comp.get("location")),
        description=_opt_str(comp.get("description")),
        all_day=all_day,
        recurrence_id=normalise(recurrence.dt, tz) if recurrence else None,
        calendar_name=calendar_name,
    )


def _span_of(comp: Any) -> tuple[Any, Any]:
    """DTSTART and the exclusive end, deriving the end when DTEND is absent."""
    dtstart = comp.get("dtstart")
    dtend = comp.get("dtend")
    start_val = dtstart.dt if dtstart is not None else None
    end_val = dtend.dt if dtend is not None else None

    if end_val is None and start_val is not None:
        duration = comp.get("duration")
        if duration is not None:
            end_val = start_val + duration.dt
        elif isinstance(start_val, datetime):
            end_val = start_val
        else:
            end_val = start_val + timedelta(days=1)
    return start_val, end_val


def _unpack(resource: Any, uid: str) -> _Stored | None:
    vcal = ICalendar.from_ical(resource.data)
    events = [c for c in vcal.walk("VEVENT") if str(c.get("uid")) == uid]
    if not events:
        return None
    master = next((c for c in events if c.get("recurrence-id") is None), None)
    overrides = [c for c in events if c.get("recurrence-id") is not None]
    return _Stored(resource, vcal, uid, master, overrides)


def _candidate_urls(calendar: Any, uid: str) -> list[str]:
    base = str(calendar.url)
    if not base.endswith("/"):
        base += "/"
    escaped = uid.replace("/", "%2F")
    urls: list[str] = []
    for safe in ("/", "/@"):
        url = base + quote(escaped, safe=safe) + ".ics"
        if url not in urls:
            urls.append(url)
    return urls


ATTENDEE_WARNING = (
    "This event has attendees. This tool does not send or manage invitations: "
    "iCloud decides whether attendees hear about this change, and if you are "
    "not the organizer, the organizer's next update can overwrite it."
)
PIN_WARNING = (
    "The location changed, so Apple's map pin for the old location was removed; "
    "Apple Calendar will show the new location as plain text until a place is "
    "picked in the app."
)


def _has_attendees(stored: _Stored) -> bool:
    comps = [c for c in (stored.master, *stored.overrides) if c is not None]
    return any(c.get("ATTENDEE") is not None for c in comps)


def _anchor_warning(
    raw: str | None, parsed: date | datetime, zone: tzinfo | None
) -> str | None:
    """Say so when a fixed offset was re-anchored to a named zone for a series."""
    if zone is None or raw is None or not isinstance(parsed, datetime):
        return None
    local = parsed.astimezone(zone)
    if parsed.utcoffset() == local.utcoffset():
        return None
    name = getattr(zone, "key", None) or str(zone)
    return (
        f"{raw!r} has a fixed UTC offset, but a repeating event must follow a "
        f"named timezone, so it is anchored to {name}: occurrences are at "
        f"{local:%H:%M} {name} time and stay there across daylight-saving "
        "changes, which can differ from the offset you gave by an hour."
    )


def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        result["warnings"] = warnings
    return result


def _require_master(stored: _Stored) -> Any:
    if stored.master is None:
        raise CalendarToolError(
            f"Event {stored.uid!r} has no master copy in this calendar, only "
            "individual occurrences (typical of an invitation to one instance). "
            "It cannot be edited as a series."
        )
    return stored.master


def _title_of(stored: _Stored) -> str:
    comp = stored.master if stored.master is not None else stored.overrides[0]
    return str(comp.get("summary") or "(no title)")


def _parse_span(
    start_time: str, end_time: str, tz: tzinfo
) -> tuple[date | datetime, date | datetime, bool]:
    """Parse caller times into (start, exclusive end, all_day)."""
    try:
        start = parse_datetime(start_time, tz, field="start_time")
        end = parse_datetime(end_time, tz, field="end_time")
    except TimeParseError as exc:
        raise CalendarToolError(str(exc)) from exc

    # Date-only on both ends means the caller wants an all-day event.
    if _is_date_only(start_time) and _is_date_only(end_time):
        start_value = start.date()
        # DTEND is exclusive in iCalendar; callers phrase end dates
        # inclusively ("24th to the 26th" covers three days).
        end_value = end.date() + timedelta(days=1)
        if end_value <= start_value:
            raise CalendarToolError(
                f"end_time ({end.date().isoformat()}) is before start_time "
                f"({start_value.isoformat()})."
            )
        return start_value, end_value, True

    if _is_date_only(start_time) or _is_date_only(end_time):
        raise CalendarToolError(
            "start_time and end_time must both be dates (all-day) or both be "
            "date-times, not one of each."
        )
    if end <= start:
        raise CalendarToolError(
            f"end_time ({end.isoformat()}) must be after start_time "
            f"({start.isoformat()})."
        )
    return start, end, False


def _resolve_span(
    start_time: str | None,
    end_time: str | None,
    cur_start: date | datetime,
    cur_end: date | datetime,
    tz: tzinfo,
) -> tuple[date | datetime, date | datetime] | None:
    """New (start, exclusive end) from optional replacements, or None if unchanged.

    Moving only the start keeps the event's duration.
    """
    if start_time is None and end_time is None:
        return None
    if start_time is not None and end_time is not None:
        start, end, _ = _parse_span(start_time, end_time, tz)
        return start, end

    all_day = not isinstance(cur_start, datetime)
    given = start_time if start_time is not None else end_time
    fname = "start_time" if start_time is not None else "end_time"
    if _is_date_only(given or "") != all_day:
        raise CalendarToolError(
            f"This is {'an all-day' if all_day else 'a timed'} event; pass both "
            "start_time and end_time to switch between all-day and timed."
        )
    try:
        parsed = parse_datetime(given or "", tz, field=fname)
    except TimeParseError as exc:
        raise CalendarToolError(str(exc)) from exc

    if start_time is not None:
        start: date | datetime = parsed.date() if all_day else parsed
        return start, start + (cur_end - cur_start)

    start = _aware(cur_start, tz)
    end: date | datetime = parsed.date() + timedelta(days=1) if all_day else parsed
    if end <= start:
        raise CalendarToolError(
            f"end_time ({given}) must be after the event's start "
            f"({normalise(start, tz)})."
        )
    return start, end


def _resolve_occurrence(master: Any, occurrence: str, tz: tzinfo) -> date | datetime:
    """Match a caller's occurrence to the series, in the master's DTSTART form."""
    rule = master.get("rrule")
    rdates = dates_of(master.get("rdate"))
    if rule is None and not rdates:
        raise CalendarToolError(
            "This event does not repeat; omit occurrence to change the event itself."
        )
    start, _ = _span_of(master)
    series = Series(start, rule, rdates, tz)
    raw = occurrence.strip()

    if _is_date_only(raw) and isinstance(start, datetime):
        day = date.fromisoformat(raw)
        lo = _as_form_of(datetime.combine(day, time.min, tzinfo=tz), start, tz)
        hits = series.between(lo, _as_form_of(
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz), start, tz
        ))
        if len(hits) != 1:
            raise CalendarToolError(
                f"{'No' if not hits else 'More than one'} occurrence of this series "
                f"falls on {raw}. Pass the recurrence_id reported by list_events."
            )
        when = hits[0]
    else:
        try:
            parsed = parse_datetime(raw, tz, field="occurrence")
        except TimeParseError as exc:
            raise CalendarToolError(str(exc)) from exc
        when = _as_form_of(parsed, start, tz)
        if not series.contains(when):
            raise CalendarToolError(
                f"{occurrence!r} is not an occurrence of this series. Pass the "
                "recurrence_id reported by list_events."
            )

    key = _key(when, tz)
    if any(_key(v, tz) == key for v in dates_of(master.get("exdate"))):
        raise CalendarToolError(f"The occurrence at {occurrence!r} has already been deleted.")
    return when


def _realign_exceptions(
    stored: _Stored,
    master: Any,
    rule: Any,
    shift_from: date | datetime | None,
    old_duration: timedelta,
    new_start: date | datetime,
    new_duration: timedelta,
    tz: tzinfo,
) -> tuple[int, list[str]]:
    """Keep EXDATEs and overrides attached after the series itself changed.

    When only the start moved, exceptions move with it by the same wall-clock
    distance. An override still sitting in its original slot (only its title
    or notes were changed) takes the new time too; one that was rescheduled
    keeps its own. Anything that no longer lands on an occurrence is dropped,
    since a RECURRENCE-ID outside the set is meaningless.

    Returns how many exceptions were dropped, and the recurrence IDs of
    overrides that kept their own time.
    """
    series = Series(new_start, rule, dates_of(master.get("rdate")), tz)

    def moved(value: date | datetime) -> date | datetime:
        value = _as_form_of(value, new_start, tz)
        if shift_from is None:
            return value
        old = _as_form_of(shift_from, new_start, tz)
        if isinstance(new_start, datetime):
            delta = new_start.replace(tzinfo=None) - old.replace(tzinfo=None)
            return (value.replace(tzinfo=None) + delta).replace(tzinfo=new_start.tzinfo)
        return value + (new_start - old)

    removed = 0
    kept_exdates = []
    for value in dates_of(master.get("exdate")):
        candidate = moved(value)
        if series.contains(candidate):
            kept_exdates.append(candidate)
        else:
            removed += 1
    master.pop("EXDATE", None)
    for value in kept_exdates:
        master.add("exdate", value)

    stale = []
    own_time = []
    for override in stored.overrides:
        rid = override["RECURRENCE-ID"].dt
        candidate = moved(rid)
        if not series.contains(candidate):
            stale.append(override)
            continue
        o_start, o_end = _span_of(override)
        in_slot = _key(o_start, tz) == _key(rid, tz) and o_end - o_start == old_duration
        override.pop("RECURRENCE-ID")
        override.add("recurrence-id", candidate)
        if in_slot:
            for prop in ("DTSTART", "DTEND", "DURATION"):
                override.pop(prop, None)
            override.add("dtstart", candidate)
            override.add("dtend", candidate + new_duration)
        elif candidate != _as_form_of(rid, new_start, tz) or new_duration != old_duration:
            own_time.append(normalise(candidate, tz) or "")
    _drop(stored, stale)
    return removed + len(stale), own_time


def _replace_dates(comp: Any, prop: str, keep: Any) -> None:
    values = [v for v in dates_of(comp.get(prop)) if keep(v)]
    comp.pop(prop, None)
    for value in values:
        comp.add(prop.lower(), value)


def _drop(stored: _Stored, components: list[Any]) -> None:
    for comp in list(components):
        stored.vcal.subcomponents.remove(comp)
        stored.overrides.remove(comp)


def _set_times(comp: Any, start: date | datetime, end: date | datetime, zone: tzinfo | None) -> None:
    for prop in ("DTSTART", "DTEND", "DURATION"):
        comp.pop(prop, None)
    comp.add("dtstart", _in_zone(start, zone))
    comp.add("dtend", _in_zone(end, zone))


def _apply_text(
    comp: Any, title: str | None, description: str | None, location: str | None
) -> bool:
    """Apply text changes; True if Apple's structured location was discarded."""
    dropped_pin = False
    if title is not None:
        text = title.strip()
        if not text:
            raise CalendarToolError("event_title cannot be blank.")
        comp.pop("SUMMARY", None)
        comp.add("summary", text)
    if description is not None:
        comp.pop("DESCRIPTION", None)
        if description.strip():
            comp.add("description", description)
    if location is not None:
        comp.pop("LOCATION", None)
        # Apple's map pin would otherwise keep pointing at the old place.
        dropped_pin = comp.pop("X-APPLE-STRUCTURED-LOCATION", None) is not None
        if location.strip():
            comp.add("location", location)
    return dropped_pin


_TEXT_FIELDS = (("event_title", "SUMMARY"), ("description", "DESCRIPTION"), ("location", "LOCATION"))


def _text_of(comp: Any) -> dict[str, str | None]:
    return {prop: _opt_str(comp.get(prop)) for _, prop in _TEXT_FIELDS}


def _propagate_text(
    overrides: list[Any],
    old_master: dict[str, str | None],
    changes: dict[str, str | None],
    tz: tzinfo,
) -> tuple[bool, dict[str, list[str]]]:
    """Carry series-wide text edits into overrides that had not diverged.

    An override that still had the series' old value for a field follows the
    edit; one with its own value keeps it. Returns whether any map pin was
    dropped, and per field the occurrences that kept their own value.
    """
    dropped_pin = False
    kept: dict[str, list[str]] = {}
    for arg, prop in _TEXT_FIELDS:
        new = changes.get(arg)
        if new is None:
            continue
        for override in overrides:
            if _opt_str(override.get(prop)) == old_master[prop]:
                dropped_pin |= _apply_text(override, **{
                    "title": new if arg == "event_title" else None,
                    "description": new if arg == "description" else None,
                    "location": new if arg == "location" else None,
                })
            else:
                rid = normalise(override["RECURRENCE-ID"].dt, tz) or ""
                kept.setdefault(arg, []).append(rid)
    return dropped_pin, kept


def _touch(comp: Any) -> None:
    now = datetime.now(tz=timezone.utc)
    sequence = int(comp.get("sequence", 0) or 0) + 1
    for prop in ("SEQUENCE", "DTSTAMP", "LAST-MODIFIED"):
        comp.pop(prop, None)
    comp.add("sequence", sequence)
    comp.add("dtstamp", now)
    comp.add("last-modified", now)


def _add_timezones(vcal: ICalendar) -> None:
    starts = [
        c["DTSTART"].dt for c in vcal.walk("VEVENT") if c.get("DTSTART") is not None
    ]
    earliest = min((_as_date(v) for v in starts), default=date.today())
    # Transitions from a year before the first event are plenty; the default
    # reaches back to 1970 and bloats every object with decades of DST rules.
    vcal.add_missing_timezones(first_date=earliest - timedelta(days=366))


# -- time helpers -----------------------------------------------------------


def _write_zone(current: date | datetime, recurring: bool, tz: tzinfo) -> tzinfo | None:
    """Timezone to write timed values in; ``None`` means UTC.

    An event already pinned to a named zone keeps it. Otherwise one-off events
    stay in UTC and recurring ones take the default zone, so they follow DST.
    """
    if isinstance(current, datetime) and current.tzinfo is not None and not _is_utc(current):
        return current.tzinfo
    return tz if recurring else None


def _in_zone(value: date | datetime, zone: tzinfo | None) -> date | datetime:
    if not isinstance(value, datetime):
        return value
    return value.astimezone(zone) if zone is not None else to_utc(value)


def _as_form_of(when: date | datetime, reference: date | datetime, tz: tzinfo) -> date | datetime:
    """Express ``when`` the way ``reference`` is stored (date, floating, or zoned)."""
    if not isinstance(reference, datetime):
        if isinstance(when, datetime):
            return _aware(when, tz).astimezone(tz).date()
        return when
    if not isinstance(when, datetime):
        when = datetime.combine(when, time.min)
    when = _aware(when, tz)
    if reference.tzinfo is None:
        return when.astimezone(tz).replace(tzinfo=None)
    return when.astimezone(reference.tzinfo)


def _key(value: date | datetime, tz: tzinfo) -> date | datetime:
    """A comparable identity for an occurrence: the date, or the UTC instant."""
    if not isinstance(value, datetime):
        return value
    return _aware(value, tz).astimezone(timezone.utc)


def _aware(value: date | datetime, tz: tzinfo) -> date | datetime:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def _is_utc(value: datetime) -> bool:
    name = getattr(value.tzinfo, "key", None) or str(value.tzinfo)
    return value.tzinfo is timezone.utc or name in ("UTC", "Etc/UTC", "Z")


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_date_only(value: str) -> bool:
    raw = (value or "").strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        return False
    return "T" not in raw and " " not in raw


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
