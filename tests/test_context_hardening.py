"""
Context layer, second pass (2026-09-26): production hardening. OFFLINE.

  KI-028  conversation state shared across workers: Postgres
          (app.conversations.context_state) is the source of truth via
          app/session_sync.py. Simulated here with three independent
          SessionStores (one per "worker") over one shared fake backend that
          applies the same revision rule as conversation_store.save_session_state.
  merge   field-level KEEP / REPLACE / CLEAR / REQUIRE_CLARIFICATION
          (app/context_policy.plan_state_merge), and what it removes from the
          resolve_entities fallback.
  kinds   follow-up kind -> context layers.
  prov    provenance per committed field; model_inference is not inherited.
  KI-029  field-specific rewrite checks: year, metric, category, names
          (villages) as well as scheme/district/block.
  budget  priority-aware SQL prompt compression; required sections untouched.
  llm     per-call usage/latency record.

Nothing here reaches the model gateway or the DB. The live counterpart is
tests/live_context_validation.py.
    .venv/Scripts/python.exe -m pytest tests/test_context_hardening.py -q
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import context_budget, context_manager as cm, context_policy as cp  # noqa: E402
from app import entity_resolver, llm, pipeline as p, prompt_builder, session_sync  # noqa: E402
from app.routers import query as router  # noqa: E402
from app.session_store import ConversationState, Session, SessionStore, Turn  # noqa: E402

Q1 = "How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?"
R1 = {"route": "data", "intent": "DATA", "schemes": ["Focus Plus"], "sql": "SELECT 1",
      "resolved_entities": {"district": "WEST GARO HILLS", "year_key": 2024},
      "rows": [{"amount_raw": 1234567}], "answer": "Focus Plus disbursed Rs 12,34,567."}


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    from app import annotations
    entity_resolver.load_all()
    annotations.load_all()          # few-shot banks, as main.lifespan loads them


@pytest.fixture(autouse=True)
def _shared_on(monkeypatch):
    monkeypatch.setattr(p.settings, "CONTEXT_STATE_SHARED", True)


class FakeBackend:
    """Stands in for app.conversations. JSON round-trips every value like
    JSONB, and applies save_session_state's revision guard."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail = False

    async def load(self, session_id):
        if self.fail:
            raise ConnectionError("db down")
        row = self.rows.get(session_id)
        return {} if row is None else {"context_state": json.loads(row["state"]),
                                       "summary": row["summary"]}

    async def save(self, *, tenant_id, user_id, session_id, title, snapshot, rev, summary):
        if self.fail:
            raise ConnectionError("db down")
        cur = self.rows.get(session_id)
        if cur and int(json.loads(cur["state"]).get("rev") or 0) >= rev:
            return False
        self.rows[session_id] = {"state": json.dumps({**snapshot, "rev": rev}, default=str),
                                 "summary": summary if summary is not None
                                 else (cur or {}).get("summary")}
        return True


def _worker():
    return SessionStore(ttl=1800, max_sessions=100, max_turns=8)


def _answered(sess, question, result):
    """What the router does after answer_question: record the turn, update state."""
    sess.turns.append(router.build_turn(question, result))
    cm.update_state(sess, question, result.get("rewritten_question") or question, result)


def _run(coro):
    return asyncio.run(coro)


# ── KI-028: worker A -> B -> C ──────────────────────────────────────────────
def test_state_crosses_workers_a_b_c(monkeypatch):
    be, sid = FakeBackend(), "chat-1"
    wa, wb, wc = _worker(), _worker(), _worker()

    a = wa.ensure(sid, "u")
    assert _run(session_sync.sync_in(a, sid, backend=be)) == "none"
    _answered(a, Q1, R1)
    assert _run(session_sync.sync_out(a, sid, tenant_id=1, user_id=2, backend=be)) is True
    assert a.rev == 1

    # Worker B has never seen this session: it reads A's turn and state...
    b = wb.ensure(sid, "u")
    assert _run(session_sync.sync_in(b, sid, backend=be)) == "applied"
    assert b.last_turn.question == Q1 and b.last_turn.route == "data"
    assert b.state.district == "WEST GARO HILLS" and b.state.year == 2024

    # ...and answers the follow-up against it, through the real pipeline.
    seen = {}

    async def classifier(prompt, **_kw):
        if "Standalone question:" in prompt:
            return "How much was disbursed under Focus Plus in Dalu block of West Garo Hills in FY 2024-25?"
        return '{"intent": "DATA"}'

    async def fake_answer_data(question, **kw):
        seen["q"], seen["prior"] = question, kw.get("prior_resolved")
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["Focus Plus"],
                "sql": "SELECT 2", "rows": [{"amount_raw": 5}], "data": [],
                "resolved_entities": {"district": "WEST GARO HILLS", "block": "DALU",
                                      "year_key": 2024}}

    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(p, "_answer_data", fake_answer_data)
    r2 = _run(p._run_pipeline("What about Dalu block?", session=b))
    assert "Dalu" in seen["q"] and "West Garo Hills" in seen["q"]
    assert seen["prior"]["district"] == "WEST GARO HILLS" and seen["prior"]["year_key"] == 2024
    _answered(b, "What about Dalu block?", r2)
    assert _run(session_sync.sync_out(b, sid, tenant_id=1, user_id=2, backend=be)) is True

    # Worker C reads B's update, and worker A's stale copy is refreshed too.
    c = wc.ensure(sid, "u")
    assert _run(session_sync.sync_in(c, sid, backend=be)) == "applied"
    assert c.state.block == "DALU" and c.rev == 2
    assert "Dalu" in c.last_turn.question
    assert _run(session_sync.sync_in(a, sid, backend=be)) == "applied"
    assert a.state.block == "DALU" and a.rev == 2


def test_current_copy_is_not_reloaded():
    be, sid = FakeBackend(), "chat-2"
    a = _worker().ensure(sid, "u")
    _answered(a, Q1, R1)
    _run(session_sync.sync_out(a, sid, tenant_id=1, user_id=2, backend=be))
    assert _run(session_sync.sync_in(a, sid, backend=be)) == "current"


def test_stale_concurrent_write_is_refused_not_last_writer_wins():
    be, sid = FakeBackend(), "chat-3"
    a = _worker().ensure(sid, "u")
    _answered(a, Q1, R1)
    _run(session_sync.sync_out(a, sid, tenant_id=1, user_id=2, backend=be))
    b = _worker().ensure(sid, "u")
    c = _worker().ensure(sid, "u")
    _run(session_sync.sync_in(b, sid, backend=be))
    _run(session_sync.sync_in(c, sid, backend=be))
    b.state.metric, c.state.metric = "from-b", "from-c"
    assert _run(session_sync.sync_out(b, sid, tenant_id=1, user_id=2, backend=be)) is True
    assert _run(session_sync.sync_out(c, sid, tenant_id=1, user_id=2, backend=be)) is False
    assert json.loads(be.rows[sid]["state"])["metric"] == "from-b"


def test_db_down_degrades_to_the_local_copy():
    be, sid = FakeBackend(), "chat-4"
    a = _worker().ensure(sid, "u")
    _answered(a, Q1, R1)
    be.fail = True
    assert _run(session_sync.sync_in(a, sid, backend=be)) == "unavailable"
    assert a.last_turn.question == Q1                     # untouched
    assert _run(session_sync.sync_out(a, sid, tenant_id=1, user_id=2, backend=be)) is False


def test_slow_db_is_bounded(monkeypatch):
    class Slow(FakeBackend):
        async def load(self, session_id):
            await asyncio.sleep(5)
    monkeypatch.setattr(p.settings, "CONTEXT_STATE_SYNC_TIMEOUT_SECONDS", 0.05)
    t = time.perf_counter()
    assert _run(session_sync.sync_in(_worker().ensure("s", "u"), "s", backend=Slow())) == "unavailable"
    assert time.perf_counter() - t < 1.0


def test_pending_clarification_crosses_workers(monkeypatch):
    """KI-001 in the same fix: the scheme pause raised on A is resumed on B."""
    be, sid = FakeBackend(), "chat-5"
    a = _worker().ensure(sid, "u")
    router.remember_pause(a, "Show beneficiaries", p._scheme_clarification("Show beneficiaries"))
    _run(session_sync.sync_out(a, sid, tenant_id=1, user_id=2, backend=be))

    b = _worker().ensure(sid, "u")
    _run(session_sync.sync_in(b, sid, backend=be))
    assert b.pending_scope_q == "Show beneficiaries" and b.pending_scope_options
    assert router.is_cacheable("focus plus please", b) is False
    seen = {}

    async def fake_answer_data(question, **kw):
        seen["q"] = question
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["Focus Plus"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}

    async def classifier(prompt, **_kw):
        return '{"intent": "DATA"}'

    monkeypatch.setattr(p, "_answer_data", fake_answer_data)
    monkeypatch.setattr(llm, "call_classifier", classifier)
    _run(p._run_pipeline("Focus Plus", session=b))
    assert seen["q"] == "Show beneficiaries for Focus Plus"
    assert b.turn_context["clarification_reply"] == "Focus Plus"


def test_old_durable_records_still_load():
    s = Session(session_id="x", user_id=None, created=0.0, last_seen=0.0)
    s.apply_snapshot({"scheme": "MGNREGA", "district": "RI BHOI", "turn_count": 3}, "old summary")
    assert s.state.scheme == "MGNREGA" and s.turns == [] and s.pending_scope_q is None
    assert s.summary == "old summary"


def test_summary_is_not_persisted_behind_the_versioned_write(monkeypatch):
    from app import conversation_store

    def boom(**_kw):
        raise AssertionError("maybe_update_summary must not write state itself")

    async def ok(prompt, **_kw):
        return "Focus Plus in West Garo Hills."

    monkeypatch.setattr(conversation_store, "save_context_state", boom)
    monkeypatch.setattr(llm, "call_classifier", ok)
    s = Session(session_id="x", user_id=None, created=0.0, last_seen=0.0)
    s.state.turn_count = 4
    s.turns = [Turn(question=Q1, raw_question=Q1, route="data")]
    _run(cm.maybe_update_summary(s))
    assert s.summary


def test_router_uses_shared_state_on_every_request():
    import inspect
    src = inspect.getsource(router.query)
    assert "await session_sync.sync_in(session, session_id)" in src
    assert src.count("await session_sync.sync_out(") == 2      # answered + clarification paths
    assert "if not session.turns and client_session" not in src


# ── Field-level merge actions ───────────────────────────────────────────────
STATE = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024,
                          metric="beneficiaries")
PREV = Turn(question="How many beneficiaries under Focus Plus in West Garo Hills in FY 2024-25?",
            raw_question="", route="data", schemes=["Focus Plus"],
            resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024})


@pytest.mark.parametrize("q,expected", [
    ("What about South Garo Hills?", {"scheme": "KEEP", "district": "REPLACE", "year": "KEEP", "metric": "KEEP"}),
    ("What about 2023-24?", {"scheme": "KEEP", "district": "KEEP", "year": "REPLACE", "metric": "KEEP"}),
    ("How many beneficiaries?", {"scheme": "KEEP", "district": "KEEP", "year": "KEEP", "metric": "REPLACE"}),
])
def test_merge_actions_from_the_spec(q, expected):
    plan = cp.plan_state_merge(q, STATE, prev=PREV)
    assert {k: plan.action(k) for k in expected} == expected


def test_ambiguous_reference_requires_clarification_and_the_pipeline_asks(monkeypatch):
    plan = cp.plan_state_merge("How about the other one?", STATE, prev=PREV)
    assert plan.action("reference") == cp.REQUIRE_CLARIFICATION
    s = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    s.state = ConversationState(**{**STATE.to_dict(), "comparison_entities": []})
    s.turns = [PREV]
    with pytest.raises(p.ClarificationNeeded) as e:
        _run(p._run_pipeline("How about the other one?", session=s))
    assert e.value.rule == "entity-ambiguous"


def test_coarser_change_clears_finer_geography_and_group_by_clears_the_filter():
    plan = cp.plan_state_merge("What about South Garo Hills?", STATE, prev=PREV)
    assert plan.action("block") == cp.CLEAR and plan.action("village") == cp.CLEAR
    plan = cp.plan_state_merge("Show it by district.", STATE, prev=PREV)
    assert plan.action("district") == cp.CLEAR and plan.kind == cp.GROUP_BY_CHANGE
    assert plan.action("year") == cp.KEEP                     # unrelated filters survive
    plan = cp.plan_state_merge("across all years", STATE, prev=PREV)
    assert plan.action("year") == cp.CLEAR


def test_relative_year_with_no_known_year_is_not_guessed():
    plan = cp.plan_state_merge("What about last year?", ConversationState(scheme="MGNREGA"),
                               prev=Turn(question="MGNREGA person-days", raw_question="",
                                         route="data", schemes=["MGNREGA"]))
    assert plan.action("year") == cp.REQUIRE_CLARIFICATION
    assert "year_key" not in cp.apply_merge_plan({"year_key": 2020}, plan)


def test_apply_merge_plan_only_removes():
    prior = {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024,
             "tranche_label": "Tranch 2 - August"}
    out = cp.apply_merge_plan(prior, cp.plan_state_merge("Show it by district.", STATE, prev=PREV))
    assert out == {"year_key": 2024, "tranche_label": "Tranch 2 - August"}
    assert cp.apply_merge_plan(prior, cp.plan_state_merge("How many beneficiaries?", STATE,
                                                          prev=PREV)) == prior


def _pipeline_prior(monkeypatch, prev_turn, followup, rewrite):
    seen = {}

    async def classifier(prompt, **_kw):
        return rewrite if "Standalone question:" in prompt else '{"intent": "DATA"}'

    async def fake_answer_data(question, **kw):
        seen["q"], seen["prior"] = question, kw.get("prior_resolved") or {}
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["Focus Plus"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}

    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(p, "_answer_data", fake_answer_data)
    s = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    s.turns = [prev_turn]
    s.state = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS",
                                block=prev_turn.resolved_entities.get("block"), year=2024)
    _run(p._run_pipeline(followup, session=s))
    return seen


def test_pipeline_drops_an_inherited_block_when_the_district_changes(monkeypatch):
    prev = Turn(question="How much was disbursed under Focus Plus in Dalu block in FY 2024-25?",
                raw_question="", route="data", schemes=["Focus Plus"],
                resolved_entities={"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024})
    seen = _pipeline_prior(monkeypatch, prev, "What about South Garo Hills?",
                           "How much was disbursed under Focus Plus in South Garo Hills in FY 2024-25?")
    assert "block" not in seen["prior"] and "district" not in seen["prior"]
    assert seen["prior"]["year_key"] == 2024


def test_pipeline_group_by_does_not_inherit_the_district_filter(monkeypatch):
    seen = _pipeline_prior(monkeypatch, Turn(question=Q1, raw_question=Q1, route="data",
                                             schemes=["Focus Plus"],
                                             resolved_entities={"district": "WEST GARO HILLS",
                                                                "year_key": 2024}),
                           "Show it by district.",
                           "Show Focus Plus disbursement in FY 2024-25 by district")
    assert "district" not in seen["prior"] and seen["prior"]["year_key"] == 2024


# ── Follow-up kinds -> context layers ──────────────────────────────────────
@pytest.mark.parametrize("q,kind", [
    ("What about South Garo Hills?", cp.GEOGRAPHY_CHANGE),
    ("What about Dalu block?", cp.GEOGRAPHY_CHANGE),
    ("What about 2023-24?", cp.TIME_CHANGE),
    ("How many beneficiaries?", cp.METRIC_CHANGE),
    ("Show it by district.", cp.GROUP_BY_CHANGE),
    ("compare it with East Garo Hills", cp.COMPARISON),
    ("What about the top one?", cp.RESULT_REFERENCE),
    ("How much was it?", cp.RESULT_REFERENCE),
    ("What about women?", cp.FILTER_CHANGE),
])
def test_followup_kinds(q, kind):
    assert cp.plan_state_merge(q, STATE, prev=PREV).kind == kind


def test_new_query_and_clarification_response_kinds():
    assert cp.plan_state_merge("x", STATE, is_followup=False).kind == cp.NEW_QUERY
    assert cp.plan_state_merge("Focus Plus", STATE, clarification_response=True).kind == \
        cp.CLARIFICATION_RESPONSE


def test_layers_follow_the_kind():
    prev = Turn(question=Q1, raw_question=Q1, route="data", schemes=["Focus Plus"],
                resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024},
                answer="long answer " * 200, result_summary="1 row: amount_raw=1234567")
    metric = cm.build_rewrite_evidence("How many beneficiaries?", prev, cp.METRIC_CHANGE)
    assert metric["tiers"] == ["filters"] and not metric["result"] and not metric["answer"]
    ref = cm.build_rewrite_evidence("How much was it?", prev, cp.RESULT_REFERENCE)
    assert ref["tiers"] == ["filters", "result"]
    assert cm.build_rewrite_evidence("x", prev, cp.NEW_QUERY)["tiers"] == []


# ── Provenance ─────────────────────────────────────────────────────────────
def _commit(sess, raw, standalone, resolved, *, reply=None, substituted=None, prev_q=""):
    before = sess.state.to_dict()
    sess.turn_context = {"state_before": {k: before.get(k) for k in
                                          ("scheme", "district", "block", "village", "year",
                                           "metric", "tranche")},
                         "previous_question": prev_q, "clarification_reply": reply,
                         "substituted_question": substituted}
    cm.update_state(sess, raw, standalone, {"route": "data", "schemes": ["Focus Plus"],
                                            "sql": "S", "resolved_entities": resolved})


def test_provenance_sources():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    _commit(s, Q1, Q1, {"district": "WEST GARO HILLS", "year_key": 2024})
    pv = s.state.provenance
    assert pv["district"]["source"] == "current_user" and pv["year"]["source"] == "current_user"
    assert pv["scheme"]["source"] == "current_user" and pv["district"]["confidence"] == 1.0

    q2 = "How much was disbursed under Focus Plus in Dalu block of West Garo Hills in FY 2024-25?"
    _commit(s, "What about Dalu block?", q2,
            {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024}, prev_q=Q1)
    pv = s.state.provenance
    assert pv["block"]["source"] == "current_user"
    assert pv["district"]["source"] == "previous_user" and pv["year"]["source"] == "previous_user"

    _commit(s, "What about last year?", "Focus Plus in Dalu block in FY 2023-24",
            {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2023},
            substituted="What about FY 2023-24?")
    assert s.state.provenance["year"]["source"] == "resolved_reference"

    _commit(s, "Show beneficiaries, South Garo Hills", "Show beneficiaries in South Garo Hills",
            {"district": "SOUTH GARO HILLS", "year_key": 2023}, reply="South Garo Hills")
    assert s.state.provenance["district"]["source"] == "clarification_response"


def test_model_inferred_values_are_not_inherited():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    _commit(s, "and that?", "Focus Plus disbursement in East Khasi Hills",
            {"district": "EAST KHASI HILLS"})
    assert s.state.provenance["district"]["source"] in ("model_inference", "validated_database")
    s.state.provenance["district"]["source"] = "model_inference"
    assert "district" not in cm.merged_prior_resolved({}, s.state)
    s.state.provenance["district"]["source"] = "current_user"
    assert cm.merged_prior_resolved({}, s.state)["district"] == "EAST KHASI HILLS"


def test_provenance_survives_the_durable_snapshot():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    _commit(s, Q1, Q1, {"district": "WEST GARO HILLS", "year_key": 2024})
    t = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    t.apply_snapshot(json.loads(json.dumps(s.to_snapshot(), default=str)))
    assert t.state.provenance["district"]["source"] == "current_user"


# ── KI-029: field-specific rewrite checks ──────────────────────────────────
ANSWER_G = ("Across Focus Plus, MGNREGA and PMAY-G: East Khasi Hills, Ri Bhoi and Jaintia Hills "
            "led; blocks Rongram, Tura, Selsella; villages Nongthymmai, Maska and Adugre; "
            "FY 2019-20 and 2021-22; women 54%, SC/ST 30%, Piggery and Poultry applicants; "
            "person-days 1.2 lakh; 4,500 beneficiaries.")


def _check(rewrite, followup, prev_q=Q1, filters="PREVIOUS filters: scheme=Focus Plus; "
           "district=WEST GARO HILLS; year=FY 2024-25"):
    return p._rewrite_provenance_violation(
        rewrite, question=followup, allowed_text="\n".join([followup, prev_q, filters]),
        allowed_schemes=["Focus Plus"], denied_text=ANSWER_G)


@pytest.mark.parametrize("rewrite,expected", [
    ("How much was disbursed under Focus Plus in Dalu block in FY 2024-25?", None),
    ("How much was disbursed under MGNREGA in Dalu block in FY 2024-25?", "scheme"),
    ("How much was disbursed under Focus Plus in Ri Bhoi in FY 2024-25?", "district"),
    ("How much was disbursed under Focus Plus in Rongram block in FY 2024-25?", "block"),
    ("How much was disbursed under Focus Plus in Dalu block in FY 2019-20?", "year"),
    ("How many beneficiaries under Focus Plus in Dalu block in FY 2024-25?", "metric"),
    ("How much was disbursed to women under Focus Plus in Dalu block in FY 2024-25?", "category"),
    ("How much was disbursed under Piggery in Dalu block in FY 2024-25?", "category"),
    ("How much was disbursed under Focus Plus in Dalu block near Nongthymmai in FY 2024-25?", "name"),
])
def test_contamination_by_field(rewrite, expected):
    assert _check(rewrite, "What about Dalu block?") == expected


def test_explicit_changes_by_the_user_are_always_allowed():
    assert _check("How much was disbursed under Focus Plus in West Garo Hills in FY 2019-20?",
                  "What about 2019-20?") is None
    assert _check("How much was disbursed under Focus Plus in West Garo Hills in FY 2023-24?",
                  "What about last year?") is None
    assert _check("How much was disbursed to women under Focus Plus in West Garo Hills in FY 2024-25?",
                  "What about women?") is None
    assert _check("How many beneficiaries under Focus Plus in West Garo Hills in FY 2024-25?",
                  "How many beneficiaries?") is None
    assert _check("How much was disbursed under Focus Plus in Nongthymmai village in FY 2024-25?",
                  "What about Nongthymmai village?") is None


def test_field_policy_table_is_complete_and_scheme_agnostic():
    required = {"district", "block", "village", "category", "year", "financial_year", "scheme",
                "metric"}
    assert required <= set(cp.FIELD_POLICIES)
    for pol in cp.FIELD_POLICIES.values():
        assert isinstance(pol.inheritable, bool) and isinstance(pol.replaceable, bool)
        assert isinstance(pol.clearable, bool) and pol.clarify_when and pol.validation
    assert cp.FIELD_POLICIES["year"].replaceable            # the user can always change a year
    blob = json.dumps({k: vars(v) for k, v in cp.FIELD_POLICIES.items()}, default=str)
    for scheme in p.SCHEME_CATALOG:
        assert f'"{scheme}"' not in blob                      # keyed by field, never by scheme


def test_a_seventh_scheme_needs_no_context_code(monkeypatch):
    import re
    monkeypatch.setitem(p._SCHEME_NAME_PATTERN, "Test Scheme",
                        re.compile(r"\btest\s+scheme\b", re.IGNORECASE))
    monkeypatch.setitem(p.SCHEME_CATALOG, "Test Scheme", "a synthetic seventh scheme")
    assert cp.plan_state_merge("what about Test Scheme?", STATE, prev=PREV).action("scheme") == \
        cp.REPLACE
    assert _check("Total under Test Scheme in West Garo Hills in FY 2024-25",
                  "Show it by district.") == "scheme"
    assert _check("Total under Test Scheme in West Garo Hills in FY 2024-25",
                  "what about Test Scheme?") is None


@pytest.mark.parametrize("scheme", list(p.SCHEME_CATALOG))
def test_merge_plan_is_identical_for_every_scheme(scheme):
    st = ConversationState(scheme=scheme, district="WEST GARO HILLS", year=2024)
    pv = Turn(question=f"Total amount under {scheme} in West Garo Hills in FY 2024-25?",
              raw_question="", route="data", schemes=[scheme],
              resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024})
    for q, kind in (("What about South Garo Hills?", cp.GEOGRAPHY_CHANGE),
                    ("What about 2023-24?", cp.TIME_CHANGE),
                    ("Show it by district.", cp.GROUP_BY_CHANGE)):
        plan = cp.plan_state_merge(q, st, prev=pv)
        assert plan.kind == kind and plan.action("scheme") == cp.KEEP


# ── Priority-aware SQL budgeting ───────────────────────────────────────────
def test_over_budget_compresses_few_shot_but_never_required_sections(monkeypatch, caplog):
    er = {"resolved": {"district": "WEST GARO HILLS", "year_key": 2024}, "notes": []}
    q = "How much was disbursed in West Garo Hills in FY 2024-25?"
    full = prompt_builder.build_sql_prompt(q, ["Focus Plus"], er)
    monkeypatch.setattr(p.settings, "PROMPT_BUDGET_SQL_TOKENS", 1)
    with caplog.at_level(logging.INFO, logger="app.context_budget"):
        small = prompt_builder.build_sql_prompt(q, ["Focus Plus"], er)
    assert len(small) < len(full)
    assert small.count("\nQ: \"") < full.count("\nQ: \"") and small.count("\nQ: \"") <= 1
    for required in (prompt_builder.build_schema_context(["Focus Plus"]),
                     prompt_builder._entities_block(er, q, ["Focus Plus"]),
                     prompt_builder._common_mistakes_block(["Focus Plus"]),
                     prompt_builder._prohibited_block(["Focus Plus"]),
                     f'\nQuestion: "{q}"\nSQL:'):
        assert required in small
    rec = [r.getMessage() for r in caplog.records if '"kind": "sql"' in r.getMessage()][-1]
    assert '"compressed": ["few_shot"' in rec and '"over_budget": true' in rec


def test_catalog_dedupe_keeps_every_distinct_line():
    out = prompt_builder._dedupe_catalog("a\nshared line\nb\n", "x\n  shared line  \n")
    assert out == "a\nb\n"


# ── Per-call measurement ───────────────────────────────────────────────────
def test_llm_call_records_usage_and_latency(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 321, "completion_tokens": 12}}

    class _Client:
        async def post(self, *_a, **_k):
            return _Resp()

    async def go():
        monkeypatch.setattr(llm, "_client", _Client())
        monkeypatch.setattr(llm, "_gate", asyncio.Semaphore(1))
        sink = []
        token = llm.llm_calls_var.set(sink)
        try:
            await llm.call_classifier("hello")
        finally:
            llm.llm_calls_var.reset(token)
        return sink

    sink = _run(go())
    assert sink and sink[0]["role"] == "classifier"
    assert sink[0]["prompt_tokens"] == 321 and sink[0]["completion_tokens"] == 12
    assert sink[0]["latency_ms"] >= 0


# ── Fixes from the first live run (2026-09-26) ─────────────────────────────
@pytest.mark.parametrize("q,expected", [
    ("How many beneficiaries were there?", True), ("What was the total?", True),
    ("How many schemes are there?", False), ("How many beneficiaries in Ri Bhoi?", False),
    ("How many beneficiaries in 2023-24?", False), ("How many person-days under MGNREGA?", False),
])
def test_scopeless_metric_question_after_a_data_turn_is_a_followup(q, expected):
    assert p.is_scopeless_followup(q, PREV) is expected
    knowledge_prev = Turn(question="who is eligible?", raw_question="", route="knowledge")
    assert p.is_scopeless_followup(q, knowledge_prev) is False


def test_scopeless_followup_is_rewritten_and_never_cached(monkeypatch):
    seen = _pipeline_prior(monkeypatch, Turn(question=Q1, raw_question=Q1, route="data",
                                             schemes=["Focus Plus"],
                                             resolved_entities={"district": "WEST GARO HILLS",
                                                                "year_key": 2024}),
                           "How many beneficiaries were there?",
                           "How many beneficiaries were there under Focus Plus in West Garo Hills "
                           "in FY 2024-25?")
    assert "West Garo Hills" in seen["q"]
    assert seen["prior"]["district"] == "WEST GARO HILLS" and seen["prior"]["year_key"] == 2024
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    assert router.is_cacheable("How many beneficiaries were there?", s) is True   # no antecedent
    s.turns = [PREV]
    assert router.is_cacheable("How many beneficiaries were there?", s) is False


def test_cleared_filter_is_told_to_the_rewrite_and_enforced(monkeypatch):
    prompts = []

    async def classifier(prompt, **_kw):
        prompts.append(prompt)
        return ("Show the disbursement under Focus Plus by district in West Garo Hills in FY 2024-25"
                if "Standalone question:" in prompt else '{"intent": "DATA"}')

    seen = {}

    async def fake_answer_data(question, **kw):
        seen["q"], seen["prior"] = question, kw.get("prior_resolved") or {}
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["Focus Plus"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}

    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(p, "_answer_data", fake_answer_data)
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.turns = [Turn(question=Q1, raw_question=Q1, route="data", schemes=["Focus Plus"],
                    resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024})]
    s.state = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024)
    _run(p._run_pipeline("Show it by district.", session=s))
    rewrite_prompt = next(x for x in prompts if "Standalone question:" in x)
    assert "REMOVED by the follow-up (do NOT include these): district=WEST GARO HILLS" in rewrite_prompt
    assert "West Garo Hills" not in seen["q"]          # the re-added district was rejected
    assert "district" not in seen["prior"] and seen["prior"]["year_key"] == 2024
