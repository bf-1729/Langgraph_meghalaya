"""
In-process response cache + lightweight metrics.

At 200 DAU a large share of questions repeat within minutes — the same
"houses completed by district", the same "who is eligible for PMAY-G". Serving
those from memory removes 2-4 model calls each and is the single biggest lever
for staying inside the 20-40 concurrent budget.

In-process (per worker) is L1 — 2 workers × 2 VMs = 4 independent caches. When
REDIS_URL is set, a shared Redis is L2: a question answered on any worker is then
served from cache on all of them. Redis is strictly best-effort — an unset URL,
an auth failure or a network blip degrades silently to in-process-only and never
fails a request.

Nothing here is authoritative — every entry expires, so a DB reload is visible
within RESPONSE_CACHE_TTL_SECONDS.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque

from app.config import settings

logger = logging.getLogger("nlp_service.cache")

_WS = re.compile(r"\s+")
# Unit separator — cannot occur in a normalized question or a scope fingerprint,
# so it joins the two into one cache key without any risk of collision.
_KEYSEP = "\x1f"

_redis_client = None
_redis_lock = asyncio.Lock()


async def _redis():
    """Lazily create one shared redis.asyncio client. Returns None — and caches
    that None — whenever REDIS_URL is unset or the server can't be reached, so
    callers transparently fall back to the in-process store."""
    global _redis_client
    if not settings.REDIS_URL:
        return None
    if _redis_client is not None:
        return _redis_client
    async with _redis_lock:
        if _redis_client is None:
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                    socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT,
                )
                await client.ping()
                _redis_client = client
                logger.info(
                    "response cache: Redis L2 connected (%s)",
                    settings.REDIS_URL.rsplit("@", 1)[-1],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "response cache: Redis unavailable — in-process only (%s)", e
                )
                _redis_client = None
    return _redis_client


async def redis_healthy() -> bool | None:
    """True / False when REDIS_URL is configured; None when the feature is off."""
    if not settings.REDIS_URL:
        return None
    try:
        r = await _redis()
        if r is None:
            return False
        await r.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _redis_key(key: str) -> str:
    return settings.REDIS_CACHE_PREFIX + hashlib.md5(key.encode()).hexdigest()


def normalize(question: str) -> str:
    return _WS.sub(" ", question.strip().lower()).rstrip("?.! ")


def cache_key(question: str, scope_key: str = "") -> str:
    """Cache entries are scoped: the same question asked by two users with
    different authorization fingerprints must not share an answer."""
    return normalize(question) + _KEYSEP + scope_key


class ResponseCache:
    def __init__(self, ttl: float, max_entries: int):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: "OrderedDict[str, tuple[dict, float]]" = OrderedDict()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.redis_hits = 0

    async def get(self, question: str, scope_key: str = "") -> dict | None:
        if not settings.RESPONSE_CACHE_ENABLED:
            return None
        key = cache_key(question, scope_key)
        async with self._lock:
            item = self._store.get(key)
            if item is not None:
                value, expiry = item
                if time.monotonic() <= expiry:
                    self._store.move_to_end(key)
                    self.hits += 1
                    return {**value, "cached": True}
                del self._store[key]

        # L1 miss — consult the shared Redis L2 (no-op when unconfigured/down).
        value = await self._redis_get(key)
        if value is not None:
            async with self._lock:
                self._store[key] = (value, time.monotonic() + self.ttl)
                self._store.move_to_end(key)
                while len(self._store) > self.max_entries:
                    self._store.popitem(last=False)
            self.hits += 1
            self.redis_hits += 1
            return {**value, "cached": True}

        self.misses += 1
        return None

    async def put(self, question: str, value: dict, scope_key: str = "") -> None:
        if not settings.RESPONSE_CACHE_ENABLED:
            return
        # Never cache a clarification prompt or an error placeholder — they are
        # not stable answers.
        if value.get("needs_clarification") or value.get("confidence") == "low":
            return
        key = cache_key(question, scope_key)
        async with self._lock:
            self._store[key] = (value, time.monotonic() + self.ttl)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)
        await self._redis_put(key, value)

    async def _redis_get(self, key: str) -> dict | None:
        r = await _redis()
        if r is None:
            return None
        try:
            raw = await r.get(_redis_key(key))
            return json.loads(raw) if raw else None
        except Exception as e:  # noqa: BLE001
            logger.debug("redis get failed: %s", e)
            return None

    async def _redis_put(self, key: str, value: dict) -> None:
        r = await _redis()
        if r is None:
            return
        try:
            await r.setex(
                _redis_key(key), int(self.ttl), json.dumps(value, default=str)
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("redis put failed: %s", e)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "redis_hits": self.redis_hits,
            "redis": "on" if settings.REDIS_URL else "off",
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


class Metrics:
    """Counters + a rolling latency window for /metrics. No external deps."""

    def __init__(self, window: int = 512):
        self.requests = 0
        self.by_route: dict[str, int] = {}
        self.errors = 0
        self.model_errors = 0
        self.busy_rejections = 0
        self._latencies: deque[float] = deque(maxlen=window)

    def record(self, route: str, latency_ms: float) -> None:
        self.requests += 1
        self.by_route[route] = self.by_route.get(route, 0) + 1
        self._latencies.append(latency_ms)

    def _pct(self, p: float) -> float:
        if not self._latencies:
            return 0.0
        ordered = sorted(self._latencies)
        idx = min(len(ordered) - 1, int(p / 100 * len(ordered)))
        return round(ordered[idx], 1)

    def snapshot(self) -> dict:
        return {
            "requests_total": self.requests,
            "requests_by_route": dict(self.by_route),
            "errors_total": self.errors,
            "model_errors_total": self.model_errors,
            "busy_rejections_total": self.busy_rejections,
            "latency_ms_p50": self._pct(50),
            "latency_ms_p95": self._pct(95),
            "latency_ms_p99": self._pct(99),
        }


response_cache = ResponseCache(
    ttl=settings.RESPONSE_CACHE_TTL_SECONDS,
    max_entries=settings.RESPONSE_CACHE_MAX_ENTRIES,
)
metrics = Metrics()
