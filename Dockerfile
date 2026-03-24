# Multi-tenant MCP CalDAV server.
#
# Two servers:
#   Port 8025: FastMCP streamable-http (MCP tools for agents)
#   Port 8026: FastAPI REST API (service-to-service event queries)

FROM python:3.11-slim
LABEL org.opencontainers.image.source=https://github.com/BaseDatum/caldav-mcp

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev

# Non-root user.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8025 8026

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:8026/health || exit 1'

ENTRYPOINT ["uv", "run", "mcp-caldav", "--transport", "streamable-http", "--port", "8025", "--api-port", "8026"]
