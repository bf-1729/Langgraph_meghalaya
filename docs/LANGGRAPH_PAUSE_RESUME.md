# Clarification pause and resume under LangGraph

## 1. Where pauses come from

A pause is a `ClarificationNeeded` raised by an existing rule. There are about 40 raise sites,
reachable from these nodes:

- `context`: reference ambiguity (`AmbiguousReference`);
- `followup`: `reference-ambiguous`, `swap-measure-unavailable`;
- `route`: bank, admin expenditure, unsupported scheme;
- `knowledge`: which scheme, which Focus;
- `scope_scheme`: unsupported, which Focus, which scheme, top-N, CM Elevate Legacy not-held;
- `entities`: acronym near-miss, stated amount, and the ~25 ambiguity rules inside
  `resolve_entities`;
- `clarification_gate`: region, scope, year, tranche;
- `deterministic`: MGNREGA women year.

**No pause is raised after SQL generation.** No rule was added or changed.

## 2. The flow

```
request 1 "How many applications under CM Elevate?"
  … node X raises ClarificationNeeded(question, options, rule, village_hint)
  _observed wrapper → state.pending_clarification = {question, options, rule, village_hint, node}
  edge → pause_gate: interrupt(payload)            ← checkpoint written, thread_id = session_id
  adapter: snapshot.interrupts → raise ClarificationNeeded(same fields)
  router (unchanged): remember_pause → Session.pending_* → sync_out (Postgres) → CLARIFY response

request 2 "East Khasi Hills"
  router (unchanged): sync_in → cacheable? no (pending) → answer_question
  adapter: aget_state(thread) has an interrupt → astream(Command(resume="East Khasi Hills"))
  pause_gate re-runs: interrupt() RETURNS the reply → returns fresh_turn_state(reply, resumed=True)
  edge → context: the EXISTING step 0a/0a'/0a'' logic reads Session.pending_* and decides
     chip click / typed scheme / typed option / scope merge / paused thread / abandon
  … the turn continues like any other → finalize → context_update → END
  adapter: no interrupt → delete the thread → return the result
```

## 3. Why the gate holds nothing but `interrupt()`

LangGraph re-executes a paused node **from its start** on resume. If `entities` (an LLM mention
extraction plus DB lookups) called `interrupt()` itself, every resume would repeat that work.
The expensive node only *records* the pause (`pending_clarification`, which is checkpointed). The
separate `pause_gate` pauses. Tests assert:

- `pause_gate` contains no `await`;
- no other node calls `interrupt()`;
- a pause and its resume spend **exactly** as many model and DB calls on the graph as on the
  sequential path.

## 4. Two stores, no second decision layer

The pending pause is in **both** stores:

- the Session snapshot in Postgres, cross-worker and versioned (D-023, KI-001);
- the LangGraph checkpoint.

The checkpoint never decides anything. The resumed graph hands the reply to `context`, and the
existing logic decides. That is deliberate:

| Case | What happens |
|---|---|
| Thread present, Session pending | graph resumes; `context` merges or abandons (the normal case) |
| Thread missing (another VM with the SQLite backend, swept by TTL, flag flipped since the pause) | a fresh run; `context` finds `Session.pending_*` and resumes it — **same result** (tested: `test_reply_resumes_even_when_the_checkpoint_is_gone`) |
| Thread present, Session has no pending (a pause the router does not remember, e.g. `reference-ambiguous` with no options) | graph resumes; `context` sees no pending; the reply is a new question — same as the sequential path |
| A cached answer was served in between | the stale interrupt is resumed by the next graph request and treated as above |

A router-level "does this reply continue the pause?" check (the guide's `_graph_pause_continues`)
was **not** written: it would duplicate `_resume_scheme_pause`, `_resume_option_pause`,
`_paused_thread_antecedent`, `_reply_abandons_scope_pause`, the chip-stem check and the edge bail-out.

## 5. Continue, abandon, acknowledge (existing rules, unchanged)

| Reply | Outcome (`pause_outcome` in the run log) |
|---|---|
| "2023-24", "East Khasi Hills", a chip, a typed scheme or option | `continued` (merged into the paused question) |
| A measure with no chip match after an option pause | `paused-thread` (continues the paused scheme) |
| "thanks", "hello", abuse, "never mind" | `abandoned`; answered as an edge reply |
| A standalone new question ("What is CM Elevate?") | `abandoned`; answered fresh |

## 6. Lifetime

- A thread exists only while a pause waits.
- A finished run deletes it. An error deletes it too.
- `sweep_loop` deletes threads idle longer than `PIPELINE_GRAPH_PAUSE_TTL_SECONDS` (1,800 s).
- The Session's own pending pause lives as long as the conversation row; it is consumed by the
  next message either way.

## 7. Concurrency

Runs on one thread are serialised **per worker** by an asyncio lock. Across workers, two
simultaneous requests on one session can both read the interrupt. The Session's stale-revision
guard (`sync_out`) still keeps the newer state, as on the sequential path.
