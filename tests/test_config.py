from __future__ import annotations

import pytest

from icloud_calendar_mcp.config import (
    DEFAULT_CALDAV_URL,
    Config,
    ConfigError,
    looks_like_app_specific_password,
)

ENV_VARS = (
    "ICLOUD_USERNAME",
    "ICLOUD_APP_PASSWORD",
    "CALDAV_URL",
    "CALDAV_DEFAULT_TIMEZONE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_the_icloud_caldav_endpoint(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "someone@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abcd-efgh-ijkl-mnop")

    config = Config.from_env()
    assert config.url == DEFAULT_CALDAV_URL
    assert str(config.default_timezone) == "UTC"


def test_missing_credentials_name_the_variables_and_mention_2fa(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "someone@icloud.com")

    with pytest.raises(ConfigError) as exc:
        Config.from_env()

    assert "ICLOUD_APP_PASSWORD" in str(exc.value)
    assert "appleid.apple.com" in str(exc.value)


def test_invalid_timezone_is_rejected(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "someone@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
    monkeypatch.setenv("CALDAV_DEFAULT_TIMEZONE", "Mars/Olympus_Mons")

    with pytest.raises(ConfigError, match="not a valid IANA timezone"):
        Config.from_env()


@pytest.mark.parametrize(
    "password,expected",
    [
        ("abcd-efgh-ijkl-mnop", True),
        ("hunter2", False),
        ("abcd-efgh-ijkl", False),
        ("abc1-efgh-ijkl-mnop", False),
    ],
)
def test_app_specific_password_shape(password, expected):
    assert looks_like_app_specific_password(password) is expected
