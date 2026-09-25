from __future__ import annotations

import json

import pytest

from icloud_calendar_mcp.config import ConfigError
from icloud_calendar_mcp.http_app import BearerTokenMiddleware, HttpConfig

TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("MCP_HOST", "MCP_PORT", "MCP_AUTH_TOKEN", "MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)


# -- configuration ------------------------------------------------------------


def test_defaults_to_loopback_without_a_token():
    config = HttpConfig.from_env()
    assert (config.host, config.port, config.token) == ("127.0.0.1", 8765, None)


def test_a_network_bind_requires_a_token():
    with pytest.raises(ConfigError, match="without MCP_AUTH_TOKEN"):
        HttpConfig.from_env(host="0.0.0.0")


def test_short_tokens_are_rejected(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "hunter2")
    with pytest.raises(ConfigError, match="at least 32"):
        HttpConfig.from_env(host="0.0.0.0")


def test_env_and_arguments(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9000")
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "pi.local:9000, 100.64.0.5:9000")

    config = HttpConfig.from_env()
    assert (config.host, config.port) == ("0.0.0.0", 9000)
    assert config.transport_security().allowed_hosts == ["pi.local:9000", "100.64.0.5:9000"]
    assert HttpConfig.from_env(port=9100).port == 9100


def test_bad_port_is_reported(monkeypatch):
    monkeypatch.setenv("MCP_PORT", "http")
    with pytest.raises(ConfigError, match="not a port number"):
        HttpConfig.from_env()


# -- bearer token middleware -------------------------------------------------------


async def _call(app, path="/mcp", headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "headers": list(headers)}
    await app(scope, receive, send)
    return sent


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Basic " + TOKEN.encode())],
    ],
)
async def test_requests_without_the_right_token_are_refused(headers):
    sent = await _call(BearerTokenMiddleware(_inner, TOKEN), headers=headers)
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="icloud-calendar-mcp"') in sent[0]["headers"]


@pytest.mark.anyio
async def test_the_right_token_reaches_the_mcp_app():
    sent = await _call(
        BearerTokenMiddleware(_inner, TOKEN),
        headers=[(b"authorization", b"bearer " + TOKEN.encode())],
    )
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"mcp"


@pytest.mark.anyio
async def test_healthz_is_open_and_says_nothing_else():
    sent = await _call(BearerTokenMiddleware(_inner, TOKEN), path="/healthz")
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"]) == {"status": "ok"}


@pytest.mark.anyio
async def test_lifespan_events_pass_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    async def noop(*_):
        return {}

    await BearerTokenMiddleware(app, TOKEN)({"type": "lifespan"}, noop, noop)
    assert seen == ["lifespan"]
