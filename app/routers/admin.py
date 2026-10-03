"""
Admin API — tenant + user management, login events, officer conversations,
and the query audit trail. All routes require an admin JWT
(admin / tenant_admin / super_admin). tenant_admin sees only their own tenant;
super_admin may pass ?tenant_id= to scope to any tenant (or omit for all).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import appdb, auth, conversation_store, schema_introspect
from app.deps import require_admin, require_super

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def _tenant_filter(scope: auth.UserScope, tenant_id: int | None) -> int | None:
    """What tenant_id to filter reads by. tenant_admin is pinned to their own;
    super_admin gets what they ask for (None = all tenants)."""
    if scope.cross_tenant:
        return tenant_id
    return scope.tenant_id


# ── tenants (super_admin) ────────────────────────────────────────────────
class TenantBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)


@router.get("/tenants")
async def list_tenants(scope: auth.UserScope = Depends(require_admin)):
    tenants = await appdb.list_tenants()
    if not scope.cross_tenant:
        tenants = [t for t in tenants if t["tenant_id"] == scope.tenant_id]
    return {"tenants": tenants}


@router.post("/tenants")
async def create_tenant(body: TenantBody, scope: auth.UserScope = Depends(require_super)):
    try:
        return {"tenant": await appdb.create_tenant(body.code, body.name)}
    except Exception as e:  # noqa: BLE001
        logger.warning("create_tenant failed: %s", e)
        raise HTTPException(422, "could not create tenant (code may already exist)") from e


# ── users ───────────────────────────────────────────────────────────────
class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=256)
    name: str | None = Field(default=None, max_length=200)
    role: str = Field(min_length=1, max_length=40)
    tenant_id: int | None = None          # super_admin only; else caller's tenant
    districts: list[str] | None = Field(default=None, max_length=64)
    blocks: list[str] | None = Field(default=None, max_length=256)
    schemes: list[str] | None = Field(default=None, max_length=16)
    granularity_cap: str | None = Field(default=None, max_length=40)


class UpdateUser(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=40)
    districts: list[str] | None = Field(default=None, max_length=64)
    blocks: list[str] | None = Field(default=None, max_length=256)
    schemes: list[str] | None = Field(default=None, max_length=16)
    granularity_cap: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


@router.get("/roles")
async def roles(_: auth.UserScope = Depends(require_admin)):
    return {"roles": auth.ROLE_PERMISSIONS}


@router.get("/users")
async def list_users(tenant_id: int | None = None,
                     scope: auth.UserScope = Depends(require_admin)):
    return {"users": await appdb.list_users(_tenant_filter(scope, tenant_id))}


@router.get("/users/{user_id}")
async def get_user(user_id: int, scope: auth.UserScope = Depends(require_admin)):
    rec = await appdb.get_user(user_id)
    if not rec:
        raise HTTPException(404, "no such user")
    if not scope.cross_tenant and rec["tenant_id"] != scope.tenant_id:
        raise HTTPException(403, "user belongs to another tenant")
    return {"user": rec, "effective_scope": auth.scope_from_user(rec).__dict__}


@router.post("/users")
async def create_user(body: NewUser, scope: auth.UserScope = Depends(require_admin)):
    if body.role not in auth.ROLE_PERMISSIONS:
        raise HTTPException(422, f"role must be one of {sorted(auth.ROLE_PERMISSIONS)}")
    if body.role in ("super_admin",) and not scope.cross_tenant:
        raise HTTPException(403, "only super_admin can create super_admin")
    tenant_id = body.tenant_id if scope.cross_tenant and body.tenant_id else scope.tenant_id
    try:
        rec = await appdb.create_user(
            tenant_id=tenant_id, username=body.username, password=body.password,
            name=body.name, role=body.role, districts=body.districts, blocks=body.blocks,
            schemes=body.schemes, granularity_cap=body.granularity_cap,
            created_by=scope.username,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("create_user failed: %s", e)
        raise HTTPException(422, "could not create user (username may already exist)") from e
    return {"user": rec, "effective_scope": auth.scope_from_user(rec).__dict__}


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UpdateUser,
                      scope: auth.UserScope = Depends(require_admin)):
    rec = await appdb.get_user(user_id)
    if not rec:
        raise HTTPException(404, "no such user")
    if not scope.cross_tenant and rec["tenant_id"] != scope.tenant_id:
        raise HTTPException(403, "user belongs to another tenant")
    if body.role and body.role not in auth.ROLE_PERMISSIONS:
        raise HTTPException(422, "bad role")
    updated = await appdb.update_user(user_id, **body.model_dump(exclude_none=True))
    return {"user": updated}


@router.delete("/users/{user_id}")
async def deactivate_user(user_id: int, hard: bool = False,
                          scope: auth.UserScope = Depends(require_admin)):
    """Soft-deactivate by default (is_active = FALSE). Pass ?hard=true to
    permanently delete the row — super_admin only, and never your own account."""
    rec = await appdb.get_user(user_id)
    if not rec:
        raise HTTPException(404, "no such user")
    if not scope.cross_tenant and rec["tenant_id"] != scope.tenant_id:
        raise HTTPException(403, "user belongs to another tenant")
    if scope.db_user_id == user_id:
        raise HTTPException(403, "you cannot delete your own account")
    if hard:
        if not scope.cross_tenant:
            raise HTTPException(403, "only super_admin can permanently delete a user")
        await appdb.purge_user(user_id)
        return {"deleted": user_id}
    await appdb.delete_user(user_id)
    return {"deactivated": user_id}


# ── login events ────────────────────────────────────────────────────────
@router.get("/logins")
async def logins(tenant_id: int | None = None, limit: int = Query(200, ge=1, le=1000),
                 scope: auth.UserScope = Depends(require_admin)):
    return {"events": await appdb.list_login_events(_tenant_filter(scope, tenant_id), limit)}


# ── officer conversations ───────────────────────────────────────────────
@router.get("/conversations")
async def conversations(tenant_id: int | None = None, user_id: int | None = None,
                        limit: int = Query(100, ge=1, le=1000),
                        scope: auth.UserScope = Depends(require_admin)):
    return {"conversations": await conversation_store.admin_list_conversations(
        tenant_id=_tenant_filter(scope, tenant_id), user_id=user_id, limit=limit)}


@router.get("/conversations/{conv_id}")
async def conversation_detail(conv_id: int, scope: auth.UserScope = Depends(require_admin)):
    turns = await conversation_store.get_conversation_turns(conv_id=conv_id, limit=500)
    if turns and not scope.cross_tenant and turns[0]["conv_id"]:
        # tenant check via the first turn's tenant
        t0 = await appdb.db.fetchrow(
            "SELECT tenant_id FROM app.conversation_turns WHERE conv_id = $1 LIMIT 1", [conv_id])
        if t0 and t0["tenant_id"] != scope.tenant_id:
            raise HTTPException(403, "conversation belongs to another tenant")
    return {"conv_id": conv_id, "turns": turns}


# ── audit ──────────────────────────────────────────────────────────────
@router.get("/audit")
async def audit(tenant_id: int | None = None, user_id: int | None = None,
                denied_only: bool = False, limit: int = Query(200, ge=1, le=2000),
                scope: auth.UserScope = Depends(require_admin)):
    recs = await conversation_store.admin_list_audit(
        tenant_id=_tenant_filter(scope, tenant_id), user_id=user_id,
        denied_only=denied_only, limit=limit)
    return {"count": len(recs), "records": recs}


@router.get("/stats")
async def stats(tenant_id: int | None = None, scope: auth.UserScope = Depends(require_admin)):
    return await conversation_store.tenant_stats(_tenant_filter(scope, tenant_id))


@router.get("/schema")
async def schema(_: auth.UserScope = Depends(require_admin)):
    return schema_introspect.snapshot()
