"""Async database access for looking up per-user calendar sources."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import CalendarSource

if TYPE_CHECKING:
    from .settings import Settings

logger = logging.getLogger("mcp-caldav.db")

# Module-level singletons (initialised on first call to ``init_db``).
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_fernet: Fernet | None = None


async def init_db(settings: Settings) -> None:
    """Create the async engine and session factory."""
    global _engine, _session_factory, _fernet  # noqa: PLW0603

    _engine = create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    if settings.encryption_key:
        _fernet = Fernet(settings.encryption_key.encode())
    else:
        logger.warning(
            "MCP_CALDAV_ENCRYPTION_KEY not set — CalDAV passwords will not "
            "be decryptable.  Set this if calendar sources use CalDAV credentials."
        )


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    global _engine, _session_factory  # noqa: PLW0603
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def decrypt_password(encrypted: str | None) -> str | None:
    """Decrypt a Fernet-encrypted password, or return None."""
    if not encrypted:
        return None
    if not _fernet:
        logger.error("Cannot decrypt password — encryption key not configured")
        return None
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt CalDAV password — invalid token")
        return None


def encrypt_password(plaintext: str) -> str:
    """Encrypt a plaintext password with Fernet."""
    if not _fernet:
        raise RuntimeError("Cannot encrypt — MCP_CALDAV_ENCRYPTION_KEY not set")
    return _fernet.encrypt(plaintext.encode()).decode()


async def get_user_sources(user_id: str) -> list[CalendarSource]:
    """Return all enabled calendar sources for *user_id*."""
    if not _session_factory:
        raise RuntimeError("Database not initialised — call init_db() first")

    async with _session_factory() as session:
        stmt = (
            select(CalendarSource)
            .where(
                CalendarSource.user_id == user_id,
                CalendarSource.enabled.is_(True),
            )
            .order_by(CalendarSource.name)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
