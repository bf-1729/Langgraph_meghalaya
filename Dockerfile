# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────────────────────
# Meghalaya NL Assistant (MGNREGA + PMAY-G) — single FastAPI service (app.main:app)
#
# One image runs everything: the API, the served UI (portal / chat / admin), the
# RAG + local-embedding path, and the NL->SQL path. It talks OUT to Postgres
# (megh_db), Qdrant, and the self-hosted Qwen gateway — none of which live in
# this image. Supply their addresses + credentials at run time via --env-file.
#
#   docker build -t megh-nlp:latest .
#   docker run --rm -p 8300:8300 --env-file .env megh-nlp:latest
#
# Build uses requirements.txt, NOT requirements.lock: the lock file pins
# pywin32 / win32-setctime, which do not exist for Linux and break the install.
# ─────────────────────────────────────────────────────────────────────────────

# ---- Stage 1: build the virtualenv ------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Pre-seed the local embedding model (bge-small-en-v1.5, ~130 MB ONNX) into the
# HF cache so the container works with no outbound HTTPS on first run. Best
# effort: a network-less build still succeeds and the model downloads at runtime.
ENV HF_HOME=/opt/hf-cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5'); print('fastembed model cached')" \
    || echo "WARN: could not pre-cache fastembed model; it will download on first use"


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim AS runtime

# libgomp1: required by onnxruntime (fastembed local embeddings).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 megh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf-cache

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf-cache /opt/hf-cache

# The tree must keep its shape: app/ resolves data/ and web/ as siblings
# (Path(__file__).resolve().parents[1]). WORKDIR is that parent.
WORKDIR /opt/meghalaya
COPY app/   ./app/
COPY data/  ./data/
COPY web/   ./web/
COPY certs/ ./certs/

# logs/ is gitignored (contents only); create it writable for the non-root user.
RUN mkdir -p logs \
    && chown -R megh:megh /opt/meghalaya /opt/hf-cache

USER megh
EXPOSE 8300

# /health returns 200 even when a downstream dependency is degraded, which is the
# behaviour we want from an orchestrator's liveness probe (the app is up).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/health', timeout=4).status==200 else 1)"

# 2 workers matches deploy/systemd/megh-nlpservice.service. Override with e.g.
#   docker run ... megh-nlp uvicorn app.main:app --host 0.0.0.0 --port 8300 --workers 4
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "2"]
