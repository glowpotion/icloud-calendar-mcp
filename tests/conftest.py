from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from caldav.lib import error as caldav_error
from icalendar import Calendar as ICalendar

from icloud_calendar_mcp.calendar_client import ICloudCalendarClient
from icloud_calendar_mcp.config import Config


class FakeEvent:
    """Stands in for caldav.Event: exposes the parsed VEVENT component."""

    def __init__(
        self,
        ical: str,
        url: str = "https://caldav.icloud.com/fake/1.ics",
        parent: "FakeCalendar | None" = None,
    ):
        self.ical = ical
        self.url = url
        self.parent = parent
        self.save_kwargs: list[dict[str, Any]] = []

    @property
    def data(self) -> str:
        return self.ical

    @data.setter
    def data(self, value: str) -> None:
        self.ical = value

    @property
    def icalendar_component(self) -> Any:
        cal = ICalendar.from_ical(self.ical)
        return next(c for c in cal.walk() if c.name == "VEVENT")

    def save(self, **kwargs: Any) -> "FakeEvent":
        self.save_kwargs.append(kwargs)
        if self.parent is not None and self.parent.read_only:
            raise caldav_error.AuthorizationError("403 Forbidden")
        return self

    def delete(self) -> None:
        if self.parent is not None:
            self.parent.stored.remove(self)


@dataclass
class FakeCalendar:
    name: str
    url: str = "https://caldav.icloud.com/fake/"
    saved: list[str] = field(default_factory=list)
    stored: list[FakeEvent] = field(default_factory=list)
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    read_only: bool = False

    def save_event(self, ical: str) -> FakeEvent:
        if self.read_only:
            raise RuntimeError("403 Forbidden")
        self.saved.append(ical)
        uid = str(ICalendar.from_ical(ical).walk("VEVENT")[0]["UID"])
        event = FakeEvent(ical, url=self.url + quote(uid, safe="/") + ".ics", parent=self)
        self.stored.append(event)
        return event

    def add_raw(self, ical: str, filename: str) -> FakeEvent:
        """Store an object the way another client (Apple Calendar) would."""
        event = FakeEvent(ical, url=self.url + filename, parent=self)
        self.stored.append(event)
        return event

    def search(self, **kwargs: Any) -> list[FakeEvent]:
        self.search_calls.append(kwargs)
        return list(self.stored)

    def event_by_url(self, url: str) -> FakeEvent:
        for event in self.stored:
            if event.url == url:
                return event
        raise caldav_error.NotFoundError(url)

    def events(self) -> list[FakeEvent]:
        return list(self.stored)


@dataclass
class FakePrincipal:
    _calendars: list[FakeCalendar]

    def calendars(self) -> list[FakeCalendar]:
        return list(self._calendars)


@pytest.fixture
def config() -> Config:
    return Config(
        username="someone@icloud.com",
        password="abcd-efgh-ijkl-mnop",
        url="https://caldav.icloud.com",
        default_timezone=ZoneInfo("Europe/London"),
    )


@pytest.fixture
def calendars() -> list[FakeCalendar]:
    return [
        FakeCalendar(name="Home", url="https://caldav.icloud.com/fake/home/"),
        FakeCalendar(name="Work", url="https://caldav.icloud.com/fake/work/"),
        FakeCalendar(name="Work Travel", url="https://caldav.icloud.com/fake/wt/"),
    ]


@pytest.fixture
def client(
    config: Config,
    calendars: list[FakeCalendar],
    monkeypatch: pytest.MonkeyPatch,
) -> ICloudCalendarClient:
    c = ICloudCalendarClient(config=config)
    monkeypatch.setattr(c, "principal", lambda: FakePrincipal(calendars))
    return c


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
