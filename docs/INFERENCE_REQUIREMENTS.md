# Inference Requirements — hand to the AIOps team

> **Status note (2026-09-26).** This is the AIOps handoff as written. The app now serves **six**
> schemes, not four. App-side model roles are current: see [AI_PIPELINE.md §0](AI_PIPELINE.md).
> Which of Tasks 1–5 and 7 were applied on the gateway is UNKNOWN — NEEDS VERIFICATION. The
> embedding model is not used, because the app runs local fastembed (`EMBEDDING_PROVIDER=local`).
> The reranker is disabled (`RERANKER_ENABLED=False`).

**Goal:** production model serving for **40 concurrent / ~200 DAU / 4 schemes**,
NL→SQL primary workload, on the 2× H200 NVL node (`meghalaya-3`).
**Cluster:** microk8s, namespace `def`, gateway `https://10.48.242.4`.

Everything below is **server-side** (KServe / vLLM deployments on the GPU box).
The application side is already done — see the last section.

---

## Model roles (current — Task 6 done 2026-09-10)

| Deployment | Model | Role | Endpoint |
|---|---|---|---|
| `qwen-model` | qwen3-coder-30b-fp8 | **SQL generation only** | `/openai/v1` |
| `qwen35-9b` | qwen3.5-9b | answer composition only | `/openai/v1` |
| `qwen4-deploy` | Qwen3-4B-Instruct | classify + intent + entity, **and** SQL semantic verifier (two roles, one deployment) | `/openai/v1` |
| `qwen3-embedding` | qwen3-embedding-0.6b | RAG embeddings | `/openai/v1` |
| `qwen3-reranker` | qwen3-reranker-0.6b | RAG rerank | `/openai/v1` |
| `qwen3-asr` | qwen3-asr-1.7b | voice input | `/openai/v1` |

Deployed under the served name `qwen4-deploy`, not the `qwen3-4b` name Task 6
below originally specified — the app config (`app/config.py`) points at
`qwen4-deploy` for both `CLASSIFIER_MODEL` and `SQL_VERIFY_MODEL`.

---

## Task 1 — Delete the duplicate

Delete InferenceService **`qwen3-coder-30b`** (huggingface runtime, stuck
`deploying` with `CardInsufficientMemory`). It duplicates `qwen-model`.

## Task 2 — Right-size GPU-memory reservations

Both cards are full **by reservation** (new pods fail `CardInsufficientMemory`
though live use is ~60%). Set explicit per-deployment GPU-memory requests +
matching `--gpu-memory-utilization`:

| Deployment | GPU mem request | `--gpu-memory-utilization` |
|---|---|---|
| `qwen-model` (30B) | 95 GiB | 0.68 |
| `qwen35-9b` | 40 GiB | 0.30 |
| `qwen3-embedding` | 6 GiB | 0.05 |
| `qwen3-reranker` | 6 GiB | 0.05 |
| `qwen3-asr` | 8 GiB | — |

Layout: **Card A** = `qwen-model` alone (big KV batch). **Card B** = 9B +
embedding + reranker + asr (~60 GiB) + headroom for the 4B (Task 6).

## Task 3 — vLLM flags on `qwen-model` (30B)

Append to the vLLM args (keep existing args). **All lossless — output unchanged.**

```
--enable-prefix-caching
--kv-cache-dtype=fp8
--speculative-config={"method":"ngram","num_speculative_tokens":5,"prompt_lookup_max":4}
--guided-decoding-backend=xgrammar
--max-num-seqs=48
--enable-chunked-prefill
--max-model-len=16384
```

- If `--speculative-config` JSON form is rejected (older vLLM):
  `--speculative-model=[ngram] --num-speculative-tokens=5 --ngram-prompt-lookup-max=4`
- If `--kv-cache-dtype=fp8` fails: try `fp8_e4m3`.
- `--enable-chunked-prefill` is default-on in vLLM ≥ 0.6.3 — omit if it errors.
- Apply one at a time; confirm `Running` between each.

## Task 4 — vLLM flags on `qwen35-9b`

Append:
```
--enable-prefix-caching
--guided-decoding-backend=xgrammar
```
(This deployment now serves composition only — classification moved to `qwen4-deploy`. The app still sends `guided_json` constraints for the composition call.)

## Task 5 — API endpoint consistency

`qwen35-9b` and `qwen3-asr` currently serve **`/v1/predict`** (prebuilt / audio
runtimes). They must answer OpenAI routes on `https://10.48.242.4/openai/v1`:

- `qwen35-9b` → `POST /openai/v1/chat/completions`, body `{"model":"qwen35-9b",…}`
- `qwen3-asr` → `POST /openai/v1/audio/transcriptions` (multipart)

Preferred: **redeploy both on the plain `vllm` runtime** (like `qwen-model`) so
all models share one URL / auth / metrics surface. If the gateway already routes
by `model` name on `/openai/v1`, confirm via the acceptance test and skip.

## Task 6 — Deploy the dedicated classifier — DONE (2026-09-10)

Deployed as **`qwen4-deploy`** (not the `qwen3-4b` served name originally
planned below — update any monitoring/acceptance-test references accordingly):
- Runtime **`vllm`** (not huggingface)
- Weights: `Qwen3-4B-Instruct` on a PVC
- GPU mem request **16 GiB**, `--gpu-memory-utilization=0.12`, `--max-model-len=8192`,
  `--guided-decoding-backend=xgrammar`
- Confirmed reachable on `/openai/v1`

App-side: `CLASSIFIER_MODEL=qwen4-deploy` and `SQL_VERIFY_MODEL=qwen4-deploy`
(`app/config.py`) — both roles share this one deployment. Watch its queue depth
(`vllm:num_requests_waiting`) since it now carries classify traffic (several
calls per turn) on top of the SQL-verify traffic (one call per SQL generation)
it already had.

## Task 7 — Monitoring to expose (per model)

`vllm:num_requests_running`, `vllm:num_requests_waiting`,
`vllm:gpu_cache_usage_perc`, `vllm:prefix_cache_hit_rate`, speculative-decode
acceptance rate, p50/p95/p99 latency, tokens/sec; plus per-card GPU util + memory
on `meghalaya-3`.

---

## Acceptance tests

**1. All models on the shared OpenAI endpoint**
```bash
curl -sk https://10.48.242.4/openai/v1/chat/completions \
  -H "Authorization: Bearer <qwen35-9b key>" \
  -d '{"model":"qwen35-9b","messages":[{"role":"user","content":"say hi"}]}'
```
HTTP 200 with `choices[0].message.content`. Repeat for `qwen-model`, `qwen4-deploy`.
ASR: `POST /openai/v1/audio/transcriptions` with a WAV → `{"text":…}`.

**2. Prefix caching** — same 2 KB SQL prompt twice; 2nd materially faster;
`prefix_cache_hit_rate > 0`.

**3. Speculative decoding** — acceptance-rate metric > 0 on `qwen-model` during
SQL generation.

**4. Load test — 40 concurrent, 5 min**
```bash
hey -z 5m -c 40 -m POST -T application/json \
  -H "Authorization: Bearer <qwen-model key>" \
  -d '{"model":"qwen-model","max_tokens":300,"messages":[{"role":"user","content":"Write one PostgreSQL SELECT: total person_days by district FY 2023-24. SQL only."}]}' \
  https://10.48.242.4/openai/v1/chat/completions
```
**Pass:** p95 ≤ 12 s, 0 errors, `num_requests_waiting` bounded (not growing
unbounded).
**If it fails:** add a 3rd H200 and run `qwen-model` at **2 replicas**
(`maxReplicas: 2`) behind the gateway — the only change that doubles SQL-gen
throughput.

---

## Already handled on the application side (no AIOps action)

- `CLASSIFIER_MODEL` → `qwen4-deploy` (was `qwen35-9b`, was `qwen-model` before
  that); the 30B does SQL only. `SQL_GENERATION_MODEL` stays `qwen-model`.
  `RESPONSE_MODEL` stays `qwen35-9b`, now composition-only. Config in `app/config.py`.
- App bounds its own gateway concurrency (`MODEL_MAX_CONCURRENCY`) and sheds with
  HTTP 503 past the limit; exact-match + semantic response caches; sends
  `guided_json` / `guided_regex` constraints (safe if the gateway ignores them).
- App = 2 uvicorn workers × 2 VMs, stateless, behind nginx. `GET /metrics` on the
  app exposes route counts, p50/p95/p99, `busy_rejections_total`, cache hit rates.
