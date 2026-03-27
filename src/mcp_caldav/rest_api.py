"""REST API endpoints for direct event queries (non-MCP).

Used by the Dialogue api-server to fetch events for the unified calendar view.
All endpoints require an OpenBao Vault token (Authorization: Bearer header).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from . import ics_client
from .client import CalDAVClient
from .database import decrypt_password, get_user_sources
from .models import CalendarSource
from .settings import Settings

# Lazy-initialised MCP auth validator singleton.
_rest_validator: Any = None

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


async def _resolve_user_id(request: Request) -> str:
    """Validate the agent's OpenBao Vault token and extract user_id."""
    from shared_mcp_auth import MCPAuthSettings, MCPAuthValidator
    from shared_mcp_auth.validator import AuthError

    global _rest_validator  # noqa: PLW0603
    if _rest_validator is None:
        _rest_validator = MCPAuthValidator.from_settings(MCPAuthSettings())

    try:
        return _rest_validator.extract_user_id_from_request(
            request.headers.get("authorization"),
        )
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/events")
async def get_events(
    request: Request,
    time_min: str | None = Query(None),
    time_max: str | None = Query(None),
    query: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Return events from all CalDAV/ICS sources for the authenticated user."""
    user_id = await _resolve_user_id(request)

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
    request: Request,
) -> list[dict[str, Any]]:
    """List calendar sources for the authenticated user."""
    user_id = await _resolve_user_id(request)

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
