"""
Semantic conversation memory — the "relevant older turns" leg of the context
layer (see app/context_manager.py), for a conversation long enough that the
answer to the current question was actually said several turns back, past
what session_store's in-process turn window or a short recent-turns slice
would still hold.

Deliberately NOT the same thing as:
  - app/vectorstore.py       — the scheme-KB collection for RAG. Different
                                content (reference docs, not conversation
                                turns), different collection, same client.
  - app/semantic_cache.py    — near-duplicate QUESTION -> cached ANSWER, keyed
                                on an authorization fingerprint, in-process
                                only. This module stores conversation TURNS in
                                Qdrant, keyed by tenant/user/session, for
                                retrieval as *context* to feed a rewrite — it
                                never short-circuits the pipeline or serves an
                                answer directly.

Reuses the existing Qdrant client (app.vectorstore.get_client) and the
existing embedding pipeline (app.llm.call_embedding) — no new connection, no
new embedding model. A dedicated collection (CONTEXT_MEMORY_COLLECTION) keeps
it out of the KB collection's point count / filters.

Every write and read is scoped by tenant_id + user_id (+ session_id for a
single-conversation lookup) as a Qdrant payload filter — the same fields the
Postgres conversation tables already partition on (see app/auth.py,
app/conversation_store.py). This is a convenience index over conversation
turns the user is already authorized to see; it grants no authorization of
its own, and nothing read from it is ever used to build or execute SQL
directly (see context_manager.py) — only to enrich a rewrite prompt.

Every function here is best-effort: indexing runs as a fire-and-forget
background task (mirroring conversation_store.persist_turn), and a search
failure (Qdrant down, collection missing, embedding error) returns an empty
list rather than raising, so the pipeline always has PostgreSQL + in-session
turns to fall back to.
"""
import asyncio
import logging
import time
import uuid

from qdrant_client import models

from app import llm, vectorstore
from app.config import settings

logger = logging.getLogger(__name__)

_bg: set[asyncio.Task] = set()
_collection_ready = False


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def _ensure_collection(dim: int) -> bool:
    """Create the conversation-memory collection on first use. Cheap after the
    first call (module-level flag); any failure just disables memory for this
    request — the collection may already exist from a previous worker, in
    which case get_collection succeeds and we skip creation."""
    global _collection_ready
    if _collection_ready:
        return True
    client = vectorstore.get_client()
    try:
        await client.get_collection(settings.CONTEXT_MEMORY_COLLECTION)
        _collection_ready = True
        return True
    except Exception:  # noqa: BLE001 — collection doesn't exist yet
        pass
    try:
        await client.create_collection(
            collection_name=settings.CONTEXT_MEMORY_COLLECTION,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        _collection_ready = True
        logger.info("conversation_memory: created collection %s (dim=%d)",
                    settings.CONTEXT_MEMORY_COLLECTION, dim)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("conversation_memory: could not create collection: %s", e)
        return False


def index_turn(*, tenant_id: int | None, user_id: int | None, session_id: str,
               question: str, standalone_question: str, answer: str,
               schemes: list[str] | None = None) -> None:
    """Fire-and-forget: embed one turn and upsert it into conversation memory.
    Never raises into the caller — see module docstring."""
    if not settings.CONTEXT_SEMANTIC_MEMORY_ENABLED:
        return
    _spawn(_index_turn(tenant_id, user_id, session_id, question,
                       standalone_question, answer, schemes or []))


async def _index_turn(tenant_id, user_id, session_id, question, standalone_question,
                      answer, schemes) -> None:
    try:
        text = standalone_question or question
        if not text or not text.strip():
            return
        vecs = await llm.call_embedding(text)
        if not vecs:
            return
        vec = vecs[0]
        if not await _ensure_collection(len(vec)):
            return
        point = models.PointStruct(
            id=uuid.uuid4().hex,
            vector=vec,
            payload={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": session_id,
                "question": question[:2000],
                "standalone_question": (standalone_question or "")[:2000],
                "answer": (answer or "")[:1000],
                "schemes": schemes,
                "ts": time.time(),
            },
        )
        await vectorstore.get_client().upsert(
            collection_name=settings.CONTEXT_MEMORY_COLLECTION, points=[point], wait=False)
    except Exception as e:  # noqa: BLE001 — indexing must never break a request
        logger.warning("conversation_memory: index_turn failed (non-fatal): %s", e)


async def search_relevant_turns(*, question: str, tenant_id: int | None, user_id: int | None,
                                session_id: str | None = None, top_k: "int | None" = None,
                                exclude_session: bool = False) -> list[dict]:
    """Up to `top_k` older turns semantically relevant to `question`, scoped to
    this tenant/user (and, unless exclude_session, this session too — the
    normal case is "this same conversation, further back than the in-process
    window"). Returns [] on ANY failure (embedding, Qdrant unreachable,
    collection missing) — callers must already have a PostgreSQL/in-session
    fallback and treat this purely as an enrichment, per the failure-tolerance
    requirement (Qdrant/memory retrieval failing must never break the core
    pipeline)."""
    if not settings.CONTEXT_SEMANTIC_MEMORY_ENABLED or not question or not question.strip():
        return []
    top_k = top_k or settings.CONTEXT_MEMORY_TOP_K
    try:
        vecs = await llm.call_embedding(question)
        if not vecs:
            return []
        must: list[models.FieldCondition] = []
        if tenant_id is not None:
            must.append(models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)))
        if user_id is not None:
            must.append(models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)))
        if session_id and not exclude_session:
            must.append(models.FieldCondition(key="session_id", match=models.MatchValue(value=session_id)))
        query_filter = models.Filter(must=must) if must else None

        client = vectorstore.get_client()
        response = await client.query_points(
            collection_name=settings.CONTEXT_MEMORY_COLLECTION,
            query=vecs[0],
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        out = []
        for h in response.points:
            if float(h.score) < settings.CONTEXT_MEMORY_MIN_SCORE:
                continue
            p = h.payload or {}
            out.append({
                "score": float(h.score),
                "question": p.get("question", ""),
                "standalone_question": p.get("standalone_question", ""),
                "answer": p.get("answer", ""),
                "schemes": p.get("schemes", []),
                "session_id": p.get("session_id"),
            })
        return out
    except Exception as e:  # noqa: BLE001 — see docstring: memory is best-effort
        logger.warning("conversation_memory: search failed, falling back (non-fatal): %s", e)
        return []
