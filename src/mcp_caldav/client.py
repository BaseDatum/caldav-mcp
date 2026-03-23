"""CalDAV client wrapper — updated for caldav library v3.x.

Key v3 migration changes applied:
* ``DAVClient`` → ``get_davclient()`` factory.
* ``principal.calendars()`` → ``principal.get_calendars()``.
* ``calendar.date_search()`` → ``calendar.search()``.
* ``calendar.save_event()`` → ``calendar.add_event()``.
* ``.icalendar_component`` → ``.get_icalendar_component()``.
* Built-in ``rate_limit_handle=True`` for automatic Retry-After.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

logger = logging.getLogger("mcp-caldav.client")

try:
    import caldav
    from caldav import get_davclient
except ImportError as err:
    raise ImportError(
        "caldav library is not installed. Install with: pip install 'caldav~=3.1.0'"
    ) from err


# ── Type definitions ────────────────────────────────────────────────


class CalendarInfo(TypedDict):
    index: int
    name: str
    url: str


class EventAttendee(TypedDict, total=False):
    email: str
    status: str
    name: str


class EventRecord(TypedDict, total=False):
    uid: str
    title: str
    start: str
    end: str
    description: str
    location: str
    all_day: bool
    categories: list[str]
    priority: int | None
    recurrence: str | None
    attendees: list[EventAttendee]


class EventCreationResult(TypedDict):
    success: bool
    uid: str
    title: str
    start_time: str
    end_time: str
    calendar: str


class EventDeletionResult(TypedDict):
    success: bool
    uid: str
    message: str


AttendeeInput = EventAttendee | str


# ── iCalendar formatting helpers ───────────────────────────────────


def _escape_ical_text(value: str | Any) -> str:
    if not isinstance(value, str):
        value = str(value)
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _format_rrule(recurrence: dict[str, Any]) -> str:
    if not recurrence:
        return ""

    frequency = recurrence.get("frequency", "DAILY").upper()
    if frequency not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise ValueError(f"Invalid frequency: {frequency}")

    parts = [f"FREQ={frequency}"]

    interval = recurrence.get("interval", 1)
    if interval > 1:
        parts.append(f"INTERVAL={interval}")

    count = recurrence.get("count")
    if count:
        parts.append(f"COUNT={count}")

    until = recurrence.get("until")
    if until:
        if isinstance(until, datetime):
            parts.append(f"UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}")
        elif isinstance(until, date):
            parts.append(f"UNTIL={until.strftime('%Y%m%d')}")
        else:
            parts.append(f"UNTIL={until}")

    for key in ("byday", "bymonthday", "bymonth"):
        val = recurrence.get(key)
        if val:
            parts.append(f"{key.upper()}={val}")

    return f"RRULE:{';'.join(parts)}"


def _format_categories(categories: list[str]) -> str:
    if not categories:
        return ""
    escaped = [c.replace(",", "\\,").replace(";", "\\;") for c in categories]
    return f"CATEGORIES:{','.join(escaped)}"


def _format_attendees(attendees: list[AttendeeInput]) -> str:
    if not attendees:
        return ""
    lines: list[str] = []
    for att in attendees:
        if isinstance(att, str):
            email, status, display = att.strip(), None, att.strip()
        elif isinstance(att, dict):
            email = att.get("email", "").strip()
            status = att.get("status", "").upper() or None
            display = att.get("name", email)
        else:
            continue
        if "@" not in email:
            continue
        cn = _escape_ical_text(display or email)
        params = ["RSVP=TRUE", f"CN={cn}"]
        if status and status in ("ACCEPTED", "DECLINED", "TENTATIVE", "NEEDS-ACTION"):
            params.append(f"PARTSTAT={status}")
        lines.append(f"ATTENDEE;{';'.join(params)}:mailto:{email}")
    return "\n".join(lines) + "\n" if lines else ""


# ── Parsing helpers ─────────────────────────────────────────────────


def _parse_categories(cats: Any) -> list[str]:
    if not cats:
        return []
    categories: list[str] = []
    try:
        if hasattr(cats, "cats"):
            for cat in cats.cats:
                categories.append(str(cat.value if hasattr(cat, "value") else cat))
        elif hasattr(cats, "value"):
            val = cats.value
            if isinstance(val, bytes):
                val = val.decode()
            categories = [c.strip() for c in str(val).split(",")]
        elif isinstance(cats, list):
            for cat in cats:
                if hasattr(cat, "value"):
                    v = cat.value
                    if isinstance(v, bytes):
                        v = v.decode()
                    categories.append(str(v))
                else:
                    categories.append(str(cat))
        else:
            cat_str = cats.decode() if isinstance(cats, bytes) else str(cats)
            categories = [c.strip() for c in cat_str.split(",")]
    except Exception:
        try:
            categories = [str(cats)]
        except Exception:
            categories = []
    return [c for c in categories if c]


def _parse_attendees(ical_component: Any) -> list[EventAttendee]:
    attendees: list[EventAttendee] = []
    att_list = ical_component.get("ATTENDEE", [])
    if not isinstance(att_list, list):
        att_list = [att_list]
    for att in att_list:
        try:
            if hasattr(att, "params"):
                email = str(att).replace("mailto:", "")
                status_raw = att.params.get("PARTSTAT", ["NEEDS-ACTION"])
                status = (
                    status_raw[0] if isinstance(status_raw, list) else str(status_raw)
                )
                attendees.append({"email": email, "status": status})
            else:
                email = str(att).replace("mailto:", "").strip()
                if email:
                    attendees.append({"email": email, "status": "NEEDS-ACTION"})
        except Exception:
            continue
    return attendees


def _event_from_component(ical_component: Any) -> EventRecord | None:
    """Parse a single iCalendar component into an EventRecord."""
    dtstart = ical_component.get("DTSTART")
    dtend = ical_component.get("DTEND")

    if not dtstart:
        return None

    start_dt = dtstart.dt
    all_day = False
    if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
        start_dt = datetime.combine(start_dt, datetime.min.time())
        all_day = True

    if dtend:
        end_dt = dtend.dt
        if isinstance(end_dt, date) and not isinstance(end_dt, datetime):
            end_dt = datetime.combine(end_dt, datetime.max.time())
    else:
        end_dt = start_dt + timedelta(hours=1)

    summary = ical_component.get("SUMMARY")
    desc = ical_component.get("DESCRIPTION")
    loc = ical_component.get("LOCATION")
    uid = str(ical_component.get("UID", ""))

    cats = ical_component.get("CATEGORIES")
    priority_raw = ical_component.get("PRIORITY")
    rrule = ical_component.get("RRULE")

    return EventRecord(
        uid=uid,
        title=str(summary) if summary else "",
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        description=str(desc) if desc else "",
        location=str(loc) if loc else "",
        all_day=all_day,
        categories=_parse_categories(cats),
        priority=int(priority_raw) if priority_raw is not None else None,
        recurrence=str(rrule) if rrule else None,
        attendees=_parse_attendees(ical_component),
    )


# ── CalDAV client class ────────────────────────────────────────────


class CalDAVClient:
    """Wrapper around the ``caldav`` library v3.x for a single CalDAV account."""

    def __init__(
        self, url: str, username: str, password: str, timeout: int = 30
    ) -> None:
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.client: Any | None = None
        self.principal: Any | None = None

    def connect(self) -> bool:
        """Connect to the CalDAV server using the v3 factory."""
        try:
            self.client = get_davclient(
                url=self.url,
                username=self.username,
                password=self.password,
                rate_limit_handle=True,
                rate_limit_default_sleep=2,
                rate_limit_max_sleep=60,
            )
            self.principal = self.client.principal()
            return True
        except Exception as e:
            raise ConnectionError(f"Failed to connect to CalDAV server: {e}") from e

    def detect_capability(self) -> str:
        """Probe whether the calendar supports writes.

        Returns ``"readwrite"`` if the server reports ``{DAV:}write``
        in ``current-user-privilege-set``, otherwise ``"read"``.
        """
        if not self.principal:
            return "read"
        try:
            calendars = self.principal.get_calendars()
            if not calendars:
                return "read"
            cal = calendars[0]
            props = cal.get_properties([caldav.dav.CurrentUserPrivilegeSet()])
            priv_set = props.get("{DAV:}current-user-privilege-set", "")
            if "{DAV:}write" in str(priv_set):
                return "readwrite"
        except Exception:
            logger.debug(
                "Could not detect write capability, assuming read-only", exc_info=True
            )
        return "read"

    def list_calendars(self) -> list[CalendarInfo]:
        if not self.principal:
            raise RuntimeError("Not connected. Call connect() first.")
        calendars = self.principal.get_calendars()
        return [
            CalendarInfo(index=i, name=cal.name or f"Calendar {i}", url=str(cal.url))
            for i, cal in enumerate(calendars)
        ]

    def create_event(
        self,
        calendar_index: int = 0,
        title: str = "Event",
        description: str = "",
        location: str = "",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        duration_hours: float = 1.0,
        reminders: list[dict[str, Any]] | None = None,
        attendees: list[AttendeeInput] | None = None,
        categories: list[str] | None = None,
        priority: int | None = None,
        recurrence: dict[str, Any] | None = None,
    ) -> EventCreationResult:
        if not self.principal:
            raise RuntimeError("Not connected. Call connect() first.")

        calendars = self.principal.get_calendars()
        if calendar_index >= len(calendars):
            raise ValueError(
                f"Calendar index {calendar_index} out of range ({len(calendars)} available)"
            )
        calendar = calendars[calendar_index]

        if start_time is None:
            start_time = datetime.now(tz=timezone.utc) + timedelta(days=1)
            start_time = start_time.replace(hour=14, minute=0, second=0, microsecond=0)
        if end_time is None:
            end_time = start_time + timedelta(hours=duration_hours)

        uid = f"{_uuid.uuid4()}@mcp-caldav"

        if recurrence and recurrence.get("until"):
            until_str = recurrence["until"]
            if isinstance(until_str, str):
                try:
                    recurrence["until"] = datetime.fromisoformat(
                        until_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        alarm_lines = ""
        if reminders:
            for rem in reminders:
                mins = rem.get("minutes_before", 15)
                action = rem.get("action", "DISPLAY").upper()
                desc = _escape_ical_text(rem.get("description", title))
                alarm_lines += (
                    f"BEGIN:VALARM\nACTION:{action}\n"
                    f"TRIGGER:-PT{mins}M\nDESCRIPTION:{desc}\nEND:VALARM\n"
                )

        attendee_lines = _format_attendees(attendees) if attendees else ""
        cat_line = (_format_categories(categories) + "\n") if categories else ""
        prio_line = f"PRIORITY:{priority}\n" if priority is not None else ""
        rrule_line = (_format_rrule(recurrence) + "\n") if recurrence else ""

        vcal = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//MCP CalDAV Server//Python//EN\n"
            "CALSCALE:GREGORIAN\nBEGIN:VEVENT\n"
            f"UID:{uid}\n"
            f"DTSTAMP:{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}\n"
            f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND:{end_time.strftime('%Y%m%dT%H%M%S')}\n"
            f"SUMMARY:{_escape_ical_text(title)}\n"
            f"DESCRIPTION:{_escape_ical_text(description)}\n"
            f"LOCATION:{_escape_ical_text(location)}\n"
            "STATUS:CONFIRMED\nSEQUENCE:0\n"
            f"{prio_line}{cat_line}{rrule_line}{attendee_lines}{alarm_lines}"
            "END:VEVENT\nEND:VCALENDAR"
        )

        calendar.add_event(vcal)

        return EventCreationResult(
            success=True,
            uid=uid,
            title=title,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            calendar=calendar.name or "",
        )

    def get_events(
        self,
        calendar_index: int = 0,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        include_all_day: bool = True,
    ) -> list[EventRecord]:
        if not self.principal:
            raise RuntimeError("Not connected. Call connect() first.")

        calendars = self.principal.get_calendars()
        if calendar_index >= len(calendars):
            raise ValueError(
                f"Calendar index {calendar_index} out of range ({len(calendars)} available)"
            )
        calendar = calendars[calendar_index]

        if start_date is None:
            start_date = datetime.now(tz=timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        if end_date is None:
            end_date = start_date + timedelta(days=7)

        # v3: search() replaces date_search().
        events = calendar.search(start=start_date, end=end_date, event=True)

        result: list[EventRecord] = []
        for event in events:
            try:
                ical = event.get_icalendar_component()
                record = _event_from_component(ical)
                if record is None:
                    continue
                if not include_all_day and record.get("all_day"):
                    continue
                result.append(record)
            except Exception:
                logger.debug("Skipping unparseable event", exc_info=True)
                continue

        result.sort(key=lambda x: x["start"])
        return result

    def get_today_events(self, calendar_index: int = 0) -> list[EventRecord]:
        now = datetime.now(tz=timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.get_events(
            calendar_index, today_start, today_start + timedelta(days=1)
        )

    def get_week_events(
        self, calendar_index: int = 0, start_from_today: bool = True
    ) -> list[EventRecord]:
        now = datetime.now(tz=timezone.utc)
        if start_from_today:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        return self.get_events(calendar_index, start, start + timedelta(days=7))

    def get_event_by_uid(self, uid: str, calendar_index: int = 0) -> EventRecord | None:
        if not self.principal:
            raise RuntimeError("Not connected. Call connect() first.")

        calendars = self.principal.get_calendars()
        if calendar_index >= len(calendars):
            raise ValueError(f"Calendar index {calendar_index} out of range")
        calendar = calendars[calendar_index]

        try:
            event = calendar.get_event_by_uid(uid)
            if event:
                ical = event.get_icalendar_component()
                return _event_from_component(ical)
        except Exception:
            logger.debug("Event UID %s not found", uid, exc_info=True)
        return None

    def delete_event(self, uid: str, calendar_index: int = 0) -> EventDeletionResult:
        if not self.principal:
            raise RuntimeError("Not connected. Call connect() first.")

        calendars = self.principal.get_calendars()
        if calendar_index >= len(calendars):
            raise ValueError(f"Calendar index {calendar_index} out of range")
        calendar = calendars[calendar_index]

        try:
            event = calendar.get_event_by_uid(uid)
            if event:
                event.delete()
                return EventDeletionResult(
                    success=True, uid=uid, message="Event deleted successfully"
                )
        except Exception as e:
            raise RuntimeError(f"Failed to delete event: {e}") from e

        raise ValueError(f"Event with UID {uid} not found")

    def search_events(
        self,
        calendar_index: int = 0,
        query: str | None = None,
        search_fields: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[EventRecord]:
        if start_date is None or end_date is None:
            raise ValueError("Both start_date and end_date are required for search.")

        all_events = self.get_events(
            calendar_index=calendar_index, start_date=start_date, end_date=end_date
        )
        if not query:
            return all_events

        query_lower = query.lower()
        if search_fields is None:
            search_fields = ["title", "description", "location", "attendees"]

        results: list[EventRecord] = []
        for ev in all_events:
            match = False
            if "title" in search_fields and query_lower in ev.get("title", "").lower():
                match = True
            elif (
                "description" in search_fields
                and query_lower in ev.get("description", "").lower()
            ):
                match = True
            elif (
                "location" in search_fields
                and query_lower in ev.get("location", "").lower()
            ):
                match = True
            elif "attendees" in search_fields:
                for att in ev.get("attendees", []):
                    if query_lower in att.get("email", "").lower():
                        match = True
                        break
            if match:
                results.append(ev)
        return results
