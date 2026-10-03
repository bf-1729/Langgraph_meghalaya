"""
Conversation persistence + query audit, in the `app` schema of megh_db.

Pattern borrowed from the NeuralAI predecessor's context_store: the in-process
session_store is the L1 read cache for follow-up context; this module is the L2
durable copy. Writes are fire-and-forget background tasks so a slow/unavailable
DB never blocks an answer. Reads (history, admin views) go straight to Postgres.

A `session_id` maps 1:1 to a row in app.conversations; every answered request
appends a row to app.conversation_turns and app.query_audit, both stamped with
tenant_id + user_id so an admin can review exactly one officer, one tenant.
"""
import asyncio
import json
import logging

from app import db

logger = logging.getLogger(__name__)

_bg: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def persist_turn(*, tenant_id, user_id, session_id, result: dict, question: str,
                 latency_ms: float, username: str | None, ip: str | None) -> None:
    """Fire-and-forget: upsert the conversation, append the turn + audit row."""
    _spawn(_persist_turn(tenant_id, user_id, session_id, result, question,
                         latency_ms, username, ip))


async def _persist_turn(tenant_id, user_id, session_id, result, question,
                        latency_ms, username, ip):
    try:
        route = result.get("route", "data")
        answer = (result.get("answer") or "")[:4000]
        rewritten = result.get("rewritten_question")
        schemes = result.get("schemes") or []
        entities = result.get("resolved_entities") or {}
        sql = result.get("sql")
        rows = result.get("row_count")
        allowed = route != "denied"
        denied_by = result.get("denied_by")

        # The payload the chat UI needs to redraw this turn when the thread is
        # reopened later — answer text, the result rows the charts are built
        # from, the SQL, and the follow-up chips. `data` is already capped at
        # 200 rows upstream in the pipeline, so this stays a few KB.
        # default=str keeps Decimal/date values from asyncpg rows serialisable.
        response_payload = json.dumps({
            "answer": result.get("answer") or "",
            "intent": result.get("intent"),
            "route": route,
            "confidence": result.get("confidence"),
            "rewritten_question": rewritten,
            "data": result.get("data") or [],
            "row_count": rows,
            "sql": sql,
            "sql_query": sql,
            "schemes": list(schemes),
            "sources": result.get("sources") or [],
            "follow_up_options": result.get("follow_up_options") or [],
            "follow_ups": result.get("follow_ups") or [],
            "follow_up": result.get("follow_up"),
            "resolved_entities": entities,
            # Present only on a clarification pause; None/False on every other
            # turn. Kept here so a reopened thread redraws the clarifying
            # question and its option chips exactly as first shown.
            "clarification": result.get("clarification"),
            "needs_clarification": result.get("needs_clarification", False),
        }, default=str)

        conv_id = await db.fetchval(
            """INSERT INTO app.conversations (tenant_id, user_id, session_id, title, turn_count)
               VALUES ($1,$2,$3,$4,1)
               ON CONFLICT (session_id) DO UPDATE
                   SET last_at = NOW(), turn_count = app.conversations.turn_count + 1
               RETURNING conv_id""",
            [tenant_id, user_id, session_id, question[:120]],
        )
        await db.execute(
            """INSERT INTO app.conversation_turns
               (conv_id, tenant_id, user_id, session_id, question, rewritten_question,
                answer, route, intent, schemes, resolved_entities, sql, row_count,
                allowed, denied_by, latency_ms, response)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
            [conv_id, tenant_id, user_id, session_id, question[:2000], rewritten,
             answer, route, result.get("intent"), list(schemes),
             json.dumps(entities, default=str), sql, rows, allowed, denied_by, latency_ms,
             response_payload],
        )
        from app.auth import infer_granularity
        await db.execute(
            """INSERT INTO app.query_audit
               (tenant_id, user_id, username, session_id, question, rewritten_question,
                route, granularity, schemes, allowed, denied_by, row_count, latency_ms, ip)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            [tenant_id, user_id, username, session_id, question[:2000], rewritten,
             route, infer_granularity(entities, sql), list(schemes), allowed,
             denied_by, rows, latency_ms, ip],
        )
    except Exception as e:  # noqa: BLE001 — persistence is best-effort
        logger.warning("conversation_store: persist failed (non-fatal): %s", e)


# ── Reads ────────────────────────────────────────────────────────────────
async def list_my_conversations(user_id: int, limit: int = 30,
                                archived: bool | None = None) -> list[dict]:
    """The sidebar list.

    archived=None (the default) returns every thread in one pass, ordered so the
    UI can render three sections without a second request:
        Pinned  ->  Recent (date-bucketed)  ->  Archived
    Passing True/False restricts to one shelf, which the admin views still use.
    """
    where = "user_id = $1"
    params: list = [user_id]
    if archived is not None:
        params.append(archived)
        where += f" AND archived = ${len(params)}"
    params.append(max(1, min(limit, 200)))
    return await db.fetch_rows(
        f"""SELECT conv_id, session_id, title, started_at, last_at, turn_count,
                   pinned, pinned_at, archived, archived_at
            FROM app.conversations
            WHERE {where}
            ORDER BY archived ASC,               -- active threads first
                     pinned DESC,                -- then pinned within active
                     pinned_at DESC NULLS LAST,
                     last_at DESC
            LIMIT ${len(params)}""",
        params,
    )


async def get_conversation_turns(*, session_id: str | None = None, conv_id: int | None = None,
                                 tenant_id: int | None = None, user_id: int | None = None,
                                 limit: int = 100) -> list[dict]:
    where, params = [], []
    if conv_id is not None:
        params.append(conv_id); where.append(f"conv_id = ${len(params)}")
    if session_id is not None:
        params.append(session_id); where.append(f"session_id = ${len(params)}")
    if tenant_id is not None:
        params.append(tenant_id); where.append(f"tenant_id = ${len(params)}")
    if user_id is not None:
        params.append(user_id); where.append(f"user_id = ${len(params)}")
    if not where:
        return []
    params.append(max(1, min(limit, 500)))
    turns = await db.fetch_rows(
        f"""SELECT id, conv_id, session_id, question, rewritten_question, answer, route,
                   schemes, sql, row_count, allowed, denied_by, latency_ms, created_at,
                   response
            FROM app.conversation_turns WHERE {' AND '.join(where)}
            ORDER BY created_at ASC LIMIT ${len(params)}""",
        params,
    )
    # asyncpg has no JSON codec registered on this pool (see app/db.py), so a
    # JSONB column comes back as raw text — decode it here so the client gets an
    # object. Turns written before the `response` column existed are NULL.
    for t in turns:
        raw = t.get("response")
        if isinstance(raw, str):
            try:
                t["response"] = json.loads(raw)
            except (ValueError, TypeError):
                t["response"] = None
    return turns


async def admin_list_conversations(*, tenant_id: int | None, user_id: int | None = None,
                                   limit: int = 100) -> list[dict]:
    where, params = [], []
    if tenant_id is not None:
        params.append(tenant_id); where.append(f"c.tenant_id = ${len(params)}")
    if user_id is not None:
        params.append(user_id); where.append(f"c.user_id = ${len(params)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 500)))
    return await db.fetch_rows(
        f"""SELECT c.conv_id, c.session_id, c.title, c.started_at, c.last_at, c.turn_count,
                   c.user_id, u.username, u.name
            FROM app.conversations c
            LEFT JOIN app.users u ON u.user_id = c.user_id
            {clause}
            ORDER BY c.last_at DESC LIMIT ${len(params)}""",
        params,
    )


async def admin_list_audit(*, tenant_id: int | None, user_id: int | None = None,
                           denied_only: bool = False, limit: int = 200) -> list[dict]:
    where, params = [], []
    if tenant_id is not None:
        params.append(tenant_id); where.append(f"tenant_id = ${len(params)}")
    if user_id is not None:
        params.append(user_id); where.append(f"user_id = ${len(params)}")
    if denied_only:
        where.append("allowed = FALSE")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 2000)))
    return await db.fetch_rows(
        f"""SELECT id, tenant_id, user_id, username, session_id, question, rewritten_question,
                   route, granularity, schemes, allowed, denied_by, row_count, latency_ms,
                   ip, created_at
            FROM app.query_audit {clause}
            ORDER BY created_at DESC LIMIT ${len(params)}""",
        params,
    )


async def tenant_stats(tenant_id: int | None) -> dict:
    t = "" if tenant_id is None else "WHERE tenant_id = $1"
    p = [] if tenant_id is None else [tenant_id]
    audit = await db.fetchrow(
        f"""SELECT count(*) AS queries,
                   count(*) FILTER (WHERE allowed = FALSE) AS denied,
                   count(DISTINCT user_id) AS active_users
            FROM app.query_audit {t}""", p)
    convs = await db.fetchval(
        f"SELECT count(*) FROM app.conversations {t}", p)
    return {**(audit or {}), "conversations": convs}


# ── Officer-owned conversation management (ChatGPT-style history) ─────────
# Every statement below is scoped by user_id in the WHERE clause, so a caller
# can only ever touch their own threads. Ownership is enforced in SQL rather
# than by a prior SELECT, which keeps it free of check-then-act races.
async def rename_conversation(*, session_id: str, user_id: int, title: str) -> bool:
    """Retitle one of the caller's own conversations. False if not theirs."""
    row = await db.fetchrow(
        """UPDATE app.conversations SET title = $1
           WHERE session_id = $2 AND user_id = $3
           RETURNING conv_id""",
        [title.strip()[:120], session_id, user_id],
    )
    return row is not None


async def delete_conversation(*, session_id: str, user_id: int) -> bool:
    """Delete one of the caller's own conversations and its turns.

    app.query_audit is deliberately NOT touched — it is the compliance trail and
    must survive a user tidying up their sidebar."""
    conv_id = await db.fetchval(
        "SELECT conv_id FROM app.conversations WHERE session_id = $1 AND user_id = $2",
        [session_id, user_id],
    )
    if conv_id is None:
        return False
    # turns first — they FK to app.conversations(conv_id)
    await db.execute("DELETE FROM app.conversation_turns WHERE conv_id = $1", [conv_id])
    await db.execute("DELETE FROM app.conversations WHERE conv_id = $1", [conv_id])
    return True


async def search_my_conversations(*, user_id: int, q: str, limit: int = 30,
                                  archived: bool | None = None) -> list[dict]:
    """Title or turn-text search across the caller's own conversations.
    archived=None searches both shelves, so a search can find an archived chat."""
    where = "c.user_id = $1"
    params: list = [user_id, q.strip()[:200]]
    if archived is not None:
        params.append(archived)
        where += f" AND c.archived = ${len(params)}"
    params.append(max(1, min(limit, 100)))
    return await db.fetch_rows(
        f"""SELECT DISTINCT c.conv_id, c.session_id, c.title, c.started_at,
                   c.last_at, c.turn_count, c.pinned, c.pinned_at,
                   c.archived, c.archived_at
            FROM app.conversations c
            LEFT JOIN app.conversation_turns t ON t.conv_id = c.conv_id
            WHERE {where}
              AND (c.title ILIKE '%' || $2 || '%'
                   OR t.question ILIKE '%' || $2 || '%'
                   OR t.answer   ILIKE '%' || $2 || '%')
            ORDER BY c.pinned DESC, c.last_at DESC LIMIT ${len(params)}""",
        params,
    )


# ── Pin / archive ────────────────────────────────────────────────────────
# Both are per-user flags on the caller's own row; ownership is enforced in the
# WHERE clause exactly as rename/delete do, so there is no check-then-act gap.
async def set_pinned(*, session_id: str, user_id: int, pinned: bool) -> bool:
    row = await db.fetchrow(
        """UPDATE app.conversations
           SET pinned = $1, pinned_at = CASE WHEN $1 THEN NOW() ELSE NULL END
           WHERE session_id = $2 AND user_id = $3
           RETURNING conv_id""",
        [pinned, session_id, user_id],
    )
    return row is not None


def save_context_state(*, session_id: str, context_state: dict,
                       summary: str | None = None) -> None:
    """L2 (durable) mirror of the context layer's structured state — see
    app.session_store.ConversationState and app.context_manager. Fire-and-
    forget, same stance as persist_turn: a slow/unavailable DB must never
    block an answer, and this is a convenience cache for resuming a
    conversation on a different worker, not the source of truth (the
    in-process Session is). Only touches a conversation that already exists
    (the turn insert in persist_turn always runs first)."""
    _spawn(_save_context_state(session_id, context_state, summary))


async def _save_context_state(session_id: str, context_state: dict, summary: str | None) -> None:
    try:
        if summary is not None:
            await db.execute(
                """UPDATE app.conversations
                   SET context_state = $1, summary = $2, summary_updated_at = NOW()
                   WHERE session_id = $3""",
                [json.dumps(context_state, default=str), summary, session_id],
            )
        else:
            await db.execute(
                """UPDATE app.conversations SET context_state = $1 WHERE session_id = $2""",
                [json.dumps(context_state, default=str), session_id],
            )
    except Exception as e:  # noqa: BLE001 — best-effort, same as persist_turn
        logger.warning("conversation_store: save_context_state failed (non-fatal): %s", e)


async def save_session_state(*, tenant_id, user_id, session_id: str, title: str,
                             snapshot: dict, rev: int, summary: str | None) -> bool:
    """AWAITED write of the session's durable snapshot (app/session_sync.py).
    Returns True when this revision was stored.

    Unlike save_context_state above, this is not fire-and-forget. The next
    request may land on another worker immediately, and it must read what
    this one wrote. It is an UPSERT on the conversation row, so it no longer
    depends on persist_turn's insert having committed first; a first-turn
    state used to be dropped when the UPDATE ran before that insert did. The
    `rev` guard rejects a stale write: two concurrent requests on the same
    session both start from rev N, the first to commit N+1 wins, and the other
    is refused rather than overwriting it. Last writer does NOT win."""
    snap = {**snapshot, "rev": int(rev)}
    row = await db.fetchrow(
        """INSERT INTO app.conversations
               (tenant_id, user_id, session_id, title, turn_count, context_state, summary)
           VALUES ($1, $2, $3, $4, 0, $5::jsonb, $6)
           ON CONFLICT (session_id) DO UPDATE
               SET context_state = EXCLUDED.context_state,
                   summary = COALESCE(EXCLUDED.summary, app.conversations.summary)
               WHERE COALESCE((app.conversations.context_state->>'rev')::int, 0) < $7
           RETURNING conv_id""",
        [tenant_id, user_id, session_id, (title or "")[:120],
         json.dumps(snap, default=str), summary, int(rev)],
    )
    return row is not None


async def load_context_state(session_id: str) -> dict:
    """The durable (L2) structured state + summary for a session — used to
    rehydrate app.session_store.Session.state when a conversation resumes on a
    worker that has never seen this session_id in-process (new worker, or the
    in-process session TTL'd out but the conversation itself hasn't). Returns
    {} (never raises) if the conversation/columns aren't there yet."""
    try:
        row = await db.fetchrow(
            "SELECT context_state, summary FROM app.conversations WHERE session_id = $1",
            [session_id],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("conversation_store: load_context_state failed (non-fatal): %s", e)
        return {}
    if not row:
        return {}
    state = row.get("context_state")
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except (ValueError, TypeError):
            state = None
    return {"context_state": state or {}, "summary": row.get("summary")}


async def set_archived(*, session_id: str, user_id: int, archived: bool) -> bool:
    """Archiving also unpins: a thread on the archive shelf should not keep a
    slot at the top of the active list if it is ever restored."""
    row = await db.fetchrow(
        """UPDATE app.conversations
           SET archived = $1,
               archived_at = CASE WHEN $1 THEN NOW() ELSE NULL END,
               pinned = CASE WHEN $1 THEN FALSE ELSE pinned END,
               pinned_at = CASE WHEN $1 THEN NULL ELSE pinned_at END
           WHERE session_id = $2 AND user_id = $3
           RETURNING conv_id""",
        [archived, session_id, user_id],
    )
    return row is not None
