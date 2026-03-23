"""MCP CalDAV Server — multi-tenant calendar integration for MCP.

Supports CalDAV (read/write) and ICS feeds (read-only) with per-user
credential lookup from PostgreSQL, Redis-backed caching, and rate limiting.
"""

from __future__ import annotations

import logging
import os

import click
from dotenv import load_dotenv

__version__ = "2.0.0"

# Logging setup.
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
    help="Transport type",
)
@click.option("--host", default="0.0.0.0", help="Listen host")
@click.option("--port", default=8025, type=int, help="Listen port")
def main(
    verbose: int,
    env_file: str | None,
    transport: str,
    host: str,
    port: int,
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

    if transport == "streamable-http":
        import uvicorn

        uvicorn.run(
            "mcp_caldav.app:app",
            host=host,
            port=port,
            log_level="info",
        )
    else:
        # Legacy stdio mode (single-user, env-var config).
        import asyncio

        asyncio.run(_run_stdio())


async def _run_stdio() -> None:
    """Run in stdio mode for local/dev use (single-user, env-var credentials)."""
    from mcp.server.stdio import stdio_server

    from .server import create_mcp_server

    mcp_server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream, write_stream, mcp_server.create_initialization_options()
        )


__all__ = ["__version__", "main"]

if __name__ == "__main__":
    main()
