# LangGraph architecture (as built)

**Status:** implemented 2026-10-02, **off by default** (D-032). Code: `app/pipeline_graph.py`
(graph, state, adapter, checkpointer), the stage functions in `app/pipeline.py`. Tests:
`tests/test_pipeline_graph.py`. Related: `CONTEXT_AND_LANGGRAPH.md`, `LANGGRAPH_PAUSE_RESUME.md`,
`LANGGRAPH_MIGRATION.md`, and the Phase-1 analysis `MEGHALAYA_LANGGRAPH_ARCHITECTURE.md`.

## 1. One implementation, two orchestrators

```
                    pipeline.answer_question(question, session, scope)
                                   │
          PIPELINE_GRAPH_ENABLED / canary? ──yes──► pipeline_graph.answer_question_via_graph
                   │ no                                       │ (LangGraph: nodes, edges,
                   ▼                                          │  interrupt, checkpoint)
       _run_pipeline (sequential driver)                      │
                   │                                          │
                   └──────────────►  the SAME stage functions ◄┘
                    _turn_*_stage · _data_*_stage · _scheme_sql_rewrites ·
                    _execute_one_attempt · _repair_sql · _data_path_error_result ·
                    _attach_followups · _update_context
```

`_run_pipeline`, `_answer_data` and `execute_with_repair` were split at their existing step
boundaries. The code was moved **verbatim** with a splice script, and each stage got a prologue
(read its inputs from the turn dict into the local names the code always used) and an epilogue
(write its outputs back). Live inputs and outputs at each boundary were computed with `ast`. The
existing suite gives the same result before and after the split (1,371 passed, the same 2 known
failures). A graph node never contains business logic. A new step goes into a stage, so the two
paths cannot drift.

## 2. Graph (21 nodes)

```
START → context → edge → followup → route ──KNOWLEDGE──► knowledge ─────────────────┐
                                           └──DATA──► data_context → scope_scheme      │
   → entities → clarification_gate → deterministic → sql_generate → authorize          │
   → execute_attempt ──repairable──► repair ──► execute_attempt   (≤ MAX_SQL_REPAIRS)   │
          └──rows──► post_rows → compose → guarantees → assemble ───────────────────────┤
 any node: a result ready ──────────────────────────────────────────────────────────►  finalize
 any node: ClarificationNeeded ──► pause_gate [interrupt()] ──resume──► context         │
                                                              finalize → context_update → END
```

| Node | Stage it wraps | May end the turn | May pause |
|---|---|---|---|
| `context` | `_turn_context_stage`: spelling, CM Elevate pin, turn facts, pause reply (steps 0a/0a'/0a''), continuation (0-c) | – | ✓ |
| `edge` | `_turn_edge_stage`: harm, definitions, scheme listing / pick / recommendation, edge (0--, 0-, 0-a, 0) | ✓ | ✓ |
| `followup` | `_turn_followup_stage`: references (0f), merge plan + rewrite (1), edge re-check (1b), hints (1f) | ✓ | ✓ |
| `route` | `_turn_route_stage`: bank, admin, unsupported, village search (1c–1g), intent (2) | ✓ | ✓ |
| `knowledge` | `_turn_knowledge_stage`: RAG (3) | ✓ | ✓ |
| `data_context` | `_turn_data_context_stage`: inherited filters (4) | – | – |
| `scope_scheme` | `_data_scheme_stage`: scheme gates, `classify_scheme`, re-routes | ✓ | ✓ |
| `entities` | `_data_entity_stage`: near-miss / amount gates, `resolve_entities` | – | ✓ |
| `clarification_gate` | `_data_clarification_stage`: region, overall summary, scope / year / tranche | ✓ | ✓ |
| `deterministic` | `_data_deterministic_stage`: MGNREGA / PMAY-G fixed-shape queries | ✓ | ✓ |
| `sql_generate` | `_data_sql_stage`: `generate_sql` or the deterministic SQL | – | – |
| `authorize` | `_data_authorize_stage`: `auth.authorize` → `_denied` | ✓ (denied) | – |
| `execute_attempt` | `_data_deterministic_rows`, or `_scheme_sql_rewrites` + `_execute_one_attempt` | ✓ (via error map) | – |
| `repair` | `_repair_sql` | – | – |
| `post_rows` | `_data_post_rows_stage`: notes, premise check, deterministic answers | ✓ | – |
| `compose` | `_data_compose_stage`: `compose_response` | – | – |
| `guarantees` | `_data_guarantees_stage`: per-scheme answer guarantees | – | – |
| `assemble` | `_data_assemble` | ✓ | – |
| `pause_gate` | `interrupt()` only | – | ✓ |
| `finalize` | `_with_rewritten_question` (DATA) + `_attach_followups` | – | – |
| `context_update` | `_update_context` (`update_state` + summary) | – | – |

**Not built, and why.** The guide's `query_analyzer`, `schema_retrieve`, `findings`, `chart` and
`defaults_engine` have no counterpart here. Schema context is assembled inside `generate_sql`.
Charts are drawn by `web/ai_query.html` from `data`. There is no findings or defaults module. There
is also no separate `denied` node: `_data_authorize_stage` already returns `_denied(...)`, and a
pass-through node would add nothing.

**Authorization.** The `authorize` node checks the first SQL. Since 2026-10-02 (D-033, KI-183)
`execute_attempt` also checks every repaired SQL before it runs (`_repaired_sql_denial`) and ends the
turn with the denial, exactly as `execute_with_repair` does on the sequential path.

## 3. State (`MeghalayaGraphState`)

Plain data only (JSON or msgpack). The full schema with comments is in `app/pipeline_graph.py`.

| Group | Keys |
|---|---|
| Request | `raw_question`, `question`, `session_id`, `thread_id`, `resumed` |
| Context | `conversation_context`, `pause_outcome`, `cm_pinned`, `scope_resumed`, `prev`, `village_hint`, `paused_state`, `signals`, `has_context`, `plan`, `thread_state`, `thread_is_session`, `is_followup_rewrite`, `intent`, `prior_resolved` |
| DATA | `data_question`, `data_village_hint`, `schemes`, `fp_amount`, `entity_result`, `mg_admin`, `mg_women`, `mg_det`, `sql`, `sql_attempt`, `attempt_error`, `rows`, `notes`, `style`, `fl_total`, `fl_unplaced`, `fl_breakdown`, `answer` |
| Outcome | `response`, `response_origin` (`turn` / `data` / `data_error`), `pending_clarification`, `context_after`, `done`, `trace` (reducer) |

- **Dataclasses as dicts.** `Turn`, `ConversationState`, `MergePlan` and `StatedAmount` are stored
  as dicts and rebuilt per node (`_hydrate_*` / `_dehydrate_*`). When a follow-up continues the
  session's own state, that state is not copied: `thread_is_session=True`, and the node gets
  `session.state` itself, so the stages' identity checks hold.
- **Not in state.** The `Session`, the `UserScope`, DB pools and LLM clients travel in the
  **runtime context** (`GraphContext`), which LangGraph never checkpoints. A resume authorizes
  with the resuming request's scope, never a stored one.
- **Rows keep `Decimal` and `date` values.** The serializer round-trips them exactly
  (`pickle_fallback=True` covers any other driver type). Converting them would change the
  numeric-faithfulness checks.
- **Every turn starts from `fresh_turn_state()`**, which resets every key, so nothing from an earlier
  run leaks into the next (SQL, rows, a pause).

## 4. Checkpointing

- `thread_id = session_id`. A session-less call gets a one-off `graph-<uuid>`.
- Backend (`PIPELINE_GRAPH_CHECKPOINTER_DSN`):
  - **empty (default):** an SQLite file `LANGGRAPH_CHECKPOINT_DIR/langgraph_checkpoints.sqlite`
    (WAL mode, busy timeout 5 s);
  - **`postgresql://…`:** the optional `langgraph-checkpoint-postgres` (**NOT verified**);
  - **on failure:** in-process memory, with a warning.
- `PIPELINE_GRAPH_DURABILITY="exit"`: one write per run, at the pause or the end. `"sync"` writes
  after every node (the tests use it to prove every intermediate state serializes).
- A finished run's thread is **deleted**. Only paused threads persist. Unanswered ones are swept
  after `PIPELINE_GRAPH_PAUSE_TTL_SECONDS` (`sweep_loop`, started by `app.main` when the graph is in
  use). `PIPELINE_GRAPH_KEEP_THREADS=true` keeps them for debugging.
- **Correctness never depends on the checkpoint.** See `LANGGRAPH_PAUSE_RESUME.md` §4.

## 5. Observability

Each line uses the existing structured-log format (`json`, `request_id` from
`context_budget.request_id_var`):

- `pipeline_graph_node {...}`, one per node run: `node`, `ms`, `outcome`
  (`ok` / `response` / `pause` / `repairable` / `data_error` / `error`), input and output
  **shapes** (`str:57`, `list:3`, `dict:4`; never values), `attempt` (execute / repair), `rule`
  (pause), the result `route`, and `thread` (a SHA-1 prefix, not the session id).
- `pipeline_graph_run {...}`, one per request: `outcome` (`answer` / `pause` / `error`), `path`
  (the node sequence, across a pause and resume), `resumed`, `pause_outcome`, `repairs`, `ms`.
- The existing `pipeline_decision`, `prompt_context` and `llm_call` records still come from the
  stages unchanged, joined by `request_id`. Together they answer: what was inherited (decision
  `followup`), which scheme, why a pause (rule), which node ran next (`path`), whether SQL was
  repaired (`repairs`, and the existing "SQL failed (attempt n/4)" warning), and what context was
  persisted (`context_after` in state).
- **No question text, SQL or rows appear in graph logs.** `tests/test_pipeline_graph.py` asserts
  that a place named in the question appears in no graph log line.

## 6. Error semantics (unchanged)

| Raised in | Sequential | Graph |
|---|---|---|
| any stage: `ClarificationNeeded` | propagates to the router | `pending_clarification` → `pause_gate` → the adapter raises the same exception (same question, options, rule, village_hint) |
| DATA stages: `OutOfScope`, an exhausted repair budget, any other error | `_data_path_error_result`: scope reply / KB fallback | the same function, inside the node wrapper |
| DATA stages: gateway busy/timeout, DB unreachable | re-raised (503/504) | the same re-raise, through `astream` |
| pre-route / KNOWLEDGE stages | propagates | propagates |

If the final checkpoint write fails after `context_update`, the answer is still returned and the
failure is logged.

## 7. Performance

The graph adds no model or DB calls: the equivalence tests assert identical call counts per
conversation, including across a pause and resume. It does add orchestration time: about **+45 to
+120 ms per turn** offline (≈50 ms of LangGraph per-step machinery, the rest SQLite checkpoint
I/O), which is about 3–7% of a live 1.7 s turn. Figures and levers: `LANGGRAPH_MIGRATION.md` §4.
With the default `exit` durability, the checkpoint costs one SQLite read per request (the pause
lookup), one write per run and one thread delete.

## 8. Adding a node

1. Put the logic in a stage function in `pipeline.py`. The sequential driver calls it too.
2. Add any new state keys to `MeghalayaGraphState` (plain data), and to `fresh_turn_state` if a
   reset is needed.
3. Write `node_x` with `@_observed(name, inputs=…, outputs=…, data=…)`. It returns only the keys it
   changed. Never put `interrupt()` in a node that does model or DB work.
4. Register it in `NODES` and wire it in `_CHAIN` or `build_graph`.
5. Update `tests/test_pipeline_graph.py` (the node count is pinned at 21), then run the suite
   flag-off and flag-on (see `TESTING.md`).
