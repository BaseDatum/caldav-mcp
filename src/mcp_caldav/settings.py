"""Application settings — all tunables are configurable via environment variables.

Auth header names, database URL, Redis URL, encryption keys, etc. are all
configurable so this fork can be reused outside of the Dialogue project.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration for mcp-caldav.

    Every field can be overridden via the corresponding environment variable
    (the ``env_prefix`` is ``MCP_CALDAV_``).
    """

    # ── Server ──────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8025
    log_level: str = "info"

    # ── Per-request auth ────────────────────────────────────────────
    # HTTP header that carries the authenticated user identifier.
    # Configurable so deployments outside Dialogue can use their own
    # header (e.g. ``X-Forwarded-User``, ``X-Auth-User-Id``, etc.).
    user_id_header: str = Field(
        default="X-Dialogue-User-Id",
        description="HTTP header containing the authenticated user ID",
    )

    # ── Database (stores CalendarSource rows) ───────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://dialogue:dialogue@postgres:5432/dialogue",
        description="Async SQLAlchemy database URL",
    )

    # ── Redis (ICS feed cache + rate-limiter backing store) ─────────
    redis_url: str = Field(
        default="redis://redis:6379/4",
        description="Redis connection URL",
    )

    # ── ICS feed cache ──────────────────────────────────────────────
    ics_cache_ttl_seconds: int = Field(
        default=300,
        description="TTL in seconds for cached ICS feed data (default: 5 min)",
    )

    # ── Credential encryption ───────────────────────────────────────
    # Fernet key used to encrypt/decrypt CalDAV passwords at rest.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = Field(
        default="",
        description="Fernet symmetric key for encrypting CalDAV passwords at rest",
    )

    # ── Rate limiting ───────────────────────────────────────────────
    # Per-user rate limit string in ``limits`` notation.
    # See https://limits.readthedocs.io/en/stable/quickstart.html#rate-limit-string-notation
    rate_limit: str = Field(
        default="60/minute",
        description="Per-user rate limit (limits library notation)",
    )

    # ── CalDAV client defaults ──────────────────────────────────────
    caldav_timeout_seconds: int = Field(
        default=30,
        description="HTTP timeout for CalDAV requests",
    )

    model_config = {"env_prefix": "MCP_CALDAV_"}
