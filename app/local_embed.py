"""
Local CPU embeddings via fastembed (ONNX). Used when EMBEDDING_PROVIDER=local,
i.e. whenever the model gateway has no embedding deployment of its own.

Runs on the calling (event-loop) thread ON PURPOSE. onnxruntime's Run() dead-
locks on Windows when the InferenceSession is driven from a ThreadPoolExecutor
worker for some input batches (observed hang inside onnxruntime_inference_
collection.run). The same batch on the main thread returns in well under a
second, so we embed synchronously and just yield the loop between sub-batches so
health checks and other requests still get serviced during a bulk ingest.

The first call downloads the ONNX weights (~130 MB for bge-small-en-v1.5) to the
HuggingFace cache; every later call is offline.
"""
import asyncio
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_model = None
_lock = asyncio.Lock()

# Yield to the event loop after this many texts during a large embed call, so a
# startup ingest doesn't stall /health for its whole duration.
_YIELD_EVERY = 16

# Known output dimensions — lets kb_ingest size the Qdrant collection if needed.
_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


def _load_sync():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=settings.LOCAL_EMBEDDING_MODEL)


def _embed_sync(texts: list[str]) -> list[list[float]]:
    # parallel=1: don't let fastembed spawn its own worker pool underneath us.
    return [v.tolist() for v in _model.embed(texts, parallel=1)]


async def _ensure_model() -> None:
    global _model
    if _model is not None:
        return
    async with _lock:
        if _model is None:
            name = settings.LOCAL_EMBEDDING_MODEL
            logger.info("local_embed: loading %s (first run downloads the model)", name)
            _model = _load_sync()
            logger.info("local_embed: %s ready", name)


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch, returning one vector (plain list[float]) per input, in order."""
    if not texts:
        return []
    await _ensure_model()
    texts = list(texts)
    if len(texts) <= _YIELD_EVERY:
        return _embed_sync(texts)
    out: list[list[float]] = []
    for i in range(0, len(texts), _YIELD_EVERY):
        out.extend(_embed_sync(texts[i : i + _YIELD_EVERY]))
        await asyncio.sleep(0)  # let the loop breathe between sub-batches
    return out


def dim() -> int:
    return _DIMS.get(settings.LOCAL_EMBEDDING_MODEL, 384)
