"""
Shared FastAPI dependencies — request identity and role gates.

  current_scope   -> UserScope for the bearer token (or anonymous in dev mode)
  require_user    -> same, but 401 if there is no valid token
  require_admin   -> require_user + role must be admin/tenant_admin/super_admin
  require_super   -> super_admin only
"""
import logging

from fastapi import Depends, Header, HTTPException

from app import appdb, auth, security
from app.config import settings

logger = logging.getLogger(__name__)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def current_scope(authorization: str | None = Header(default=None)) -> auth.UserScope:
    """Never raises — returns an anonymous scope when auth is off and no token is
    given. Endpoints that must have a real user depend on require_user instead."""
    token = _bearer(authorization)
    if not token:
        if settings.AUTH_ENABLED:
            # Anonymous is allowed to reach current_scope; require_user is the gate.
            return auth.anonymous_scope()
        return auth.anonymous_scope()
    try:
        claims = security.decode_token(token)
    except security.JWTError as e:
        raise HTTPException(401, f"invalid token: {e}") from e
    rec = await appdb.get_user(int(claims["sub"]))
    if not rec or not rec.get("is_active"):
        raise HTTPException(401, "user not found or inactive")
    return auth.scope_from_user(rec)


async def require_user(scope: auth.UserScope = Depends(current_scope)) -> auth.UserScope:
    if scope.db_user_id is None:
        raise HTTPException(401, "authentication required")
    return scope


async def require_admin(scope: auth.UserScope = Depends(require_user)) -> auth.UserScope:
    if not scope.is_admin:
        raise HTTPException(403, "admin role required")
    return scope


async def require_super(scope: auth.UserScope = Depends(require_user)) -> auth.UserScope:
    if not scope.cross_tenant:
        raise HTTPException(403, "super_admin role required")
    return scope
