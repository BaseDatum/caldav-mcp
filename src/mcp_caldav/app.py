"""Starlette ASGI application — mounts FastMCP + REST API routes.

Wires together:
* FastMCP streamable HTTP transport (stateless mode)
* REST API endpoints for service-to-service calendar queries
* Redis connection for ICS cache + rate limiter
* Database init/shutdown
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from .rest_api import get_events as rest_get_events
from .rest_api import list_sources as rest_list_sources
from .server import mcp
from .settings import Settings

logger = logging.getLogger("mcp-caldav.app")

# ── Module-level state ──────────────────────────────────────────────

_settings: Settings | None = None
_rate_limiter: FixedWindowRateLimiter | None = None
_rate_limit_item = None
_redis_client = None

# Create the FastMCP sub-app once at module level so session_manager
# is accessible in the lifespan.
_mcp_http_app = mcp.streamable_http_app()


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
    _rate_limit_item = parse_rate_limit(_settings.rate_limit)
    async_redis_url = _settings.redis_url
    if not async_redis_url.startswith("async+"):
        async_redis_url = f"async+{async_redis_url}"
    storage = RedisStorage(async_redis_url, implementation="redispy")
    _rate_limiter = FixedWindowRateLimiter(storage)
    logger.info("Rate limiter configured: %s", _settings.rate_limit)

    # Start the FastMCP session manager task group — required for
    # handling incoming MCP requests over streamable HTTP.
    async with mcp.session_manager.run():
        logger.info("MCP session manager started")
        yield

    # Shutdown.
    await close_db()
    if _redis_client:
        await _redis_client.aclose()
    logger.info("Shutdown complete")


# ── Simple routes ───────────────────────────────────────────────────


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
            # MCP Streamable HTTP — FastMCP sub-app serves /mcp internally.
            # Openfang connects to http://caldav-mcp:8025/mcp
            Mount("/", app=_mcp_http_app),
        ],
    )


app = create_app()
