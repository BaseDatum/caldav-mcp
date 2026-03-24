"""Multi-tenant MCP server for CalDAV and ICS calendar integration.

Uses FastMCP in stateless HTTP mode.  On every tool call the user ID
is extracted from the MCP request headers (configurable, default:
``X-Dialogue-User-Id``), calendar sources are loaded from PostgreSQL,
and the appropriate CalDAV or ICS client call is dispatched.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import CalDAVClient
from .database import decrypt_password, get_user_sources
from .models import CalendarSource
from .settings import Settings

logger = logging.getLogger("mcp-caldav.server")


# ── Helpers ─────────────────────────────────────────────────────────


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


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


async def _get_user_id(ctx: Context) -> str:
    """Extract user ID from the MCP request headers."""
    settings = Settings()
    header_name = settings.user_id_header.lower()

    # FastMCP exposes the transport request's headers via the session's
    # request context.  For stateless HTTP, each request is independent.
    request_context = ctx.request_context
    if request_context and hasattr(request_context, "request"):
        req = request_context.request
        if hasattr(req, "headers"):
            user_id = req.headers.get(header_name)
            if user_id:
                return user_id

    # Fallback: check the meta headers on the session.
    if request_context and hasattr(request_context, "meta") and request_context.meta:
        meta = request_context.meta
        if hasattr(meta, "headers") and meta.headers:
            for k, v in meta.headers.items():
                if k.lower() == header_name:
                    return v

    raise ValueError(f"Missing {settings.user_id_header} header — cannot identify user")


async def _load_sources(ctx: Context) -> tuple[str, list[CalendarSource]]:
    """Load calendar sources for the current user."""
    user_id = await _get_user_id(ctx)
    sources = await get_user_sources(user_id)
    return user_id, sources


# ── FastMCP server ──────────────────────────────────────────────────


mcp = FastMCP(
    "mcp-caldav",
    stateless_http=True,
)

# ASGI app for uvicorn — just the MCP server on its own port.
mcp_asgi_app = mcp.streamable_http_app()


# ── Read tools ──────────────────────────────────────────────────────


@mcp.tool()
async def calendar_list_sources(ctx: Context) -> str:
    """List all configured calendar sources with their capabilities (read-only vs read-write)."""
    _, sources = await _load_sources(ctx)
    info = [
        {
            "name": s.name,
            "type": s.source_type,
            "url": s.url,
            "capability": s.capability,
            "enabled": s.enabled,
        }
        for s in sources
    ]
    return _fmt(info)


@mcp.tool()
async def calendar_get_events(
    ctx: Context,
    source_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_all_day: bool = True,
) -> str:
    """Get events from a specific source (or all sources) for a date range.

    Args:
        source_name: Name of the calendar source (omit to query all sources)
        start_date: Start date in ISO format (defaults to today 00:00 UTC)
        end_date: End date in ISO format (defaults to 7 days from start)
        include_all_day: Include all-day events (default: true)
    """
    from . import ics_client

    _, sources = await _load_sources(ctx)
    target = [s for s in sources if s.name == source_name] if source_name else sources
    if source_name and not target:
        return _fmt(
            {
                "error": f"No source named '{source_name}'. Available: {[s.name for s in sources]}"
            }
        )

    start = _parse_iso(start_date)
    end = _parse_iso(end_date)

    all_events: list[dict[str, Any]] = []
    for src in target:
        try:
            if src.source_type == "ics":
                events = await ics_client.get_events(src.url, start, end)
            else:
                client = _connect_caldav(src)
                events = client.get_events(
                    start_date=start, end_date=end, include_all_day=include_all_day
                )
            for ev in events:
                ev["source"] = src.name
            all_events.extend(events)
        except Exception as e:
            all_events.append({"source": src.name, "error": str(e)})

    all_events.sort(key=lambda x: x.get("start", ""))
    return _fmt(all_events)


@mcp.tool()
async def calendar_get_today_events(
    ctx: Context, source_name: str | None = None
) -> str:
    """Get all events for today across all calendar sources.

    Args:
        source_name: Name of a specific source (omit for all)
    """
    from . import ics_client

    _, sources = await _load_sources(ctx)
    target = [s for s in sources if s.name == source_name] if source_name else sources

    now = datetime.now(tz=timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    all_events: list[dict[str, Any]] = []
    for src in target:
        try:
            if src.source_type == "ics":
                events = await ics_client.get_events(src.url, start, end)
            else:
                client = _connect_caldav(src)
                events = client.get_today_events()
            for ev in events:
                ev["source"] = src.name
            all_events.extend(events)
        except Exception as e:
            all_events.append({"source": src.name, "error": str(e)})

    all_events.sort(key=lambda x: x.get("start", ""))
    return _fmt(all_events)


@mcp.tool()
async def calendar_get_week_events(
    ctx: Context, source_name: str | None = None, start_from_today: bool = True
) -> str:
    """Get all events for the current week across all sources.

    Args:
        source_name: Name of a specific source (omit for all)
        start_from_today: Start from today (true) or Monday (false)
    """
    from . import ics_client

    _, sources = await _load_sources(ctx)
    target = [s for s in sources if s.name == source_name] if source_name else sources

    now = datetime.now(tz=timezone.utc)
    if start_from_today:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    end = start + timedelta(days=7)

    all_events: list[dict[str, Any]] = []
    for src in target:
        try:
            if src.source_type == "ics":
                events = await ics_client.get_events(src.url, start, end)
            else:
                client = _connect_caldav(src)
                events = client.get_week_events(start_from_today=start_from_today)
            for ev in events:
                ev["source"] = src.name
            all_events.extend(events)
        except Exception as e:
            all_events.append({"source": src.name, "error": str(e)})

    all_events.sort(key=lambda x: x.get("start", ""))
    return _fmt(all_events)


@mcp.tool()
async def calendar_search_events(
    ctx: Context,
    start_date: str,
    end_date: str,
    source_name: str | None = None,
    query: str | None = None,
    search_fields: list[str] | None = None,
) -> str:
    """Search events by text, attendees, or location.

    Args:
        start_date: Start date in ISO format
        end_date: End date in ISO format
        source_name: Name of a specific source (omit for all)
        query: Search query string
        search_fields: Fields to search: title, description, location, attendees (default: all)
    """
    from . import ics_client

    _, sources = await _load_sources(ctx)
    target = [s for s in sources if s.name == source_name] if source_name else sources

    start = _parse_iso(start_date)
    end = _parse_iso(end_date)
    if not start or not end:
        return _fmt({"error": "start_date and end_date are required"})

    all_events: list[dict[str, Any]] = []
    for src in target:
        try:
            if src.source_type == "ics":
                events = await ics_client.search_events(
                    src.url, start, end, query, search_fields
                )
            else:
                client = _connect_caldav(src)
                events = client.search_events(
                    query=query,
                    search_fields=search_fields,
                    start_date=start,
                    end_date=end,
                )
            for ev in events:
                ev["source"] = src.name
            all_events.extend(events)
        except Exception as e:
            all_events.append({"source": src.name, "error": str(e)})

    return _fmt(all_events)


@mcp.tool()
async def calendar_get_event_by_uid(ctx: Context, source_name: str, uid: str) -> str:
    """Get a specific event by its UID.

    Args:
        source_name: Name of the source to search in
        uid: Event UID
    """
    _, sources = await _load_sources(ctx)
    matched = [s for s in sources if s.name == source_name]
    if not matched:
        return _fmt({"error": f"No source named '{source_name}'"})
    target = matched[0]

    if target.source_type == "ics":
        return _fmt(
            {
                "error": "get_event_by_uid is not supported for ICS feeds — use calendar_search_events instead"
            }
        )

    client = _connect_caldav(target)
    event = client.get_event_by_uid(uid)
    if event:
        event["source"] = target.name  # type: ignore[index]
        return _fmt(event)
    return _fmt({"error": f"Event {uid} not found in source '{source_name}'"})


# ── Write tools ─────────────────────────────────────────────────────


@mcp.tool()
async def calendar_create_event(
    ctx: Context,
    source_name: str,
    title: str,
    description: str = "",
    location: str = "",
    start_time: str | None = None,
    end_time: str | None = None,
    duration_hours: float = 1.0,
    calendar_index: int = 0,
    reminders: list[dict[str, Any]] | None = None,
    attendees: list[Any] | None = None,
    categories: list[str] | None = None,
    priority: int | None = None,
    recurrence: dict[str, Any] | None = None,
) -> str:
    """Create a new event on a read-write CalDAV calendar.

    Args:
        source_name: Name of the CalDAV source (must be read-write)
        title: Event title
        description: Event description
        location: Event location
        start_time: Start time ISO format (default: tomorrow 14:00 UTC)
        end_time: End time ISO format (uses duration_hours if omitted)
        duration_hours: Duration in hours if end_time not set (default: 1.0)
        calendar_index: Index of the calendar within this source (default: 0)
        reminders: List of reminders with minutes_before and action
        attendees: List of attendee emails or objects with email/status
        categories: List of event categories
        priority: Event priority (0-9)
        recurrence: Recurrence rule with frequency, interval, count, until, etc.
    """
    _, sources = await _load_sources(ctx)
    matched = [s for s in sources if s.name == source_name]
    if not matched:
        return _fmt({"error": f"No source named '{source_name}'"})
    target = matched[0]

    if target.source_type == "ics":
        return _fmt(
            {
                "error": f"Source '{source_name}' is an ICS subscription (read-only). Cannot create events."
            }
        )
    if target.capability != "readwrite":
        return _fmt(
            {"error": f"Source '{source_name}' is read-only. Cannot create events."}
        )

    client = _connect_caldav(target)
    result = client.create_event(
        calendar_index=calendar_index,
        title=title,
        description=description,
        location=location,
        start_time=_parse_iso(start_time),
        end_time=_parse_iso(end_time),
        duration_hours=duration_hours,
        reminders=reminders,
        attendees=attendees,
        categories=categories,
        priority=priority,
        recurrence=recurrence,
    )
    return _fmt(result)


@mcp.tool()
async def calendar_delete_event(
    ctx: Context, source_name: str, uid: str, calendar_index: int = 0
) -> str:
    """Delete an event by UID from a read-write CalDAV calendar.

    Args:
        source_name: Name of the CalDAV source
        uid: Event UID to delete
        calendar_index: Calendar index within the source (default: 0)
    """
    _, sources = await _load_sources(ctx)
    matched = [s for s in sources if s.name == source_name]
    if not matched:
        return _fmt({"error": f"No source named '{source_name}'"})
    target = matched[0]

    if target.source_type == "ics":
        return _fmt(
            {
                "error": f"Source '{source_name}' is an ICS subscription (read-only). Cannot delete events."
            }
        )
    if target.capability != "readwrite":
        return _fmt(
            {"error": f"Source '{source_name}' is read-only. Cannot delete events."}
        )

    client = _connect_caldav(target)
    result = client.delete_event(uid=uid, calendar_index=calendar_index)
    return _fmt(result)
