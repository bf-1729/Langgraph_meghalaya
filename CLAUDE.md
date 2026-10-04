# CLAUDE.md — Megh One AI (Meghalaya Conversational Analytics Chatbot)

Read this first, every session. It is short on purpose; details live in `docs/`.

## 1. What this is

One FastAPI service (`app/`) that answers Meghalaya government officers' natural-language
questions about **six scheme datasets**: MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy
and CM Elevate Legacy.

- **Data questions** → NL→SQL against PostgreSQL `megh_db` (schema `curated`), then an
  LLM-composed answer.
- **"How does the scheme work"** questions → RAG over Qdrant (reference docs in `data/reference/`).
- **Greetings / off-topic / abuse** → regex edge layer, with no model call.

All model inference is remote, on a self-hosted vLLM gateway (`10.48.242.4`). The UI is vanilla
HTML/JS in `web/`, served by the same app. **No React, no WebSocket, no streaming.**

## 2. Architecture in one breath

`POST /api/query` → JWT auth → cache (exact, then semantic) → `pipeline.answer_question` →
deterministic pre-routing and edge → follow-up rewrite → intent (DATA/KNOWLEDGE) →
- **DATA:** scheme gate → `classify_scheme` → `resolve_entities` → clarification gates →
  `generate_sql` → `auth.authorize` → `execute_with_repair` (regex guards + LLM verifier + up to
  3 repairs) → `compose_response` (numeric-faithfulness checks).
- **KNOWLEDGE:** `rag.answer_from_kb`.

The orchestration lives in [app/pipeline.py](app/pipeline.py), which is about 15,000 lines, written as
**stage functions** (`_turn_*_stage`, `_data_*_stage`, `_execute_one_attempt`, …). Two orchestrators run
them: the sequential `_run_pipeline` (the default) and the opt-in LangGraph graph
[app/pipeline_graph.py](app/pipeline_graph.py) (`PIPELINE_GRAPH_ENABLED`, D-032). **Change behaviour
in a stage, never in a graph node**, so the two paths cannot drift.
Full map: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/AI_PIPELINE.md](docs/AI_PIPELINE.md),
[docs/LANGGRAPH_ARCHITECTURE.md](docs/LANGGRAPH_ARCHITECTURE.md).

## 3. Documentation map

| Doc | Read it when |
|---|---|
| [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) | Onboarding: what, why, capabilities, constraints |
| [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) | **Every session.** What works, what's broken, what's next |
| [docs/HANDOFF.md](docs/HANDOFF.md) | **Every session.** Where the last session stopped |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, API, auth, deploy, infrastructure |
| [docs/AI_PIPELINE.md](docs/AI_PIPELINE.md) | Any change to routing, prompts, SQL gen/repair, composer, RAG |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Any change touching SQL, tables, joins, units, years |
| [docs/SCHEMES.md](docs/SCHEMES.md) | Any scheme-specific change, or adding a scheme |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | Before fixing a bug (it may already be tracked) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | **Before proposing any architectural replacement** |
| [docs/TESTING.md](docs/TESTING.md) | Before and after changing code |
| [docs/SECURITY.md](docs/SECURITY.md) | OWASP control map, deploy security checklist |
| [docs/LANGGRAPH_ARCHITECTURE.md](docs/LANGGRAPH_ARCHITECTURE.md) | The LangGraph orchestrator: nodes, state, checkpointing, logs, adding a node |
| [docs/CONTEXT_AND_LANGGRAPH.md](docs/CONTEXT_AND_LANGGRAPH.md) | How the context window runs inside the graph |
| [docs/LANGGRAPH_PAUSE_RESUME.md](docs/LANGGRAPH_PAUSE_RESUME.md) | Clarification pauses as `interrupt()`; resume; the two stores |
| [docs/LANGGRAPH_MIGRATION.md](docs/LANGGRAPH_MIGRATION.md) | Flags, rollout gates, rollback, measured overhead |
| [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) | Deploying to the VMs |
| `data/<scheme>/README.md` | The SME contract for one scheme's YAMLs (authoritative for that scheme) |

Historical and reference documents: `docs/TECHNICAL_BRIEF.md` (a 2026-09-24 analysis snapshot
whose line numbers are stale), `docs/*_DB_Issues.md`, `docs/*Fix_Verification*.md` (QA records)
`docs/INFERENCE_REQUIREMENTS.md` (the AIOps handoff), `docs/MEGHALAYA_LANGGRAPH_ARCHITECTURE.md`
(the 2026-10-02 pre-implementation analysis for the LangGraph work) and
`docs/Context_Validation_Report_2026-09-26.md` (the live end-to-end context validation).

## 4. Development principles

- **Evidence order:** source code > DB schema > tests > config > docs > git history > chat memory.
  If a doc disagrees with the code, the code wins, and you fix the doc.
- **Don't break working behaviour.** No drive-by refactors, API changes, schema changes or
  "cleaner" rewrites. Make small, targeted, tested changes.
- **Almost every regex, gate and guard in `pipeline.py` exists because of a real reported
  failure.** The comment above it usually says which one. Read that comment before changing or
  removing it.
- **Schemes are enumerated by hand in about 14 registries** (listed in
  [docs/SCHEMES.md §Adding a scheme](docs/SCHEMES.md#adding-a-scheme)). Changing one scheme's
  name or pattern means checking all of them.
- **Two name collisions must never be guessed:**
  - A bare "Focus" → ask *which* Focus (Focus Plus or Focus Legacy).
  - A bare "CM Elevate" → pinned by keywords (`_pin_cm_elevate_dataset`).

  See [docs/SCHEMES.md](docs/SCHEMES.md).
- **Match the surrounding code.** Comments explain *why*, often citing the incident and date.
  Keep that style.

## 5. AI / LLM rules

- **Never trust a prose rule alone** for a wrong-number bug. The project's pattern is:
  1. a prompt rule;
  2. a few-shot example;
  3. a **deterministic guard** in `execute_with_repair` or `compose_response`.

  Prose rules have repeatedly failed under sampling.
- **Model roles are fixed** (see [app/config.py](app/config.py)):
  - `qwen-model` (30B coder) = SQL generation only;
  - `qwen4-deploy` (4B) = classifier + SQL verifier;
  - `qwen35-9b` = composer;
  - embeddings are local fastembed;
  - the reranker is **off**.

  Do not move roles without a DECISIONS entry.
- The composer must never state a number that isn't in the result. The faithfulness checks in
  `compose_response` enforce this. Don't weaken them.
- **Few-shot examples are per scheme, ranked by IDF** (`annotations.few_shot_examples`). The
  CM Elevate Legacy bank is `cmelevatelegacy_prompt_few_shots.yaml` (v2), not the v1 file.
- **Follow-up context is structured, not sliced** (D-022). The rewrite gets the previous turn's
  filters, and gets its result rows only when the follow-up points into them. Its output is
  provenance-checked field by field (D-024). Do not put raw previous-answer text back into a
  prompt.
- **Conversation state lives in Postgres** (`app.conversations.context_state`, versioned;
  `session_sync`, D-023). The in-process `SessionStore` is a per-worker cache. Anything the next
  turn needs must be in `Session.to_snapshot()`.
- After adding or editing KB docs in `data/reference/`, re-ingest: admin `POST /api/rag/reingest`,
  or restart the service. The Qdrant collection is shared.

## 6. SQL safety and database protection

- LLM SQL runs **only** through `db.run_readonly()`. It enforces a single statement, a
  `SELECT`/`WITH` lead, no write/DDL keywords, and an auto-`LIMIT 1000`. Never bypass it. Never
  run model text through `fetch_rows` or `execute`.
- The app's own SQL is **always parameter-bound**.
- **Privacy tables must never be queried:**
  - `curated.fact_focus_legacy_disbursement` and `curated.bridge_pg_bank_history` (unmasked
    account numbers);
  - `curated.fact_cm_elevate_disbursement` (applicant names).

  Only the prompt protects them today; see KNOWN_ISSUES KI-004.
- **`Focus Legacy to share to BLH.csv`** (repo root) contains **unmasked bank account numbers and
  holder names** (KI-022). Never print, quote, copy or upload its rows. Count or aggregate them
  only. Never commit new raw source files.
- `app/users.yaml` holds plaintext seed passwords (KI-023). Never repeat them.
- **Do not modify database data or schema.** `megh_db.curated` belongs to the ingestion team
  (`megh-ingestion`). Report data defects to them as a `docs/*_DB_Issues.md` note.
- The app **creates and migrates only the `app.*` schema** (`appdb.ensure_schema`, idempotent
  `IF NOT EXISTS`).
- For ad-hoc investigation against the live DB: use read-only queries only, ideally inside a
  `READ ONLY` transaction that you roll back.

## 7. Testing rules

- Use the repo venv: `.venv/Scripts/python.exe` (Windows). System Python lacks `fastembed`, and
  RAG answers silently degrade under it.
- Run pytest on the **27 pytest-style files only**, then the **14 plain-script suites**
  (`python tests/test_X.py`). The exact commands are in [docs/TESTING.md](docs/TESTING.md).
  Do **not** run bare `pytest tests`: it crashes with INTERNALERROR (KNOWN_ISSUES KI-018).
- Baseline (2026-10-03, after the all-places fixes KI-188..198): 1,453 pytest passed, 0 failed (27 files),
  with the graph off AND on; 14/14 scripts. Live context suite 43/43 on both the sequential and the
  LangGraph path. Live all-places bulk 30,280/30,280 (docs/SCHEMES.md "All-places QA").
  The live multi-turn suite `tests/live_context_validation.py` (needs the VPN) passed 43/43 on
  2026-09-26 and again on 2026-09-27 after the Focus Plus and the CM Elevate fixes. Re-run it after any routing, rewrite or state change.
- Every bug fix gets a regression test that calls the **real function**, never a
  re-implementation of it.
- Some tests pin exact source strings in `pipeline.py`. If one fails after a refactor, read it
  before "fixing" it.
- **Green tests do not prove NL→SQL accuracy.** There is no golden-set benchmark. Behaviour
  changes need a live check: call `pipeline.answer_question` in-process with the `.venv` Python,
  from the repo root so `.env` loads. The DB and gateway require the office VPN.

## 8. DOCUMENTATION MAINTENANCE IS PART OF EVERY DEVELOPMENT TASK.

This rule is permanent and mandatory.

**Before work:**
1. Read this file.
2. Read `docs/CURRENT_STATE.md`, and `docs/HANDOFF.md` if you are continuing work.
3. Read the docs relevant to the task (see the map in §3).
4. Check `git status` and recent commits.
5. Inspect the actual source code, and understand existing behaviour before changing it.

**During work**, keep track of:
- architectural changes;
- behaviour changes;
- files changed;
- tests run and their results;
- newly discovered issues;
- significant decisions.

**After work:**
1. Run the relevant tests.
2. **Always review and update `docs/CURRENT_STATE.md` and `docs/HANDOFF.md`.**
3. Update the other docs that the change affects:

| If you changed… | Update |
|---|---|
| Routing, prompts, SQL gen/verify/repair, composer, RAG, models, timeouts | AI_PIPELINE.md (+ TESTING.md, KNOWN_ISSUES.md) |
| DB objects used, joins, units, years, SQL execution | DATA_MODEL.md (+ AI_PIPELINE.md if query behaviour changed) |
| Anything scheme-specific, or added a scheme | SCHEMES.md (+ AI_PIPELINE.md if pipeline behaviour changed) |
| Components, API, auth, deploy, infra | ARCHITECTURE.md, PROJECT_CONTEXT.md, DECISIONS.md |
| A significant technical choice | DECISIONS.md |
| Test strategy or suites | TESTING.md |
| A bug found, fixed or re-scoped | KNOWN_ISSUES.md |

4. Run a consistency check: look for stale scheme counts, model roles, file names, test counts
   and issue statuses. Mark anything you cannot resolve as
   `DOCUMENTATION CONFLICT — NEEDS VERIFICATION`.
5. Finish with a summary covering: WHAT CHANGED · FILES CHANGED · TESTS RUN · TEST RESULTS ·
   DOCUMENTATION UPDATED · KNOWN ISSUES · REMAINING WORK · NEXT STEP.

**Never finish a significant task with stale documentation.** Do not assume earlier conversation
context exists: the repository must carry everything a new session, or a different Claude
account, needs. Don't create new top-level Markdown files for project knowledge. Extend the docs
above instead.

**Labels used in docs:**
- **VERIFIED:** read in code, config or a test run.
- **INFERRED:** reasoned from evidence, not directly confirmed.
- **UNKNOWN — NEEDS VERIFICATION:** could not be confirmed.
- **PLANNED:** not implemented.

## 9. Environment quick facts

- Windows dev box. Run from the repo root, because `.env` is read relative to the working
  directory.
- Start the app: `.venv/Scripts/python.exe -m uvicorn app.main:app --port 8400`, then open
  `/megh-chat` (chat), `/admin-ui` and `/health`.
- `megh_db` and the model gateway are on `10.48.242.4`, which needs the Fortinet VPN.
  The DB connection sometimes times out briefly, so retry.
- Qdrant moved on 2026-10-03 to `http://115.124.102.167:6335` (no VPN). It also holds six
  `Metadata_*` collections that are NOT ours (1024-dim schema metadata); never write to or delete them.
- `.env` is gitignored and holds real secrets. Never print or commit it.
