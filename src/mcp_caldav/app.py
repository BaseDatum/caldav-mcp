"""FastAPI REST API server for service-to-service calendar queries.

Runs on port 8026.  The MCP server runs separately on port 8025.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from fastapi import FastAPI
from limits import parse as parse_rate_limit
from limits.aio.storage import RedisStorage
from limits.aio.strategies import FixedWindowRateLimiter

from .database import close_db, init_db
from .ics_client import init_ics_cache
from .rest_api import router as rest_router
from .settings import Settings

logger = logging.getLogger("mcp-caldav.app")

_settings: Settings | None = None
_rate_limiter: FixedWindowRateLimiter | None = None
_rate_limit_item = None
_redis_client = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _settings, _rate_limiter, _rate_limit_item, _redis_client  # noqa: PLW0603

    _settings = Settings()
    logger.setLevel(_settings.log_level.upper())

    await init_db(_settings)
    logger.info("Database initialised")

    _redis_client = aioredis.from_url(_settings.redis_url, decode_responses=False)
    init_ics_cache(_redis_client, _settings.ics_cache_ttl_seconds)
    logger.info("Redis connected (%s)", _settings.redis_url)

    _rate_limit_item = parse_rate_limit(_settings.rate_limit)
    async_redis_url = _settings.redis_url
    if not async_redis_url.startswith("async+"):
        async_redis_url = f"async+{async_redis_url}"
    storage = RedisStorage(async_redis_url, implementation="redispy")
    _rate_limiter = FixedWindowRateLimiter(storage)
    logger.info("Rate limiter configured: %s", _settings.rate_limit)

    yield

    await close_db()
    if _redis_client:
        await _redis_client.aclose()
    logger.info("Shutdown complete")


app = FastAPI(title="caldav-mcp REST API", lifespan=_lifespan)
app.include_router(rest_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mcp-caldav-api"}
