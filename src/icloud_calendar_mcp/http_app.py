"""Streamable-HTTP transport, for running the server on another machine.

The MCP SDK's built-in auth is OAuth, which is far more than a server on a
home network needs, so this wraps the MCP app in a static bearer-token check.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server.transport_security import TransportSecuritySettings

from .config import ConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MIN_TOKEN_LENGTH = 32
LOOPBACK = ("127.0.0.1", "localhost", "::1")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class HttpConfig:
    host: str
    port: int
    token: str | None
    allowed_hosts: list[str]

    @classmethod
    def from_env(cls, host: str | None = None, port: int | None = None) -> "HttpConfig":
        host = host or os.environ.get("MCP_HOST", "").strip() or DEFAULT_HOST
        raw_port = os.environ.get("MCP_PORT", "").strip()
        try:
            port = port or (int(raw_port) if raw_port else DEFAULT_PORT)
        except ValueError as exc:
            raise ConfigError(f"MCP_PORT={raw_port!r} is not a port number.") from exc

        token = os.environ.get("MCP_AUTH_TOKEN", "").strip() or None
        if token is None and host not in LOOPBACK:
            raise ConfigError(
                f"Refusing to listen on {host} without MCP_AUTH_TOKEN: anyone on the "
                "network could read and change your calendars. Generate one with "
                "`openssl rand -hex 32`."
            )
        if token is not None and len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"MCP_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters. "
                "Generate one with `openssl rand -hex 32`."
            )

        allowed = [
            h.strip()
            for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]
        return cls(host=host, port=port, token=token, allowed_hosts=allowed)

    def transport_security(self) -> TransportSecuritySettings | None:
        """Host-header checking against DNS rebinding, if hosts were listed.

        Without a list the SDK only protects loopback binds; the bearer token
        is what guards a LAN bind either way.
        """
        if not self.allowed_hosts:
            return None
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=self.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in self.allowed_hosts],
        )


class BearerTokenMiddleware:
    """Reject HTTP requests without ``Authorization: Bearer <token>``.

    ``/healthz`` stays open so monitoring can check the process is up without
    holding the token; it reveals nothing about the calendars.
    """

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token.encode() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # Lifespan events must reach the MCP app to start its session manager.
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Nothing here serves websockets; refuse rather than skip the token check.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("path") == "/healthz":
            await _respond(send, 200, {"status": "ok"})
            return
        if self.token is not None and not self._authorised(scope):
            await _respond(
                send,
                401,
                {"error": "Missing or invalid bearer token."},
                extra_headers=[(b"www-authenticate", b'Bearer realm="icloud-calendar-mcp"')],
            )
            return
        await self.app(scope, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, credential = value.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(
                    credential.strip(), self.token or b""
                )
        return False


async def _respond(
    send: Send,
    status: int,
    body: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def build_app(mcp: Any, config: HttpConfig) -> ASGIApp:
    app = mcp.streamable_http_app(
        host=config.host,
        transport_security=config.transport_security(),
    )
    return BearerTokenMiddleware(app, config.token)


def serve(mcp: Any, config: HttpConfig) -> None:
    import uvicorn

    uvicorn.run(
        build_app(mcp, config),
        host=config.host,
        port=config.port,
        log_level=os.environ.get("MCP_LOG_LEVEL", "info").lower(),
        # No reverse proxy is expected, so X-Forwarded-* headers are not trusted.
        proxy_headers=False,
    )
