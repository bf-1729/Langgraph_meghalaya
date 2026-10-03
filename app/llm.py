"""
Shared OpenAI-compatible client for every model role.

One module-level httpx.AsyncClient is reused across requests to avoid
creating a new client and TLS handshake for every call.

The client supports an optional custom CA bundle.

SSL behavior:
- If AI_MODEL_CA_BUNDLE_PATH is configured and exists, use it.
- If a custom CA bundle is not configured, SSL verification is disabled
  to support internal/self-signed model gateways.
"""

import asyncio
import contextvars
import logging
import time
import os
import re
from contextlib import asynccontextmanager

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Bounds total concurrent calls to the shared model gateway across the
# whole worker. The 30B SQL model is the scarce resource.
_gate: asyncio.Semaphore | None = None


class ModelBusyError(RuntimeError):
    """Raised when a model slot can't be acquired within the queue timeout."""


def _get_ssl_verify() -> str | bool:
    """
    Return the SSL verification configuration.

    If AI_MODEL_CA_BUNDLE_PATH is configured and the file exists,
    use that CA bundle.

    If the CA bundle is configured but missing, fall back to
    SSL verification disabled so self-signed internal gateways
    can still be reached.
    """

    ca_bundle = settings.AI_MODEL_CA_BUNDLE_PATH

    if ca_bundle:
        ca_bundle = os.path.expanduser(str(ca_bundle))

        if os.path.isfile(ca_bundle):
            logger.info(
                "Using custom AI model CA bundle: %s",
                ca_bundle,
            )
            return ca_bundle

        logger.warning(
            "AI_MODEL_CA_BUNDLE_PATH is configured but the file does "
            "not exist: %s. Falling back to verify=False for the "
            "internal/self-signed AI model gateway.",
            ca_bundle,
        )

        return False

    logger.warning(
        "No AI_MODEL_CA_BUNDLE_PATH configured. "
        "Using verify=False for internal/self-signed AI model HTTPS connections."
    )

    return False


async def init_client() -> None:
    """Initialize the shared HTTP client and model concurrency gate."""

    global _client, _gate

    # Avoid creating the client twice.
    if _client is not None:
        logger.info("LLM HTTP client already initialized.")
        return

    verify = _get_ssl_verify()

    _client = httpx.AsyncClient(
        verify=verify,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=40,
        ),
    )

    _gate = asyncio.Semaphore(
        settings.MODEL_MAX_CONCURRENCY
    )

    logger.info(
        "LLM HTTP client initialized. "
        "MODEL_MAX_CONCURRENCY=%s SSL_VERIFY=%s",
        settings.MODEL_MAX_CONCURRENCY,
        verify,
    )


async def close_client() -> None:
    """Close the shared HTTP client."""

    global _client, _gate

    if _client is not None:
        await _client.aclose()
        _client = None

    _gate = None

    logger.info("LLM HTTP client closed.")


@asynccontextmanager
async def _slot():
    """
    Hold one model-gateway concurrency slot for the duration of a call.

    Past MODEL_QUEUE_TIMEOUT_SECONDS callers receive ModelBusyError
    instead of piling up indefinitely.
    """

    if _gate is None:
        raise RuntimeError(
            "llm client not initialized — call init_client() at startup"
        )

    try:
        await asyncio.wait_for(
            _gate.acquire(),
            timeout=settings.MODEL_QUEUE_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError as e:
        raise ModelBusyError(
            "model gateway saturated — try again shortly"
        ) from e

    try:
        yield

    finally:
        _gate.release()


# ── Per-call measurement ─────────────────────────────────────────────────────
# One `llm_call` log line per chat completion: the role, the model, the time
# spent waiting for a concurrency slot, the gateway round trip, and the
# gateway's own token counts (`usage`, when it returns one). There is no
# streaming here (see CLAUDE.md), so time-to-first-token equals the round trip.
# A caller that wants the numbers themselves (tests/live_context_validation.py)
# sets llm_calls_var to a list for the duration of a request.
llm_calls_var: "contextvars.ContextVar[list | None]" = contextvars.ContextVar(
    "llm_calls", default=None)


def _record_call(role: str, model: str, t_wait: float, t_start: float, usage: dict) -> None:
    try:
        now = time.perf_counter()
        rec = {
            "role": role, "model": model,
            "queue_ms": round((t_start - t_wait) * 1000, 1),
            "latency_ms": round((now - t_start) * 1000, 1),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        sink = llm_calls_var.get()
        if sink is not None:
            sink.append(rec)
        logger.info("llm_call role=%s model=%s queue_ms=%s latency_ms=%s prompt_tokens=%s "
                    "completion_tokens=%s", rec["role"], model, rec["queue_ms"],
                    rec["latency_ms"], rec["prompt_tokens"], rec["completion_tokens"])
    except Exception:  # noqa: BLE001 — measurement never costs an answer
        logger.debug("llm_call record failed", exc_info=True)


async def call_model(
    *,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    timeout: float = 30.0,
    guided: dict | None = None,
    role: str = "",
) -> str:

    if _client is None:
        raise RuntimeError(
            "llm client not initialized — call init_client() at startup"
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,

        # Prevent Qwen3 thinking/reasoning text from being returned
        # inline in the response.
        "chat_template_kwargs": {
            "enable_thinking": False
        },
    }

    if guided:
        # vLLM OpenAI-compatible guided decoding fields.
        payload.update(guided)

    t_wait = time.perf_counter()
    async with _slot():
        t_start = time.perf_counter()

        resp = await _client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            json=payload,
            timeout=timeout,
        )

        # Some gateways don't support guided decoding.
        # Retry once without the guided fields.
        if guided and resp.status_code in (400, 404, 422):

            logger.warning(
                "Guided decoding rejected by gateway (%s) — "
                "retrying without constraints",
                resp.status_code,
            )

            for key in guided:
                payload.pop(key, None)

            resp = await _client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
                timeout=timeout,
            )

    resp.raise_for_status()

    data = resp.json()
    _record_call(role or model, model, t_wait, t_start, data.get("usage") or {})

    return data["choices"][0]["message"]["content"]


async def call_classifier(
    prompt: str,
    *,
    guided: dict | None = None,
) -> str:

    return await call_model(
        base_url=settings.CLASSIFIER_BASE_URL,
        model=settings.CLASSIFIER_MODEL,
        api_key=settings.CLASSIFIER_API_KEY,
        prompt=prompt,
        temperature=settings.CLASSIFIER_TEMPERATURE,
        max_tokens=200,
        timeout=settings.CLASSIFIER_TIMEOUT_SECONDS,
        role="classifier",
        guided=guided if settings.GUIDED_DECODING_ENABLED else None,
    )


async def call_sql_generator(
    prompt: str,
    *,
    guided: dict | None = None,
) -> str:

    return await call_model(
        base_url=settings.SQL_GENERATION_BASE_URL,
        model=settings.SQL_GENERATION_MODEL,
        api_key=settings.SQL_GENERATION_API_KEY,
        prompt=prompt,
        temperature=settings.SQL_GENERATION_TEMPERATURE,
        max_tokens=1024,
        timeout=settings.SQL_GENERATION_TIMEOUT_SECONDS,
        role="sql",
        guided=guided if settings.SQL_GUIDED_DECODING_ENABLED else None,
    )


async def call_response_composer(prompt: str) -> str:

    return await call_model(
        base_url=settings.RESPONSE_MODEL_BASE_URL,
        model=settings.RESPONSE_MODEL,
        api_key=settings.RESPONSE_MODEL_API_KEY,
        prompt=prompt,
        temperature=settings.RESPONSE_TEMPERATURE,
        max_tokens=settings.RESPONSE_MAX_TOKENS,
        timeout=settings.RESPONSE_TIMEOUT_SECONDS,
        role="compose",
    )


async def call_sql_verifier(
    prompt: str,
    *,
    guided: dict | None = None,
) -> str:
    """Second opinion on already-generated SQL — a distinct role from
    call_classifier even though SQL_VERIFY_MODEL and CLASSIFIER_MODEL both
    point at the same qwen4-deploy deployment."""

    return await call_model(
        base_url=settings.SQL_VERIFY_BASE_URL,
        model=settings.SQL_VERIFY_MODEL,
        api_key=settings.SQL_VERIFY_API_KEY,
        prompt=prompt,
        temperature=settings.SQL_VERIFY_TEMPERATURE,
        max_tokens=200,
        timeout=settings.SQL_VERIFY_TIMEOUT_SECONDS,
        role="sql_verify",
        guided=guided if settings.GUIDED_DECODING_ENABLED else None,
    )


# ---------------------------------------------------------------------------
# RAG-path model roles
# ---------------------------------------------------------------------------


async def call_embedding(
    texts: str | list[str],
) -> list[list[float]]:
    """
    qwen3-embedding via the gateway's OpenAI-compatible /embeddings route.

    Always returns a list of vectors, one per input, in input order.
    """

    if _client is None:
        raise RuntimeError(
            "llm client not initialized — call init_client() at startup"
        )

    batch = [texts] if isinstance(texts, str) else list(texts)

    if not batch:
        return []

    if settings.EMBEDDING_PROVIDER == "local":
        # Local CPU model — no gateway hop and no concurrency slot needed.
        from app import local_embed

        return await local_embed.embed(batch)

    async with _slot():

        resp = await _client.post(
            f"{settings.EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
            },
            json={
                "model": settings.EMBEDDING_MODEL,
                "input": batch,
            },
            timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
        )

    resp.raise_for_status()

    data = resp.json()["data"]

    # The spec guarantees order, but sort defensively.
    ordered = sorted(
        data,
        key=lambda d: d.get("index", 0),
    )

    return [
        d["embedding"]
        for d in ordered
    ]


async def call_reranker(
    query: str,
    docs: list[str],
) -> list[tuple[int, float]]:
    """
    qwen3-reranker — returns (original_index, score) pairs,
    best score first.

    Tries the gateway rerank route first.

    If that route isn't available, falls back to scoring each
    document with the classifier model.
    """

    if _client is None:
        raise RuntimeError(
            "llm client not initialized — call init_client() at startup"
        )

    if not docs:
        return []

    try:

        async with _slot():

            resp = await _client.post(
                f"{settings.RERANKER_BASE_URL.rstrip('/')}/rerank",
                headers={
                    "Authorization": f"Bearer {settings.RERANKER_API_KEY}",
                },
                json={
                    "model": settings.RERANKER_MODEL,
                    "query": query,
                    "documents": docs,
                },
                timeout=settings.RERANKER_TIMEOUT_SECONDS,
            )

        resp.raise_for_status()

        results = resp.json()["results"]

        pairs = [
            (
                r["index"],
                float(
                    r.get(
                        "relevance_score",
                        r.get("score", 0.0),
                    )
                ),
            )
            for r in results
        ]

        pairs.sort(
            key=lambda p: -p[1]
        )

        return pairs

    except Exception as e:

        logger.warning(
            "Reranker route unavailable (%s) — "
            "falling back to prompt scoring",
            e,
        )

    scored: list[tuple[int, float]] = []

    for i, doc in enumerate(docs):

        prompt = (
            "Score from 0.0 to 1.0 how well the passage "
            "answers the question. "
            "Reply with ONLY the number.\n\n"
            f'Question: "{query}"\n\n'
            f"Passage:\n{doc[:1500]}\n\n"
            "Score:"
        )

        try:

            raw = await call_classifier(prompt)

            m = re.search(
                r"[01](?:\.\d+)?",
                raw,
            )

            scored.append(
                (
                    i,
                    float(m.group(0)) if m else 0.0,
                )
            )

        except Exception:

            scored.append(
                (
                    i,
                    0.0,
                )
            )

    scored.sort(
        key=lambda p: -p[1]
    )

    return scored


# Codes this ASR deployment accepts (its own 400 enumerates them). Anything
# else is a hard 400, so it is normalised to "en" rather than forwarded.
_ASR_LANGUAGES = frozenset({
    "af", "ar", "hy", "az", "be", "bs", "bg", "ca", "zh", "hr", "cs", "da",
    "nl", "en", "et", "fi", "fr", "gl", "de", "el", "he", "hi", "hu", "is",
    "id", "it", "ja", "kn", "kk", "ko", "lv", "lt", "mk", "ms", "mr", "mi",
    "ne", "no", "fa", "pl", "pt", "ro", "ru", "sr", "sk", "sl", "es", "sw",
    "sv", "tl", "ta", "th", "tr", "uk", "ur", "vi", "cy",
})


def _asr_language(language: str | None) -> str:
    """A code the ASR will accept. Unknown/blank -> "en"."""
    code = (language or "").strip().lower()
    return code if code in _ASR_LANGUAGES else "en"


async def call_asr(
    audio: bytes,
    filename: str = "audio.wav",
    language: str = "en",
) -> str:
    """
    qwen3-asr via the gateway's OpenAI-compatible
    /audio/transcriptions route.

    `language` is optional for this deployment — real speech transcribes
    identically with and without it — but the web UI already sends the field,
    so it is forwarded rather than silently dropped.

    It is VALIDATED first, because the gateway rejects an unknown code outright
    (`language=kha` -> 400 "Unsupported language") and that 400 surfaces as a
    502 with no usable message. A hint the model does not need must never be
    able to fail the call, so anything outside _ASR_LANGUAGES falls back to
    "en". This matters for the Khasi/Garo UI options: they map to "en" today,
    but the ASR has no code for either language.
    """

    if _client is None:
        raise RuntimeError(
            "llm client not initialized — call init_client() at startup"
        )

    form = {
        "model": settings.ASR_MODEL,
        "language": _asr_language(language),
    }
    # Domain vocabulary (see settings.ASR_PROMPT). The caller must run the
    # result through asr_guard.is_prompt_echo(): on silence the model repeats it.
    if settings.ASR_PROMPT:
        form["prompt"] = settings.ASR_PROMPT

    async with _slot():

        resp = await _client.post(
            f"{settings.ASR_BASE_URL.rstrip('/')}/audio/transcriptions",
            headers={
                "Authorization": f"Bearer {settings.ASR_API_KEY}",
            },
            data=form,
            files={
                "file": (
                    filename,
                    audio,
                    "application/octet-stream",
                )
            },
            timeout=settings.ASR_TIMEOUT_SECONDS,
        )

    resp.raise_for_status()

    data = resp.json()

    return (
        data.get("text", "")
        if isinstance(data, dict)
        else str(data)
    )
