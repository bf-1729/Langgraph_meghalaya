"""
Qdrant access for the scheme-knowledge base (the RAG path).

One module-level AsyncQdrantClient, opened at startup and closed at shutdown —
same reasoning as the shared httpx client and the asyncpg pool: a fresh client
per request wastes connections at the 20-40 concurrent target.

Qdrant runs on its own server (115.124.102.167:6335, since 2026-10-03; no VPN needed). It
is only used for scheme-knowledge retrieval; all numeric
answers come from megh_db, never from here.
"""
import logging

from qdrant_client import AsyncQdrantClient, models

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncQdrantClient | None = None


async def init_qdrant() -> None:
    global _client
    _client = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY or None,
        timeout=30,
    )
    logger.info("qdrant client ready (%s)", settings.QDRANT_URL)


async def close_qdrant() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_client() -> AsyncQdrantClient:
    if _client is None:
        raise RuntimeError("qdrant client not initialized — call init_qdrant() at startup")
    return _client


async def collection_count() -> int:
    """Point count for the KB collection, or -1 if the collection is absent."""
    client = get_client()
    try:
        info = await client.get_collection(settings.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:  # noqa: BLE001 — missing collection is not an error here
        return -1


async def distinct_schemes() -> set[str]:
    """Every distinct `scheme` payload tag currently in the collection.

    Used by kb_ingest to tell a merely-smaller collection from a stale one: a
    scheme present in data/ but absent here can never be retrieved (search
    filters on an exact scheme match), so its questions all answer "not
    covered". Returns an empty set on any error, which the caller reads as
    "cannot confirm" and rebuilds — the safe direction.
    """
    client = get_client()
    schemes: set[str] = set()
    offset = None
    try:
        while True:
            points, offset = await client.scroll(
                collection_name=settings.QDRANT_COLLECTION,
                limit=512,
                offset=offset,
                with_payload=["scheme"],
                with_vectors=False,
            )
            for p in points:
                tag = (p.payload or {}).get("scheme")
                if tag:
                    schemes.add(tag)
            if offset is None:
                break
    except Exception as e:  # noqa: BLE001 — missing/unreachable collection
        logger.warning("qdrant: could not list scheme tags (%s)", e)
        return set()
    return schemes


async def recreate_collection(dim: int) -> None:
    client = get_client()
    await client.recreate_collection(
        collection_name=settings.QDRANT_COLLECTION,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
    )
    logger.info("qdrant collection %s (re)created, dim=%d", settings.QDRANT_COLLECTION, dim)


async def upsert(points: list[models.PointStruct]) -> None:
    client = get_client()
    await client.upsert(collection_name=settings.QDRANT_COLLECTION, points=points, wait=True)


async def search(vector: list[float], top_k: int, scheme: str | None = None) -> list[dict]:
    """Returns [{score, text, doc, heading, scheme}, ...] best score first.
    `scheme`, when given, restricts the search to chunks payload-tagged with
    that exact scheme — every chunk carries a `scheme` tag at ingest time
    (kb_ingest.py), but without this filter search runs unscoped across the
    whole collection and can blend another scheme's content into the answer."""
    client = get_client()
    query_filter = None
    if scheme:
        query_filter = models.Filter(
            must=[models.FieldCondition(key="scheme", match=models.MatchValue(value=scheme))]
        )
    # qdrant-client >= 1.10 replaced .search() with .query_points().
    response = await client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=vector,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )
    hits = response.points
    return [
        {
            "score": float(h.score),
            "text": h.payload.get("text", ""),
            "doc": h.payload.get("doc", ""),
            "heading": h.payload.get("heading", ""),
            "scheme": h.payload.get("scheme", ""),
            "source_type": h.payload.get("source_type", "sme"),
        }
        for h in hits
    ]
