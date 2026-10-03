"""
Cross-worker conversation state (KI-028, KI-001).

The in-process session_store is per worker. The deploy runs 2 workers on each
of 2 VMs behind an nginx upstream with no stickiness, so consecutive requests
from one conversation routinely land on different workers. Before this module:
  * the previous turn a follow-up is rewritten against, and the pending
    clarification a reply resumes, existed ONLY in the worker that served them;
  * the durable copy (app.conversations.context_state) held the structured
    state alone, was written fire-and-forget as an UPDATE that could run
    before the conversation row existed, and was read back only when the
    local session had no turns at all, so a worker holding an OLD copy of the
    session kept using it.

Now Postgres (the same app.conversations row, no new store) is the source of
truth:
  sync_in   at the start of every request: load the snapshot and apply it
            whenever its revision is newer than this worker's copy;
  sync_out  at the end of every request, AWAITED: write the snapshot at
            revision + 1 (conversation_store.save_session_state is an upsert
            with a stale-write guard).
The in-process copy is only a fallback for when the DB is unreachable. That is
the same degraded behaviour as before, not a second source of truth.

Redis (settings.REDIS_URL) was considered and not used: in this codebase it is
an optional, best-effort cache layer (app/cache.py) that is unset in dev,
while app.conversations always exists wherever the app can answer at all.

Both calls are bounded by CONTEXT_STATE_SYNC_TIMEOUT_SECONDS and never raise:
a slow or down DB costs at most that much per request, and then the request
runs on the local copy exactly as it did before.
"""
import asyncio
import logging

from app.config import settings
from app.session_store import Session

logger = logging.getLogger(__name__)


class PostgresBackend:
    """The production backend: app.conversations via conversation_store."""

    async def load(self, session_id: str) -> dict:
        from app import conversation_store
        return await conversation_store.load_context_state(session_id)

    async def save(self, *, tenant_id, user_id, session_id, title, snapshot, rev, summary) -> bool:
        from app import conversation_store
        return await conversation_store.save_session_state(
            tenant_id=tenant_id, user_id=user_id, session_id=session_id, title=title,
            snapshot=snapshot, rev=rev, summary=summary)


_default_backend = PostgresBackend()


async def sync_in(session: Session, session_id: str, backend=None) -> str:
    """Bring this worker's copy of the session up to the durable revision.
    Returns "applied" | "current" | "none" | "unavailable" (logged, and
    handy in tests)."""
    if not settings.CONTEXT_STATE_SHARED:
        return "none"
    backend = backend or _default_backend
    try:
        durable = await asyncio.wait_for(backend.load(session_id),
                                         timeout=settings.CONTEXT_STATE_SYNC_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 — never fail a request over shared state
        logger.warning("session_sync.sync_in: durable state unavailable (%s) — using local copy", e)
        return "unavailable"
    state = (durable or {}).get("context_state") or {}
    if not state:
        return "none"
    rev = int(state.get("rev") or 0)
    if rev <= session.rev and session.turns:
        return "current"
    session.apply_snapshot(state, (durable or {}).get("summary"))
    session.rev = rev
    logger.info("session_sync.sync_in: session %s rehydrated at rev %d", session_id, rev)
    return "applied"


async def sync_out(session: Session, session_id: str, *, tenant_id, user_id,
                   title: str = "", backend=None) -> bool:
    """Write this worker's copy as the next durable revision. Awaited, so the
    next request finds it on any worker. Returns True when stored."""
    if not settings.CONTEXT_STATE_SHARED:
        return False
    backend = backend or _default_backend
    rev = session.rev + 1
    try:
        ok = await asyncio.wait_for(
            backend.save(tenant_id=tenant_id, user_id=user_id, session_id=session_id,
                         title=title, snapshot=session.to_snapshot(), rev=rev,
                         summary=session.summary),
            timeout=settings.CONTEXT_STATE_SYNC_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("session_sync.sync_out: could not store state (%s) — "
                       "the next request on another worker will not see this turn", e)
        return False
    if ok:
        session.rev = rev
    else:
        # A concurrent request on the same session committed this revision
        # first. Its state stands; ours is dropped rather than overwriting it.
        logger.warning("session_sync.sync_out: stale revision %d for %s — newer state kept",
                       rev, session_id)
    return bool(ok)
