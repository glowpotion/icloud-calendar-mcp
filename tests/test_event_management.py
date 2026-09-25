from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar as ICalendar

from icloud_calendar_mcp.calendar_client import CalendarToolError, EventNotFoundError
from icloud_calendar_mcp.recurrence import Series, parse_rule

LONDON = ZoneInfo("Europe/London")

# An event as Apple Calendar stores it: UPPERCASE UID filename, TZID times,
# and Apple-specific properties that an edit must not throw away.
APPLE_EVENT = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//iPhone OS 18.0//EN
BEGIN:VEVENT
UID:2578833D-7E71-4728-AAA9-C78530113AEF
DTSTAMP:20260101T000000Z
DTSTART;TZID=Europe/London:20260701T081500
DTEND;TZID=Europe/London:20260701T090000
SUMMARY:Dentist
LOCATION:Old Street
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Old Street:geo:51.5,-0.1
X-APPLE-CREATOR-IDENTITY:com.apple.mobilecal
SEQUENCE:3
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
END:VCALENDAR
"""


def _home(calendars):
    return next(c for c in calendars if c.name == "Home")


def _stored_vcal(calendars, name="Home") -> ICalendar:
    cal = next(c for c in calendars if c.name == name)
    assert len(cal.stored) == 1
    return ICalendar.from_ical(cal.stored[0].data)


def _vevents(vcal: ICalendar):
    master = next(c for c in vcal.walk("VEVENT") if c.get("RECURRENCE-ID") is None)
    overrides = [c for c in vcal.walk("VEVENT") if c.get("RECURRENCE-ID") is not None]
    return master, overrides


def _weekly_standup(client) -> str:
    # Mondays 09:00 London, from before the October DST change to after it.
    return client.create_event(
        "Home", "Standup", "2026-10-05T09:00", "2026-10-05T09:15",
        recurrence="FREQ=WEEKLY;BYDAY=MO;COUNT=6",
    )["event_id"]


# -- creating recurring events ---------------------------------------------


def test_recurring_event_is_written_in_local_time_with_a_vtimezone(client, calendars):
    result = client.create_event(
        "Home", "Standup", "2026-10-05T09:00", "2026-10-05T09:15",
        recurrence="RRULE:FREQ=WEEKLY;BYDAY=MO",
    )
    ical = _home(calendars).saved[-1]

    assert result["recurrence"] == "FREQ=WEEKLY;BYDAY=MO"
    assert "DTSTART;TZID=Europe/London:20261005T090000" in ical
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in ical
    assert "BEGIN:VTIMEZONE" in ical and "TZID:Europe/London" in ical


def test_recurring_series_keeps_local_time_across_dst(client, calendars):
    _weekly_standup(client)
    master, _ = _vevents(_stored_vcal(calendars))
    series = Series(master["DTSTART"].dt, master["RRULE"], (), LONDON)

    occurrences = series.between(
        datetime(2026, 10, 1, tzinfo=LONDON), datetime(2026, 11, 30, tzinfo=LONDON)
    )
    assert len(occurrences) == 6
    # BST ends 25 October; every occurrence is still 09:00 on the wall clock.
    assert {o.astimezone(LONDON).hour for o in occurrences} == {9}
    assert {o.utcoffset().total_seconds() for o in occurrences} == {0, 3600}


def test_one_off_events_are_still_written_in_utc(client, calendars):
    client.create_event("Home", "Lunch", "2026-07-01T12:00", "2026-07-01T13:00")
    ical = _home(calendars).saved[-1]
    assert "DTSTART:20260701T110000Z" in ical
    assert "VTIMEZONE" not in ical


def test_until_date_on_a_timed_series_becomes_end_of_day_utc():
    rule = parse_rule(
        "FREQ=DAILY;UNTIL=20261010", datetime(2026, 10, 5, 9, 0, tzinfo=LONDON), LONDON
    )
    # 23:59:59 BST on the 10th is 22:59:59Z.
    assert "UNTIL=20261010T225959Z" in rule.to_ical().decode()


def test_all_day_series_keeps_until_as_a_date(client, calendars):
    client.create_event(
        "Home", "Bins", "2026-10-06", "2026-10-06", recurrence="FREQ=WEEKLY;UNTIL=20261231"
    )
    ical = _home(calendars).saved[-1]
    assert "DTSTART;VALUE=DATE:20261006" in ical
    assert "UNTIL=20261231" in ical and "UNTIL=20261231T" not in ical


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("FREQ=FORTNIGHTLY", "not a valid RRULE"),
        ("BYDAY=MO", "needs FREQ"),
        ("FREQ=DAILY;COUNT=3;UNTIL=20261231", "COUNT or UNTIL, not both"),
        ("FREQ=WEEKLY;BYDAY=TU", "not an occurrence"),  # 5 Oct 2026 is a Monday
        ("FREQ=DAILY;UNTIL=20260101", "before the event starts"),
    ],
)
def test_bad_recurrence_rules_are_rejected(client, calendars, rule, message):
    with pytest.raises(CalendarToolError, match=message):
        client.create_event(
            "Home", "Standup", "2026-10-05T09:00", "2026-10-05T09:15", recurrence=rule
        )
    assert not _home(calendars).saved


def test_mixing_a_date_and_a_datetime_is_rejected(client):
    with pytest.raises(CalendarToolError, match="both be dates"):
        client.create_event("Home", "Odd", "2026-07-01", "2026-07-01T15:00")


# -- finding events -----------------------------------------------------------


def test_get_event_finds_apple_created_events_by_uid(client, calendars):
    _home(calendars).add_raw(APPLE_EVENT, "2578833D-7E71-4728-AAA9-C78530113AEF.ics")
    event = client.get_event("Home", "2578833D-7E71-4728-AAA9-C78530113AEF")

    assert event["title"] == "Dentist"
    assert event["start_time"] == "2026-07-01T08:15:00+01:00"
    assert "recurrence" not in event


def test_lookup_falls_back_to_scanning_when_the_filename_differs(client, calendars):
    _home(calendars).add_raw(APPLE_EVENT, "some-other-name.ics")
    assert client.get_event("Home", "2578833D-7E71-4728-AAA9-C78530113AEF")["title"] == "Dentist"


def test_unknown_event_id_is_reported(client):
    with pytest.raises(EventNotFoundError, match="Use list_events"):
        client.get_event("Home", "nope")


# -- updating -----------------------------------------------------------------


def test_update_changes_only_the_fields_given(client, calendars):
    event_id = client.create_event(
        "Home", "Dentist", "2026-07-01T14:00", "2026-07-01T15:00",
        description="Check-up", location="High Street",
    )["event_id"]

    result = client.update_event("Home", event_id, event_title="Dentist (moved)", location="")

    assert result["title"] == "Dentist (moved)"
    assert result["description"] == "Check-up"
    assert "location" not in result
    assert result["start_time"] == "2026-07-01T14:00:00+01:00"
    event = _home(calendars).stored[0]
    assert event.save_kwargs == [{"increase_seqno": False, "only_this_recurrence": False}]
    assert "SEQUENCE:1" in event.data


def test_moving_only_the_start_keeps_the_duration(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T14:45")["event_id"]
    result = client.update_event("Home", event_id, start_time="2026-07-02T09:00")
    assert result["start_time"] == "2026-07-02T09:00:00+01:00"
    assert result["end_time"] == "2026-07-02T09:45:00+01:00"


def test_moving_only_the_end_before_the_start_is_rejected(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    with pytest.raises(CalendarToolError, match="must be after"):
        client.update_event("Home", event_id, end_time="2026-07-01T13:00")


def test_switching_all_day_needs_both_ends(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    with pytest.raises(CalendarToolError, match="pass both"):
        client.update_event("Home", event_id, start_time="2026-07-02")

    result = client.update_event("Home", event_id, start_time="2026-07-02", end_time="2026-07-03")
    assert result["all_day"] is True
    assert result["end_time"] == "2026-07-03"


def test_editing_an_apple_event_keeps_its_timezone_and_apple_properties(client, calendars):
    _home(calendars).add_raw(APPLE_EVENT, "2578833D-7E71-4728-AAA9-C78530113AEF.ics")
    client.update_event(
        "Home", "2578833D-7E71-4728-AAA9-C78530113AEF", start_time="2026-07-01T10:00"
    )
    ical = _home(calendars).stored[0].data

    assert "DTSTART;TZID=Europe/London:20260701T100000" in ical
    assert "DTEND;TZID=Europe/London:20260701T104500" in ical
    assert "X-APPLE-CREATOR-IDENTITY:com.apple.mobilecal" in ical
    assert "BEGIN:VALARM" in ical
    assert "SEQUENCE:4" in ical
    # Location untouched, so the map pin stays.
    assert "X-APPLE-STRUCTURED-LOCATION" in ical


def test_changing_the_location_drops_apples_stale_map_pin(client, calendars):
    _home(calendars).add_raw(APPLE_EVENT, "2578833D-7E71-4728-AAA9-C78530113AEF.ics")
    client.update_event("Home", "2578833D-7E71-4728-AAA9-C78530113AEF", location="New Street")
    ical = _home(calendars).stored[0].data
    assert "LOCATION:New Street" in ical
    assert "X-APPLE-STRUCTURED-LOCATION" not in ical


def test_nothing_to_change_is_rejected(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    with pytest.raises(CalendarToolError, match="Nothing to change"):
        client.update_event("Home", event_id)


def test_read_only_calendar_edit_is_reported(client, calendars):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    _home(calendars).read_only = True
    with pytest.raises(CalendarToolError, match="Not permitted"):
        client.update_event("Home", event_id, event_title="x")


def test_making_a_one_off_event_repeat_moves_it_into_local_time(client, calendars):
    event_id = client.create_event("Home", "Gym", "2026-10-06T18:00", "2026-10-06T19:00")["event_id"]
    result = client.update_event("Home", event_id, recurrence="FREQ=WEEKLY;BYDAY=TU")

    assert result["recurrence"] == "FREQ=WEEKLY;BYDAY=TU"
    assert result["scope"] == "series"
    assert "DTSTART;TZID=Europe/London:20261006T180000" in _home(calendars).stored[0].data


# -- single occurrences ---------------------------------------------------------


def test_updating_one_occurrence_adds_an_override(client, calendars):
    event_id = _weekly_standup(client)

    result = client.update_event(
        "Home", event_id, occurrence="2026-10-12T09:00:00+01:00",
        start_time="2026-10-12T10:00", event_title="Standup (late)",
    )
    assert result["scope"] == "occurrence"
    assert result["recurrence_id"] == "2026-10-12T09:00:00+01:00"
    assert result["start_time"] == "2026-10-12T10:00:00+01:00"
    assert result["end_time"] == "2026-10-12T10:15:00+01:00"

    master, overrides = _vevents(_stored_vcal(calendars))
    assert str(master["SUMMARY"]) == "Standup"
    assert len(overrides) == 1
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 10, 12, 9, 0, tzinfo=LONDON)

    # Editing the same occurrence again updates that override, not a new one.
    client.update_event("Home", event_id, occurrence="2026-10-12", location="Room 2")
    _, overrides = _vevents(_stored_vcal(calendars))
    assert len(overrides) == 1
    assert str(overrides[0]["LOCATION"]) == "Room 2"
    assert str(overrides[0]["SUMMARY"]) == "Standup (late)"


def test_occurrence_accepts_utc_or_offset_forms_of_the_same_instant(client, calendars):
    event_id = _weekly_standup(client)
    # 2 November is after the DST change, so 09:00 London is 09:00Z.
    client.update_event("Home", event_id, occurrence="2026-11-02T09:00:00Z", event_title="x")
    _, overrides = _vevents(_stored_vcal(calendars))
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 11, 2, 9, 0, tzinfo=LONDON)


def test_an_occurrence_outside_the_series_is_rejected(client):
    event_id = _weekly_standup(client)
    with pytest.raises(CalendarToolError, match="not an occurrence"):
        client.update_event("Home", event_id, occurrence="2026-10-13T09:00", event_title="x")
    with pytest.raises(CalendarToolError, match="No occurrence"):
        client.update_event("Home", event_id, occurrence="2026-10-13", event_title="x")


def test_occurrence_on_a_one_off_event_is_rejected(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    with pytest.raises(CalendarToolError, match="does not repeat"):
        client.update_event("Home", event_id, occurrence="2026-07-01T14:00", event_title="x")


def test_recurrence_cannot_be_changed_on_a_single_occurrence(client):
    event_id = _weekly_standup(client)
    with pytest.raises(CalendarToolError, match="whole series"):
        client.update_event(
            "Home", event_id, occurrence="2026-10-12", recurrence="FREQ=DAILY"
        )


def test_get_event_lists_the_exceptions_of_a_series(client):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")
    client.delete_event("Home", event_id, occurrence="2026-10-19")

    event = client.get_event("Home", event_id)
    assert event["recurrence"] == "FREQ=WEEKLY;COUNT=6;BYDAY=MO"
    assert event["excluded_occurrences"] == ["2026-10-19T09:00:00+01:00"]
    assert [o["title"] for o in event["modified_occurrences"]] == ["Moved"]
    assert event["modified_occurrences"][0]["recurrence_id"] == "2026-10-12T09:00:00+01:00"


# -- changing a whole series ---------------------------------------------------


def test_moving_a_series_moves_its_exceptions_with_it(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")
    client.delete_event("Home", event_id, occurrence="2026-11-02")  # after DST ends

    result = client.update_event("Home", event_id, start_time="2026-10-05T10:30")
    assert "removed_exceptions" not in result

    master, overrides = _vevents(_stored_vcal(calendars))
    assert master["DTSTART"].dt == datetime(2026, 10, 5, 10, 30, tzinfo=LONDON)
    assert overrides[0]["RECURRENCE-ID"].dt == datetime(2026, 10, 12, 10, 30, tzinfo=LONDON)
    # Moved by wall-clock time, so still 10:30 local after the clocks change.
    assert master["EXDATE"].dts[0].dt == datetime(2026, 11, 2, 10, 30, tzinfo=LONDON)


def test_moving_a_series_off_its_rule_is_rejected(client):
    event_id = _weekly_standup(client)
    with pytest.raises(CalendarToolError, match="Pass `recurrence` as well"):
        client.update_event("Home", event_id, start_time="2026-10-06T09:00")


def test_changing_the_rule_drops_exceptions_that_no_longer_fit(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")
    client.delete_event("Home", event_id, occurrence="2026-10-19")

    # Fortnightly from 5 Oct keeps the 19th but not the 12th.
    result = client.update_event("Home", event_id, recurrence="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO")
    assert result["removed_exceptions"] == 1

    master, overrides = _vevents(_stored_vcal(calendars))
    assert overrides == []
    assert master["EXDATE"].dts[0].dt == datetime(2026, 10, 19, 9, 0, tzinfo=LONDON)


def test_clearing_the_recurrence_makes_a_one_off_event(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")
    client.delete_event("Home", event_id, occurrence="2026-10-19")

    result = client.update_event("Home", event_id, recurrence="")
    assert result["scope"] == "event"
    assert result["removed_exceptions"] == 2
    assert "recurrence" not in result

    master, overrides = _vevents(_stored_vcal(calendars))
    assert overrides == []
    assert master.get("RRULE") is None and master.get("EXDATE") is None


# -- deleting -------------------------------------------------------------------


def test_delete_removes_the_whole_event(client, calendars):
    event_id = _weekly_standup(client)
    result = client.delete_event("Home", event_id)
    assert result["deleted"] == "event"
    assert result["title"] == "Standup"
    assert _home(calendars).stored == []


def test_deleting_one_occurrence_adds_an_exdate_and_drops_its_override(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")

    result = client.delete_event("Home", event_id, occurrence="2026-10-12T09:00")
    assert result["deleted"] == "occurrence"

    master, overrides = _vevents(_stored_vcal(calendars))
    assert overrides == []
    assert master["EXDATE"].dts[0].dt == datetime(2026, 10, 12, 9, 0, tzinfo=LONDON)
    assert "EXDATE;TZID=Europe/London:20261012T090000" in _home(calendars).stored[0].data

    with pytest.raises(CalendarToolError, match="already been deleted"):
        client.delete_event("Home", event_id, occurrence="2026-10-12T09:00")


def test_delete_and_following_ends_the_series_before_the_occurrence(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Kept")
    client.update_event("Home", event_id, occurrence="2026-10-26", event_title="Dropped")
    client.delete_event("Home", event_id, occurrence="2026-11-02")

    result = client.delete_event("Home", event_id, occurrence="2026-10-19", and_following=True)
    assert result["deleted"] == "occurrence_and_following"
    # COUNT is replaced by an UNTIL one second before 09:00 BST on the 19th.
    assert result["recurrence"] == "FREQ=WEEKLY;UNTIL=20261019T075959Z;BYDAY=MO"

    master, overrides = _vevents(_stored_vcal(calendars))
    assert [str(o["SUMMARY"]) for o in overrides] == ["Kept"]
    assert master.get("EXDATE") is None

    series = Series(master["DTSTART"].dt, master["RRULE"], (), LONDON)
    remaining = series.between(
        datetime(2026, 10, 1, tzinfo=LONDON), datetime(2026, 12, 31, tzinfo=LONDON)
    )
    assert [o.day for o in remaining] == [5, 12]


def test_delete_and_following_from_the_first_occurrence_deletes_the_series(client, calendars):
    event_id = _weekly_standup(client)
    result = client.delete_event("Home", event_id, occurrence="2026-10-05", and_following=True)
    assert result["deleted"] == "series"
    assert _home(calendars).stored == []


def test_all_day_series_occurrences_can_be_deleted(client, calendars):
    event_id = client.create_event(
        "Home", "Bins", "2026-10-06", "2026-10-06", recurrence="FREQ=WEEKLY;COUNT=4"
    )["event_id"]
    client.delete_event("Home", event_id, occurrence="2026-10-13")
    assert "EXDATE;VALUE=DATE:20261013" in _home(calendars).stored[0].data


def test_and_following_without_an_occurrence_is_rejected(client):
    event_id = _weekly_standup(client)
    with pytest.raises(CalendarToolError, match="needs an occurrence"):
        client.delete_event("Home", event_id, and_following=True)


# -- warnings -------------------------------------------------------------------


MEETING = APPLE_EVENT.replace(
    "SEQUENCE:3\n",
    "SEQUENCE:3\nORGANIZER:mailto:boss@example.com\nATTENDEE:mailto:someone@icloud.com\n",
)


def test_plain_successes_carry_no_warnings(client):
    event_id = client.create_event("Home", "Call", "2026-07-01T14:00", "2026-07-01T15:00")["event_id"]
    assert "warnings" not in client.update_event("Home", event_id, event_title="x")
    assert "warnings" not in client.get_event("Home", event_id)
    assert "warnings" not in client.list_events("Home", "2026-07-01", "2026-07-02")
    assert "warnings" not in client.delete_event("Home", event_id)


def test_events_with_attendees_warn_that_invitations_are_not_sent(client, calendars):
    uid = "2578833D-7E71-4728-AAA9-C78530113AEF"
    _home(calendars).add_raw(MEETING, f"{uid}.ics")

    assert "not sent" in client.get_event("Home", uid)["warnings"][0]
    assert any("organizer" in w for w in client.update_event("Home", uid, event_title="x")["warnings"])
    assert any("organizer" in w for w in client.delete_event("Home", uid)["warnings"])


def test_dropping_apples_map_pin_is_reported(client, calendars):
    uid = "2578833D-7E71-4728-AAA9-C78530113AEF"
    _home(calendars).add_raw(APPLE_EVENT, f"{uid}.ics")
    assert any("map pin" in w for w in client.update_event("Home", uid, location="New")["warnings"])


def test_a_fixed_offset_on_a_series_reports_the_zone_it_was_anchored_to(client):
    result = client.create_event(
        "Home", "Standup", "2026-10-05T09:00:00-04:00", "2026-10-05T09:15:00-04:00",
        recurrence="FREQ=WEEKLY;BYDAY=MO",
    )
    assert result["start_time"] == "2026-10-05T14:00:00+01:00"
    assert "anchored to Europe/London" in result["warnings"][0]
    assert "14:00" in result["warnings"][0]


def test_an_offset_matching_the_default_zone_does_not_warn(client):
    result = client.create_event(
        "Home", "Standup", "2026-10-05T09:00:00+01:00", "2026-10-05T09:15:00+01:00",
        recurrence="FREQ=WEEKLY;BYDAY=MO",
    )
    assert "warnings" not in result


def test_discarded_exceptions_are_explained(client):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Moved")
    result = client.update_event("Home", event_id, recurrence="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO")
    assert any("were discarded" in w for w in result["warnings"])

    client.delete_event("Home", event_id, occurrence="2026-10-19")
    result = client.update_event("Home", event_id, recurrence="")
    assert any("no longer repeats" in w for w in result["warnings"])


def test_renaming_a_series_renames_occurrences_that_had_not_diverged(client, calendars):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", location="Room 2")
    client.update_event("Home", event_id, occurrence="2026-10-19", event_title="Special")

    result = client.update_event("Home", event_id, event_title="Daily sync")

    _, overrides = _vevents(_stored_vcal(calendars))
    titles = {o["RECURRENCE-ID"].dt.day: str(o["SUMMARY"]) for o in overrides}
    assert titles == {12: "Daily sync", 19: "Special"}
    assert result["warnings"] == [
        "These occurrences have their own title and were left unchanged: "
        "2026-10-19T09:00:00+01:00. Update them with `occurrence` if they should match."
    ]


def test_moving_a_series_takes_unmoved_overrides_along_and_reports_rescheduled_ones(
    client, calendars
):
    event_id = _weekly_standup(client)
    client.update_event("Home", event_id, occurrence="2026-10-12", event_title="Renamed only")
    client.update_event("Home", event_id, occurrence="2026-10-19", start_time="2026-10-19T15:00")

    result = client.update_event("Home", event_id, start_time="2026-10-05T10:00")

    _, overrides = _vevents(_stored_vcal(calendars))
    starts = {o["RECURRENCE-ID"].dt.day: o["DTSTART"].dt for o in overrides}
    assert starts[12] == datetime(2026, 10, 12, 10, 0, tzinfo=LONDON)
    assert starts[19] == datetime(2026, 10, 19, 15, 0, tzinfo=LONDON)
    assert result["warnings"] == [
        "These occurrences had been individually rescheduled and kept their own "
        "times: 2026-10-19T10:00:00+01:00. Update them with `occurrence` if they "
        "should move too."
    ]


def test_list_events_warns_when_repeating_events_could_not_be_expanded(client, calendars):
    home = _home(calendars)
    original = home.search

    def picky_search(**kwargs):
        if kwargs.get("expand"):
            raise RuntimeError("expand unsupported")
        return original(**kwargs)

    home.search = picky_search
    result = client.list_events("Home", "2026-07-01", "2026-07-02")
    assert "refused to expand" in result["warnings"][0]


def test_get_event_warns_about_an_invitation_to_a_single_occurrence(client, calendars):
    lone = APPLE_EVENT.replace(
        "SEQUENCE:3\n", "SEQUENCE:3\nRECURRENCE-ID;TZID=Europe/London:20260701T081500\n"
    )
    _home(calendars).add_raw(lone, "2578833D-7E71-4728-AAA9-C78530113AEF.ics")
    result = client.get_event("Home", "2578833D-7E71-4728-AAA9-C78530113AEF")
    assert "Only individual occurrences" in result["warnings"][0]
