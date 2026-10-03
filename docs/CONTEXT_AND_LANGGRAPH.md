# Conversation context and LangGraph

How the existing context window (D-022, D-023, D-024, D-030) works when the LangGraph orchestrator
is on. **Short version: the context logic is unchanged and still the source of truth. The graph
runs it as nodes and records what it decided.**

## 1. Three layers, kept separate

| Layer | What | Where it lives | Lifetime |
|---|---|---|---|
| Long-term semantic context | `ConversationState` (scheme, district, block, village, year, metric, tranche, comparison, provenance), the last turn (+ row summary), the pending pause, recent questions, the rolling summary | `Session` (L1, per worker) ↔ `app.conversations.context_state` (Postgres, versioned, D-023) | the conversation |
| Short-term execution state | this turn's question, merge plan, inherited filters, schemes, entities, SQL, rows, answer | LangGraph state (`MeghalayaGraphState`) | one run; reset by `fresh_turn_state` |
| Checkpoint | the execution state of a **paused** run | SQLite / Postgres checkpointer, `thread_id = session_id` | while a pause waits |

Raw chat history is **not** copied into graph state. The state holds the previous turn as one
structured `Turn` (question, schemes, resolved entities, a 2,000-character answer cap, the row
summary), exactly what the sequential path holds. The existing optimisations are unchanged and
still apply:

- tiered rewrite evidence (filters, then the result summary, then an answer excerpt);
- token budgets (`context_budget`);
- semantic recall of older turns (`conversation_memory`);
- the rolling summary.

## 2. Lifecycle of one turn

```
router: session_store.ensure → session_sync.sync_in   (load the durable state; unchanged)
graph:
  context        reads session.state (recorded as conversation_context), consumes a pending
                 pause (merge / pick / paused-thread / abandon → pause_outcome), and computes
                 the continuation signals
  edge           a message with no continuation signal is a new question (no inheritance)
  followup       reference substitution ("the previous year"), the field-level merge plan
                 (KEEP / REPLACE / CLEAR / REQUIRE_CLARIFICATION), the provenance-checked
                 rewrite, and the scheme / year hints
  route          DATA or KNOWLEDGE (the user's own words win for a rewritten follow-up)
  data_context   prior_resolved = turn entities + session state (merged_prior_resolved),
                 minus every field the plan REPLACEs / CLEARs (apply_merge_plan),
                 minus model_inference values (provenance)
  … DATA nodes use prior_resolved through resolve_entities …
  context_update update_state (commit or drop each field, scheme-change reset, provenance)
                 + rolling summary; the result is recorded as context_after
router: add_turn → persist_turn → index_turn → session_sync.sync_out  (persist; unchanged)
```

## 3. Inheritance rules (unchanged, now visible per node)

| Rule | Implemented by | Where it runs in the graph |
|---|---|---|
| Explicit value in the question replaces the old one | `context_policy.plan_state_merge` → REPLACE | `followup` |
| Elliptical follow-up keeps scheme / place / year / metric | plan KEEP + `merged_prior_resolved` | `followup`, `data_context` |
| "by district" or a new district clears the old block / village | plan CLEAR + `apply_merge_plan` | `data_context` |
| Scheme switch drops the old scheme's filters | `update_state` `_scheme_changed`; swap detection `_SCHEME_SWAP_FOLLOWUP`; `_swap_measure_gap` | `followup`, `context_update` |
| A KNOWLEDGE or edge digression neither inherits nor overwrites | `update_state` (non-data turn), `_drop_inherited_time` / `_drop_inherited_place`, `_data_thread_antecedent` | `knowledge`, `followup`, `context_update` |
| No continuation signal → no inheritance | `context_policy.continuation_signals` | `context` |
| A value no user ever supplied is not inherited | provenance (`model_inference`) | `data_context` |
| A failed DATA turn does not commit | `update_state` guard | `context_update` |
| "Which one?" with nothing to pick → ask | plan REQUIRE_CLARIFICATION | `followup` → `pause_gate` |
| A pause reply continues the PAUSED thread, not the last answer | `_paused_thread_antecedent` | `context` |

There is no second merge policy: the graph calls these same functions.

## 4. Examples (all in `tests/test_pipeline_graph.py`; identical on both paths)

| Conversation | Result |
|---|---|
| "How many applications under CM Elevate in East Khasi Hills?" → "What about West Khasi Hills?" | scheme CM Elevate kept, district replaced: the SQL filters WEST KHASI HILLS |
| … → "What about MGNREGA?" → "2023-24" | the scheme switches; MGNREGA's year pause names MGNREGA only; the answer and state are MGNREGA |
| "How many applications under CM Elevate?" → pause → "East Khasi Hills" | the reply merges into the paused question |
| … → pause → "thanks" | the pause is abandoned (an edge reply); the pending pause is consumed |
| … → pause → "What is CM Elevate?" | the pause is abandoned; the reply is answered as KNOWLEDGE |
| data → "What is CM Elevate?" → "What about West Khasi Hills?" | the digression keeps the data context; the follow-up continues it |

## 5. Debugging a context decision

1. Find the request's `pipeline_graph_run` line: `path` shows which nodes ran, and
   `pause_outcome` shows how a pending pause was read.
2. Read the `pipeline_decision` lines with the same `request_id`: stages `continuation`,
   `followup` (kind, actions, antecedent), `pause_reply`, and `intent`.
3. With `PIPELINE_GRAPH_KEEP_THREADS=true` locally, `graph.aget_state({"configurable":
   {"thread_id": sid}})` shows `conversation_context`, `plan`, `prior_resolved` and
   `context_after` for the last run.
