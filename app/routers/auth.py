"""
/api/auth — officer login (username + password -> JWT), identity, logout,
and one-time super_admin bootstrap.
"""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import appdb, auth, security
from app.config import settings
from app.deps import require_user
from app.net import client_ip as _resolve_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class BootstrapBody(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=256)
    name: str | None = Field(default=None, max_length=200)
    admin_token: str | None = Field(default=None, max_length=512)


def _client_ip(request: Request) -> str:
    return _resolve_ip(request)


@router.post("/login")
async def login(body: LoginBody, request: Request):
    ua = request.headers.get("user-agent", "")
    ip = _client_ip(request)
    rec = await appdb.get_user_by_username(body.username.strip())
    if not rec or not rec.get("is_active") or not security.verify_password(
        body.password, rec.get("password_hash", "")
    ):
        await appdb.log_login(event="login_fail", ok=False, username=body.username,
                              user_id=(rec or {}).get("user_id"),
                              tenant_id=(rec or {}).get("tenant_id"), ip=ip, user_agent=ua,
                              detail="bad credentials or inactive")
        raise HTTPException(401, "invalid username or password")

    scope = auth.scope_from_user(rec)
    token = security.issue_token({
        "sub": str(rec["user_id"]),
        "username": rec["username"],
        "tenant_id": rec["tenant_id"],
        "role": rec["role"],
    })
    await appdb.touch_login(rec["user_id"])
    await appdb.log_login(event="login_ok", ok=True, username=rec["username"],
                          user_id=rec["user_id"], tenant_id=rec["tenant_id"],
                          ip=ip, user_agent=ua)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in_minutes": settings.JWT_TTL_MINUTES,
        "user": _public_user(rec, scope),
    }


@router.post("/logout")
async def logout(request: Request, scope: auth.UserScope = Depends(require_user)):
    # Stateless JWT — logout is advisory (client drops the token). We log it.
    await appdb.log_login(event="logout", ok=True, username=scope.username,
                          user_id=scope.db_user_id, tenant_id=scope.tenant_id,
                          ip=_client_ip(request))
    return {"ok": True}


@router.get("/me")
async def me(scope: auth.UserScope = Depends(require_user)):
    tenant = await appdb.get_tenant(scope.tenant_id) if scope.tenant_id else None
    return {
        "user_id": scope.db_user_id,
        "username": scope.username,
        "name": scope.name,
        "role": scope.role,
        "is_admin": scope.is_admin,
        "tenant": tenant,
        "scope": {
            "granularity_cap": scope.granularity_cap,
            "schemes": scope.schemes,
            "geographies": scope.geographies,
        },
    }


@router.post("/bootstrap")
async def bootstrap(body: BootstrapBody):
    """Create the first super_admin. Allowed only while app.users is empty
    (or with the correct ADMIN_TOKEN if one is configured)."""
    count = await appdb.db.fetchval("SELECT count(*) FROM app.users")
    if int(count or 0) > 0:
        if not settings.ADMIN_TOKEN or body.admin_token != settings.ADMIN_TOKEN:
            raise HTTPException(403, "bootstrap is closed (users already exist)")
    tid = await appdb._seed_default_tenant()
    try:
        rec = await appdb.create_user(
            tenant_id=tid, username=body.username.strip(), password=body.password,
            name=body.name or "Super Admin", role="super_admin", created_by="bootstrap",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("bootstrap create_user failed: %s", e)
        raise HTTPException(422, "could not create super_admin") from e
    await appdb.log_login(event="bootstrap", ok=True, username=rec["username"],
                          user_id=rec["user_id"], tenant_id=tid)
    return {"created": _public_user(rec, auth.scope_from_user(rec))}


def _public_user(rec: dict, scope: auth.UserScope) -> dict:
    return {
        "user_id": rec["user_id"],
        "username": rec["username"],
        "name": rec.get("name"),
        "role": rec["role"],
        "tenant_id": rec["tenant_id"],
        "is_admin": scope.is_admin,
        "districts": list(rec.get("districts") or []),
        "blocks": list(rec.get("blocks") or []),
        "schemes": list(rec.get("schemes") or []),
        "granularity_cap": scope.granularity_cap,
    }
