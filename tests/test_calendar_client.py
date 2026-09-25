from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from icloud_calendar_mcp.calendar_client import (
    CalendarNotFoundError,
    CalendarToolError,
)


# -- calendar lookup --------------------------------------------------------


def test_list_calendars_returns_names_and_urls(client):
    assert client.list_calendars() == [
        {"name": "Home", "url": "https://caldav.icloud.com/fake/home/"},
        {"name": "Work", "url": "https://caldav.icloud.com/fake/work/"},
        {"name": "Work Travel", "url": "https://caldav.icloud.com/fake/wt/"},
    ]


def test_find_calendar_is_case_insensitive(client):
    assert client.find_calendar("home").name == "Home"


def test_exact_match_wins_over_partial(client):
    # "Work" is a substring of "Work Travel"; the exact name must still resolve.
    assert client.find_calendar("Work").name == "Work"


def test_ambiguous_partial_match_is_rejected(client):
    with pytest.raises(CalendarNotFoundError, match="more than one calendar"):
        client.find_calendar("wor")


def test_unknown_calendar_lists_the_available_ones(client):
    with pytest.raises(CalendarNotFoundError, match="'Home', 'Work', 'Work Travel'"):
        client.find_calendar("Holidays")


# -- event creation ---------------------------------------------------------


def _saved_ical(calendars, name="Home") -> str:
    cal = next(c for c in calendars if c.name == name)
    assert cal.saved, "no event was saved"
    return cal.saved[-1]


def test_create_event_writes_utc_and_returns_uid(client, calendars):
    result = client.create_event(
        calendar_name="Home",
        event_title="Dentist",
        start_time="2026-07-01T14:00:00",  # naive -> Europe/London (BST, +01:00)
        end_time="2026-07-01T15:00:00",
        description="Check-up",
        location="12 High Street",
    )

    assert result["status"] == "success"
    assert result["event_id"].endswith("@icloud-calendar-mcp")

    ical = _saved_ical(calendars)
    # Naive input in BST must land on the server as 13:00Z, not 14:00Z.
    assert "DTSTART:20260701T130000Z" in ical
    assert "DTEND:20260701T140000Z" in ical
    # No TZID parameters, so no VTIMEZONE component is required.
    assert "TZID" not in ical
    assert "SUMMARY:Dentist" in ical
    assert "LOCATION:12 High Street" in ical


def test_create_event_honours_an_explicit_offset(client, calendars):
    client.create_event(
        calendar_name="Home",
        event_title="Standup",
        start_time="2026-01-15T09:30:00+05:30",
        end_time="2026-01-15T10:00:00+05:30",
    )
    assert "DTSTART:20260115T040000Z" in _saved_ical(calendars)


def test_date_only_input_creates_an_all_day_event(client, calendars):
    result = client.create_event(
        calendar_name="Home",
        event_title="Leave",
        start_time="2026-08-03",
        end_time="2026-08-07",
    )

    ical = _saved_ical(calendars)
    assert "DTSTART;VALUE=DATE:20260803" in ical
    # DTEND is exclusive on the wire...
    assert "DTEND;VALUE=DATE:20260808" in ical
    # ...but the caller sees the inclusive last day they asked for.
    assert result["all_day"] is True
    assert result["start_time"] == "2026-08-03"
    assert result["end_time"] == "2026-08-07"


def test_end_before_start_is_rejected(client):
    with pytest.raises(CalendarToolError, match="must be after start_time"):
        client.create_event("Home", "Backwards", "2026-07-01T15:00", "2026-07-01T14:00")


def test_blank_title_is_rejected(client):
    with pytest.raises(CalendarToolError, match="event_title is required"):
        client.create_event("Home", "   ", "2026-07-01T14:00", "2026-07-01T15:00")


def test_unparseable_time_is_rejected(client):
    with pytest.raises(CalendarToolError, match="not valid ISO 8601"):
        client.create_event("Home", "Lunch", "next tuesday", "2026-07-01T15:00")


def test_read_only_calendar_failure_is_reported(client, calendars):
    next(c for c in calendars if c.name == "Home").read_only = True
    with pytest.raises(CalendarToolError, match="iCloud rejected the event"):
        client.create_event("Home", "Nope", "2026-07-01T14:00", "2026-07-01T15:00")


# -- event retrieval --------------------------------------------------------


def test_list_events_normalises_to_the_default_timezone(client, calendars):
    client.create_event(
        calendar_name="Home",
        event_title="Dentist",
        start_time="2026-07-01T14:00:00",
        end_time="2026-07-01T15:00:00",
        location="12 High Street",
    )

    result = client.list_events("Home", "2026-07-01", "2026-07-02")
    assert result["count"] == 1

    event = result["events"][0]
    assert event["title"] == "Dentist"
    # Stored as 13:00Z, handed back in Europe/London as 14:00+01:00.
    assert event["start_time"] == "2026-07-01T14:00:00+01:00"
    assert event["end_time"] == "2026-07-01T15:00:00+01:00"
    assert event["location"] == "12 High Street"
    assert event["event_id"].endswith("@icloud-calendar-mcp")


def test_all_day_events_round_trip_inclusively(client, calendars):
    client.create_event("Home", "Leave", "2026-08-03", "2026-08-07")
    event = client.list_events("Home", "2026-08-01", "2026-08-10")["events"][0]

    assert event["all_day"] is True
    assert event["start_time"] == "2026-08-03"
    assert event["end_time"] == "2026-08-07"


def test_events_are_sorted_by_start(client, calendars):
    client.create_event("Home", "Later", "2026-07-02T09:00", "2026-07-02T10:00")
    client.create_event("Home", "Earlier", "2026-07-01T09:00", "2026-07-01T10:00")

    titles = [e["title"] for e in client.list_events("Home")["events"]]
    assert titles == ["Earlier", "Later"]


def test_default_window_is_the_next_thirty_days(client, calendars):
    client.list_events("Home")

    call = next(c for c in calendars if c.name == "Home").search_calls[-1]
    assert call["event"] is True
    assert call["expand"] is True

    # 30 days of wall-clock time in the default zone, so a window that
    # crosses a DST change is 30 days +/- an hour of elapsed time.
    now = datetime.now(tz=ZoneInfo("Europe/London"))
    assert abs(call["start"] - now) < timedelta(minutes=1)
    assert abs(call["end"] - (now + timedelta(days=30))) < timedelta(minutes=1)


def test_oversized_range_is_rejected(client):
    with pytest.raises(CalendarToolError, match="Keep it to 400 days"):
        client.list_events("Home", "2026-01-01", "2028-01-01")


def test_backwards_range_is_rejected(client):
    with pytest.raises(CalendarToolError, match="must be after start_date"):
        client.list_events("Home", "2026-02-01", "2026-01-01")


def test_search_falls_back_when_the_server_rejects_expand(client, calendars):
    home = next(c for c in calendars if c.name == "Home")
    client.create_event("Home", "Dentist", "2026-07-01T14:00", "2026-07-01T15:00")

    original = home.search

    def picky_search(**kwargs):
        if kwargs.get("expand"):
            raise RuntimeError("expand unsupported")
        return original(**kwargs)

    home.search = picky_search
    result = client.list_events("Home", "2026-07-01", "2026-07-02")

    assert result["count"] == 1
    assert home.search_calls[-1].get("expand") is None
