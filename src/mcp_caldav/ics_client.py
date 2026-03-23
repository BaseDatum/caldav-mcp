"""Read-only ICS feed client with Redis-backed caching.

Fetches ``.ics`` subscription URLs, parses them with the ``icalendar``
library, and caches the raw feed data in Redis with a configurable TTL
(default 5 minutes).  This allows the MCP server to scale horizontally —
any replica can serve cached data.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from icalendar import Calendar

logger = logging.getLogger("mcp-caldav.ics")

# Redis client — set by ``init_ics_cache()`` at startup.
_redis = None
_cache_ttl: int = 300  # seconds


def init_ics_cache(redis_client: Any, ttl_seconds: int = 300) -> None:
    """Inject the Redis client and TTL for ICS feed caching."""
    global _redis, _cache_ttl  # noqa: PLW0603
    _redis = redis_client
    _cache_ttl = ttl_seconds


def _cache_key(url: str) -> str:
    """Deterministic Redis key for an ICS feed URL."""
    import hashlib

    return f"ics_feed:{hashlib.sha256(url.encode()).hexdigest()}"


async def _fetch_ics(url: str) -> str:
    """Fetch an ICS feed, using Redis cache if available."""
    # Try cache first.
    if _redis is not None:
        cached = await _redis.get(_cache_key(url))
        if cached is not None:
            logger.debug("ICS cache hit for %s", url)
            return cached.decode() if isinstance(cached, bytes) else cached

    # Fetch from remote.
    # Convert webcal:// to https://
    fetch_url = url
    if fetch_url.startswith("webcal://"):
        fetch_url = "https://" + fetch_url[len("webcal://") :]

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(fetch_url)
        resp.raise_for_status()
        ics_data = resp.text

    # Store in cache.
    if _redis is not None:
        await _redis.set(_cache_key(url), ics_data, ex=_cache_ttl)
        logger.debug("ICS cached for %s (TTL=%ds)", url, _cache_ttl)

    return ics_data


def _parse_events(
    ics_data: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[dict[str, Any]]:
    """Parse VEVENT components from raw iCalendar data, optionally filtering by date range."""
    cal = Calendar.from_ical(ics_data)
    events: list[dict[str, Any]] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        try:
            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")

            if not dtstart:
                continue

            start_dt = dtstart.dt
            all_day = False
            if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
                start_dt = datetime.combine(
                    start_dt, datetime.min.time(), tzinfo=timezone.utc
                )
                all_day = True

            # Ensure timezone-aware for comparison.
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

            if dtend:
                end_dt = dtend.dt
                if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
                    end_dt = datetime.combine(
                        end_dt, datetime.max.time(), tzinfo=timezone.utc
                    )
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            else:
                end_dt = start_dt + timedelta(hours=1)

            # Date-range filter.
            if start_date and end_date:
                s = (
                    start_date
                    if start_date.tzinfo
                    else start_date.replace(tzinfo=timezone.utc)
                )
                e = (
                    end_date
                    if end_date.tzinfo
                    else end_date.replace(tzinfo=timezone.utc)
                )
                if end_dt < s or start_dt > e:
                    continue

            summary = component.get("SUMMARY")
            description = component.get("DESCRIPTION")
            location = component.get("LOCATION")
            uid = component.get("UID")

            # Parse categories.
            categories: list[str] = []
            cats = component.get("CATEGORIES")
            if cats:
                if hasattr(cats, "cats"):
                    categories = [str(c) for c in cats.cats]
                elif isinstance(cats, list):
                    for cat_group in cats:
                        if hasattr(cat_group, "cats"):
                            categories.extend(str(c) for c in cat_group.cats)
                        else:
                            categories.append(str(cat_group))
                else:
                    categories = [str(cats)]

            # Parse attendees.
            attendees: list[dict[str, str]] = []
            att_list = component.get("ATTENDEE", [])
            if not isinstance(att_list, list):
                att_list = [att_list]
            for att in att_list:
                email = str(att).replace("mailto:", "")
                status = "NEEDS-ACTION"
                if hasattr(att, "params"):
                    status = att.params.get("PARTSTAT", "NEEDS-ACTION")
                    if isinstance(status, list):
                        status = status[0] if status else "NEEDS-ACTION"
                attendees.append({"email": email, "status": str(status)})

            events.append(
                {
                    "uid": str(uid) if uid else "",
                    "title": str(summary) if summary else "",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "description": str(description) if description else "",
                    "location": str(location) if location else "",
                    "all_day": all_day,
                    "categories": categories,
                    "priority": None,
                    "recurrence": None,
                    "attendees": attendees,
                }
            )

        except Exception:
            logger.debug("Skipping unparseable VEVENT", exc_info=True)
            continue

    events.sort(key=lambda x: x["start"])
    return events


async def get_events(
    url: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch an ICS feed and return events, optionally filtered by date range."""
    ics_data = await _fetch_ics(url)
    return _parse_events(ics_data, start_date, end_date)


async def list_calendars(url: str) -> list[dict[str, Any]]:
    """ICS feeds are a single calendar — return a list of one."""
    return [{"name": "ICS Feed", "url": url, "source_type": "ics"}]


async def search_events(
    url: str,
    start_date: datetime,
    end_date: datetime,
    query: str | None = None,
    search_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search events in an ICS feed by text."""
    events = await get_events(url, start_date, end_date)

    if not query:
        return events

    if search_fields is None:
        search_fields = ["title", "description", "location", "attendees"]

    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    for event in events:
        match = False
        if "title" in search_fields and query_lower in event.get("title", "").lower():
            match = True
        elif (
            "description" in search_fields
            and query_lower in event.get("description", "").lower()
        ):
            match = True
        elif (
            "location" in search_fields
            and query_lower in event.get("location", "").lower()
        ):
            match = True
        elif "attendees" in search_fields:
            for att in event.get("attendees", []):
                if query_lower in att.get("email", "").lower():
                    match = True
                    break

        if match:
            results.append(event)

    return results
