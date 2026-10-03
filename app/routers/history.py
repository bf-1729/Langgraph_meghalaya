"""
/api/history — an officer's own recent conversations. Officer-private: every
query is filtered by the caller's user_id, so one officer can never read
another's chats (only an admin can, via /admin/conversations).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import auth, conversation_store
from app.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/history", tags=["history"])


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class PinRequest(BaseModel):
    pinned: bool = True


class ArchiveRequest(BaseModel):
    archived: bool = True


@router.get("")
async def my_conversations(limit: int = Query(60, ge=1, le=500),
                           q: str | None = Query(None, max_length=200),
                           archived: bool | None = None,
                           scope: auth.UserScope = Depends(require_user)):
    """The sidebar list — every thread in one call, ordered so the client can
    render Pinned / Recent / Archived as sections. `archived=true|false`
    restricts to one shelf; `q` searches across all of them."""
    if q and q.strip():
        convs = await conversation_store.search_my_conversations(
            user_id=scope.db_user_id, q=q, limit=limit, archived=None)
    else:
        convs = await conversation_store.list_my_conversations(
            scope.db_user_id, limit, archived=archived)
    return {"conversations": convs}


@router.get("/{session_id}")
async def my_conversation(session_id: str, scope: auth.UserScope = Depends(require_user)):
    turns = await conversation_store.get_conversation_turns(
        session_id=session_id, user_id=scope.db_user_id, limit=500)
    if not turns:
        raise HTTPException(404, "no conversation for that session, or not yours")
    return {"session_id": session_id, "turns": turns}


@router.patch("/{session_id}")
async def rename_my_conversation(session_id: str, body: RenameRequest,
                                 scope: auth.UserScope = Depends(require_user)):
    """Retitle a thread. The auto-title is the first question, truncated."""
    if not body.title or not body.title.strip():
        raise HTTPException(400, "title must not be empty")
    ok = await conversation_store.rename_conversation(
        session_id=session_id, user_id=scope.db_user_id, title=body.title)
    if not ok:
        raise HTTPException(404, "no conversation for that session, or not yours")
    return {"session_id": session_id, "title": body.title.strip()[:120]}


@router.delete("/{session_id}")
async def delete_my_conversation(session_id: str,
                                 scope: auth.UserScope = Depends(require_user)):
    """Remove a thread from my sidebar. The audit trail is retained."""
    ok = await conversation_store.delete_conversation(
        session_id=session_id, user_id=scope.db_user_id)
    if not ok:
        raise HTTPException(404, "no conversation for that session, or not yours")
    return {"deleted": session_id}


@router.post("/{session_id}/pin")
async def pin_my_conversation(session_id: str, body: PinRequest,
                              scope: auth.UserScope = Depends(require_user)):
    """Pin/unpin a thread to the top of my sidebar."""
    ok = await conversation_store.set_pinned(
        session_id=session_id, user_id=scope.db_user_id, pinned=body.pinned)
    if not ok:
        raise HTTPException(404, "no conversation for that session, or not yours")
    return {"session_id": session_id, "pinned": body.pinned}


@router.post("/{session_id}/archive")
async def archive_my_conversation(session_id: str, body: ArchiveRequest,
                                  scope: auth.UserScope = Depends(require_user)):
    """Archive/unarchive a thread. Archiving keeps the data and the audit trail
    — it only moves the thread off the active shelf."""
    ok = await conversation_store.set_archived(
        session_id=session_id, user_id=scope.db_user_id, archived=body.archived)
    if not ok:
        raise HTTPException(404, "no conversation for that session, or not yours")
    return {"session_id": session_id, "archived": body.archived}
