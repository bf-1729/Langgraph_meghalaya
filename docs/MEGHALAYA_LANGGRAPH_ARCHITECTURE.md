# Meghalaya × LangGraph — Phase-1 architecture analysis

**Date:** 2026-10-02. **Status:** design written before any application code changed (Phase 1 of the
LangGraph integration task). Companion docs: `LANGGRAPH_ARCHITECTURE.md` (the graph as built),
`CONTEXT_AND_LANGGRAPH.md`, `LANGGRAPH_PAUSE_RESUME.md`, `LANGGRAPH_MIGRATION.md`.

## 0. Findings that shaped the design (VERIFIED)

| Finding | Consequence |
|---|---|
| The supplied `langgraph.md` guide describes a **different codebase**: `pipeline_graph.py`, `langgraph_followup.py`, `query_analyzer`, `findings`, `chart_spec`, `schema_retrieval`, `defaults_engine`, `_scheme_stage`, `_clarification_stage` … none exist here. | The integration is built from scratch, following the guide's *rules* (thin nodes, serializable state, interrupt split from expensive work, flags, compat adapter), not its file list. |
| No query analyzer, schema-retrieval step, findings module or server-side chart builder exists. Schema context is built inside `generate_sql` (`prompt_builder` + `schema_introspect`); charts are drawn client-side from `data` (`web/ai_query.html`). | **No** `query_analyzer`, `schema_retrieve`, `findings` or `chart` nodes — the task forbids nodes with no real logic behind them. |
| **No WebSocket, no streaming** (CLAUDE.md §1). REST `POST /api/query` is the only entry. | §19 (WebSocket adapter) is not applicable. The graph returns the same dict as before. |
| `pipeline.py` header and D-002 deliberately chose "no graph framework". | The graph is **opt-in** behind flags; a DECISIONS entry (D-032) records the change. |
| The DB, gateway and Qdrant (`10.48.242.4`) were unreachable all session (VPN down). | Verification is offline: real functions, stubbed model/DB I/O. Live checks are listed as NEXT STEPS. |

## 1. Current architecture (VERIFIED in code)

```
POST /api/query  (app/routers/query.py)
  session_id = client id | "day-<user>-<date>" | "one-<uuid>"
  session_store.ensure()            in-process L1 Session
  session_sync.sync_in()            Postgres app.conversations.context_state (D-023)
  response_cache / semantic_cache   (skipped for follow-ups and pause replies)
  pipeline.answer_question()
     _run_pipeline()                pre-routing, context, edge, rewrite, intent, KNOWLEDGE / DATA
        _answer_data()              scheme → entities → gates → deterministic → SQL → auth → execute → compose
     _attach_followups()
     context_manager.update_state() + maybe_update_summary()
  except ClarificationNeeded → remember_pause() → sync_out() → CLARIFY response
  session_store.add_turn(), persist_turn(), index_turn(), sync_out(), audit, cache put
```

### 1.1 The twenty questions

| # | Question | Answer (file → symbol) |
|---|---|---|
| 1 | Request entry point | `routers/query.query` → `pipeline.answer_question` |
| 2 | Session identifier | `session_id` (client, day bucket, or one-off); the Session in `session_store` |
| 3 | Context-window implementation | `context_manager` (references, hints, merged prior, update, summary), `context_policy` (field merge plan, continuation signals, provenance), `context_budget` (token budgets + decision logs), `conversation_memory` (semantic recall) |
| 4 | Previous user messages | `Session.turns` (L1, max 8), `Turn.question/raw_question`; durable `recent_questions` in the snapshot; `app.conversation_turns` |
| 5 | Previous assistant responses | `Turn.answer` (snapshot keeps 2,000 chars), `Turn.result_summary` (deterministic row summary) |
| 6 | Resolved entities | `Turn.resolved_entities`; `ConversationState` district/block/village/year/tranche + `provenance` |
| 7 | Active scheme | `ConversationState.scheme` (single pinned) |
| 8 | Year / FY context | `ConversationState.year` (year_key), `previous_year`, `year_all` |
| 9 | Clarification state | `Session.pending_scope_q / pending_village_hint / pending_scope_rule / pending_scope_options`, part of `to_snapshot()["_session"]["pending"]` |
| 10 | Context injection | `_run_pipeline` steps 0f (`substitute_references`), 1 (`plan_state_merge`, `build_followup_context`, `rewrite_followup`), 1f (`inject_scheme_hint`, `inject_year_scope`), 4 (`merged_prior_resolved`, `apply_merge_plan` → `resolve_entities(prior_resolved=…)`) |
| 11 | Scheme switching | `_SCHEME_SWAP_FOLLOWUP`, `is_scheme_substitution`, `_scheme_substitution`, `_swap_measure_gap`; `update_state` drops filters on a scheme change |
| 12 | Follow-up interpretation | `looks_like_followup`, `is_scopeless_followup`, `continuation_signals`, `_followup_thread_state`, `_data_thread_antecedent`, provenance-checked `rewrite_followup` (D-022, D-024) |
| 13 | SQL generation context | `generate_sql(question, schemes, entity_result)`; entity_result carries the inherited filters |
| 14 | Answer composition | `compose_response` + per-scheme deterministic guarantees in `_answer_data` |
| 15 | Context update | `answer_question` → `context_manager.update_state` + `maybe_update_summary`; router → `add_turn`, `sync_out` |
| 16 | Errors / retries | `execute_with_repair` (≤3 repairs); `_run_pipeline` except-chain: OutOfScope → scope reply; gateway/DB errors re-raised (503/504); other → KB fallback |
| 17 | WebSocket streaming | none |
| 18 | Concurrent requests per session | possible; `sync_out` refuses a stale revision (newer state wins) |
| 19 | Persistence / checkpoints | Postgres snapshot (versioned JSONB), L1 per-worker cache; no LangGraph |
| 20 | Context tests | `test_context_manager.py`, `test_context_hardening.py`, `test_context_semantic_state.py`, `test_context_relevance_and_contract.py`, `test_stale_clarification.py`, `test_followup_scheme_scope.py`, `test_scheme_substitution.py`, `live_context_validation.py` |

### 1.2 Pause / resume today

A pause is an exception (`ClarificationNeeded`) raised from ~40 sites (pre-routing, the KNOWLEDGE
route, `_answer_data` gates and mostly `resolve_entities`). **No pause is raised after SQL
generation.** The router remembers it in `Session.pending_*` (durable via `sync_out`). On the next
turn, `_run_pipeline` step 0a/0a'/0a'' decides: chip click, typed scheme pick, typed option pick,
scope merge, paused-thread continuation, or abandonment (edge reply such as "thanks", or a
standalone new question). Then the **whole pipeline re-runs** on the merged question.

## 2. Proposed LangGraph architecture

**Principle:** LangGraph owns orchestration, per-node observability and the paused execution;
the Session snapshot in Postgres stays the source of truth for semantic conversation context
(D-023). No business logic moves into nodes.

### 2.1 Refactor that makes thin nodes possible

`_run_pipeline`, `_answer_data` and `execute_with_repair` are split at their existing step
boundaries into **stage functions** with the code moved verbatim. The bespoke path becomes a
sequential driver over those stages. The graph calls the same stages. One implementation, two
orchestrators, so the paths cannot drift.

### 2.2 Graph

```
START → context → edge → followup → route ─KNOWLEDGE→ knowledge ─────────────┐
                                        └─DATA→ data_context → scope_scheme  │
          → entities → clarification_gate → deterministic → sql_generate     │
          → authorize ─deny→ denied ────────────────────────────────────────┤
          → execute_attempt ⇄ repair (≤ 3)                                   │
          → post_rows → compose → guarantees → assemble ─────────────────────┤
  any node that produced a response ─────────────────────────────────────────┤
                                                                finalize → context_update → END
  any node that raised ClarificationNeeded → pause_gate [interrupt()] ─resume→ context
```

### 2.3 Node mapping

| Node | Existing code (moved verbatim) |
|---|---|
| `context` | `_run_pipeline` start: spelling, CM Elevate pin, turn_context, steps 0a/0a'/0a'' (pause reply), 0-c (continuation) |
| `edge` | steps 0--, 0-, 0-a, 0 |
| `followup` | steps 0f, 1, 1b, 1f |
| `route` | steps 1c, 1d, 1e, 1g, 2 (intent) |
| `knowledge` | step 3 |
| `data_context` | step 4 prior_resolved (merged_prior_resolved + apply_merge_plan) |
| `scope_scheme` | `_answer_data` scheme gates + `classify_scheme` + scheme reroutes |
| `entities` | near-miss / stated-amount gates + `resolve_entities` |
| `clarification_gate` | region, overall summary, scope / year / tranche gates |
| `deterministic` | MGNREGA admin / women / combined, PMAY-G facts |
| `sql_generate` | `generate_sql` (or the deterministic SQL) |
| `authorize` / `denied` | `auth.authorize` / `_denied` |
| `execute_attempt` / `repair` | one `execute_with_repair` iteration / its repair call |
| `post_rows` | scheme notes, premise check, deterministic post-row answers |
| `compose` / `guarantees` / `assemble` | `compose_response` / per-scheme guarantees / result dict |
| `finalize` / `context_update` | `_attach_followups` / `update_state` + `maybe_update_summary` |
| `pause_gate` | `interrupt()` only (the guide's `entities_gate`, generalised to every pause source) |

## 3. State mapping

Every field is JSON/msgpack-serializable. Dataclasses (`Turn`, `ConversationState`, `MergePlan`,
`StatedAmount`, `AuthDecision`) are stored as dicts and rebuilt inside the stage that needs them.
Request-bound objects (Session, UserScope) travel in LangGraph's **runtime context**, which is
never checkpointed. Result rows keep their `Decimal` and `date` values, because LangGraph's
serializer round-trips them exactly (VERIFIED). Converting them would change the faithfulness checks.

## 4. Lifecycles

- **Context:** read at `context` from the synced Session. Applied in `followup` and `data_context`.
  Written in `context_update` via `update_state`. Persisted by the router's `sync_out`
  (unchanged).
- **Checkpoint:** `thread_id = session_id`. Backend: SQLite file (default) or Postgres DSN. Threads
  exist only while a pause waits; they are deleted when a run finishes. A TTL sweep removes
  unanswered pauses.
- **Pause/resume:** a node catches `ClarificationNeeded` and stores `pending_clarification`.
  `pause_gate` calls `interrupt(payload)`. The adapter re-raises the same exception, so the router
  behaves exactly as before. On the next request on that thread, the adapter resumes with
  `Command(resume=reply)`. The gate returns a fresh turn state and the edge loops back to `context`,
  where the **existing** step-0a logic decides whether to merge or abandon. No second decision
  layer is written.

## 5. Compatibility strategy

`answer_question` is the only switch point (`PIPELINE_GRAPH_ENABLED`, or a session-sticky
`PIPELINE_GRAPH_CANARY_PERCENT`). The router is unchanged. Both paths return the same dict and raise the
same exceptions. Correctness never depends on the checkpoint: if the thread is missing (another
host, expired, flag flipped), the Session's `pending_*` still resumes the pause.

## 6. Risks

| Risk | Mitigation |
|---|---|
| A verbatim move drops or renames a local variable | Live-variable sets checked with `ast`; the full suite run with the flag off **and on**; path-equivalence tests |
| The default recursion limit (25) is exceeded by the longest path | `recursion_limit` set explicitly |
| A SQLite checkpoint shared by several workers locks under load | One write per run (`durability="exit"`); checkpoint failure is non-fatal by design (the pause also lives in the Session) |
| A SQLite checkpoint is not visible across the 2 VMs | Documented; the Session fallback keeps behaviour correct; use the Postgres DSN for cross-host threads |
| Postgres checkpointer (psycopg 3) is an extra driver and creates tables | Optional, lazily imported, UNVERIFIED; the DSN should pin `search_path=app` |
| New dependency tree (langchain-core, langsmith) | Pinned in requirements; LangSmith tracing stays off unless `LANGSMITH_TRACING` is set |
| No live verification this session | Listed first in NEXT STEPS: `live_context_validation.py` with the flag on |
