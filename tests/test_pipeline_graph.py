"""
The LangGraph orchestrator (app/pipeline_graph.py, D-032).

The graph runs the same pipeline stages as the sequential path, so the main
check here is EQUIVALENCE: whole conversations are driven through both
orchestrators with identical stubs (the 4B / 30B / 9B models, RAG and megh_db
are stubbed; every pipeline stage, gate, guard and context function is real),
and every result, every pause and the session state after each turn must be
identical. On top of that: graph structure, the explicit repair loop and its
bound, pause -> checkpoint -> resume, abandoning a pause, the checkpoint not
being needed for correctness, serialization, the canary, and PII-free logs.

Nothing here needs the VPN.
    .venv/Scripts/python.exe -m pytest tests/test_pipeline_graph.py -q
"""
import asyncio
import dataclasses
import datetime
import inspect
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, context_manager as cm, entity_resolver, llm, pipeline as p, pipeline_graph as pg, rag  # noqa: E402
from app.config import settings  # noqa: E402
from app.routers.query import build_turn, pause_question, remember_pause  # noqa: E402
from app.session_store import Session  # noqa: E402

CME = "How many applications under CM Elevate in East Khasi Hills?"
SQL_OK = ("SELECT SUM(no_of_applications) AS applications FROM curated.v_cm_elevate "
          "WHERE lgd_district = 'EAST KHASI HILLS'")
_DISTRICTS = ("East Khasi Hills", "West Khasi Hills", "West Garo Hills", "Ri Bhoi")


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


class Calls:
    """Counts of every stubbed model / DB call, to prove the graph spends no
    extra calls (a resume must not repeat expensive work)."""

    def __init__(self):
        self.n: dict[str, int] = {}

    def hit(self, k):
        self.n[k] = self.n.get(k, 0) + 1


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The stubs. `env.run_rows` is a list of results / exceptions run_readonly
    returns in turn (the last one repeats)."""
    calls = Calls()

    class Env:
        pass
    e = Env()
    e.calls = calls
    e.run_rows = [[{"applications": 42}]]
    e.repair_sql = SQL_OK

    async def classify_intent(q):
        calls.hit("intent")
        return "KNOWLEDGE" if q.lower().startswith(("what is", "tell me about")) else "DATA"

    async def classifier(prompt, **_k):
        calls.hit("classifier")
        return '{"intent": "DATA"}'

    async def gen(question, schemes, er):
        calls.hit("generate_sql")
        res = er.get("resolved") or {}
        sql = SQL_OK.replace("EAST KHASI HILLS", res.get("district") or "EAST KHASI HILLS")
        if schemes == ["MGNREGA"]:
            sql = sql.replace("SUM(no_of_applications) AS applications", "SUM(person_days) AS applications")                      .replace("v_cm_elevate", "v_employment")
        # the resolved year is a mandatory filter (_resolved_scope_missing)
        return sql + (f" AND year_key = {res['year_key']}" if res.get("year_key") else "")

    async def run_readonly(sql):
        calls.hit("run_readonly")
        nxt = e.run_rows[0] if len(e.run_rows) == 1 else e.run_rows.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return [dict(r) for r in nxt]

    async def fetch_rows(sql, params=None):
        calls.hit("fetch_rows")
        return []

    async def sql_generator(prompt, **_k):
        calls.hit("repair")
        return e.repair_sql

    async def verify(*_a):
        calls.hit("verify")
        return None

    async def compose(question, sql, rows, **_k):
        calls.hit("compose")
        return f"There were {rows[0].get('applications') if rows else 0} applications." if rows else "none"

    async def rewrite(question, prev, **_k):
        """Like the live rewrite: the previous question with the new place / scheme."""
        calls.hit("rewrite")
        base = prev.question
        for d in _DISTRICTS:
            if d.lower() in question.lower():
                for old in _DISTRICTS:
                    base = base.replace(old, d)
                return base
        if "mgnrega" in question.lower():
            return base.replace("applications under CM Elevate", "MGNREGA person-days")
        return base

    async def kb(question, scheme=None, **_k):
        calls.hit("kb")
        return {"answer": f"About {scheme or 'the schemes'}.", "confidence": "medium", "sources": []}

    async def kb_multi(question, schemes=None, **_k):
        calls.hit("kb")
        return {"answer": "About several schemes.", "confidence": "medium", "sources": []}

    async def no_ctx(*_a, **_k):
        return ""

    async def villages(*_a, **_k):
        return {}

    monkeypatch.setattr(p, "classify_intent", classify_intent)
    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(llm, "call_sql_generator", sql_generator)
    monkeypatch.setattr(p, "generate_sql", gen)
    monkeypatch.setattr(p, "run_readonly", run_readonly)
    monkeypatch.setattr(p, "fetch_rows", fetch_rows)
    monkeypatch.setattr(p, "_verify_sql", verify)
    monkeypatch.setattr(p, "compose_response", compose)
    monkeypatch.setattr(p, "rewrite_followup", rewrite)
    monkeypatch.setattr(p, "village_names_exact", villages)
    monkeypatch.setattr(entity_resolver, "fetch_rows", fetch_rows)
    monkeypatch.setattr(rag, "answer_from_kb", kb)
    monkeypatch.setattr(rag, "answer_from_kb_multi", kb_multi)
    monkeypatch.setattr(cm, "build_followup_context", no_ctx)
    monkeypatch.setattr(settings, "CONTEXT_SEMANTIC_MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "CONTEXT_SUMMARY_ENABLED", False)
    monkeypatch.setattr(settings, "LANGGRAPH_CHECKPOINT_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_CHECKPOINTER_DSN", "")
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_KEEP_THREADS", False)
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_CANARY_PERCENT", 0)
    monkeypatch.setattr(pg, "_rt", None)
    return e


def _session(sid="t-1"):
    return Session(session_id=sid, user_id="u", created=time.time(), last_seen=time.time())


def converse(turns, *, graph: bool, monkeypatch, scope=None, between=None, sid="t-1"):
    """Drive `turns` through answer_question the way routers.query does
    (build_turn after an answer; remember_pause after a pause), in one event
    loop. Returns one outcome per turn and the session.
    `between(i, session)` runs before turn i (async), e.g. to drop a thread."""
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_ENABLED", graph)
    session = _session(sid)
    outcomes = []

    async def main():
        for i, q in enumerate(turns):
            if between is not None:
                await between(i, session)
            session.turn_context = {}
            try:
                r = await p.answer_question(q, session=session, scope=scope)
                outcomes.append(("answer", r))
                session.turns.append(build_turn(q, r))
            except p.ClarificationNeeded as e:
                outcomes.append(("pause", {"question": e.question, "options": e.options,
                                           "rule": e.rule, "village_hint": e.village_hint}))
                remember_pause(session, pause_question(session, q), e)
            except Exception as e:  # noqa: BLE001 — compared by type and text
                outcomes.append(("error", (type(e).__name__, str(e))))
            outcomes[-1] += (session.state.to_dict(), session.pending_scope_q, session.pending_scope_rule)
        await pg.close()
    asyncio.run(main())
    return outcomes, session


def both(env, monkeypatch, turns, **kw):
    """Run the conversation on both orchestrators; assert identical outcomes and
    identical model / DB call counts. Returns the graph run's outcomes."""
    rows0 = list(env.run_rows)
    # Warm-up: the first run of a process fills module-level caches (block /
    # village name lists, scheme years). Without it the second orchestrator
    # would look cheaper only because it ran second.
    converse(turns, graph=False, monkeypatch=monkeypatch, **kw)
    env.run_rows = list(rows0)
    env.calls.n.clear()
    seq, _ = converse(turns, graph=False, monkeypatch=monkeypatch, **kw)
    seq_calls = dict(env.calls.n)
    env.calls.n.clear()
    env.run_rows = list(rows0)
    gra, _ = converse(turns, graph=True, monkeypatch=monkeypatch, **kw)
    assert gra == seq
    assert env.calls.n == seq_calls, (env.calls.n, seq_calls)
    return gra


# ── Structure ────────────────────────────────────────────────────────────────

def test_graph_compiles_with_the_expected_nodes():
    g = pg.build_graph().compile()
    nodes = set(g.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == set(pg.NODES)
    assert len(pg.NODES) == 21
    # nodes the guide names but this codebase has no logic for are NOT invented
    assert not nodes & {"query_analyzer", "schema_retrieve", "findings", "chart"}


def test_graph_edges():
    g = pg.build_graph().compile().get_graph()
    edges = {(e.source, e.target) for e in g.edges}
    for e in [("__start__", "context"), ("context", "edge"), ("edge", "followup"), ("followup", "route"),
              ("route", "knowledge"), ("route", "data_context"), ("data_context", "scope_scheme"),
              ("sql_generate", "authorize"), ("authorize", "execute_attempt"),
              ("execute_attempt", "repair"), ("repair", "execute_attempt"),
              ("execute_attempt", "post_rows"), ("assemble", "finalize"),
              ("pause_gate", "context"), ("finalize", "context_update"), ("context_update", "__end__")]:
        assert e in edges, e
    # every stage node can pause and can finish early
    for n in ("context", "followup", "route", "knowledge", "scope_scheme", "entities",
              "clarification_gate", "deterministic"):
        assert (n, "pause_gate") in edges and (n, "finalize") in edges, n


def test_pause_gate_does_no_expensive_work():
    # LangGraph replays a paused node in full on resume: the gate may hold
    # interrupt() and nothing that calls a model or the DB.
    src = inspect.getsource(pg.node_pause_gate)
    assert "interrupt(" in src and "await" not in src
    for n in ("node_entities", "node_scope_scheme", "node_clarification_gate"):
        assert "interrupt(" not in inspect.getsource(getattr(pg, n).stage_fn)


def test_nodes_are_thin_wrappers_over_pipeline_stages():
    for name, stage in [("node_context", "_turn_context_stage"), ("node_followup", "_turn_followup_stage"),
                        ("node_entities", "_data_entity_stage"), ("node_compose", "_data_compose_stage"),
                        ("node_guarantees", "_data_guarantees_stage"), ("node_repair", "_repair_sql")]:
        assert f"pipeline.{stage}" in inspect.getsource(getattr(pg, name).stage_fn), name


def test_sequential_and_graph_share_the_repair_budget():
    assert inspect.signature(p.execute_with_repair).parameters["max_repairs"].default == p.MAX_SQL_REPAIRS
    assert "pipeline.MAX_SQL_REPAIRS" in inspect.getsource(pg.node_execute_attempt.stage_fn)


# ── Equivalence: whole conversations ─────────────────────────────────────────

def test_data_answer_is_identical(env, monkeypatch):
    out = both(env, monkeypatch, [CME])
    assert out[0][0] == "answer" and out[0][1]["answer"] == "There were 42 applications."
    assert out[0][1]["route"] == "data" and out[0][2]["scheme"] == "CM Elevate"


def test_followup_changes_only_the_district(env, monkeypatch):
    out = both(env, monkeypatch, [CME, "What about West Khasi Hills?"])
    r = out[1][1]
    assert r["resolved_entities"].get("district") == "WEST KHASI HILLS"
    assert "WEST KHASI HILLS" in r["sql"]
    assert out[1][2]["scheme"] == "CM Elevate" and out[1][2]["district"] == "WEST KHASI HILLS"


def test_scheme_switch_does_not_keep_the_old_scheme(env, monkeypatch):
    # "What about MGNREGA?" switches the scheme; MGNREGA (unlike CM Elevate)
    # needs a financial year, so it asks, and the reply finishes the switch.
    out = both(env, monkeypatch, [CME, "What about MGNREGA?", "2023-24"])
    assert out[1][0] == "pause" and out[1][1]["rule"] == "year-not-specified"
    assert "MGNREGA" in out[1][1]["question"] and "CM Elevate" not in out[1][1]["question"]
    assert out[2][0] == "answer" and out[2][1]["schemes"] == ["MGNREGA"]
    assert out[2][2]["scheme"] == "MGNREGA"


def test_pause_then_reply_resumes(env, monkeypatch):
    out = both(env, monkeypatch, ["How many applications under CM Elevate?", "East Khasi Hills"])
    assert out[0][0] == "pause" and out[0][1]["rule"]
    assert out[1][0] == "answer" and out[1][1]["resolved_entities"].get("district") == "EAST KHASI HILLS"


def test_thanks_abandons_a_pause(env, monkeypatch):
    out = both(env, monkeypatch, ["How many applications under CM Elevate?", "thanks"])
    assert out[0][0] == "pause"
    assert out[1][0] == "answer" and out[1][1]["route"] == "edge"
    assert out[1][3] is None             # the pending pause is consumed


def test_new_question_abandons_a_pause(env, monkeypatch):
    out = both(env, monkeypatch, ["How many applications under CM Elevate?", "What is CM Elevate?"])
    assert out[1][0] == "answer" and out[1][1]["route"] == "knowledge"


def test_knowledge_digression_keeps_the_data_context(env, monkeypatch):
    out = both(env, monkeypatch, [CME, "What is CM Elevate?", "What about West Khasi Hills?"])
    assert out[1][1]["route"] == "knowledge"
    assert out[2][2]["district"] == "WEST KHASI HILLS"


def test_repair_loop_is_identical(env, monkeypatch):
    env.run_rows = [RuntimeError('column "applicationz" does not exist'), [{"applications": 7}]]
    out = both(env, monkeypatch, [CME])
    assert out[0][1]["answer"] == "There were 7 applications."
    assert env.calls.n["repair"] == 1 and env.calls.n["run_readonly"] == 2


def test_repair_budget_is_bounded_and_falls_back_identically(env, monkeypatch):
    env.run_rows = [RuntimeError("syntax error at or near FROM")]
    out = both(env, monkeypatch, [CME])
    assert env.calls.n["run_readonly"] == p.MAX_SQL_REPAIRS + 1
    assert env.calls.n["repair"] == p.MAX_SQL_REPAIRS
    assert out[0][1]["route"] in ("knowledge", "data")    # the KB fallback, as before


def test_database_outage_raises_identically(env, monkeypatch):
    env.run_rows = [ConnectionRefusedError("connection refused")]
    out = both(env, monkeypatch, [CME])
    assert out[0][0] == "error" and out[0][1][0] == "DatabaseUnavailableError"
    assert env.calls.n.get("repair", 0) == 0     # never repaired (KI-025)


def test_denial_is_identical(env, monkeypatch):
    scope = auth.UserScope(user_id="o", role="district_officer", granularity_cap="village",
                           schemes=["MGNREGA"], geographies="all")
    out = both(env, monkeypatch, [CME], scope=scope)
    assert out[0][1]["route"] == "denied" and out[0][1]["denied_by"] == "scheme"
    assert "run_readonly" not in env.calls.n


def test_edge_and_greeting_are_identical(env, monkeypatch):
    out = both(env, monkeypatch, ["hello", "who is harshit"])
    assert all(o[1]["route"] == "edge" for o in out)


# ── Graph-only behaviour ─────────────────────────────────────────────────────

def test_pause_is_checkpointed_and_the_thread_dropped_after_the_answer(env, monkeypatch):
    seen = {}

    async def between(i, session):
        seen[i] = await pg.pending_pause(session.session_id)
    out, _ = converse(["How many applications under CM Elevate?", "East Khasi Hills", CME],
                      graph=True, monkeypatch=monkeypatch, between=between)
    assert seen[0] is None
    assert seen[1] and seen[1]["rule"] == out[0][1]["rule"] and seen[1]["node"]
    assert seen[2] is None                 # finished runs leave no thread behind


def test_reply_resumes_even_when_the_checkpoint_is_gone(env, monkeypatch):
    # Another host (SQLite is per host), an expired thread: the Session's own
    # pending pause still resumes it, exactly like the sequential path.
    async def drop(i, session):
        if i == 1:
            rt = await pg._runtime()
            await rt.saver.adelete_thread(session.session_id)
    seq, _ = converse(["How many applications under CM Elevate?", "East Khasi Hills"],
                      graph=False, monkeypatch=monkeypatch)
    gra, _ = converse(["How many applications under CM Elevate?", "East Khasi Hills"],
                      graph=True, monkeypatch=monkeypatch, between=drop)
    assert gra == seq and gra[1][0] == "answer"


def test_every_node_checkpoint_serializes(env, monkeypatch):
    # durability="sync" writes the state after EVERY node: proves each
    # intermediate state (rows with Decimal / date included) is serializable.
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_DURABILITY", "sync")
    env.run_rows = [[{"applications": Decimal("42"), "as_of": datetime.date(2025, 3, 31)}]]
    out = both(env, monkeypatch, [CME, "What about West Khasi Hills?"])
    assert out[0][0] == "answer"


def test_state_holds_plain_data_only():
    st = pg.fresh_turn_state("q", session_id="s", thread_id="s")
    ser = pg._serde()
    assert ser.loads_typed(ser.dumps_typed({k: v for k, v in st.items() if k != "trace"}))["question"] == "q"
    hints = pg.MeghalayaGraphState.__annotations__
    assert "session" not in hints and "scope" not in hints      # runtime context, never checkpointed


def test_turn_objects_round_trip():
    from app.context_policy import MergePlan
    from app.session_store import ConversationState, Turn
    s = _session()
    t = {**p._new_turn("q"), "prev": Turn(question="a", raw_question="a", route="data", schemes=["MGNREGA"]),
         "plan": MergePlan(actions={"district": "REPLACE"}, reasons={"district": "named"}, kind="filter_change"),
         "thread_state": ConversationState(scheme="PMAY-G", year=2023), "paused_state": None}
    back = pg._hydrate_turn(pg._dehydrate_turn(t, s), s)
    assert back["prev"] == t["prev"] and back["plan"] == t["plan"]
    assert back["thread_state"].to_dict() == t["thread_state"].to_dict()
    t["thread_state"] = s.state                     # the session's own state keeps its identity
    assert pg._hydrate_turn(pg._dehydrate_turn(t, s), s)["thread_state"] is s.state


def test_run_trace_and_logs_carry_no_question_text(env, monkeypatch, caplog):
    secret = "How many applications under CM Elevate in Ri Bhoi?"
    with caplog.at_level(logging.INFO, logger="app.pipeline_graph"):
        out, _ = converse([secret], graph=True, monkeypatch=monkeypatch)
    graph_logs = [r.getMessage() for r in caplog.records if r.name == "app.pipeline_graph"]
    nodes = [m for m in graph_logs if m.startswith("pipeline_graph_node ")]
    runs = [m for m in graph_logs if m.startswith("pipeline_graph_run ")]
    assert nodes and runs
    assert all("Ri Bhoi" not in m and "RI BHOI" not in m for m in graph_logs)
    assert '"path": ["context", "edge", "followup", "route", "data_context"' in runs[-1]


def test_canary_is_sticky_per_session(monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_ENABLED", False)
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_CANARY_PERCENT", 50)
    picks = {sid: p._pipeline_graph_selected(_session(sid)) for sid in (f"s{i}" for i in range(200))}
    assert 40 < sum(picks.values()) < 160
    assert all(p._pipeline_graph_selected(_session(sid)) == v for sid, v in picks.items())
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_CANARY_PERCENT", 0)
    assert not any(p._pipeline_graph_selected(_session(sid)) for sid in picks)
    assert not p._pipeline_graph_selected(None)


def test_default_is_the_sequential_path():
    # the declared defaults, not the live values (the flag-on sweep sets them via the env)
    fields = type(settings).model_fields
    assert fields["PIPELINE_GRAPH_ENABLED"].default is False
    assert fields["PIPELINE_GRAPH_CANARY_PERCENT"].default == 0


def test_expired_pause_threads_are_swept(env, monkeypatch):
    async def main():
        monkeypatch.setattr(settings, "PIPELINE_GRAPH_ENABLED", True)
        s = _session("t-sweep")
        with pytest.raises(p.ClarificationNeeded):
            await p.answer_question("How many applications under CM Elevate?", session=s)
        assert await pg.pending_pause("t-sweep")
        assert await pg.sweep_expired_pauses(now=time.time()) == 0
        assert await pg.sweep_expired_pauses(now=time.time() + settings.PIPELINE_GRAPH_PAUSE_TTL_SECONDS + 5) == 1
        assert await pg.pending_pause("t-sweep") is None
        await pg.close()
    asyncio.run(main())


def test_dataclass_shapes_the_graph_relies_on():
    # if one of these grows a non-dataclass field the (de)hydration must follow
    from app.context_policy import MergePlan
    from app.premise_check import StatedAmount
    from app.session_store import Turn
    assert {f.name for f in dataclasses.fields(MergePlan)} == {"actions", "reasons", "kind"}
    assert dataclasses.is_dataclass(StatedAmount) and dataclasses.is_dataclass(Turn)


# ── Pauses raised by other nodes (2026-10-02, node coverage) ─────────────────
@pytest.mark.parametrize("question,node", [
    # entities: a district acronym with its letters reordered (KI-130 rule)
    ("Compare CM Elevate applications in WHK and EKH", "entities"),
    # scope_scheme: a bare "Focus" names neither Focus scheme (D-009)
    ("How many beneficiaries under Focus in East Khasi Hills?", "scope_scheme"),
    # route: a bank question that names no scheme asks which one (_bank_clarification)
    ("What is the bank-wise disbursement?", "route"),
])
def test_pauses_from_other_nodes_are_identical(env, monkeypatch, question, node):
    out = both(env, monkeypatch, [question])
    assert out[0][0] == "pause", out[0]
    seen = {}

    async def between(i, session):
        seen[i] = await pg.pending_pause(session.session_id)
    converse([question, "thanks"], graph=True, monkeypatch=monkeypatch, between=between)
    assert seen[1]["node"] == node and seen[1]["rule"] == out[0][1]["rule"]


# ── KI-183: a repaired query is re-authorized on both paths ──────────────────
def test_repaired_sql_outside_the_scope_is_denied_identically(env, monkeypatch):
    # an East Khasi Hills officer: the first SQL (EKH) is allowed; the repair
    # rewrites it to West Khasi Hills, which must be denied BEFORE it runs
    scope = auth.UserScope(user_id="o", role="district_officer", granularity_cap="village",
                           schemes=["CM Elevate"], geographies={"districts": ["EAST KHASI HILLS"], "blocks": []})
    env.run_rows = [RuntimeError('column "applicationz" does not exist'), [{"applications": 7}]]
    env.repair_sql = SQL_OK.replace("EAST KHASI HILLS", "WEST KHASI HILLS")
    out = both(env, monkeypatch, [CME], scope=scope)
    assert out[0][1]["route"] == "denied" and out[0][1]["denied_by"] == "geography"
    assert env.calls.n["run_readonly"] == 1          # only the first, allowed query ran
    assert env.calls.n["repair"] == 1


def test_repaired_sql_inside_the_scope_still_runs(env, monkeypatch):
    scope = auth.UserScope(user_id="o", role="district_officer", granularity_cap="village",
                           schemes=["CM Elevate"], geographies={"districts": ["EAST KHASI HILLS"], "blocks": []})
    env.run_rows = [RuntimeError('column "applicationz" does not exist'), [{"applications": 7}]]
    out = both(env, monkeypatch, [CME], scope=scope)
    assert out[0][1]["route"] == "data" and out[0][1]["answer"] == "There were 7 applications."


# ── KI-184/185: the shared Postgres checkpointer lives in app.* ──────────────
@pytest.mark.parametrize("dsn,expected_tail", [
    ("postgresql://u:p@h:5432/db", "?options=-csearch_path%3Dapp"),
    ("postgresql://u:p@h/db?sslmode=require", "?sslmode=require&options=-csearch_path%3Dapp"),
])
def test_postgres_checkpoints_are_pinned_to_the_app_schema(dsn, expected_tail):
    # megh_db's public schema already holds another service's checkpoint tables
    # (1,892 threads, 2026-10-02): ours must never share them
    assert pg._postgres_conninfo(dsn).endswith(expected_tail)


def test_postgres_keyword_reuses_the_app_database_url(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/megh_db")
    assert pg._postgres_conninfo("postgres") == "postgresql://u:p@h:5432/megh_db?options=-csearch_path%3Dapp"


def test_postgres_on_a_windows_proactor_loop_falls_back_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PIPELINE_GRAPH_CHECKPOINTER_DSN", "postgres")
    monkeypatch.setattr(settings, "LANGGRAPH_CHECKPOINT_DIR", str(tmp_path))

    class ProactorEventLoop:   # only the class name is checked
        pass
    monkeypatch.setattr(pg.asyncio, "get_running_loop", lambda: ProactorEventLoop())

    async def main():
        saver, closer, backend = await pg._open_saver()
        await closer()
        return backend
    assert asyncio.run(main()) == "sqlite"
