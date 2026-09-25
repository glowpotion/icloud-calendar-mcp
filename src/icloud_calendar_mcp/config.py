"""Configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CALDAV_URL = "https://caldav.icloud.com"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    url: str
    default_timezone: ZoneInfo

    @classmethod
    def from_env(cls) -> "Config":
        username = os.environ.get("ICLOUD_USERNAME", "").strip()
        password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()

        missing = [
            name
            for name, value in (
                ("ICLOUD_USERNAME", username),
                ("ICLOUD_APP_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "ICLOUD_APP_PASSWORD must be an app-specific password generated at "
                "https://appleid.apple.com — a regular Apple ID password will always "
                "fail against iCloud when two-factor authentication is enabled."
            )

        # An app-specific password is 16 lowercase letters in 4 hyphenated groups.
        # Warn-by-shape rather than reject, since Apple could change the format.
        tz_name = os.environ.get("CALDAV_DEFAULT_TIMEZONE", "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"CALDAV_DEFAULT_TIMEZONE={tz_name!r} is not a valid IANA timezone "
                "name (e.g. 'Europe/London', 'America/New_York', 'UTC')."
            ) from exc

        return cls(
            username=username,
            password=password,
            url=os.environ.get("CALDAV_URL", "").strip() or DEFAULT_CALDAV_URL,
            default_timezone=tz,
        )


def looks_like_app_specific_password(password: str) -> bool:
    """True if the password matches Apple's xxxx-xxxx-xxxx-xxxx app password shape."""
    groups = password.split("-")
    return len(groups) == 4 and all(len(g) == 4 and g.isalpha() for g in groups)
