# LangGraph migration and rollout

## 1. Flags (`app/config.py`, all in `.env`)

| Flag | Default | Effect |
|---|---|---|
| `PIPELINE_GRAPH_ENABLED` | `false` | every request on the graph |
| `PIPELINE_GRAPH_CANARY_PERCENT` | `0` | that share of **sessions** on the graph (CRC32 of the session id, so a conversation never switches orchestrator mid-thread) |
| `PIPELINE_GRAPH_CHECKPOINTER_DSN` | `""` | empty = SQLite; `postgresql://…` = Postgres (optional package, unverified, KI-185) |
| `LANGGRAPH_CHECKPOINT_DIR` | `logs` | where the SQLite file lives |
| `PIPELINE_GRAPH_DURABILITY` | `exit` | `exit` = one write per run; `async` / `sync` = one per node |
| `PIPELINE_GRAPH_KEEP_THREADS` | `false` | keep finished threads (debug only; they grow without bound) |
| `PIPELINE_GRAPH_PAUSE_TTL_SECONDS` | `1800` | sweep unanswered pauses after this |
| `PIPELINE_GRAPH_SWEEP_EVERY_SECONDS` | `600` | sweep period (minimum 60) |

There is no `CONTEXT_IN_GRAPH_ENABLED`. The guide's flag turned the context node into a no-op.
Here the context stages *are* the follow-up logic, so a graph without them would answer
follow-ups wrongly. The context layer's own flags (`CONTEXT_LAYER_ENABLED`,
`CONTEXT_STATE_ENABLED`, …) apply to both paths unchanged.

```
                    ┌── flags off (default) ──► _run_pipeline (sequential driver) ─┐
answer_question ────┤                                                              ├─► same stages
                    └── flag / canary ─────────► pipeline_graph (LangGraph) ───────┘
```

## 2. Rollout gates

| Gate | How | Status 2026-10-02 |
|---|---|---|
| G1 Stage refactor is behaviour-neutral | full pytest flag-off, before and after | **PASSED**: 1,371 → 1,371 passed, the same 2 known failures |
| G2 Graph ≡ sequential, offline | `tests/test_pipeline_graph.py` (27); full pytest with `PIPELINE_GRAPH_ENABLED=true` (proves only that the flag breaks nothing: no existing test calls `answer_question`) | **PASSED** for the 27 conversations tested; wider graph coverage needs G3/G4 |
| G3 Live context suite on the graph | `PIPELINE_GRAPH_ENABLED=true .venv/Scripts/python.exe tests/live_context_validation.py` (43 checks) | **PASSED 2026-10-02**: 43/43 on both paths |
| G4 Live use-case sample per scheme on the graph | 36 live turns across all six schemes, flag on vs off (session scratchpad `g4.py`); OFF-009 re-test (`off009.py`) | **PASSED 2026-10-02**: 36/36 identical; OFF-009 49/49 on both |
| G5 Canary | `PIPELINE_GRAPH_CANARY_PERCENT=10` on one VM; watch `pipeline_graph_run outcome=error`, p95 latency, 5xx | not started |
| G6 Cross-VM pauses | either accept KI-184 (correct, untraced) or verify Postgres (KI-185) | open |
| G7 Full rollout | `PIPELINE_GRAPH_ENABLED=true` | not started |
| G8 Remove the sequential driver | only after G7 has run cleanly for a period the team chooses; the stages stay, only `_run_pipeline` / `_answer_data` driver code goes | not planned |

## 3. Rollback

Set `PIPELINE_GRAPH_ENABLED=false` and `PIPELINE_GRAPH_CANARY_PERCENT=0`, then restart. Nothing
else is needed. Conversations continue: their state is in the Session snapshot, which both paths
read and write. A pause taken on the graph resumes on the sequential path through
`Session.pending_*`. The leftover checkpoint file can be deleted.

## 4. Performance (offline, model and DB stubbed: pure orchestration cost)

Measured 2026-10-02 on the Windows dev box. Each figure is the p50 per turn, the median of 3
interleaved rounds of 30 conversations, after warm-up. The model and DB are stubbed at zero
latency, so these are **orchestration costs only**; the real model and DB time is added equally
to both paths.

| Turn | Sequential | Graph, in-memory checkpointer | Graph, SQLite (default) | Added by the graph |
|---|---|---|---|---|
| Data answer | 95.8 ms | 142.2 ms | 168.5 ms | +73 ms |
| Follow-up (rewrite + merge) | 407.5 ms | 441.5 ms | 528.9 ms | +121 ms |
| Clarification pause | 18.6 ms | 49.1 ms | 62.3 ms | +44 ms |
| Pause reply (resume) | 16.6 ms | 73.2 ms | 96.7 ms | +80 ms |
| One-off per worker: compile + open SQLite | – | – | 161 ms | once |

- **Same work, nothing repeated.** cProfile shows an identical call count for every
  entity-resolver, edge, context-policy and pipeline function on both paths. The equivalence tests
  assert identical model and DB call counts, including across a resume, so the replay rule is
  respected.
- **Where the time goes.** Of a 137 ms data turn, 85 ms is inside the nodes (the same stage
  work). About 50 ms is LangGraph's per-step machinery: ~2.9 ms per step over 18 steps (task
  scheduling, callback managers, channel updates). The rest is the SQLite checkpoint (one read for
  the pause lookup, one write at exit, one thread delete), with `synchronous=NORMAL` under WAL.
  Stream mode (`values` / `updates` / `ainvoke`) made no difference.
- **In context.** A live turn's p50 was 1.70 s and p95 3.12 s (live context suite, 2026-09-29),
  so the graph adds about 3–7% at p50, and less on slow model turns. The 60 s request ceiling is
  unaffected.
- **Levers, not taken** (each trades away something the integration was asked for): fewer, coarser
  nodes; the in-memory checkpointer (−25 ms, but pauses no longer survive a worker restart);
  skipping the per-request pause lookup.
- **Not measured:** live latency on the graph path (KI-186). Re-measure from `pipeline_graph_run`
  `ms` against `/metrics` latency during the canary (G5).

## 5. Operating notes

- **Logs:** `grep pipeline_graph_run` gives one line per request (path, outcome, ms);
  `grep pipeline_graph_node` gives per-node timing and outcome.
- **`/health`:** with the graph in use, `components.pipeline_graph` shows the checkpointer
  backend (`sqlite`, `postgres`, `memory`, or `not-started` before the first graph request). It is
  informational and does not change `status`.
- **SQLite file:** `logs/langgraph_checkpoints.sqlite` (+ `-wal`, `-shm`). It is small, because
  only paused threads persist. Safe to delete while the service is stopped.
- **LangSmith:** the langgraph dependency ships `langsmith`. Tracing stays off unless
  `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` are set. **Never set them in this deploy**: they
  would send prompts and answers to an external service.
- **Dependency note:** `langgraph-sdk` requires `websockets<17` (16.1.1 installed). The app has no
  WebSocket endpoint.
