from __future__ import annotations


import pytest

from icloud_calendar_mcp import server


@pytest.fixture(autouse=True)
def reset_client(monkeypatch):
    monkeypatch.setattr(server, "_client", None)


def _payload(result):
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_all_tools_are_registered():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "list_calendars",
        "create_event",
        "get_event",
        "update_event",
        "delete_event",
        "list_events",
    }


@pytest.mark.anyio
async def test_create_event_schema_matches_the_agreed_interface():
    tool = next(t for t in await server.mcp.list_tools() if t.name == "create_event")
    props = tool.input_schema["properties"]

    assert set(tool.input_schema["required"]) == {
        "calendar_name",
        "event_title",
        "start_time",
        "end_time",
    }
    assert set(props) == {
        "calendar_name",
        "event_title",
        "start_time",
        "end_time",
        "description",
        "location",
        "recurrence",
    }


@pytest.mark.anyio
async def test_missing_credentials_surface_as_a_tool_error(monkeypatch):
    for name in ("ICLOUD_USERNAME", "ICLOUD_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    result = await server.mcp.call_tool("list_calendars", {})
    payload = _payload(result)

    assert payload["status"] == "error"
    assert "ICLOUD_USERNAME" in payload["error"]


@pytest.mark.anyio
async def test_list_events_round_trips_through_the_tool(monkeypatch, client, calendars):
    monkeypatch.setattr(server, "_get_client", lambda: client)
    client.create_event("Work", "Retro", "2026-07-01T14:00", "2026-07-01T15:00")

    result = await server.mcp.call_tool(
        "list_events",
        {"calendar_name": "Work", "start_date": "2026-07-01", "end_date": "2026-07-02"},
    )
    payload = _payload(result)

    assert payload["status"] == "success"
    assert payload["count"] == 1
    assert payload["events"][0]["title"] == "Retro"


@pytest.mark.anyio
async def test_a_bad_calendar_name_is_returned_as_an_error_not_an_exception(
    monkeypatch, client
):
    monkeypatch.setattr(server, "_get_client", lambda: client)

    result = await server.mcp.call_tool(
        "create_event",
        {
            "calendar_name": "Nonexistent",
            "event_title": "Ghost",
            "start_time": "2026-07-01T14:00",
            "end_time": "2026-07-01T15:00",
        },
    )
    payload = _payload(result)

    assert payload["status"] == "error"
    assert "No calendar named 'Nonexistent'" in payload["error"]
