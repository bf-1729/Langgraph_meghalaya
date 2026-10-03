# Project Context — Megh One AI

*High-level onboarding. Last reconciled against the code: 2026-09-26 (commit `7064ab6`).*
*Labels: **VERIFIED** (read in code, config or a test run) · **INFERRED** · **UNKNOWN — NEEDS VERIFICATION** · **PLANNED**.*

## What it is

**Megh One AI** (repo/service name: `nlp-service`) is a conversational analytics assistant for
officers of the Government of Meghalaya. An officer types or speaks a question in English, for
example *"MGNREGA person-days in West Garo Hills in 2023-24"* or *"who is eligible for PMAY-G?"*.
The assistant replies with a short plain-English answer and, for data questions, the rows, the
SQL, a chart/table, and suggested next questions.

## Why it exists / problem solved

Scheme data sits in a PostgreSQL warehouse (`megh_db`), curated by a separate ingestion team
(`megh-ingestion`). Officers can't write SQL, and the schemes differ in grain, money unit, time
coverage and naming. The assistant turns a question into:
- a correct, read-only SQL query, or
- a retrieval over the schemes' reference documents,

and refuses or asks a clarifying question when the data can't answer honestly. The recurring
design theme is to **never return a confident wrong number**. A false zero, a fanned-out join or
a mislabelled unit is treated as worse than a clarification or a refusal. (INFERRED from the
code's comments and guards, which consistently prioritise this.)

## Supported schemes (VERIFIED: `schema_context.SCHEME_CATALOG`, `annotations._SCHEME_DIRS`)

| Scheme | What it is | Grain | Query surface |
|---|---|---|---|
| MGNREGA | Rural employment guarantee | source row (many per village-year) | `curated.v_employment`, `v_expenditure`, `v_district_year_summary` |
| PMAY-G | Rural housing | one sanctioned house | `curated.v_pmay`, `v_pmay_monthly_sanctions` |
| Focus Plus | State farmer cash benefit (DBT) | one payment to one member in one tranche | `curated.v_focus_plus` |
| CM Elevate | 15 livelihood/enterprise schemes: **applications** | one application; **no money, no dates** | `curated.v_cm_elevate` |
| Focus Legacy | "FOCUS" producer-group payments | one payment to a producer group | `curated.v_focus_legacy` |
| CM Elevate Legacy | CM Elevate **sanctions and disbursements** (13 schemes) | one applicant's sanction and disbursement | `curated.v_cm_elevate_disbursement` |

Two pairs share a name and **no key**: Focus Plus vs Focus Legacy, and CM Elevate vs CM Elevate
Legacy. Details: [SCHEMES.md](SCHEMES.md).

## Current capabilities (VERIFIED in code)

- NL→SQL over all six schemes, including selected cross-scheme questions (money ranking, and
  MGNREGA+PMAY coverage and money views).
- RAG answers on eligibility, documents, benefits and process, from SME reference docs and FAQs.
- Clarification pauses with one-tap chips for:
  - which scheme;
  - which Focus;
  - top-N count;
  - area/year scope;
  - financial year;
  - tranche (Focus Plus);
  - ambiguous place names or levels (block vs village vs assembly constituency).
- Multi-turn follow-ups: an LLM rewrite of fragments, plus deterministic references
  ("previous year", "the former").
- Deterministic answers for: district/block abbreviations, scheme listing/comparison, scheme
  recommendation, and producer-group name lookups.
- Voice input via ASR (`/api/query/transcribe`).
- JWT login, multi-tenant users, role/geography/granularity authorization on generated SQL,
  per-user chat history, an admin console, and an audit trail.
- Exact-match and semantic response caches; `/health` and `/metrics`.

## Not implemented (VERIFIED absent)

- Streaming responses, WebSocket and SSE: every answer is one JSON response.
- A React/Vite frontend: the UI is three vanilla HTML/JS files.
- An end-to-end accuracy benchmark (see [TESTING.md](TESTING.md)).

## Major technologies

- **Backend:** Python 3.11, FastAPI + uvicorn, `asyncpg` (no ORM), `httpx` (hand-built
  OpenAI-compatible calls; no LangChain model or prompt abstractions), `qdrant-client`, `fastembed` (local
  `BAAI/bge-small-en-v1.5`), `rapidfuzz`, PyYAML, optional Redis. Source: `requirements.txt`.
- **Orchestration (opt-in, 2026-10-02, D-032):** `langgraph` runs the same pipeline stages as a
  graph with checkpointed clarification pauses (`app/pipeline_graph.py`), **off by default**; see
  [LANGGRAPH_ARCHITECTURE.md](LANGGRAPH_ARCHITECTURE.md).
- **Models** (a remote vLLM gateway at `10.48.242.4`):
  - `qwen-model` (Qwen3-Coder-30B FP8) for SQL generation;
  - `qwen4-deploy` (Qwen3-4B) for classification and SQL verification;
  - `qwen35-9b` for answer composition;
  - `qwen3-asr` for voice input;
  - `qwen3-reranker`, which is deployed but **disabled**.
- **Data:** PostgreSQL 18.4 `megh_db` (schemas raw/staging/curated/semantic/meta/app), and
  Qdrant on the same host.
- **Deploy target:** 2 Ubuntu VMs, nginx + systemd (`uvicorn --workers 2`). A Dockerfile and
  docker-compose also exist.

## Overall request flow (summary)

```
Browser (web/ai_query.html) ──POST /api/query──► FastAPI (app/routers/query.py)
   JWT → session → exact cache → semantic cache → pipeline.answer_question (60 s cap)
     ├─ deterministic pre-routes (harmful, geo definition, recommender, listing)
     ├─ edge.detect_edge_case (regex; greetings / off-topic)          → route "edge"
     ├─ follow-up rewrite (LLM 4B) + context substitution
     ├─ classify_intent (regex fast path, else LLM 4B)
     ├─ KNOWLEDGE → rag.answer_from_kb (bge-small → Qdrant → 9B compose) → route "knowledge"
     └─ DATA → gates → classify_scheme → resolve_entities → gates → generate_sql (30B)
               → authorize → execute_with_repair (guards + 4B verifier + ≤3 repairs)
               → compose_response (9B + faithfulness checks)            → route "data"
   ClarificationNeeded → route "clarification" (chips)   auth deny → route "denied"
   persist turn + audit (Postgres app.*), index memory (Qdrant), cache write
```

Both orchestrators (the sequential `_run_pipeline`, default, and the opt-in LangGraph graph) run
this same flow.

Details: [ARCHITECTURE.md](ARCHITECTURE.md), [AI_PIPELINE.md](AI_PIPELINE.md).

## Important constraints

**Business**
- The data covers only Meghalaya, so "in Meghalaya" means *no* geographic filter.
- Each scheme has its own years:
  - MGNREGA: FY 2022-23 to 2025-26.
  - PMAY-G: `year_key` 2017 to 2023.
  - Focus Plus: 2022-23 and 2025-26.
  - Focus Legacy: 2021-22, 2022-23, 2024-25 and 2025-26. **FY 2023-24 is absent.**
  - CM Elevate Legacy: 2024-25 and 2025-26.
  - CM Elevate: **no time dimension**.
- Money units differ:
  - MGNREGA in **lakh** rupees;
  - PMAY-G in rupees, but the monthly and cross-scheme views are in crore;
  - Focus Plus and Focus Legacy: unit unverified;
  - CM Elevate Legacy in rupees;
  - CM Elevate: no money at all.
- Some metrics users ask for do not exist (MGNREGA dues, admin expenditure, bank channel,
  CM Elevate money). The assistant must refuse clearly, never estimate.

**Security**
- LLM SQL is read-only, validated in `db.run_readonly`.
- Role scope is checked on the SQL before it runs.
- Privacy tables (unmasked bank accounts, applicant names) must never be queried. **This is
  enforced only by the prompt today** (KNOWN_ISSUES KI-004).

**Development**
- Do not change `megh_db.curated`; it is owned by the ingestion team.
- Schemes are hand-registered in about 14 places.
- Prose prompt rules are backed by deterministic guards.
- Tests are regression locks, not accuracy measures.

## Maturity / status

- **Stage** (INFERRED from git history and QA records): internal UAT with live QA passes. It was
  built 2026-08 to 2026-09, with scheme 5 (Focus Legacy) added 2026-09-22 and scheme 6
  (CM Elevate Legacy) added 2026-09-24.
- **Latest QA** (2026-09-25):
  - Focus Legacy: 28/28 use cases.
  - CM Elevate Legacy: 36/36.
  - Focus Legacy bulk: 168/168 block and 580/580 PG questions. These are per session notes; the
    report's formulas have no cached values, so they are not machine-verified.
- **Production readiness:** UNKNOWN — NEEDS VERIFICATION. Whether the two-VM deployment is live,
  and with which DB role, is not recorded in the repo.

Current details: [CURRENT_STATE.md](CURRENT_STATE.md).
