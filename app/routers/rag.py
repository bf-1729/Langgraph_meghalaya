"""RAG / knowledge-base management endpoints — Qdrant scheme KB."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from app import auth
from app import kb_ingest, vectorstore
from app.config import settings
from app.deps import current_scope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rag", tags=["rag"])


async def _require_user(scope: auth.UserScope = Depends(current_scope)) -> auth.UserScope:
    """A real user when AUTH_ENABLED; anonymous is fine in local dev mode."""
    if settings.AUTH_ENABLED and scope.db_user_id is None:
        raise HTTPException(401, "authentication required")
    return scope


async def _require_admin(scope: auth.UserScope = Depends(_require_user)) -> auth.UserScope:
    """Admin (A01) when AUTH_ENABLED; unguarded in local dev mode."""
    if settings.AUTH_ENABLED and not scope.is_admin:
        raise HTTPException(403, "admin role required")
    return scope


@router.get("/status")
async def status(_: auth.UserScope = Depends(_require_user)):
    count = await vectorstore.collection_count()
    return {
        "collection": settings.QDRANT_COLLECTION,
        "points": count if count >= 0 else 0,
        "present": count >= 0,
    }


@router.post("/reingest")
async def reingest(_: auth.UserScope = Depends(_require_admin)):
    """Force a full rebuild of the scheme KB collection from the data/ docs.
    Admin-only (A01) — it is an expensive, embed-heavy operation."""
    try:
        result = await kb_ingest.ingest_kb(force=True)
    except Exception as e:
        logger.exception("KB reingest failed")
        raise HTTPException(502, "reingest failed") from e
    return result
