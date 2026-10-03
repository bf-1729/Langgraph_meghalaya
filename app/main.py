import asyncio
import logging

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import appdb, pipeline, schema_introspect
from app.annotations import load_all as load_annotations
from app.config import insecure_secrets, settings
from app.db import close_pool, init_pool
from app.deps import current_scope
from app.entity_resolver import load_all as load_entity_resolver
from app.kb_ingest import ingest_kb
from app.llm import close_client, init_client
from app.middleware.limits import BodyLimitMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware, apply_security_headers
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.history import router as history_router
from app.routers.query import router as query_router
from app.routers.rag import router as rag_router
from app.vectorstore import close_qdrant, collection_count, init_qdrant

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nlp_service")

# app/main.py -> repo root -> web/
_FRONTEND = Path(__file__).resolve().parents[1] / "web"


async def _ingest_kb_bg():
    """KB ingest runs in the background so a cold Qdrant / slow embed batch
    never blocks the service from accepting traffic."""
    try:
        result = await ingest_kb()
        logger.info("scheme KB: %s", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("scheme KB ingest failed (RAG path degraded): %s", e)


async def _try(label: str, coro):
    """Run one startup step; log and continue if the remote box (10.48.242.4)
    is unreachable, so the server always comes up (degraded, see /health)."""
    try:
        await coro
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("startup step %r failed — continuing degraded: %s", label, e)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    bad = insecure_secrets()
    if bad:
        msg = ("insecure configuration — rotate before real traffic: "
               + "; ".join(bad) + " (see docs/SECURITY.md)")
        # Loud, but never blocks boot — the deploy checklist owns enforcement.
        (logger.critical if settings.ENV != "dev" else logger.warning)(msg)

    await init_pool()                     # best-effort; rebuilds lazily on first query
    await init_client()
    await _try("qdrant", init_qdrant())
    load_annotations()                    # local YAML, no network
    load_entity_resolver()                # local YAML, no network
    await _try("app_schema", appdb.ensure_schema())     # app.* tables + seed users
    await _try("schema_catalog", schema_introspect.load())  # semantic.* -> SQL prompt
    await _try("scheme_years", pipeline.refresh_scheme_years())  # per-scheme FY coverage from megh_db
    asyncio.create_task(_ingest_kb_bg())
    # LangGraph orchestrator (D-032): only when some traffic can reach it. The
    # checkpointer opens lazily on the first graph request; this sweeps paused
    # threads nobody answered (the Session's own pending pause expires anyway).
    _sweep = None
    if settings.PIPELINE_GRAPH_ENABLED or settings.PIPELINE_GRAPH_CANARY_PERCENT > 0:
        from app import pipeline_graph
        _sweep = asyncio.create_task(pipeline_graph.sweep_loop())
    logger.info("nlp-service startup complete (see /health for component status)")
    yield
    if _sweep is not None:
        _sweep.cancel()
        from app import pipeline_graph
        await pipeline_graph.close()
    await close_pool()
    await close_client()
    await close_qdrant()


# /docs, /redoc and /openapi.json are dev-only — they enumerate every route and
# schema for an attacker (A05). ENV=dev re-enables them.
_docs_kw = {} if settings.ENV == "dev" else dict(docs_url=None, redoc_url=None, openapi_url=None)
app = FastAPI(title=settings.APP_NAME, lifespan=lifespan, **_docs_kw)

# Middleware runs bottom-up on the way in: rate limit first, then (going out)
# body limit, then security headers outermost so every response — errors
# included — carries them.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodyLimitMiddleware)
if settings.CORS_ALLOW_ORIGINS.strip():
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Session-Id", "X-Request-ID"],
        max_age=600,
    )
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Last resort: log the trace server-side, return a generic body with the
    request id so support can correlate without the client ever seeing internals
    (A05 — no stack traces / DB errors in responses)."""
    rid = getattr(request.state, "request_id", "-")
    logger.exception("unhandled error (request_id=%s) on %s", rid, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "internal error", "request_id": rid})
    resp.headers["X-Request-ID"] = rid
    apply_security_headers(resp.headers, request.url.path)
    return resp


app.include_router(auth_router)
app.include_router(query_router)
app.include_router(rag_router)
app.include_router(history_router)
app.include_router(admin_router)


async def _privileged_view(authorization: str | None, x_metrics_token: str | None) -> bool:
    """True when the caller may see internal detail on /health and /metrics —
    a valid X-Metrics-Token, or an admin JWT."""
    if settings.METRICS_TOKEN and x_metrics_token == settings.METRICS_TOKEN:
        return True
    try:
        scope = await current_scope(authorization)
        return bool(scope.is_admin)
    except HTTPException:
        return False


@app.get("/health")
async def health(authorization: str | None = Header(default=None),
                 x_metrics_token: str | None = Header(default=None)):
    """ok when every dependency answers; degraded (with the failing component
    named) otherwise. Kept cheap — no model call.

    Anonymous callers get only {"status": ...} — which dependency is down, and
    whether secrets are unrotated, is internal detail (A01/A05). An admin JWT or
    the metrics token unlocks the full breakdown."""
    privileged = await _privileged_view(authorization, x_metrics_token)
    components: dict[str, str] = {}

    try:
        from app.db import fetch_rows

        await fetch_rows("SELECT 1")
        components["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        components["db"] = f"error: {e}"

    try:
        qcount = await collection_count()
        components["qdrant"] = "ok" if qcount >= 0 else "collection missing"
    except Exception as e:  # noqa: BLE001 — health must never 500
        components["qdrant"] = f"error: {e}"

    # Core dependencies decide the status; the Redis L2 cache is best-effort and
    # reported for visibility only (in-process cache covers a Redis outage).
    status = "ok" if all(v == "ok" for v in components.values()) else "degraded"

    from app.cache import redis_healthy

    rh = await redis_healthy()
    if rh is not None:
        components["redis_cache"] = "ok" if rh else "unreachable (in-process cache active)"

    if settings.PIPELINE_GRAPH_ENABLED or settings.PIPELINE_GRAPH_CANARY_PERCENT > 0:
        from app import pipeline_graph
        components["pipeline_graph"] = pipeline_graph.status()["checkpointer"]

    if not privileged:
        return {"status": status}

    payload = {"status": status, "components": components}
    bad = insecure_secrets()
    if bad:
        payload["insecure_config"] = bad
    return payload


@app.get("/metrics")
async def metrics_endpoint(authorization: str | None = Header(default=None),
                           x_metrics_token: str | None = Header(default=None)):
    """Plain-JSON operational counters — request volume by route, error counts,
    busy rejections, latency percentiles, and cache hit rate. Poll this to
    confirm the service is inside the 20-40 concurrent / 200 DAU budget.

    Gated (A01): send X-Metrics-Token (when METRICS_TOKEN is set) or an admin JWT."""
    if not await _privileged_view(authorization, x_metrics_token):
        raise HTTPException(401, "metrics require X-Metrics-Token or an admin token")
    from app.cache import metrics, response_cache
    from app.semantic_cache import semantic_cache
    from app.session_store import session_store

    return {
        **metrics.snapshot(),
        "response_cache": response_cache.stats(),
        "semantic_cache": semantic_cache.stats(),
        "sessions": session_store.stats(),
    }


# ── Static UI — the one service also serves the portal + chat console ─────────
# The pages are served with no-cache (revalidate every load; the ETag keeps it a
# cheap 304). Without it Chrome heuristically reused a stale ai_query.html after
# a fix shipped, so the user kept running the old voice-input code.
_NO_CACHE = {"Cache-Control": "no-cache"}

if _FRONTEND.is_dir():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    async def portal():
        f = _FRONTEND / "Meghalaya_UnifiedPortal_UI.html"
        return FileResponse(f, headers=_NO_CACHE) if f.exists() else JSONResponse({"app": settings.APP_NAME})

    @app.get("/ai-query", include_in_schema=False)
    async def ai_query():
        f = _FRONTEND / "ai_query.html"
        return FileResponse(f, headers=_NO_CACHE) if f.exists() else JSONResponse({"app": settings.APP_NAME})

    @app.get("/admin-ui", include_in_schema=False)
    async def admin_ui():
        f = _FRONTEND / "admin.html"
        return FileResponse(f, headers=_NO_CACHE) if f.exists() else JSONResponse({"error": "admin.html missing"})
else:
    logger.warning("web/ not found at %s — API-only mode", _FRONTEND)
