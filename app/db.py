"""
megh_db access — read-only, pooled.

The connection itself should already be a SELECT-only role (megh_readonly, per
data/schema/schema_for_developers.md), but that is a second layer, not a substitute
for checking here: a generated query is untrusted input until proven otherwise.
"""
import logging
import re

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_dsn: str | None = None

# First keyword only — a CTE ("WITH ... SELECT") is legitimate and starts with
# WITH, so this is deliberately not just "must start with SELECT".
_ALLOWED_LEAD = ("select", "with")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b",
    re.IGNORECASE,
)


class UnsafeSQLError(ValueError):
    """Raised when generated SQL fails the read-only / single-statement check."""


class DatabaseUnavailableError(RuntimeError):
    """megh_db could not be reached (VPN / network drop, connection refused or
    lost, server out of connection slots). Not a problem with the question or
    its SQL — the caller must say "try again", never repair the SQL or answer
    from the reference documents instead (KI-025)."""


# asyncpg's connection-class failures. A slow query's command_timeout
# (asyncio.TimeoutError) is deliberately NOT here: it keeps its own 504 path.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    OSError,                       # ConnectionRefused/Reset, WinError 121 "semaphore timeout"
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.CannotConnectNowError,
)


def is_connection_error(e: BaseException) -> bool:
    """True for a failure to reach / stay connected to megh_db, anywhere in the
    exception's cause chain."""
    seen = 0
    while e is not None and seen < 6:
        # TimeoutError is an OSError subclass (and IS asyncio.TimeoutError on
        # 3.11): a slow query's timeout keeps its own 504 path, so it is excluded.
        if isinstance(e, DatabaseUnavailableError) or (
                isinstance(e, _CONNECTION_ERRORS) and not isinstance(e, (UnsafeSQLError, TimeoutError))):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


async def init_pool() -> None:
    """Build the megh_db pool. Best-effort: if the DB host is unreachable at
    startup (the 10.48.242.4 box flaps), log and carry on with `_pool = None` —
    the server still boots (degraded), and `ensure_pool()` rebuilds it lazily on
    the first query once the host is back."""
    global _pool, _pool_dsn
    _pool_dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    try:
        _pool = await asyncpg.create_pool(
            _pool_dsn,
            min_size=settings.DB_POOL_MIN_SIZE,
            max_size=settings.DB_POOL_MAX_SIZE,
            timeout=settings.DB_POOL_TIMEOUT,
            command_timeout=settings.SQL_EXECUTION_TIMEOUT_MS / 1000,
        )
        logger.info(
            "megh_db pool ready (min=%d max=%d)",
            settings.DB_POOL_MIN_SIZE, settings.DB_POOL_MAX_SIZE,
        )
    except Exception as e:  # noqa: BLE001
        _pool = None
        logger.warning("megh_db pool init failed — starting degraded, will retry on first query: %s", e)


async def ensure_pool() -> asyncpg.Pool:
    """Return the live pool, building it now if startup couldn't. Raises if the
    DB is still unreachable."""
    global _pool
    if _pool is not None:
        return _pool
    dsn = _pool_dsn or settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        timeout=settings.DB_POOL_TIMEOUT,
        command_timeout=settings.SQL_EXECUTION_TIMEOUT_MS / 1000,
    )
    logger.info("megh_db pool ready (lazy) (min=%d max=%d)",
                settings.DB_POOL_MIN_SIZE, settings.DB_POOL_MAX_SIZE)
    return _pool


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


def _assert_safe(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise UnsafeSQLError("multiple statements are not allowed")
    lead = stripped.split(None, 1)[0].lower() if stripped else ""
    if lead not in _ALLOWED_LEAD:
        raise UnsafeSQLError(f"query must start with SELECT or WITH, got '{lead}'")
    if _FORBIDDEN.search(stripped):
        raise UnsafeSQLError("query contains a write/DDL keyword")


async def fetch_rows(sql: str, params: list | None = None) -> list[dict]:
    """For our own hardcoded, parametrized queries (entity resolution) — trusted
    SQL text, untrusted values only, safe via asyncpg's parameter binding. Not
    for LLM-generated SQL — use run_readonly() for that."""
    pool = await ensure_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *(params or []))
    return [dict(r) for r in rows]


async def fetchrow(sql: str, params: list | None = None) -> dict | None:
    pool = await ensure_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *(params or []))
    return dict(row) if row else None


async def fetchval(sql: str, params: list | None = None):
    pool = await ensure_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *(params or []))


async def execute(sql: str, params: list | None = None) -> str:
    """Run one trusted, parametrised write/DDL statement (our own `app.*` SQL —
    NEVER model-generated text). Values are still bound, never interpolated."""
    pool = await ensure_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *(params or []))


async def execute_script(sql: str) -> None:
    """Run a multi-statement DDL script (schema bootstrap only, no parameters)."""
    pool = await ensure_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def run_readonly(sql: str) -> list[dict]:
    """Validate, then execute a single read-only statement against megh_db."""
    _assert_safe(sql)
    pool = await ensure_pool()

    limited = sql.strip().rstrip(";")
    if "limit" not in limited.lower():
        limited = f"{limited} LIMIT {settings.SQL_MAX_RESULT_ROWS}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(limited)
    return [dict(r) for r in rows]
