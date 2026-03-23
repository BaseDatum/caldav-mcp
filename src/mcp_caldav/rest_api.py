"""REST API endpoints for direct event queries (non-MCP).

These endpoints are used by the Dialogue api-server to fetch events
for the unified calendar view.  They are NOT the MCP protocol —
they are plain JSON REST endpoints for service-to-service calls.

All endpoints require the user ID header (configurable, default:
``X-Dialogue-User-Id``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import ics_client
from .client import CalDAVClient
from .database import decrypt_password, get_user_sources
from .models import CalendarSource

logger = logging.getLogger("mcp-caldav.rest")


def _connect_caldav(source: CalendarSource) -> CalDAVClient:
    """Create and connect a CalDAVClient for a database source row."""
    password = decrypt_password(source.encrypted_password)
    if not password:
        raise RuntimeError(f"Cannot decrypt password for source '{source.name}'")
    client = CalDAVClient(
        url=source.url,
        username=source.username or "",
        password=password,
    )
    client.connect()
    return client


async def get_events(request: Request) -> Response:
    """GET /api/events?time_min=...&time_max=...

    Returns events from all CalDAV/ICS sources for the authenticated user.

    Query params:
        time_min  — ISO 8601 start (default: today 00:00 UTC)
        time_max  — ISO 8601 end   (default: 7 days from time_min)
        query     — optional free-text search filter
    """
    from .app import _settings

    assert _settings is not None

    user_id = request.headers.get(_settings.user_id_header)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": f"Missing {_settings.user_id_header} header"},
        )

    # Parse query params.
    time_min_str = request.query_params.get("time_min")
    time_max_str = request.query_params.get("time_max")
    query = request.query_params.get("query")

    now = datetime.now(tz=timezone.utc)
    if time_min_str:
        time_min = datetime.fromisoformat(time_min_str.replace("Z", "+00:00"))
    else:
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if time_max_str:
        time_max = datetime.fromisoformat(time_max_str.replace("Z", "+00:00"))
    else:
        time_max = time_min + timedelta(days=7)

    # Load user's calendar sources.
    try:
        sources = await get_user_sources(user_id)
    except Exception as e:
        logger.error("Failed to load sources for user %s: %s", user_id, e)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load calendar sources: {e}"},
        )

    # Gather events from all sources.
    all_events: list[dict[str, Any]] = []

    for src in sources:
        try:
            if src.source_type == "ics":
                events = await ics_client.get_events(src.url, time_min, time_max)
            else:
                client = _connect_caldav(src)
                if query:
                    events = client.search_events(
                        start_date=time_min,
                        end_date=time_max,
                        query=query,
                    )
                else:
                    events = client.get_events(start_date=time_min, end_date=time_max)

            for ev in events:
                ev["source"] = src.name
                ev["source_type"] = src.source_type
                ev["provider"] = "caldav-mcp"
            all_events.extend(events)
        except Exception as e:
            logger.warning("Failed to fetch events from source '%s': %s", src.name, e)
            all_events.append(
                {
                    "source": src.name,
                    "source_type": src.source_type,
                    "provider": "caldav-mcp",
                    "error": str(e),
                }
            )

    # If search query was given, filter ICS results client-side.
    if query and any(src.source_type == "ics" for src in sources):
        query_lower = query.lower()
        filtered: list[dict[str, Any]] = []
        for ev in all_events:
            if "error" in ev:
                filtered.append(ev)
                continue
            if (
                query_lower in ev.get("title", "").lower()
                or query_lower in ev.get("description", "").lower()
                or query_lower in ev.get("location", "").lower()
            ):
                filtered.append(ev)
                continue
            # Keep CalDAV results (already filtered server-side).
            if ev.get("source_type") == "caldav":
                filtered.append(ev)
        all_events = filtered

    # Sort by start time.
    all_events.sort(key=lambda x: x.get("start", ""))

    return JSONResponse(content=all_events)


async def list_sources(request: Request) -> Response:
    """GET /api/sources — list calendar sources for the authenticated user."""
    from .app import _settings

    assert _settings is not None

    user_id = request.headers.get(_settings.user_id_header)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": f"Missing {_settings.user_id_header} header"},
        )

    try:
        sources = await get_user_sources(user_id)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    return JSONResponse(
        content=[
            {
                "id": str(src.id),
                "name": src.name,
                "source_type": src.source_type,
                "url": src.url,
                "capability": src.capability,
                "enabled": src.enabled,
            }
            for src in sources
        ]
    )
