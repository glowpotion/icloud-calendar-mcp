"""MCP server exposing iCloud Calendar over CalDAV."""

from __future__ import annotations

from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer

from .calendar_client import CalendarToolError, ICloudCalendarClient
from .config import Config, ConfigError

INSTRUCTIONS = """\
Read and write events on the user's iCloud calendars over CalDAV.

Call `list_calendars` first when you do not already know the exact calendar
name — `create_event` and `list_events` both require one, and iCloud names are
whatever the user typed in the Calendar app ("Home", "Work", "Family").

Times are ISO 8601. A value without an offset is interpreted in the server's
configured default timezone; pass an explicit offset when you know it. Passing
date-only values for both ends of `create_event` produces an all-day event,
where `end_time` is the last day the event covers.

To change or remove an event, take its `event_id` from `list_events` and call
`update_event` or `delete_event`. Repeating events use an iCalendar RRULE such
as `FREQ=WEEKLY;BYDAY=MO,WE`. `list_events` returns each occurrence of a series
separately with a `recurrence_id`; pass that as `occurrence` to change or
delete just that one. `get_event` shows a series' rule and its exceptions.

Every response has `status`: `success`, or `error` with an `error` message
saying what to fix. A successful response may also carry `warnings`: things
that worked but not quite as asked, or side effects the user should know about.
Pass warnings on to the user rather than dropping them.

Known limitations: invitations are never sent, so edits to events with
attendees are not communicated by this tool; "this and all following" edits
are not supported (end the series with `delete_event(..., and_following=true)`
and create a new one); only events are handled, not reminders or tasks.
"""

mcp = MCPServer(
    name="icloud-calendar",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

_client: ICloudCalendarClient | None = None


def _get_client() -> ICloudCalendarClient:
    global _client
    if _client is None:
        _client = ICloudCalendarClient(config=Config.from_env())
    return _client


async def _run(fn: Any, *args: Any) -> dict[str, Any]:
    """Run a blocking CalDAV call off the event loop, mapping errors to results."""
    try:
        client = _get_client()
        return await anyio.to_thread.run_sync(lambda: fn(client, *args))
    except (CalendarToolError, ConfigError) as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool(
    title="List calendars",
    description=(
        "List the iCloud calendars available to the account, with their names "
        "and CalDAV URLs. Use this to discover the exact `calendar_name` that "
        "`create_event` and `list_events` expect."
    ),
)
async def list_calendars() -> dict[str, Any]:
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        calendars = client.list_calendars()
        return {"status": "success", "count": len(calendars), "calendars": calendars}

    return await _run(_call)


@mcp.tool(
    title="Create calendar event",
    description=(
        "Publish a new event to an iCloud calendar. Returns the created event's "
        "ID (its iCalendar UID). Times are ISO 8601; a value with no UTC offset "
        "is read in the configured default timezone. Give date-only values for "
        "both `start_time` and `end_time` to create an all-day event, where "
        "`end_time` is the last day it covers. To make it repeat, pass "
        "`recurrence` as an iCalendar RRULE, e.g. 'FREQ=WEEKLY;BYDAY=MO,WE', "
        "'FREQ=DAILY;COUNT=10' or 'FREQ=MONTHLY;BYMONTHDAY=1;UNTIL=20271231'; "
        "`start_time`/`end_time` then describe the first occurrence, which must "
        "itself match the rule. Repeating timed events keep their local time "
        "across daylight-saving changes."
    ),
)
async def create_event(
    calendar_name: str,
    event_title: str,
    start_time: str,
    end_time: str,
    description: str | None = None,
    location: str | None = None,
    recurrence: str | None = None,
) -> dict[str, Any]:
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        return client.create_event(
            calendar_name=calendar_name,
            event_title=event_title,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
            recurrence=recurrence,
        )

    return await _run(_call)


@mcp.tool(
    title="Get calendar event",
    description=(
        "Fetch one event by its `event_id`. For a repeating event this is the "
        "series itself: its first occurrence, its `recurrence` rule, the "
        "`excluded_occurrences` that were deleted, and the "
        "`modified_occurrences` that were changed individually."
    ),
)
async def get_event(calendar_name: str, event_id: str) -> dict[str, Any]:
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        return client.get_event(calendar_name=calendar_name, event_id=event_id)

    return await _run(_call)


@mcp.tool(
    title="Update calendar event",
    description=(
        "Change an existing event. Only the fields you pass are changed; pass "
        "an empty string to clear `description` or `location`. Changing only "
        "`start_time` moves the event and keeps its duration. For a repeating "
        "event, leave `occurrence` out to change the whole series, or set it "
        "to an occurrence's `recurrence_id` from `list_events` to change just "
        "that one. `recurrence` (whole series only) replaces the RRULE; an "
        "empty string stops the event repeating, and an RRULE makes a one-off "
        "event repeat. Moving a series moves its individually changed and "
        "deleted occurrences with it; any that no longer fit the new rule are "
        "discarded and counted in `removed_exceptions`. Changing \"this and all "
        "following\" occurrences is not supported: end the series with "
        "`delete_event(..., and_following=true)` and create a new one. "
        "Invitations are not sent to attendees. Check `warnings` in the result."
    ),
)
async def update_event(
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
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        return client.update_event(
            calendar_name=calendar_name,
            event_id=event_id,
            event_title=event_title,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
            recurrence=recurrence,
            occurrence=occurrence,
        )

    return await _run(_call)


@mcp.tool(
    title="Delete calendar event",
    description=(
        "Delete an event. Without `occurrence` this removes the event, or the "
        "whole series if it repeats. For a repeating event, set `occurrence` "
        "to an occurrence's `recurrence_id` from `list_events` to delete only "
        "that one, and add `and_following=true` to also delete every "
        "occurrence after it (the series then ends before it). Cancellations "
        "are not sent to attendees. Check `warnings` in the result."
    ),
)
async def delete_event(
    calendar_name: str,
    event_id: str,
    occurrence: str | None = None,
    and_following: bool = False,
) -> dict[str, Any]:
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        return client.delete_event(
            calendar_name=calendar_name,
            event_id=event_id,
            occurrence=occurrence,
            and_following=and_following,
        )

    return await _run(_call)


@mcp.tool(
    title="List upcoming events",
    description=(
        "Fetch events from an iCloud calendar within a time range. Defaults to "
        "the next 30 days when no range is given. Recurring events are expanded, "
        "so each occurrence in the range is returned separately. Ranges longer "
        "than 400 days are rejected."
    ),
)
async def list_events(
    calendar_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    def _call(client: ICloudCalendarClient) -> dict[str, Any]:
        return client.list_events(
            calendar_name=calendar_name,
            start_date=start_date,
            end_date=end_date,
        )

    return await _run(_call)


def main() -> None:
    """Run the server over stdio or HTTP, or verify the connection with ``--check``."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="icloud-calendar-mcp", description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="print the reachable calendars and exit"
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio").strip() or "stdio",
        help="stdio for a local client (default), http to serve over the network",
    )
    parser.add_argument("--host", help="HTTP bind address (default MCP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default MCP_PORT or 8765)")
    args = parser.parse_args()

    if args.check:
        raise SystemExit(_check())
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    from .http_app import HttpConfig, serve

    try:
        Config.from_env()  # fail at startup, not on the first tool call
        http = HttpConfig.from_env(host=args.host, port=args.port)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    serve(mcp, http)


def _check() -> int:
    """Print the reachable calendars so setup problems surface before wiring up."""
    import sys

    try:
        client = _get_client()
        calendars = client.list_calendars()
    except (CalendarToolError, ConfigError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"OK: connected to {client.config.url} as {client.config.username}")
    print(f"Default timezone: {client.config.default_timezone}")
    print(f"{len(calendars)} calendar(s):")
    for cal in calendars:
        print(f"  - {cal['name']}")
    return 0


if __name__ == "__main__":
    main()
