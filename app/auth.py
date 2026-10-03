"""
Authorization — compare the caller's role and scope against what a query is
actually asking for, and refuse politely when the query reaches past the role.

Spec 5.7: three checks against a role-to-permissions map plus a per-session
user-scope object —
  1. granularity vs role cap   (state < district < block < village)
  2. geography scope           (which districts/blocks the user may see)
  3. scheme scope              (which schemes the user may query)

The checks are pure Python. Users/tenants/audit live in the `app` schema of
megh_db (see backend/appdb.py); a JSONL mirror of the audit trail is also kept
for offline grep. Admin roles bypass the three query checks but stay
tenant-scoped for what they can *see*.
"""
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]  # repo root

# ── Granularity lattice ─────────────────────────────────────────────────────
# Coarse -> fine. A role's cap is the finest level it may ask for.
GRANULARITY_ORDER = ["state", "district", "block", "village"]


def _finer(a: str, b: str) -> bool:
    """True if a is strictly finer-grained than b."""
    return GRANULARITY_ORDER.index(a) > GRANULARITY_ORDER.index(b)


# ── Role -> permissions ────────────────────────────────────────────────────
# A user record may narrow these (e.g. pin `districts`), never widen them.
# `geographies: "all"` = no geographic restriction.
ROLE_PERMISSIONS: dict[str, dict] = {
    # Platform + tenant administration. is_admin bypasses the three *query*
    # checks (an admin exploring data isn't 5.7's threat model) but stays
    # tenant-scoped for what they can see (conversations, audit, users).
    "super_admin": {
        "granularity_cap": "village", "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all", "is_admin": True, "cross_tenant": True,
    },
    "tenant_admin": {
        "granularity_cap": "village", "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all", "is_admin": True,
    },
    "admin": {
        "granularity_cap": "village",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all",
        "is_admin": True,
    },
    "state_officer": {
        "granularity_cap": "district",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all",
    },
    "district_officer": {
        "granularity_cap": "block",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": {"districts": [], "blocks": []},  # must be pinned per user
    },
    "block_officer": {
        "granularity_cap": "village",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": {"districts": [], "blocks": []},
    },
    "analyst": {
        "granularity_cap": "village",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all",
    },
    # No identity on the request -> this. Statewide aggregates only.
    "public": {
        "granularity_cap": "state",
        "schemes": ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
                    "CM Elevate Legacy"],
        "geographies": "all",
    },
}


@dataclass
class UserScope:
    user_id: str
    role: str
    granularity_cap: str
    schemes: list[str]
    geographies: object  # "all" | {"districts": [UPPER...], "blocks": [UPPER...]}
    name: str = ""
    tenant_id: int | None = None
    db_user_id: int | None = None
    username: str = ""

    @property
    def is_admin(self) -> bool:
        return bool(ROLE_PERMISSIONS.get(self.role, {}).get("is_admin"))

    @property
    def cross_tenant(self) -> bool:
        return bool(ROLE_PERMISSIONS.get(self.role, {}).get("cross_tenant"))

    @property
    def geo_restricted(self) -> bool:
        return self.geographies != "all"

    def allowed_districts(self) -> list[str]:
        return [] if self.geographies == "all" else list(self.geographies.get("districts", []))

    def allowed_blocks(self) -> list[str]:
        return [] if self.geographies == "all" else list(self.geographies.get("blocks", []))

    def cache_fingerprint(self) -> str:
        """Stable identity for response-cache bucketing. Two scopes with the same
        fingerprint are guaranteed the same authorization outcome for any given
        question — the auth decision is a pure function of (scope, schemes,
        resolved_entities, sql) — so they may safely share a cached answer.
        tenant_id is deliberately excluded: the curated warehouse the query path
        reads is not tenant-partitioned, so it never changes the answer."""
        if self.geographies == "all":
            geo = "geo=all"
        else:
            geo = ("d=" + ",".join(sorted(self.allowed_districts()))
                   + "|b=" + ",".join(sorted(self.allowed_blocks())))
        return ";".join((
            f"role={self.role}",
            f"gran={self.granularity_cap}",
            f"schemes={','.join(sorted(self.schemes))}",
            geo,
        ))


@dataclass
class AuthDecision:
    allow: bool
    reason: str = ""
    query_granularity: str = "state"
    check: str = ""  # which check denied: "scheme" | "geography" | "granularity"


def _upper_list(values) -> list[str]:
    return [str(v).strip().upper() for v in (values or []) if str(v).strip()]


def anonymous_scope() -> UserScope:
    """AUTH_ENABLED=false dev mode only — real requests always carry a JWT."""
    base = ROLE_PERMISSIONS.get(settings.AUTH_DEFAULT_ROLE, ROLE_PERMISSIONS["analyst"])
    return UserScope(user_id="(anonymous)", role=settings.AUTH_DEFAULT_ROLE,
                     granularity_cap=base["granularity_cap"], schemes=list(base["schemes"]),
                     geographies="all", name="anonymous")


def scope_from_user(rec: dict) -> UserScope:
    """Build the effective scope from an app.users row. The role sets the caps;
    the per-user district/block/scheme lists only narrow them, never widen."""
    role = rec.get("role") or settings.AUTH_DEFAULT_ROLE
    base = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[settings.AUTH_DEFAULT_ROLE])

    schemes = list(base["schemes"])
    if rec.get("schemes"):
        allowed = {str(s).strip() for s in rec["schemes"]}
        schemes = [s for s in schemes if s in allowed] or schemes

    districts, blocks = _upper_list(rec.get("districts")), _upper_list(rec.get("blocks"))
    if base["geographies"] == "all" and not districts and not blocks:
        geographies: object = "all"
    else:
        geographies = {"districts": districts, "blocks": blocks}

    return UserScope(
        user_id=rec.get("username") or str(rec.get("user_id")),
        username=rec.get("username", ""),
        db_user_id=rec.get("user_id"),
        tenant_id=rec.get("tenant_id"),
        role=role,
        granularity_cap=rec.get("granularity_cap") or base["granularity_cap"],
        schemes=schemes,
        geographies=geographies,
        name=rec.get("name") or "",
    )


# ── Query inspection ──────────────────────────────────────────────────────
_DISTRICT_LITERAL = re.compile(r"lgd_district\s*(?:=|like|in)\s*\(?\s*'([^']+)'", re.IGNORECASE)
_BLOCK_LITERAL = re.compile(r"lgd_block\s*(?:=|like|in)\s*\(?\s*'([^']+)'", re.IGNORECASE)
_GROUPS_VILLAGE = re.compile(r"group by[^;]*\b(village_code|lgd_village_name|geography_key)\b", re.IGNORECASE)
_GROUPS_BLOCK = re.compile(r"group by[^;]*\blgd_block\b", re.IGNORECASE)
_GROUPS_DISTRICT = re.compile(r"group by[^;]*\blgd_district\b", re.IGNORECASE)
_MENTIONS_VILLAGE = re.compile(r"\b(village_code|lgd_village_name)\b", re.IGNORECASE)
_MENTIONS_BLOCK = re.compile(r"\blgd_block\b", re.IGNORECASE)
_MENTIONS_DISTRICT = re.compile(r"\blgd_district\b", re.IGNORECASE)


def infer_granularity(resolved_entities: dict, sql: str | None) -> str:
    """The finest level the query touches — from resolved entities first, then
    the SQL's GROUP BY, then any column mention."""
    e = resolved_entities or {}
    sql = sql or ""
    if e.get("village_code") or e.get("village") or _GROUPS_VILLAGE.search(sql) or _MENTIONS_VILLAGE.search(sql):
        return "village"
    if e.get("block") or _GROUPS_BLOCK.search(sql) or _MENTIONS_BLOCK.search(sql):
        return "block"
    if e.get("district") or _GROUPS_DISTRICT.search(sql) or _MENTIONS_DISTRICT.search(sql):
        return "district"
    return "state"


def _districts_in_sql(sql: str | None) -> list[str]:
    return _upper_list(_DISTRICT_LITERAL.findall(sql or ""))


def _blocks_in_sql(sql: str | None) -> list[str]:
    return _upper_list(_BLOCK_LITERAL.findall(sql or ""))


def authorize(scope: UserScope, *, schemes: list[str], resolved_entities: dict,
              sql: str | None) -> AuthDecision:
    """The three checks, in order of cost. Returns allow/deny + a user-facing reason."""
    gran = infer_granularity(resolved_entities, sql)

    # Admins bypass the query checks (5.7 targets officers reaching past their
    # posting, not administrators). They are still tenant-scoped elsewhere.
    if scope.is_admin:
        return AuthDecision(True, "", gran, "")

    # 1. scheme scope
    over = [s for s in (schemes or []) if s not in scope.schemes]
    if over:
        return AuthDecision(
            False,
            f"This query relates to {', '.join(over)}. Your account is authorised "
            f"for {', '.join(scope.schemes)} only. Please raise a query within the "
            f"authorised scheme, or request scheme access from your department "
            f"administrator.",
            gran, "scheme",
        )

    # 2. geography scope
    if scope.geo_restricted:
        allowed_d = set(scope.allowed_districts())
        allowed_b = set(scope.allowed_blocks())
        want_d = set()
        if (resolved_entities or {}).get("district"):
            want_d.add(str(resolved_entities["district"]).upper())
        want_d.update(_districts_in_sql(sql))
        want_b = set(_blocks_in_sql(sql))
        if (resolved_entities or {}).get("block"):
            want_b.add(str(resolved_entities["block"]).upper())

        stray_d = {d for d in want_d if d not in allowed_d}
        if stray_d:
            return AuthDecision(
                False,
                f"This query relates to {', '.join(sorted(stray_d))}. Your account is "
                f"authorised for "
                f"{', '.join(sorted(allowed_d)) or 'no district at present'}. "
                f"Please restrict the query to your jurisdiction, or request revised "
                f"access from your department administrator.",
                gran, "geography",
            )
        if allowed_b and want_b:
            stray_b = {b for b in want_b if b not in allowed_b}
            if stray_b:
                return AuthDecision(
                    False,
                    f"This query relates to {', '.join(sorted(stray_b))} block. Your "
                    f"account is authorised for "
                    f"{', '.join(sorted(allowed_b)) or 'no block at present'}. "
                    f"Please restrict the query to your jurisdiction, or request "
                    f"revised access from your department administrator.",
                    gran, "geography",
                )
        # Geo-restricted user asking a question with no geography filter at
        # district-or-finer grain would return data outside their area.
        if not want_d and not want_b and _finer(gran, "state"):
            return AuthDecision(
                False,
                f"This query does not specify an area. Your account is authorised "
                f"for {', '.join(sorted(allowed_d)) or 'your assigned jurisdiction'}, "
                f"and a query at this level of detail must name the jurisdiction "
                f"concerned — for example, "
                f"\"... in {sorted(allowed_d)[0] if allowed_d else 'your district'}\".",
                gran, "geography",
            )

    # 3. granularity vs role cap
    if _finer(gran, scope.granularity_cap):
        return AuthDecision(
            False,
            f"This query requires data at {gran} level. Your account is authorised "
            f"up to {scope.granularity_cap} level. Please raise the query at "
            f"{scope.granularity_cap} level, or request revised access from your "
            f"department administrator.",
            gran, "granularity",
        )

    return AuthDecision(True, "", gran, "")


# ── Audit trail (JSONL append) ────────────────────────────────────────────
_audit_path = (_ROOT / settings.AUDIT_FILE) if not Path(settings.AUDIT_FILE).is_absolute() \
    else Path(settings.AUDIT_FILE)
_audit_lock = threading.Lock()


def audit(record: dict) -> None:
    """Append one query record. Best-effort — an audit failure must not fail the
    request."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    try:
        with _audit_lock:
            _audit_path.parent.mkdir(parents=True, exist_ok=True)
            with _audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning("auth.audit: could not write audit record: %s", e)


def read_audit(limit: int = 200, user_id: str | None = None) -> list[dict]:
    try:
        lines = _audit_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if user_id and rec.get("user_id") != user_id:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out
