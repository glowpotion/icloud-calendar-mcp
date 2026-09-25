"""iCloud Calendar access over CalDAV, exposed as an MCP server."""

from .calendar_client import (
    CalendarNotFoundError,
    CalendarToolError,
    EventRecord,
    ICloudCalendarClient,
)
from .config import Config, ConfigError

__all__ = [
    "CalendarNotFoundError",
    "CalendarToolError",
    "Config",
    "ConfigError",
    "EventRecord",
    "ICloudCalendarClient",
]
