"""MCP CalDAV Server — multi-tenant calendar integration for MCP.

Two processes:
  - Port 8025: FastMCP streamable-http server (MCP tools)
  - Port 8026: FastAPI REST server (service-to-service event queries)
"""

from __future__ import annotations

import logging
import os

import click
from dotenv import load_dotenv

__version__ = "2.0.0"

_log_level = logging.WARNING
if os.getenv("MCP_VERBOSE", "").lower() in ("true", "1", "yes"):
    _log_level = logging.DEBUG

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp-caldav")


@click.command()
@click.option("-v", "--verbose", count=True, help="Increase verbosity")
@click.option(
    "--env-file", type=click.Path(exists=True, dir_okay=False), help="Path to .env file"
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "streamable-http"]),
    default="streamable-http",
)
@click.option("--host", default="0.0.0.0", help="Listen host")
@click.option("--port", default=8025, type=int, help="MCP server port")
@click.option("--api-port", default=8026, type=int, help="REST API port")
def main(
    verbose: int,
    env_file: str | None,
    transport: str,
    host: str,
    port: int,
    api_port: int,
) -> None:
    """MCP CalDAV Server — multi-tenant calendar integration."""
    if verbose == 1:
        logging.getLogger("mcp-caldav").setLevel(logging.INFO)
    elif verbose >= 2:
        logging.getLogger("mcp-caldav").setLevel(logging.DEBUG)

    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    if transport == "stdio":
        from .server import mcp

        mcp.run(transport="stdio")
    else:
        import asyncio

        asyncio.run(_run_both(host, port, api_port))


async def _run_both(host: str, mcp_port: int, api_port: int) -> None:
    """Run both the MCP server and the REST API server concurrently."""
    import uvicorn

    from .database import init_db, close_db
    from .ics_client import init_ics_cache
    from .settings import Settings
    import redis.asyncio as aioredis

    # Shared init (DB + Redis) before starting either server.
    settings = Settings()
    await init_db(settings)
    logger.info("Database initialised")

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    init_ics_cache(redis_client, settings.ics_cache_ttl_seconds)
    logger.info("Redis connected (%s)", settings.redis_url)

    mcp_config = uvicorn.Config(
        "mcp_caldav.server:mcp_asgi_app",
        host=host,
        port=mcp_port,
        log_level="info",
    )
    api_config = uvicorn.Config(
        "mcp_caldav.app:app",
        host=host,
        port=api_port,
        log_level="info",
    )

    mcp_server = uvicorn.Server(mcp_config)
    api_server = uvicorn.Server(api_config)

    import asyncio

    await asyncio.gather(mcp_server.serve(), api_server.serve())


__all__ = ["__version__", "main"]

if __name__ == "__main__":
    main()
