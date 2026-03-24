"""REST API endpoints for direct event queries (non-MCP).

Used by the Dialogue api-server to fetch events for the unified calendar view.
All endpoints require the user ID header (configurable, default: X-Dialogue-User-Id).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from . import ics_client
from .client import CalDAVClient
from .database import decrypt_password, get_user_sources
from .models import CalendarSource
from .settings import Settings

logger = logging.getLogger("mcp-caldav.rest")

router = APIRouter(prefix="/api")

_settings_cache: Settings | None = None


def _get_settings() -> Settings:
    global _settings_cache  # noqa: PLW0603
    if _settings_cache is None:
        _settings_cache = Settings()
    return _settings_cache


def _connect_caldav(source: CalendarSource) -> CalDAVClient:
    password = decrypt_password(source.encrypted_password)
    if not password:
        raise RuntimeError(f"Cannot decrypt password for source '{source.name}'")
    client = CalDAVClient(
        url=source.url, username=source.username or "", password=password
    )
    client.connect()
    return client


async def _resolve_user_id(x_dialogue_user_id: str | None = Header(None)) -> str:
    """Extract user ID from the configurable header."""
    settings = _get_settings()
    # FastAPI normalises header names to lowercase with underscores.
    # X-Dialogue-User-Id → x_dialogue_user_id
    if x_dialogue_user_id:
        return x_dialogue_user_id
    raise HTTPException(
        status_code=401, detail=f"Missing {settings.user_id_header} header"
    )


@router.get("/events")
async def get_events(
    x_dialogue_user_id: str | None = Header(None),
    time_min: str | None = Query(None),
    time_max: str | None = Query(None),
    query: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Return events from all CalDAV/ICS sources for the authenticated user."""
    user_id = await _resolve_user_id(x_dialogue_user_id)

    now = datetime.now(tz=timezone.utc)
    start = (
        datetime.fromisoformat(time_min.replace("Z", "+00:00"))
        if time_min
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    end = (
        datetime.fromisoformat(time_max.replace("Z", "+00:00"))
        if time_max
        else start + timedelta(days=7)
    )

    try:
        sources = await get_user_sources(user_id)
    except Exception as e:
        logger.error("Failed to load sources for user %s: %s", user_id, e)
        raise HTTPException(
            status_code=500, detail=f"Failed to load calendar sources: {e}"
        )

    all_events: list[dict[str, Any]] = []
    for src in sources:
        try:
            if src.source_type == "ics":
                events = await ics_client.get_events(src.url, start, end)
            else:
                client = _connect_caldav(src)
                if query:
                    events = client.search_events(
                        start_date=start, end_date=end, query=query
                    )
                else:
                    events = client.get_events(start_date=start, end_date=end)

            for ev in events:
                ev["source"] = src.name
                ev["source_type"] = src.source_type
                ev["provider"] = "caldav-mcp"
            all_events.extend(events)
        except Exception as e:
            logger.warning("Failed to fetch from source '%s': %s", src.name, e)
            all_events.append(
                {
                    "source": src.name,
                    "source_type": src.source_type,
                    "provider": "caldav-mcp",
                    "error": str(e),
                }
            )

    all_events.sort(key=lambda x: x.get("start", ""))
    return all_events


@router.get("/sources")
async def list_sources(
    x_dialogue_user_id: str | None = Header(None),
) -> list[dict[str, Any]]:
    """List calendar sources for the authenticated user."""
    user_id = await _resolve_user_id(x_dialogue_user_id)

    try:
        sources = await get_user_sources(user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return [
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
