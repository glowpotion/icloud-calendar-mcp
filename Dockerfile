# Runs over stdio by default (docker run -i); set MCP_TRANSPORT=http, or pass
# --transport http, for the streamable-HTTP server. See the README.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/glowpotion/icloud-calendar-mcp" \
      org.opencontainers.image.description="MCP server for reading and publishing iCloud Calendar events over CalDAV" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --system --uid 10001 --no-create-home mcp
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER mcp
EXPOSE 8765
ENTRYPOINT ["icloud-calendar-mcp"]
