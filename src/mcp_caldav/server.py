"""Multi-tenant MCP server for CalDAV and ICS calendar integration.

Runs as a Streamable HTTP server.  On every request it reads the
authenticated user ID from a configurable HTTP header (default:
``X-Dialogue-User-Id``), looks up that user's calendar sources from
PostgreSQL, and dispatches the appropriate CalDAV or ICS client calls.

Rate limiting is enforced per-user via the ``limits`` library with a
Redis backing store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from .client import CalDAVClient
from .database import decrypt_password, get_user_sources
from .models import CalendarSource

logger = logging.getLogger("mcp-caldav.server")


# ── Tool definitions ────────────────────────────────────────────────


def _read_tools() -> list[Tool]:
    """Tools available for any source (read-only)."""
    return [
        Tool(
            name="calendar_list_sources",
            description="List all configured calendar sources with their capabilities (read-only vs read-write)",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="calendar_get_events",
            description="Get events from a specific source (or all sources) for a date range",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of the calendar source (omit to query all sources)",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in ISO format (defaults to today 00:00 UTC)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in ISO format (defaults to 7 days from start)",
                    },
                    "include_all_day": {
                        "type": "boolean",
                        "description": "Include all-day events (default: true)",
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="calendar_get_today_events",
            description="Get all events for today across all calendar sources",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of a specific source (omit for all)",
                    },
                },
            },
        ),
        Tool(
            name="calendar_get_week_events",
            description="Get all events for the current week across all sources",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of a specific source (omit for all)",
                    },
                    "start_from_today": {
                        "type": "boolean",
                        "description": "Start from today (true) or Monday (false)",
                        "default": True,
                    },
                },
            },
        ),
        Tool(
            name="calendar_search_events",
            description="Search events by text, attendees, or location",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of a specific source (omit for all)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query string",
                    },
                    "search_fields": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["title", "description", "location", "attendees"],
                        },
                        "description": "Fields to search (default: all)",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in ISO format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in ISO format",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        ),
        Tool(
            name="calendar_get_event_by_uid",
            description="Get a specific event by its UID",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of the source to search in",
                    },
                    "uid": {"type": "string", "description": "Event UID"},
                },
                "required": ["source_name", "uid"],
            },
        ),
    ]


def _write_tools() -> list[Tool]:
    """Tools only available for read-write CalDAV sources."""
    return [
        Tool(
            name="calendar_create_event",
            description="Create a new event on a read-write CalDAV calendar",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of the CalDAV source (must be read-write)",
                    },
                    "title": {"type": "string", "description": "Event title"},
                    "description": {
                        "type": "string",
                        "description": "Event description",
                        "default": "",
                    },
                    "location": {
                        "type": "string",
                        "description": "Event location",
                        "default": "",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start time ISO format (default: tomorrow 14:00 UTC)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time ISO format (uses duration_hours if omitted)",
                    },
                    "duration_hours": {"type": "number", "default": 1.0},
                    "calendar_index": {
                        "type": "integer",
                        "description": "Index of the calendar within this source (default: 0)",
                        "default": 0,
                    },
                    "reminders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "minutes_before": {"type": "integer"},
                                "action": {
                                    "type": "string",
                                    "enum": ["DISPLAY", "EMAIL", "AUDIO"],
                                },
                                "description": {"type": "string"},
                            },
                            "required": ["minutes_before", "action"],
                        },
                    },
                    "attendees": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "email": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "ACCEPTED",
                                                "DECLINED",
                                                "TENTATIVE",
                                                "NEEDS-ACTION",
                                            ],
                                        },
                                    },
                                    "required": ["email"],
                                },
                            ],
                        },
                    },
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 9},
                    "recurrence": {
                        "type": "object",
                        "properties": {
                            "frequency": {
                                "type": "string",
                                "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
                            },
                            "interval": {"type": "integer", "default": 1},
                            "count": {"type": "integer"},
                            "until": {"type": "string"},
                            "byday": {"type": "string"},
                            "bymonthday": {"type": "integer"},
                            "bymonth": {"type": "integer"},
                        },
                        "required": ["frequency"],
                    },
                },
                "required": ["source_name", "title"],
            },
        ),
        Tool(
            name="calendar_delete_event",
            description="Delete an event by UID from a read-write CalDAV calendar",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of the CalDAV source",
                    },
                    "uid": {"type": "string", "description": "Event UID to delete"},
                    "calendar_index": {"type": "integer", "default": 0},
                },
                "required": ["source_name", "uid"],
            },
        ),
    ]


# ── Helper: connect to a CalDAV source ──────────────────────────────


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


def _json_text(data: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False))


# ── MCP Server factory ──────────────────────────────────────────────


def create_mcp_server() -> Server:
    """Build the MCP ``Server`` instance with tool handlers.

    This function registers tools and the ``call_tool`` handler.
    The server itself is stateless — all per-user state is loaded on
    each request from the database.
    """
    app = Server("mcp-caldav")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        """Return the full set of tools.

        We always advertise both read and write tools.  Write tool calls
        against read-only sources return a clear error message rather
        than hiding the tools (which would require a per-session DB
        lookup at list_tools time).
        """
        return _read_tools() + _write_tools()

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
        """Dispatch a tool call.

        The ``user_id`` is extracted from the MCP request context by the
        transport layer (see ``app.py``) and threaded through the server's
        ``request_context``.
        """
        # Retrieve user_id from request context (set by transport layer).
        ctx = app.request_context
        user_id: str | None = None
        if ctx and hasattr(ctx, "lifespan_context") and ctx.lifespan_context:
            user_id = getattr(ctx.lifespan_context, "user_id", None)

        if not user_id:
            return [
                _json_text({"error": "No authenticated user — missing user ID header"})
            ]

        try:
            sources = await get_user_sources(user_id)
        except Exception as e:
            logger.error("Failed to load calendar sources for user %s: %s", user_id, e)
            return [_json_text({"error": f"Failed to load calendar sources: {e}"})]

        if not sources and name != "calendar_list_sources":
            return [
                _json_text(
                    {
                        "error": "No calendar sources configured. Add sources in Settings > Integrations."
                    }
                )
            ]

        try:
            return await _dispatch(name, arguments, sources)
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e, exc_info=True)
            return [_json_text({"error": str(e)})]

    return app


# ── Tool dispatch ───────────────────────────────────────────────────


async def _dispatch(
    name: str, args: dict[str, Any], sources: list[CalendarSource]
) -> Sequence[TextContent]:
    """Route a tool call to the correct handler."""
    from . import ics_client

    source_name: str | None = args.get("source_name")

    # ── calendar_list_sources ───────────────────────────────────────
    if name == "calendar_list_sources":
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
        return [_json_text(info)]

    # ── Helper to filter sources by name ────────────────────────────
    def _get_sources(name_filter: str | None) -> list[CalendarSource]:
        if name_filter:
            matched = [s for s in sources if s.name == name_filter]
            if not matched:
                raise ValueError(
                    f"No calendar source named '{name_filter}'. Available: {[s.name for s in sources]}"
                )
            return matched
        return sources

    # ── calendar_get_events ─────────────────────────────────────────
    if name == "calendar_get_events":
        target_sources = _get_sources(source_name)
        start = _parse_iso(args.get("start_date"))
        end = _parse_iso(args.get("end_date"))
        include_all_day = args.get("include_all_day", True)

        all_events: list[dict[str, Any]] = []
        for src in target_sources:
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
        return [_json_text(all_events)]

    # ── calendar_get_today_events ───────────────────────────────────
    if name == "calendar_get_today_events":
        target_sources = _get_sources(source_name)
        all_events = []
        for src in target_sources:
            try:
                if src.source_type == "ics":
                    from datetime import datetime as dt, timedelta, timezone

                    now = dt.now(tz=timezone.utc)
                    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    events = await ics_client.get_events(
                        src.url, start, start + timedelta(days=1)
                    )
                else:
                    client = _connect_caldav(src)
                    events = client.get_today_events()
                for ev in events:
                    ev["source"] = src.name
                all_events.extend(events)
            except Exception as e:
                all_events.append({"source": src.name, "error": str(e)})
        all_events.sort(key=lambda x: x.get("start", ""))
        return [_json_text(all_events)]

    # ── calendar_get_week_events ────────────────────────────────────
    if name == "calendar_get_week_events":
        target_sources = _get_sources(source_name)
        start_from_today = args.get("start_from_today", True)
        all_events = []
        for src in target_sources:
            try:
                if src.source_type == "ics":
                    from datetime import datetime as dt, timedelta, timezone

                    now = dt.now(tz=timezone.utc)
                    if start_from_today:
                        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    else:
                        start = (now - timedelta(days=now.weekday())).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                    events = await ics_client.get_events(
                        src.url, start, start + timedelta(days=7)
                    )
                else:
                    client = _connect_caldav(src)
                    events = client.get_week_events(start_from_today=start_from_today)
                for ev in events:
                    ev["source"] = src.name
                all_events.extend(events)
            except Exception as e:
                all_events.append({"source": src.name, "error": str(e)})
        all_events.sort(key=lambda x: x.get("start", ""))
        return [_json_text(all_events)]

    # ── calendar_search_events ──────────────────────────────────────
    if name == "calendar_search_events":
        target_sources = _get_sources(source_name)
        start = _parse_iso(args.get("start_date"))
        end = _parse_iso(args.get("end_date"))
        query = args.get("query")
        search_fields = args.get("search_fields")

        if not start or not end:
            return [_json_text({"error": "start_date and end_date are required"})]

        all_events = []
        for src in target_sources:
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
        return [_json_text(all_events)]

    # ── calendar_get_event_by_uid ───────────────────────────────────
    if name == "calendar_get_event_by_uid":
        if not source_name:
            return [
                _json_text({"error": "source_name is required for get_event_by_uid"})
            ]
        target = _get_sources(source_name)[0]
        uid = args.get("uid", "")
        if target.source_type == "ics":
            return [
                _json_text(
                    {
                        "error": "get_event_by_uid is not supported for ICS feeds — use calendar_search_events instead"
                    }
                )
            ]
        client = _connect_caldav(target)
        event = client.get_event_by_uid(uid)
        if event:
            event["source"] = target.name  # type: ignore[index]
            return [_json_text(event)]
        return [
            _json_text({"error": f"Event {uid} not found in source '{source_name}'"})
        ]

    # ── calendar_create_event ───────────────────────────────────────
    if name == "calendar_create_event":
        if not source_name:
            return [_json_text({"error": "source_name is required for create_event"})]
        target = _get_sources(source_name)[0]
        if target.source_type == "ics":
            return [
                _json_text(
                    {
                        "error": f"Source '{source_name}' is an ICS subscription (read-only). Cannot create events."
                    }
                )
            ]
        if target.capability != "readwrite":
            return [
                _json_text(
                    {
                        "error": f"Source '{source_name}' is read-only. Cannot create events."
                    }
                )
            ]

        client = _connect_caldav(target)
        result = client.create_event(
            calendar_index=args.get("calendar_index", 0),
            title=args.get("title", "Event"),
            description=args.get("description", ""),
            location=args.get("location", ""),
            start_time=_parse_iso(args.get("start_time")),
            end_time=_parse_iso(args.get("end_time")),
            duration_hours=args.get("duration_hours", 1.0),
            reminders=args.get("reminders"),
            attendees=args.get("attendees"),
            categories=args.get("categories"),
            priority=args.get("priority"),
            recurrence=args.get("recurrence"),
        )
        return [_json_text(result)]

    # ── calendar_delete_event ───────────────────────────────────────
    if name == "calendar_delete_event":
        if not source_name:
            return [_json_text({"error": "source_name is required for delete_event"})]
        target = _get_sources(source_name)[0]
        if target.source_type == "ics":
            return [
                _json_text(
                    {
                        "error": f"Source '{source_name}' is an ICS subscription (read-only). Cannot delete events."
                    }
                )
            ]
        if target.capability != "readwrite":
            return [
                _json_text(
                    {
                        "error": f"Source '{source_name}' is read-only. Cannot delete events."
                    }
                )
            ]

        client = _connect_caldav(target)
        result = client.delete_event(
            uid=args["uid"], calendar_index=args.get("calendar_index", 0)
        )
        return [_json_text(result)]

    return [_json_text({"error": f"Unknown tool: {name}"})]
