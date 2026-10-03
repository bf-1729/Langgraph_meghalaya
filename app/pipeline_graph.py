"""
The pipeline as a LangGraph graph (D-032). Opt-in: PIPELINE_GRAPH_ENABLED or
PIPELINE_GRAPH_CANARY_PERCENT; pipeline.answer_question is the only switch.

LangGraph owns orchestration here and nothing else:
  * the order of the stages, as explicit nodes and edges;
  * one structured log record per node (shapes, never values);
  * the SQL repair loop as an explicit execute_attempt -> repair cycle;
  * a clarification pause as interrupt(), checkpointed under
    thread_id = session_id, and resumed with Command(resume=reply).

Every node is a thin wrapper over a stage function in app/pipeline.py — the
same functions the sequential _run_pipeline / _answer_data / execute_with_repair
call — so the two orchestrators run the same code. Put new logic in a stage,
never in a node.

Two stores, two jobs:
  * the Session snapshot in Postgres (app.conversations.context_state, D-023)
    stays the source of truth for the conversation: structured state, the last
    turn, and the pending pause. The router reads and writes it as before.
  * the LangGraph checkpoint holds the paused EXECUTION only. A thread exists
    while a pause waits; it is deleted when a run finishes. Correctness never
    depends on it: a reply that lands where the thread is not visible (another
    host with the SQLite backend, an expired thread, the flag flipped) still
    resumes from the Session's pending pause, exactly as the sequential path.

On a resume the pause_gate returns the reply as a fresh turn and the graph
loops back to `context`, where the EXISTING pause-reply logic
(_turn_context_stage, steps 0a/0a'/0a'') decides whether the reply continues
the pause ("2023", a chip, a typed scheme) or abandons it ("thanks", a new
question). No second decision layer exists here.

Graph state holds plain data only. Request-bound objects (Session, UserScope)
travel in LangGraph's runtime context, which is never checkpointed. Dataclasses
the stages use (Turn, ConversationState, MergePlan, StatedAmount) are stored as
dicts and rebuilt per node (_hydrate_* / _dehydrate_*).
"""
import asyncio
import dataclasses
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from app import context_budget, pipeline
from app.config import settings
from app.context_policy import MergePlan
from app.db import DatabaseUnavailableError, UnsafeSQLError, is_connection_error as db_is_connection_error
from app.premise_check import StatedAmount
from app.session_store import ConversationState, Turn

logger = logging.getLogger(__name__)

# The longest path is ~30 node steps (all three repairs); a resume adds the
# pause gate and a second pass. LangGraph's default limit is 25.
_RECURSION_LIMIT = 100


def _trace_reducer(old: list | None, new: list | None) -> list:
    """Append node records; a list starting with {"reset": True} replaces them."""
    if new and isinstance(new[0], dict) and new[0].get("reset"):
        return list(new[1:])
    return (old or []) + (new or [])


class MeghalayaGraphState(TypedDict, total=False):
    # ── request ──
    raw_question: str                 # as typed (a pause reply on a resume)
    question: str                     # the routed question (pin, merge, rewrite applied)
    session_id: str
    thread_id: str
    resumed: bool                     # this run continued a paused thread
    # ── context (pipeline._turn_* stages; dataclasses stored as dicts) ──
    conversation_context: dict | None # session.state at the start of the turn (read-only)
    pause_outcome: str | None         # None | "continued" | "paused-thread" | "abandoned"
    cm_pinned: bool
    scope_resumed: bool
    prev: dict | None                 # the antecedent Turn
    village_hint: str | None
    paused_state: dict | None         # ConversationState of a paused thread
    signals: list
    has_context: bool
    plan: dict | None                 # MergePlan: kind, actions, reasons
    thread_state: dict | None         # ConversationState a follow-up continues ...
    thread_is_session: bool           # ... or True: it is session.state itself
    is_followup_rewrite: bool
    intent: str | None                # "DATA" | "KNOWLEDGE"
    prior_resolved: dict | None       # filters a DATA follow-up inherits
    # ── DATA (pipeline._data_* stages) ──
    data_question: str | None         # may become a year-gap rewrite inside the DATA path
    data_village_hint: str | None
    schemes: list | None
    fp_amount: dict | None            # StatedAmount
    entity_result: dict | None
    mg_admin: Any
    mg_women: dict | None
    mg_det: Any
    sql: str | None
    sql_attempt: int                  # repairs done so far (0..MAX_SQL_REPAIRS)
    attempt_error: str | None         # the last attempt's error, for the repair call
    rows: list | None
    notes: list | None
    style: str
    fl_total: Any
    fl_unplaced: Any
    fl_breakdown: str | None
    answer: str | None
    # ── outcome ──
    response: dict | None
    response_origin: str | None       # "turn" | "data" | "data_error"
    pending_clarification: dict | None
    context_after: dict | None        # session.state after context_update
    done: bool
    trace: Annotated[list, _trace_reducer]


_TURN_KEYS = ("raw_question", "question", "cm_pinned", "scope_resumed", "prev", "village_hint",
              "paused_state", "signals", "has_context", "plan", "thread_state", "is_followup_rewrite",
              "intent", "prior_resolved")
# data-turn key -> graph-state key (the rest share a name)
_DATA_KEY = {"question": "data_question", "skip_scope_clarify": "scope_resumed",
             "village_hint": "data_village_hint"}
_DATA_KEYS = ("question", "skip_scope_clarify", "prior_resolved", "village_hint", "schemes",
              "fp_amount", "entity_result", "mg_admin", "mg_women", "mg_det", "sql", "rows", "notes",
              "style", "fl_total", "fl_unplaced", "fl_breakdown", "answer")
# Read by the data stages but owned by the turn stages: never written back.
_DATA_READ_ONLY = ("skip_scope_clarify", "prior_resolved")


def fresh_turn_state(question: str, *, session_id: str = "", thread_id: str = "",
                     resumed: bool = False) -> dict:
    """Every key reset for a new turn. Input values overwrite the thread's
    previous ones, so nothing from an earlier run (SQL, rows, a pause) leaks
    into this one even when a thread is kept."""
    t = pipeline._new_turn(question)
    state: dict = {k: t[k] for k in _TURN_KEYS}
    state.update(prev=None, paused_state=None, plan=None, thread_state=None, thread_is_session=False)
    d = pipeline._new_data_turn(question)
    for k in _DATA_KEYS:
        if k not in _DATA_READ_ONLY:
            state[_DATA_KEY.get(k, k)] = d[k]
    state.update(session_id=session_id, thread_id=thread_id, resumed=resumed,
                 conversation_context=None, pause_outcome=None, data_question=None,
                 sql_attempt=0, attempt_error=None, response=None, response_origin=None,
                 pending_clarification=None, context_after=None, done=False,
                 trace=[{"reset": True}])
    return state


# ── (de)hydration: live objects for the stages, plain data for the state ─────

def _as_dict(obj) -> "dict | None":
    if obj is None:
        return None
    if isinstance(obj, ConversationState):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return {f.name: getattr(obj, f.name, f.default if f.default is not dataclasses.MISSING else None)
            for f in dataclasses.fields(Turn)}


def _hydrate_turn(state: dict, session) -> dict:
    t = {k: state.get(k) for k in _TURN_KEYS}
    t["prev"] = Turn(**state["prev"]) if state.get("prev") else None
    t["paused_state"] = (ConversationState.from_dict(state["paused_state"])
                         if state.get("paused_state") else None)
    p = state.get("plan")
    t["plan"] = (MergePlan(actions=dict(p.get("actions") or {}), reasons=dict(p.get("reasons") or {}),
                           kind=p.get("kind")) if p else None)
    if state.get("thread_is_session"):
        t["thread_state"] = pipeline._turn_ctx_state(session)
    else:
        t["thread_state"] = (ConversationState.from_dict(state["thread_state"])
                             if state.get("thread_state") else None)
    t["signals"] = list(state.get("signals") or [])
    return t


def _dehydrate_turn(t: dict, session) -> dict:
    out = {k: t.get(k) for k in _TURN_KEYS}
    out["prev"] = _as_dict(t.get("prev"))
    out["paused_state"] = _as_dict(t.get("paused_state"))
    plan = t.get("plan")
    out["plan"] = (None if plan is None else
                   {"kind": plan.kind, "actions": dict(plan.actions), "reasons": dict(plan.reasons)})
    ts = t.get("thread_state")
    is_session = ts is pipeline._turn_ctx_state(session)
    out["thread_is_session"] = is_session
    out["thread_state"] = None if is_session else _as_dict(ts)
    out["signals"] = list(t.get("signals") or [])
    return out


def _hydrate_data(state: dict) -> dict:
    d = {k: state.get(_DATA_KEY.get(k, k)) for k in _DATA_KEYS}
    d["skip_scope_clarify"] = bool(d["skip_scope_clarify"])
    fp = state.get("fp_amount")
    d["fp_amount"] = StatedAmount(**{**fp, "span": tuple(fp.get("span") or ())}) if fp else None
    d["style"] = d["style"] or ""
    return d


def _dehydrate_data(d: dict) -> dict:
    out = {_DATA_KEY.get(k, k): d.get(k) for k in _DATA_KEYS if k not in _DATA_READ_ONLY}
    out["fp_amount"] = dataclasses.asdict(d["fp_amount"]) if d.get("fp_amount") is not None else None
    return out


# ── Runtime context (never checkpointed) ────────────────────────────────────

@dataclass
class GraphContext:
    session: Any = None               # app.session_store.Session
    scope: Any = None                 # app.auth.UserScope (this request's, never a stored one)


# ── Observability ───────────────────────────────────────────────────────────

def _shape(v) -> Any:
    """A value's shape for logs: type and size, never content."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return f"str:{len(v)}"
    if isinstance(v, (list, tuple)):
        return f"list:{len(v)}"
    if isinstance(v, dict):
        return f"dict:{len(v)}"
    return type(v).__name__


def _thread_tag(thread_id: str) -> str:
    return hashlib.sha1((thread_id or "").encode("utf-8")).hexdigest()[:10]


def _log(event: str, **facts) -> dict:
    record = {"event": event, "request_id": context_budget.request_id_var.get(), **facts}
    logger.info("%s %s", event, json.dumps(record, default=str, sort_keys=True))
    return record


def _pause_payload(e: "pipeline.ClarificationNeeded", node: str) -> dict:
    return {"question": e.question, "options": list(e.options or []), "rule": e.rule,
            "village_hint": e.village_hint, "node": node}


def _observed(name: str, *, inputs: tuple = (), outputs: tuple = (), data: bool = False):
    """One `pipeline_graph_node` record per run of the node (input and output
    SHAPES, the outcome, the duration, the pause rule) and a trace entry in the
    state. A ClarificationNeeded becomes `pending_clarification` (routed to the
    pause gate). On the DATA path (`data=True`) any other failure is mapped
    exactly as _run_pipeline maps it (pipeline._data_path_error_result): a
    result, or the same re-raise."""
    def deco(fn):
        async def node(state: MeghalayaGraphState, runtime: Runtime[GraphContext]) -> dict:
            ctx = runtime.context or GraphContext()
            t0 = time.perf_counter()
            token = None
            if data:
                # the typed text of a rewritten follow-up, for the PMAY-G facts path
                token = pipeline._PMAY_TYPED_TURN.set(
                    state.get("raw_question") if state.get("question") != state.get("raw_question") else None)
            extra: dict = {}
            try:
                upd = await fn(state, ctx)
                outcome = "response" if upd.get("response") is not None else "ok"
            except pipeline.ClarificationNeeded as e:
                upd = {"pending_clarification": _pause_payload(e, name)}
                outcome, extra = "pause", {"rule": e.rule}
            except Exception as e:  # noqa: BLE001 — mapped below, or re-raised unchanged
                if not data:
                    _log("pipeline_graph_node", node=name, outcome="error", error=type(e).__name__,
                         ms=round((time.perf_counter() - t0) * 1000, 1), thread=_thread_tag(state.get("thread_id")))
                    raise
                try:
                    resp = await pipeline._data_path_error_result(e, state.get("question"),
                                                                 state.get("raw_question"))
                except Exception as raised:
                    _log("pipeline_graph_node", node=name, outcome="error", error=type(raised).__name__,
                         ms=round((time.perf_counter() - t0) * 1000, 1), thread=_thread_tag(state.get("thread_id")))
                    raise
                upd = {"response": resp, "response_origin": "data_error"}
                outcome, extra = "data_error", {"error": type(e).__name__}
            finally:
                if token is not None:
                    pipeline._PMAY_TYPED_TURN.reset(token)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            if upd.get("attempt_error"):
                outcome = "repairable"
            entry = {"node": name, "ms": ms, "outcome": outcome, **extra}
            _log("pipeline_graph_node", node=name, outcome=outcome, ms=ms,
                 thread=_thread_tag(state.get("thread_id")),
                 inputs={k: _shape(state.get(k)) for k in inputs},
                 outputs={k: _shape(upd.get(k)) for k in outputs if k in upd},
                 attempt=state.get("sql_attempt") if name in ("execute_attempt", "repair") else None,
                 route=(upd.get("response") or {}).get("route"), **extra)
            upd["trace"] = [entry]
            return upd
        node.__name__ = f"node_{name}"
        node.__qualname__ = node.__name__
        # Not __wrapped__: LangGraph reads the unwrapped signature to decide
        # whether to pass `runtime`, and fn takes (state, ctx).
        node.stage_fn = fn
        return node
    return deco


# ── Turn nodes (pipeline._turn_* stages) ────────────────────────────────────

async def _turn(stage, state: dict, ctx: GraphContext) -> dict:
    t = _hydrate_turn(state, ctx.session)
    result = await stage(t, ctx.session, ctx.scope)
    upd = _dehydrate_turn(t, ctx.session)
    if result is not None:
        upd.update(response=result, response_origin="turn")
    return upd


@_observed("context", inputs=("raw_question", "resumed"),
           outputs=("question", "scope_resumed", "has_context", "signals", "pause_outcome"))
async def node_context(state, ctx):
    session = ctx.session
    had_pending = bool(getattr(session, "pending_scope_q", None))
    before = session.state.to_dict() if session is not None else None
    upd = await _turn(pipeline._turn_context_stage, state, ctx)
    outcome = None
    if had_pending:
        reply = (getattr(session, "turn_context", None) or {}).get("clarification_reply")
        outcome = ("continued" if reply or upd.get("scope_resumed")
                   else "paused-thread" if upd.get("paused_state") else "abandoned")
    upd.update(conversation_context=before, pause_outcome=outcome)
    return upd


@_observed("edge", inputs=("question", "has_context"), outputs=("response",))
async def node_edge(state, ctx):
    return await _turn(pipeline._turn_edge_stage, state, ctx)


@_observed("followup", inputs=("question", "prev", "has_context"),
           outputs=("question", "is_followup_rewrite", "plan", "thread_is_session"))
async def node_followup(state, ctx):
    return await _turn(pipeline._turn_followup_stage, state, ctx)


@_observed("route", inputs=("question", "is_followup_rewrite"), outputs=("intent", "response"))
async def node_route(state, ctx):
    return await _turn(pipeline._turn_route_stage, state, ctx)


@_observed("knowledge", inputs=("question", "prev"), outputs=("response",))
async def node_knowledge(state, ctx):
    return await _turn(pipeline._turn_knowledge_stage, state, ctx)


@_observed("data_context", inputs=("prev", "plan", "is_followup_rewrite"),
           outputs=("prior_resolved", "data_question"))
async def node_data_context(state, ctx):
    upd = await _turn(pipeline._turn_data_context_stage, state, ctx)
    upd["data_question"] = upd["question"]
    upd["data_village_hint"] = upd["village_hint"] if upd["scope_resumed"] else None
    return upd


# ── DATA nodes (pipeline._data_* stages) ────────────────────────────────────

async def _data(stage, state: dict, ctx: GraphContext) -> dict:
    d = _hydrate_data(state)
    result = await stage(d, ctx.scope)
    upd = _dehydrate_data(d)
    if result is not None:
        upd.update(response=result, response_origin="data")
    return upd


@_observed("scope_scheme", inputs=("data_question",), outputs=("schemes", "response"), data=True)
async def node_scope_scheme(state, ctx):
    return await _data(pipeline._data_scheme_stage, state, ctx)


@_observed("entities", inputs=("data_question", "schemes", "prior_resolved"),
           outputs=("entity_result", "fp_amount"), data=True)
async def node_entities(state, ctx):
    return await _data(pipeline._data_entity_stage, state, ctx)


@_observed("clarification_gate", inputs=("entity_result", "scope_resumed"),
           outputs=("entity_result", "response"), data=True)
async def node_clarification_gate(state, ctx):
    return await _data(pipeline._data_clarification_stage, state, ctx)


@_observed("deterministic", inputs=("schemes", "entity_result"),
           outputs=("mg_admin", "mg_women", "mg_det", "response"), data=True)
async def node_deterministic(state, ctx):
    return await _data(pipeline._data_deterministic_stage, state, ctx)


@_observed("sql_generate", inputs=("schemes", "entity_result", "mg_det"), outputs=("sql",), data=True)
async def node_sql_generate(state, ctx):
    upd = await _data(pipeline._data_sql_stage, state, ctx)
    upd["sql_attempt"] = 0
    upd["attempt_error"] = None
    return upd


@_observed("authorize", inputs=("schemes", "sql"), outputs=("response",), data=True)
async def node_authorize(state, ctx):
    d = _hydrate_data(state)
    denied = await pipeline._data_authorize_stage(d, ctx.scope)
    return {} if denied is None else {"response": denied, "response_origin": "data"}


@_observed("execute_attempt", inputs=("sql", "sql_attempt"), outputs=("rows", "attempt_error"), data=True)
async def node_execute_attempt(state, ctx):
    """One iteration of execute_with_repair: the deterministic query, or the
    rewrites + guards + verifier + execution. A repairable failure goes to the
    repair node; the DB, an exhausted budget and everything else raise exactly
    where execute_with_repair raises."""
    d = _hydrate_data(state)
    if d["mg_det"]:
        await pipeline._data_deterministic_rows(d, ctx.scope)
        return {"rows": d["rows"], "attempt_error": None}
    question, schemes, entity_result = d["question"], d["schemes"], d["entity_result"]
    attempt = int(state.get("sql_attempt") or 0)
    sql = await pipeline._scheme_sql_rewrites(question, schemes, entity_result, d["sql"])
    if attempt:
        # a repaired query passes the same scope check as the first one (KI-183)
        denial = pipeline._repaired_sql_denial(ctx.scope, schemes, entity_result, sql)
        if denial is not None:
            return {"sql": sql, "attempt_error": None, "response_origin": "data",
                    "response": pipeline._denied(denial, schemes, entity_result["resolved"])}
    try:
        rows = await pipeline._execute_one_attempt(question, schemes, entity_result, sql)
        return {"sql": sql, "rows": rows, "attempt_error": None}
    except (UnsafeSQLError, Exception) as e:
        # the same decisions, in the same order, as execute_with_repair's except
        if db_is_connection_error(e):
            raise DatabaseUnavailableError(str(e)) from e
        if attempt == pipeline.MAX_SQL_REPAIRS:
            raise  # repair budget spent — mapped like the sequential path
        logger.warning("SQL failed (attempt %d/%d), repairing: %s",
                       attempt + 1, pipeline.MAX_SQL_REPAIRS + 1, e)
        return {"sql": sql, "attempt_error": str(e) or type(e).__name__}


@_observed("repair", inputs=("sql", "attempt_error", "sql_attempt"), outputs=("sql", "sql_attempt"), data=True)
async def node_repair(state, ctx):
    d = _hydrate_data(state)
    sql = await pipeline._repair_sql(d["question"], d["schemes"], d["entity_result"],
                                     d["sql"], state.get("attempt_error") or "")
    return {"sql": sql, "sql_attempt": int(state.get("sql_attempt") or 0) + 1, "attempt_error": None}


@_observed("post_rows", inputs=("sql", "rows"), outputs=("notes", "rows", "response"), data=True)
async def node_post_rows(state, ctx):
    return await _data(pipeline._data_post_rows_stage, state, ctx)


@_observed("compose", inputs=("rows", "notes"), outputs=("answer",), data=True)
async def node_compose(state, ctx):
    return await _data(pipeline._data_compose_stage, state, ctx)


@_observed("guarantees", inputs=("answer", "rows"), outputs=("answer",), data=True)
async def node_guarantees(state, ctx):
    return await _data(pipeline._data_guarantees_stage, state, ctx)


@_observed("assemble", inputs=("answer", "rows"), outputs=("response",), data=True)
async def node_assemble(state, ctx):
    return {"response": pipeline._data_assemble(_hydrate_data(state)), "response_origin": "data"}


# ── Pause, finish, context update ───────────────────────────────────────────

async def node_pause_gate(state: MeghalayaGraphState) -> dict:
    """interrupt() and nothing else. LangGraph replays a paused node in full on
    resume, so no model or DB call may sit in this node (the expensive work
    that raised the pause ran in an earlier node). The reply comes back as a
    fresh turn; `context` decides whether it continues the pause."""
    payload = state.get("pending_clarification") or {}
    reply = interrupt(payload)
    _log("pipeline_graph_node", node="pause_gate", outcome="resumed", rule=payload.get("rule"),
         thread=_thread_tag(state.get("thread_id")), reply=_shape(reply))
    fresh = fresh_turn_state(str(reply), session_id=state.get("session_id") or "",
                             thread_id=state.get("thread_id") or "", resumed=True)
    # keep the paused half of the trace: one run reads as one conversation step
    fresh["trace"] = [{"node": "pause_gate", "outcome": "resumed", "rule": payload.get("rule")}]
    return fresh


@_observed("finalize", inputs=("response_origin",), outputs=("response",))
async def node_finalize(state, ctx):
    """The tail of answer_question: a DATA result names the standalone question
    it answered (_run_pipeline's success branch), then the 'Next steps'."""
    result = state["response"]
    if state.get("response_origin") == "data":
        result = pipeline._with_rewritten_question(result, state["question"], state["raw_question"])
    pipeline._attach_followups(result, result.get("rewritten_question") or state["raw_question"])
    return {"response": result}


@_observed("context_update", inputs=("response",), outputs=("context_after",))
async def node_context_update(state, ctx):
    """update_state + the rolling summary (pipeline._update_context). The
    router then persists session.state with the turn (session_sync.sync_out)."""
    await pipeline._update_context(ctx.session, state["raw_question"], state["response"])
    after = ctx.session.state.to_dict() if ctx.session is not None else None
    return {"context_after": after, "done": True}


# ── Edges ───────────────────────────────────────────────────────────────────

def _next(default: str):
    def route(state: MeghalayaGraphState) -> str:
        if state.get("pending_clarification"):
            return "pause"
        if state.get("response") is not None:
            return "finish"
        return default
    route.__name__ = f"route_to_{default}"
    return route


def _route_after_route(state: MeghalayaGraphState) -> str:
    if state.get("pending_clarification"):
        return "pause"
    if state.get("response") is not None:
        return "finish"
    return "knowledge" if state.get("intent") == "KNOWLEDGE" else "data"


def _route_after_attempt(state: MeghalayaGraphState) -> str:
    if state.get("pending_clarification"):
        return "pause"
    if state.get("response") is not None:
        return "finish"
    return "repair" if state.get("attempt_error") else "done"


NODES = {
    "context": node_context, "edge": node_edge, "followup": node_followup, "route": node_route,
    "knowledge": node_knowledge, "data_context": node_data_context,
    "scope_scheme": node_scope_scheme, "entities": node_entities,
    "clarification_gate": node_clarification_gate, "deterministic": node_deterministic,
    "sql_generate": node_sql_generate, "authorize": node_authorize,
    "execute_attempt": node_execute_attempt, "repair": node_repair,
    "post_rows": node_post_rows, "compose": node_compose, "guarantees": node_guarantees,
    "assemble": node_assemble, "pause_gate": node_pause_gate,
    "finalize": node_finalize, "context_update": node_context_update,
}

# (node, next node when it neither paused nor answered)
_CHAIN = [("context", "edge"), ("edge", "followup"), ("followup", "route"),
          ("knowledge", "data_context"), ("data_context", "scope_scheme"),
          ("scope_scheme", "entities"), ("entities", "clarification_gate"),
          ("clarification_gate", "deterministic"), ("deterministic", "sql_generate"),
          ("sql_generate", "authorize"), ("authorize", "execute_attempt"),
          ("post_rows", "compose"), ("compose", "guarantees"), ("guarantees", "assemble")]


def build_graph() -> StateGraph:
    graph = StateGraph(MeghalayaGraphState, context_schema=GraphContext)
    for name, fn in NODES.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "context")
    for src, dst in _CHAIN:
        graph.add_conditional_edges(src, _next(dst),
                                    {dst: dst, "pause": "pause_gate", "finish": "finalize"})
    graph.add_conditional_edges("route", _route_after_route,
                                {"knowledge": "knowledge", "data": "data_context",
                                 "pause": "pause_gate", "finish": "finalize"})
    graph.add_conditional_edges("execute_attempt", _route_after_attempt,
                                {"repair": "repair", "done": "post_rows",
                                 "pause": "pause_gate", "finish": "finalize"})
    graph.add_conditional_edges("repair", _next("execute_attempt"),
                                {"execute_attempt": "execute_attempt", "pause": "pause_gate",
                                 "finish": "finalize"})
    graph.add_edge("assemble", "finalize")
    graph.add_edge("pause_gate", "context")      # the reply runs as the next turn
    graph.add_edge("finalize", "context_update")
    graph.add_edge("context_update", END)
    return graph


# ── Checkpointer and compiled graph (one per event loop) ────────────────────

class _Runtime:
    def __init__(self, loop, graph, saver, closer, backend: str):
        self.loop, self.graph, self.saver, self.closer, self.backend = loop, graph, saver, closer, backend
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, thread_id: str) -> asyncio.Lock:
        # Serialises runs on one thread within this worker (two tabs, a double
        # click). Across workers the Session's stale-revision guard still holds.
        lk = self.locks.get(thread_id)
        if lk is None:
            if len(self.locks) > 5000:
                self.locks = {k: v for k, v in self.locks.items() if v.locked()}
            lk = self.locks[thread_id] = asyncio.Lock()
        return lk


_rt: "_Runtime | None" = None
_rt_lock: "asyncio.Lock | None" = None
_rt_lock_loop = None


def _serde() -> JsonPlusSerializer:
    # Result rows keep their Decimal / date values (the faithfulness checks
    # compare them exactly). JsonPlus round-trips those; pickle_fallback covers
    # any other driver type, so a checkpoint write can never fail on a cell.
    return JsonPlusSerializer(pickle_fallback=True)


def _postgres_conninfo(dsn: str) -> str:
    """The checkpoint DSN, pinned to the app's own schema. "postgres" means
    reuse DATABASE_URL (the app's megh_db login). search_path=app puts the
    four checkpoint tables in app.*, the only schema the app creates objects in
    (CLAUDE.md §6), never in curated / public."""
    url = (settings.DATABASE_URL if dsn == "postgres" else dsn).replace("postgresql+asyncpg://", "postgresql://")
    if "search_path" in url:
        return url
    return url + ("&" if "?" in url else "?") + "options=-csearch_path%3Dapp"


async def _open_saver():
    dsn = (settings.PIPELINE_GRAPH_CHECKPOINTER_DSN or "").strip()
    wants_pg = dsn == "postgres" or dsn.startswith(("postgres://", "postgresql://"))
    if wants_pg and type(asyncio.get_running_loop()).__name__ == "ProactorEventLoop":
        # psycopg's async driver cannot run on Windows' default Proactor loop
        # (dev boxes only; the Ubuntu VMs use the selector loop).
        logger.warning("pipeline_graph: the Postgres checkpointer needs a selector event loop on Windows — "
                       "using the SQLite checkpointer instead")
        wants_pg = False
    if wants_pg:
        # Shared across workers and both VMs (KI-184). A small pool, not the
        # package's single connection, so concurrent requests in one worker do
        # not queue behind each other. setup() is idempotent (IF NOT EXISTS).
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(_postgres_conninfo(dsn), min_size=1, max_size=4, open=False,
                                   kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        await pool.open(wait=True, timeout=10)
        saver = AsyncPostgresSaver(pool, serde=_serde())
        await saver.setup()
        return saver, pool.close, "postgres"
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    path = Path(settings.LANGGRAPH_CHECKPOINT_DIR or "logs")
    path.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path / "langgraph_checkpoints.sqlite"))
    # several workers on one host share the file
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    # No fsync per commit: a checkpoint is never the source of truth (a lost
    # pause still resumes from the Session snapshot), and WAL + NORMAL stays
    # consistent across an app crash.
    await conn.execute("PRAGMA synchronous=NORMAL")
    saver = AsyncSqliteSaver(conn, serde=_serde())
    await saver.setup()
    return saver, conn.close, "sqlite"


async def _runtime() -> _Runtime:
    global _rt, _rt_lock, _rt_lock_loop
    loop = asyncio.get_running_loop()
    if _rt is not None and _rt.loop is loop:
        return _rt
    if _rt_lock is None or _rt_lock_loop is not loop:
        _rt_lock, _rt_lock_loop = asyncio.Lock(), loop
    async with _rt_lock:
        if _rt is not None and _rt.loop is loop:
            return _rt
        try:
            saver, closer, backend = await _open_saver()
        except Exception:  # noqa: BLE001 — a pause still resumes from the Session
            logger.warning("pipeline_graph: checkpointer unavailable — using in-process memory; "
                           "pauses resume from the Session snapshot as on the sequential path",
                           exc_info=True)
            saver, closer, backend = InMemorySaver(serde=_serde()), None, "memory"
        _rt = _Runtime(loop, build_graph().compile(checkpointer=saver), saver, closer, backend)
        logger.info("pipeline_graph: compiled, %d nodes, checkpointer=%s", len(NODES), backend)
        return _rt


async def close() -> None:
    """Close the checkpointer (app shutdown)."""
    global _rt
    rt, _rt = _rt, None
    if rt is not None and rt.closer is not None:
        try:
            await rt.closer()
        except Exception:  # noqa: BLE001
            logger.warning("pipeline_graph: checkpointer close failed", exc_info=True)


def status() -> dict:
    return {"enabled": settings.PIPELINE_GRAPH_ENABLED,
            "canary_percent": settings.PIPELINE_GRAPH_CANARY_PERCENT,
            "checkpointer": _rt.backend if _rt is not None else "not-started"}


async def _drop_thread(rt: _Runtime, thread_id: str) -> None:
    if settings.PIPELINE_GRAPH_KEEP_THREADS:
        return
    try:
        await rt.saver.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001 — the sweep removes it later
        logger.warning("pipeline_graph: could not delete thread %s", _thread_tag(thread_id), exc_info=True)


async def pending_pause(thread_id: str) -> "dict | None":
    """The interrupt payload this thread is paused on, or None."""
    rt = await _runtime()
    snap = await rt.graph.aget_state({"configurable": {"thread_id": thread_id}})
    return snap.interrupts[0].value if snap.interrupts else None


# ── Entry point (the compatibility adapter) ─────────────────────────────────

async def answer_question_via_graph(question: str, session=None, scope=None) -> dict:
    """Same contract as pipeline.answer_question: the same result dict, the
    same ClarificationNeeded (question, options, rule, village_hint) for a
    pause, the same exceptions for the router to map to 503/504/5xx."""
    rt = await _runtime()
    thread_id = session.session_id if session is not None else f"graph-{uuid.uuid4().hex[:16]}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": _RECURSION_LIMIT}
    context = GraphContext(session=session, scope=scope)
    t0 = time.perf_counter()
    async with rt.lock(thread_id):
        snap = await rt.graph.aget_state(config)
        resumed = bool(snap.interrupts)
        graph_input = (Command(resume=question) if resumed else
                       fresh_turn_state(question, session_id=getattr(session, "session_id", "") or "",
                                        thread_id=thread_id))
        final: dict = {}
        try:
            async for event in rt.graph.astream(graph_input, config, context=context,
                                                stream_mode="values",
                                                durability=settings.PIPELINE_GRAPH_DURABILITY):
                final = event
        except Exception as e:
            if final.get("done") and final.get("response") is not None:
                # The turn finished and the context was updated; only the
                # final checkpoint write failed. The answer stands.
                logger.warning("pipeline_graph: final checkpoint write failed (%s) — answer kept", e)
                await _drop_thread(rt, thread_id)
                return final["response"]
            await _drop_thread(rt, thread_id)
            _log("pipeline_graph_run", outcome="error", error=type(e).__name__, resumed=resumed,
                 thread=_thread_tag(thread_id), ms=round((time.perf_counter() - t0) * 1000, 1),
                 path=[x.get("node") for x in final.get("trace") or []])
            raise
        snap = await rt.graph.aget_state(config)
        path = [x.get("node") for x in (snap.values.get("trace") or final.get("trace") or [])]
        if snap.interrupts:
            p = snap.interrupts[0].value or {}
            _log("pipeline_graph_run", outcome="pause", rule=p.get("rule"), node=p.get("node"),
                 resumed=resumed, thread=_thread_tag(thread_id), path=path,
                 ms=round((time.perf_counter() - t0) * 1000, 1))
            raise pipeline.ClarificationNeeded(p.get("question") or "", options=p.get("options") or [],
                                               rule=p.get("rule"), village_hint=p.get("village_hint"))
        result = final.get("response")
        await _drop_thread(rt, thread_id)
        _log("pipeline_graph_run", outcome="answer", route=(result or {}).get("route"), resumed=resumed,
             pause_outcome=final.get("pause_outcome"), repairs=final.get("sql_attempt") or 0,
             thread=_thread_tag(thread_id), path=path, ms=round((time.perf_counter() - t0) * 1000, 1))
        if result is None:   # unreachable: every path ends in a response or a pause
            raise RuntimeError("pipeline graph finished without a response")
        return result


# ── Expired pauses ──────────────────────────────────────────────────────────

async def sweep_expired_pauses(now: "float | None" = None) -> int:
    """Delete threads whose last checkpoint is older than the pause TTL (an
    unanswered pause, or a run killed mid-way). Returns how many."""
    from datetime import datetime, timezone
    rt = await _runtime()
    cutoff = (now if now is not None else time.time()) - settings.PIPELINE_GRAPH_PAUSE_TTL_SECONDS
    latest: dict[str, float] = {}
    async for item in rt.saver.alist(None):
        tid = item.config["configurable"]["thread_id"]
        try:
            dt = datetime.fromisoformat(item.checkpoint["ts"])
        except Exception:  # noqa: BLE001
            continue
        ts = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        latest[tid] = max(latest.get(tid, 0.0), ts)
    expired = [tid for tid, ts in latest.items() if ts < cutoff]
    for tid in expired:
        try:
            await rt.saver.adelete_thread(tid)
        except Exception:  # noqa: BLE001
            logger.warning("pipeline_graph: sweep could not delete %s", _thread_tag(tid), exc_info=True)
    if expired:
        logger.info("pipeline_graph: swept %d expired paused thread(s)", len(expired))
    return len(expired)


async def sweep_loop() -> None:
    """Background task started by app.main when the graph is in use."""
    while True:
        await asyncio.sleep(max(60, settings.PIPELINE_GRAPH_SWEEP_EVERY_SECONDS))
        try:
            await sweep_expired_pauses()
        except Exception:  # noqa: BLE001
            logger.warning("pipeline_graph: pause sweep failed", exc_info=True)
