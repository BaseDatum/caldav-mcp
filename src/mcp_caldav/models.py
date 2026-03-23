"""SQLAlchemy models for calendar sources.

This module defines the ``calendar_sources`` table that stores per-user
CalDAV credentials and ICS subscription URLs.  It intentionally mirrors
the model style used in the Dialogue ``shared-models`` package but is
self-contained so the MCP server can be deployed independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all models in this service."""


class CalendarSource(Base):
    """A single calendar source (CalDAV server or ICS feed) linked to a user.

    Users may have many sources.  Each source is either:
    * ``caldav`` — a full CalDAV endpoint with credentials (may be read-write)
    * ``ics`` — a read-only ICS subscription feed URL

    CalDAV passwords are stored Fernet-encrypted (see ``encryption_key``
    in :class:`~mcp_caldav.settings.Settings`).
    """

    __tablename__ = "calendar_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Human-readable label chosen by the user (e.g. "Work", "Personal").
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # "caldav" or "ics".
    source_type: Mapped[str] = mapped_column(String(10), nullable=False)

    # CalDAV server URL  *or*  ICS feed URL.
    url: Mapped[str] = mapped_column(Text, nullable=False)

    # CalDAV credentials (NULL for ICS sources).
    username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Detected capability after first successful connection:
    #   "read"      — ICS feeds or read-only CalDAV calendars
    #   "readwrite" — CalDAV calendars with write privileges
    capability: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'read'")
    )

    # Whether the agent is allowed to use this source.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    # Health tracking.
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
