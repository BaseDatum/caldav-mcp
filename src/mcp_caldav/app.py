"""Starlette ASGI application — Streamable HTTP transport with per-request auth.

Wires together:
* MCP Streamable HTTP transport
* Per-request user ID extraction from configurable header
* Redis connection for ICS cache + rate limiter
* Database init/shutdown
* Rate limiting via ``limits`` library with Redis backing store
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import redis.asyncio as aioredis
from limits import parse as parse_rate_limit
from limits.aio.storage import RedisStorage
from limits.aio.strategies import FixedWindowRateLimiter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .database import close_db, init_db
from .ics_client import init_ics_cache
from .rest_api import get_events as rest_get_events, list_sources as rest_list_sources
from .server import create_mcp_server
from .settings import Settings

logger = logging.getLogger("mcp-caldav.app")

# ── Module-level state ──────────────────────────────────────────────

_settings: Settings | None = None
_rate_limiter: FixedWindowRateLimiter | None = None
_rate_limit_item = None  # parsed limit expression
_redis_client = None


@dataclass
class UserContext:
    """Attached to the MCP lifespan context so tool handlers can read user_id."""

    user_id: str


# ── Lifespan ────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    global _settings, _rate_limiter, _rate_limit_item, _redis_client  # noqa: PLW0603

    _settings = Settings()
    logger.setLevel(_settings.log_level.upper())

    # Database.
    await init_db(_settings)
    logger.info("Database initialised")

    # Redis.
    _redis_client = aioredis.from_url(_settings.redis_url, decode_responses=False)
    init_ics_cache(_redis_client, _settings.ics_cache_ttl_seconds)
    logger.info("Redis connected (%s)", _settings.redis_url)

    # Rate limiter.
    # The limits library requires "async+" prefix for async Redis storage,
    # and we use the redispy implementation since we already depend on redis[hiredis].
    _rate_limit_item = parse_rate_limit(_settings.rate_limit)
    async_redis_url = _settings.redis_url
    if not async_redis_url.startswith("async+"):
        async_redis_url = f"async+{async_redis_url}"
    storage = RedisStorage(async_redis_url, implementation="redispy")
    _rate_limiter = FixedWindowRateLimiter(storage)
    logger.info("Rate limiter configured: %s", _settings.rate_limit)

    yield

    # Shutdown.
    await close_db()
    if _redis_client:
        await _redis_client.aclose()
    logger.info("Shutdown complete")


# ── Streamable HTTP handler ─────────────────────────────────────────


async def _handle_mcp(request: Request) -> Response:
    """Handle Streamable HTTP MCP requests with user auth + rate limiting."""
    assert _settings is not None

    # Extract user ID from the configurable header.
    user_id = request.headers.get(_settings.user_id_header)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": f"Missing {_settings.user_id_header} header"},
        )

    # Rate limit check.
    if _rate_limiter and _rate_limit_item:
        allowed = await _rate_limiter.hit(_rate_limit_item, "user", user_id)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded. Please try again later."},
            )

    # Create a fresh MCP server per request and inject user context.
    mcp_server = create_mcp_server()

    # Use the MCP SDK's StreamableHTTP transport.
    from mcp.server.streamable_http import StreamableHTTPServerTransport

    transport = StreamableHTTPServerTransport(
        mcp_messages_endpoint="/mcp",
        is_json_response_enabled=True,
    )

    # Run the MCP session.  The transport handles reading the request
    # body and writing the response.
    async with transport.connect() as (read_stream, write_stream):
        # Inject user context so tool handlers can access user_id.
        # The MCP SDK's Server.run() accepts initialization_options which
        # we use to thread through the user context via the lifespan_context.
        import asyncio

        async def _run_session() -> None:
            # We override the lifespan context on the server's request_context
            # inside the tool handler.  Since the SDK doesn't directly expose
            # a per-request context injection point for streamable HTTP, we
            # store user_id on the transport object and read it in the tool handler.
            mcp_server._user_id = user_id  # type: ignore[attr-defined]
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

        # The transport.handle_request() reads from the Starlette request
        # and writes to the response.
        response = await transport.handle_request(
            request.scope, request.receive, request._send
        )
        asyncio.ensure_future(_run_session())

    return response


async def _health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "mcp-caldav"})


# ── App factory ─────────────────────────────────────────────────────


def create_app() -> Starlette:
    """Create the Starlette ASGI application."""
    return Starlette(
        debug=False,
        lifespan=_lifespan,
        routes=[
            Route("/health", _health, methods=["GET"]),
            # REST API — used by the api-server for unified calendar queries.
            Route("/api/events", rest_get_events, methods=["GET"]),
            Route("/api/sources", rest_list_sources, methods=["GET"]),
            # MCP Streamable HTTP transport.
            Route("/mcp", _handle_mcp, methods=["GET", "POST", "DELETE"]),
            Route("/mcp/", _handle_mcp, methods=["GET", "POST", "DELETE"]),
        ],
    )


app = create_app()
