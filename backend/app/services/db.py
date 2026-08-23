"""MySQL connection helper (T-008).

Hard-scoped to the independent `hackathontrip` database (D-006/D-014) — this
project must never touch `botc_db` or `alonkitchen`. Refuses to connect to
any other database name, even if misconfigured via environment variables.

Connectivity is treated as optional, not required: callers (factory.py) probe
once at startup and fall back to local JSON on any failure — missing
MySQL, wrong credentials, unreachable host, etc. This mirrors the existing
SerpApi/DeepSeek zero-key fallback pattern (D-010).
"""

from __future__ import annotations

import logging

import pymysql
import pymysql.cursors

from ..config import Settings

logger = logging.getLogger(__name__)

ALLOWED_DATABASE = "hackathontrip"


def get_connection(settings: Settings) -> pymysql.connections.Connection:
    """Open a new connection to the hackathontrip database.

    Raises ``ValueError`` immediately (no network call) if configuration
    points anywhere other than the allowed database — this is a hard
    safety rail, not a soft warning.
    """
    if settings.mysql_database != ALLOWED_DATABASE:
        raise ValueError(
            f"refusing to connect to database {settings.mysql_database!r}; "
            f"this project may only use {ALLOWED_DATABASE!r}"
        )
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def can_connect(settings: Settings) -> bool:
    """Best-effort connectivity probe used by the provider factory.

    Never raises — any failure (auth, unreachable host, wrong database)
    is logged and treated as "use the local-json fallback instead".
    """
    if not settings.mysql_password:
        return False
    try:
        conn = get_connection(settings)
        conn.close()
        return True
    except ValueError:
        raise  # wrong database name is a config bug, not a transient failure
    except Exception as exc:  # noqa: BLE001 — any DB failure means "fall back"
        logger.info("MySQL unavailable (%s); falling back to local-json", exc)
        return False
