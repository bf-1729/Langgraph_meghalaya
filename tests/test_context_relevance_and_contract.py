"""
Context relevance and the semantic query contract (reported 2026-09-29).

The reported conversation, after "beneficiaries in focus+ across all financial
years" (Focus+ 105,813):
    "give me same for mgnrega"        -> must stay the same DATA operation on MGNREGA
    "who is harshit"                  -> was answered from Focus Plus reference docs
    "he is my collik remember"        -> was answered as Focus Plus
    "now give me five thousand loan for me i am in crisis"
                                      -> became a Focus Plus DATA question (year pause)
    "What is the total disbursement amount for the Focus Plus scheme for a loan of
     five thousand across all financial years for all of Meghalaya"
                                      -> SELECT SUM(amount_disbursed) FROM curated.v_focus_plus
                                         (the ₹5,000 silently dropped)
Plus: "Compare … in WHK and EKH" lost WHK; "Which one?" was guessed; "How many
villages are named Songsak?" counted one resolved place; a resolved district /
year could be missing from the SQL and still run (KI-034); a CLEARed block
stayed in the committed state (KI-032); an all-years choice was not carried
(KI-030).

Every test calls the real function. The model, RAG and DB are stubbed where a
test drives _run_pipeline; nothing here needs the VPN.
    .venv/Scripts/python.exe -m pytest tests/test_context_relevance_and_contract.py -q
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import context_manager as cm  # noqa: E402
from app import context_policy as cp  # noqa: E402
from app import edge, entity_resolver, llm, premise_check, pipeline as p  # noqa: E402
from app.session_store import ConversationState, Session, Turn  # noqa: E402

Q1 = "beneficiaries in focus+ across all financial years"


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


def _session(prev_turn, state):
    s = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    s.turns, s.state = [prev_turn], state
    return s


def _run(followup, prev_turn=None, state=None):
    """_run_pipeline with the 4B model, RAG and the DATA path stubbed. The stub
    rewrite behaves like the live model did: it glues the previous scheme on."""
    prev_turn = prev_turn or Turn(question=Q1, raw_question=Q1, route="data", schemes=["Focus Plus"],
                                  answer="Focus+ shows 105,813 beneficiaries across all financial years.")
    state = state or ConversationState(scheme="Focus Plus", metric="beneficiaries", year_all=True)
    seen = {}

    async def classifier(prompt, **_k):
        if "Standalone question:" in prompt:
            seen["rewrite"] = True
            return "rewritten under Focus Plus?"
        return '{"intent": "KNOWLEDGE"}'

    async def fake_answer_data(question, **kw):
        seen["data"] = question
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["MGNREGA"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}

    async def kb(question, scheme=None, **_k):
        seen["kb"] = scheme
        return {"answer": "info", "confidence": "medium", "sources": []}

    async def no_ctx(*_a, **_k):
        return ""

    import app.rag as rag
    orig = (llm.call_classifier, p._answer_data, rag.answer_from_kb, cm.build_followup_context)
    llm.call_classifier, p._answer_data, rag.answer_from_kb, cm.build_followup_context = (
        classifier, fake_answer_data, kb, no_ctx)
    s = _session(prev_turn, state)
    try:
        try:
            res = asyncio.run(p._run_pipeline(followup, session=s))
            seen["route"], seen["edge"] = res.get("route"), res.get("edge_type")
        except p.ClarificationNeeded as e:
            seen["route"], seen["rule"] = "clarification", e.rule
    finally:
        llm.call_classifier, p._answer_data, rag.answer_from_kb, cm.build_followup_context = orig
    seen["plan"] = s.turn_context.get("plan") or {}
    return seen


# ── Failures B / C: unrelated messages do not inherit the scheme ────────────
@pytest.mark.parametrize("q", ["who is harshit", "he is my collik remember", "Who is Harshit?"])
def test_unrelated_message_is_not_rewritten_into_the_previous_scheme(q):
    seen = _run(q)
    assert seen["route"] == "edge" and seen["edge"] == "off_topic"
    assert "rewrite" not in seen and "kb" not in seen and "data" not in seen


@pytest.mark.parametrize("q", ["now give me five thousand loan for me i am in crisis",
                               "Give me five thousand loan for me.", "lend me 5000 rupees",
                               "I need a loan urgently", "i am in crisis please help"])
def test_personal_money_request_is_declined_not_queried(q):
    seen = _run(q)
    assert seen["route"] == "edge" and seen["edge"] == "personal_request"
    assert "data" not in seen and "rewrite" not in seen


@pytest.mark.parametrize("q", ["how many loans were disbursed under CM Elevate Legacy?",
                               "give me the amount disbursed in West Garo Hills",
                               "give me the total payment for Focus Plus",
                               "loan entity wise disbursement", "what is the loan amount sanctioned",
                               "which scheme gives loans to women?"])
def test_personal_request_does_not_fire_on_data_questions(q):
    assert not edge.is_personal_request(q)


def test_personal_request_reply_never_promises_money():
    r = edge.detect_edge_case("give me five thousand loan for me")
    assert r["type"] == "personal_request"
    assert "can't give, lend or approve" in r["response"] and "not a loan" in r["response"]


@pytest.mark.parametrize("q", ["How many beneficiaries?", "What about South Garo Hills?", "What about 2023-24?",
                               "Documents?", "Eligibility?", "who can apply", "what about Mawlai?",
                               "show it by district", "which is higher", "when was it launched",
                               "and the second one?", "what did I ask before?", "same for mgnrega",
                               "इसके बाद क्या हुआ"])
def test_real_followups_still_carry_context(q):
    assert cp.continuation_signals(q), q


@pytest.mark.parametrize("q", ["who is harshit", "he is my collik remember", "tell me about harshit",
                               "remember my friend"])
def test_no_continuation_signal(q):
    assert cp.continuation_signals(q) == []


@pytest.mark.parametrize("scheme", list(p.SCHEME_CATALOG))
def test_every_scheme_substitution_keeps_its_signal(scheme):
    # the relevance gate must never block a substitution, for any registry scheme
    assert "scheme" in cp.continuation_signals(f"give me same for {scheme}")


# ── Test 1: scheme substitution stays DATA, only the scheme changes ─────────
def test_reported_substitution_is_the_same_query_on_mgnrega():
    seen = _run("give me same for mgnrega")
    assert seen["route"] == "data" and "kb" not in seen
    assert seen["data"] == "beneficiaries in MGNREGA across all financial years"
    assert {k for k, v in seen["plan"]["actions"].items() if v != "KEEP"} == {"scheme"}


# ── Test 8 / 9 on the KNOWLEDGE fallback too ────────────────────────────────
def test_knowledge_fallback_scheme_needs_a_continuing_message():
    # "who is harshit" never reaches RAG with the previous scheme
    assert "kb" not in _run("who is harshit")


# ── "Which one?" asks, never guesses ────────────────────────────────────────
@pytest.mark.parametrize("q", ["Which one?", "which one", "that one?", "and which one then?", "which of them?"])
def test_bare_reference_asks(q):
    seen = _run(q)
    assert seen["route"] == "clarification" and seen["rule"] == "reference-ambiguous"
    assert "rewrite" not in seen and "data" not in seen


@pytest.mark.parametrize("q", ["which one had the highest?", "the top one?", "which one is bigger"])
def test_reference_with_a_predicate_is_not_bare(q):
    assert not cp.is_bare_reference(q)


# ── WHK: a letter-swapped acronym is asked about, never dropped ─────────────
def test_whk_offers_west_khasi_hills():
    q = "Compare the total disbursement in WHK and EKH for Focus Plus across all financial years"
    near = entity_resolver.acronym_near_misses(q)
    assert near == {"WHK": ["WEST KHASI HILLS"]}
    e = p._acronym_near_miss_clarification(q, near)
    assert e.rule == "entity-ambiguous" and "WHK" in e.question
    assert e.options[0]["question"].startswith("Compare the total disbursement in West Khasi Hills and EKH")
    # the chip text resolves both districts deterministically
    assert entity_resolver.named_places(e.options[0]["question"]) == {
        "WEST KHASI HILLS": "district", "EAST KHASI HILLS": "district"}


@pytest.mark.parametrize("q", ["WKH and EKH", "Compare WHK and EKH, West Khasi Hills", "the key was hew",
                               "total in SWKH", "how many in EJH"])
def test_known_acronyms_and_answered_names_do_not_ask(q):
    assert entity_resolver.acronym_near_misses(q) == {}


# ── Failure D / Test 10: a stated Focus Plus payment amount ─────────────────
REPORTED_D = ("What is the total disbursement amount for the Focus Plus scheme for a loan of five "
              "thousand across all financial years for all of Meghalaya")


def test_five_thousand_is_read_as_a_payment_filter():
    [a] = premise_check.stated_amount_filters(REPORTED_D)
    assert (a.value, a.noun) == (5000.0, "loan")
    assert premise_check.extract_premises("total of ₹5,000 payments") == []   # a filter, not a premise


@pytest.mark.parametrize("q,val", [("payments of Rs 2,500 in 2025-26", 2500), ("₹5,000 payments", 5000),
                                   ("two thousand five hundred payments", 2500), ("loans of 5 lakh", 500000),
                                   ("payments of 5k", 5000)])
def test_amount_forms(q, val):
    assert premise_check.stated_amount_filters(q)[0].value == val


@pytest.mark.parametrize("q", ["payment for 2023-24", "a loan for 2024", "tranche of 4",
                               "amount disbursed in tranche 1", "give me the amount disbursed"])
def test_not_amounts(q):
    assert premise_check.stated_amount_filters(q) == []


def test_reported_sql_is_rejected_for_dropping_the_amount():
    bad = "SELECT SUM(amount_disbursed) AS total_disbursement_amount FROM curated.v_focus_plus LIMIT 1"
    issue = p._focusplus_stated_amount_missing(REPORTED_D, ["Focus Plus"], bad)
    assert issue and "amount_disbursed = 5000" in issue
    good = "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus WHERE amount_disbursed = 5000"
    assert p._focusplus_stated_amount_missing(REPORTED_D, ["Focus Plus"], good) is None
    # other schemes are not touched by this Focus Plus contract
    assert p._focusplus_stated_amount_missing(REPORTED_D, ["CM Elevate Legacy"], bad) is None


def test_amount_focus_plus_never_pays_is_asked():
    q = "total Focus Plus disbursement for payments of 3000"
    amt = p._focusplus_stated_amount(q, ["Focus Plus"])
    e = p._focusplus_amount_clarification(q, amt)
    assert e.rule == "amount-not-held" and "₹5,000" in e.question and "₹2,500" in e.question
    assert [o["label"] for o in e.options][-1] == "All Focus Plus payments"
    assert "3000" not in e.options[-1]["question"]


def test_amount_note_says_not_a_loan():
    [a] = premise_check.stated_amount_filters(REPORTED_D)
    note = p._focusplus_amount_note(a)
    assert "not a loan" in note and "Tranch 1" in note


# ── Failure E / KI-034: resolved scope must reach the SQL ───────────────────
def _er(**resolved):
    return {"resolved": resolved}


def test_k8_sql_without_where_is_rejected():
    sql = "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus"
    miss = p._resolved_scope_missing("disbursed in South Garo Hills in FY 2025-26", ["Focus Plus"],
                                     _er(district="SOUTH GARO HILLS", year_key=2025), sql)
    assert miss and miss[0] == "district"
    sql2 = sql + " WHERE lgd_district = 'SOUTH GARO HILLS'"
    miss2 = p._resolved_scope_missing("disbursed in South Garo Hills in FY 2025-26", ["Focus Plus"],
                                      _er(district="SOUTH GARO HILLS", year_key=2025), sql2)
    assert miss2 and miss2[0] == "year"
    ok = sql2 + " AND financial_year_short = '2025-26'"
    assert p._resolved_scope_missing("q", ["Focus Plus"], _er(district="SOUTH GARO HILLS", year_key=2025),
                                     ok) is None


def test_comparison_must_carry_both_districts():
    er = _er(district_list=["WEST KHASI HILLS", "EAST KHASI HILLS"])
    one = "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus WHERE lgd_district = 'EAST KHASI HILLS'"
    miss = p._resolved_scope_missing("compare", ["Focus Plus"], er, one)
    assert miss and miss[0] == "district_list" and "WEST KHASI HILLS" in miss[1]
    both = ("SELECT lgd_district, SUM(amount_disbursed) FROM curated.v_focus_plus WHERE lgd_district IN "
            "('WEST KHASI HILLS','EAST KHASI HILLS') GROUP BY lgd_district")
    assert p._resolved_scope_missing("compare", ["Focus Plus"], er, both) is None


@pytest.mark.parametrize("question,schemes,er,sql", [
    # all financial years: no year filter is correct
    ("total across all financial years", ["Focus Plus"], {"year_key": 2025},
     "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus"),
    # CM Elevate has no time dimension
    ("applications in 2024", ["CM Elevate"], {"year_key": 2024}, "SELECT COUNT(*) FROM curated.v_cm_elevate"),
    # a resolved village supersedes its district / block (suppressed in the prompt)
    ("x", ["MGNREGA"], {"village_code": 273249, "district": "WEST GARO HILLS", "block": "DALU"},
     "SELECT SUM(person_days) FROM curated.v_employment WHERE village_code = 273249"),
    # a hill-range expansion may be covered by pattern
    ("x", ["Focus Plus"], {"district_list": ["WEST GARO HILLS", "EAST GARO HILLS"],
                           "district_list_region": "Garo Hills"},
     "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus WHERE lgd_district LIKE '%GARO HILLS'"),
    # a LIKE on the block's distinctive word
    ("x", ["Focus Plus"], {"block": "TURA MUNICIPAL BOARD-MUNICIPAL BOARD"},
     "SELECT COUNT(*) FROM curated.v_focus_plus WHERE lgd_block ILIKE '%tura%'"),
    # an apostrophe doubled in SQL
    ("x", ["MGNREGA"], {"block": "O'KHLA"}, "SELECT 1 FROM curated.v_employment WHERE lgd_block = 'O''KHLA'"),
    # a date range carries the FY start year
    ("x", ["PMAY-G"], {"year_key": 2023},
     "SELECT COUNT(*) FROM curated.v_pmay WHERE sanction_date BETWEEN '2023-04-01' AND '2024-03-31'"),
])
def test_scope_guard_does_not_over_validate(question, schemes, er, sql):
    assert p._resolved_scope_missing(question, schemes, {"resolved": er}, sql) is None


def test_scope_guard_runs_before_the_verifier():
    # a guard failure must become a repair instruction, not reach the paid verifier
    src = Path(p.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def execute_with_repair"):src.index("# ── Numeric faithfulness guard")]
    assert body.index("_resolved_scope_missing(") < body.index("await _verify_sql(")
    assert body.index("_focusplus_stated_amount_missing(") < body.index("await _verify_sql(")


# ── Test 4: a village NAME search is a search, not a lookup ─────────────────
@pytest.mark.parametrize("q,expected", [
    ("How many villages are named Songsak?", ("exact", "Songsak")),
    ("Which villages contain Songsak in their name?", ("contains", "Songsak")),
    ("villages whose name contains songsak", ("contains", "songsak")),
    ("number of villages named \"Rongram Bazar\"", ("exact", "Rongram Bazar")),
])
def test_name_search_detected(q, expected):
    assert p._village_name_search(q) == expected


@pytest.mark.parametrize("q", ["How much has been disbursed in Songsak?", "How many villages in Songsak block?",
                               "how many villages received employment in Songsak",
                               "How much has been disbursed in Songsak village?"])
def test_lookups_are_not_name_searches(q):
    assert p._village_name_search(q) is None


def test_name_search_counts_every_village_with_the_name(monkeypatch):
    seen = {}

    async def fake_fetch(sql, params):
        seen["sql"], seen["params"] = sql, params
        return [{"village_code": 1, "name": "Songsak", "block": "SONGSAK", "district": "EAST GARO HILLS"},
                {"village_code": 2, "name": "Songsak", "block": "RONGJENG", "district": "EAST GARO HILLS"}]

    monkeypatch.setattr(p, "fetch_rows", fake_fetch)
    res = asyncio.run(p._village_name_search_answer("How many villages are named Songsak?"))
    assert res["row_count"] == 2 and res["answer"].startswith("2 villages named \"Songsak\"")
    assert "also the name of a block" in res["answer"]
    assert "curated.dim_geography" in seen["sql"] and seen["params"] == ["SONGSAK"]
    assert "$1" in seen["sql"]                    # parameter-bound app SQL
    # a district after the name narrows the search
    res2 = asyncio.run(p._village_name_search_answer(
        "How many villages are named Songsak in West Garo Hills?"))
    assert res2["row_count"] == 0


# ── KI-032 / KI-030: committed state follows the plan; all-years is carried ─
def _commit(state, plan, resolved, question, schemes=("Focus Plus",)):
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.state = state
    s.turn_context = {"plan": plan} if plan else {}
    cm.update_state(s, question, question, {"route": "data", "schemes": list(schemes), "sql": "S",
                                            "resolved_entities": resolved})
    return s.state


def test_cleared_block_leaves_the_committed_state():
    st = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", block="DALU", year=2024)
    plan = cp.plan_state_merge("What about South Garo Hills?", st).to_dict()
    assert plan["actions"]["block"] == cp.CLEAR
    st = _commit(st, plan, {"district": "SOUTH GARO HILLS", "year_key": 2024}, "…South Garo Hills…")
    assert st.district == "SOUTH GARO HILLS" and st.block is None and st.year == 2024


def test_a_new_question_does_not_keep_old_places():
    st = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", block="DALU", year=2024)
    st = _commit(st, None, {"year_key": 2023}, "How many PMAY houses in 2023-24?", schemes=("PMAY-G",))
    assert st.district is None and st.block is None and st.year == 2023


def test_kept_fields_survive_a_followup():
    st = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", block="DALU", year=2024)
    plan = cp.plan_state_merge("How many beneficiaries?", st).to_dict()
    st = _commit(st, plan, {}, "How many beneficiaries under Focus Plus?")
    assert (st.district, st.block, st.year) == ("WEST GARO HILLS", "DALU", 2024)


def test_all_years_is_recorded_and_carried():
    st = _commit(ConversationState(), None, {}, Q1)
    assert st.year_all and st.year is None
    out = cm.inject_year_scope("Show it by district under Focus Plus?", st, "KEEP")
    assert out == "Show it by district under Focus Plus across all financial years?"
    assert p._ALL_YEARS_CUE.search(out)                  # the year gate reads it
    # not when the follow-up changes the year, asks a knowledge question, or has no years
    assert cm.inject_year_scope("What about 2023-24?", st, "REPLACE") == "What about 2023-24?"
    assert cm.inject_year_scope("What documents are needed?", st, "KEEP") == "What documents are needed?"
    st.scheme = "CM Elevate"
    assert cm.inject_year_scope("Show it by district?", st, "KEEP") == "Show it by district?"


def test_a_named_year_clears_all_years():
    st = _commit(ConversationState(year_all=True), None, {"year_key": 2025}, "… in FY 2025-26")
    assert not st.year_all and st.year == 2025


def test_year_all_survives_a_worker_switch():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.state = ConversationState(scheme="Focus Plus", year_all=True)
    assert ConversationState.from_dict(s.state.to_dict()).year_all


# ── Test 2: West Garo Hills FY 2024-25 -> Dalu block -> beneficiaries ───────
def test_three_turn_state():
    st = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024)
    p2 = cp.plan_state_merge("What about Dalu block?", st)
    assert (p2.action("scheme"), p2.action("district"), p2.action("block"), p2.action("year")) == \
        ("KEEP", "KEEP", "REPLACE", "KEEP")
    st = _commit(st, p2.to_dict(), {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024}, "…")
    p3 = cp.plan_state_merge("How many beneficiaries?", st)
    assert p3.action("metric") == "REPLACE" and p3.kind == cp.METRIC_CHANGE
    assert all(p3.action(f) == "KEEP" for f in ("scheme", "district", "block", "year"))
    inherited = cm.merged_prior_resolved({}, st)
    assert {k: inherited.get(k) for k in ("district", "block", "year_key")} == \
        {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024}


# ── Observability: one pipeline_decision line per decision, no text ─────────
def test_decision_log_carries_labels_not_text(caplog):
    import logging
    from app import context_budget
    with caplog.at_level(logging.INFO, logger="app.context_budget"):
        _run("who is harshit")
    lines = [r.getMessage() for r in caplog.records if "pipeline_decision" in r.getMessage()]
    assert any('"stage": "continuation"' in ln for ln in lines)
    assert any('"edge_type": "off_topic"' in ln for ln in lines)
    assert not any("harshit" in ln for ln in lines)
    assert context_budget.log_decision("x", a=1)["event"] == "pipeline_decision"


# ── Reported 2026-09-29 (second conversation): "what is focus" → CM Elevate ──
# After a CM Elevate Legacy turn, "what is focus" was rewritten "What is the
# focus of the CM Elevate Legacy scheme in FY 2024-25?" (the scheme name read as
# a noun); "give me beneficiaries" became "…disbursement for Focus Plus in FY
# 2024-25" (metric swapped, another scheme's year) or a reference-docs answer;
# a typed year reply lost the scheme; "all of them combined" kept the last year.
CMEL = "How much subsidy was disbursed under CM Elevate Legacy in FY 2024-25?"


@pytest.mark.parametrize("q,expected", [
    ("what is focus", True), ("focus beneficiaries", True), ("tell me about focus", True),
    ("what is the main focus of this scheme", False), ("what does it focus on", False),
    ("what is the focus of CM Elevate", False), ("focus plus beneficiaries", False),
    ("what are its focus areas", False)])
def test_bare_focus_scheme_vs_noun(q, expected):
    assert p._names_bare_focus_scheme(q) is expected


def test_what_is_focus_after_cm_elevate_asks_which_focus():
    prev = Turn(question=CMEL, raw_question=CMEL, route="data", schemes=["CM Elevate Legacy"],
                resolved_entities={"year_key": 2024})
    seen = _run("what is focus", prev, ConversationState(scheme="CM Elevate Legacy", year=2024))
    assert seen["route"] == "clarification" and seen["rule"] == "focus-scheme-ambiguous"
    assert "rewrite" not in seen and "kb" not in seen


def test_knowledge_question_drops_an_inherited_year():
    assert p._drop_inherited_time("What are the eligibility criteria for Focus Plus for 2024-25?",
                                  "eligibility?") == "What are the eligibility criteria for Focus Plus?"
    assert p._drop_inherited_time("Eligibility for MGNREGA in FY 2023-24?", "eligibility in 2023-24") == \
        "Eligibility for MGNREGA in FY 2023-24?"


def test_rewrite_must_keep_the_followups_own_metric():
    v = cp.rewrite_violation("What is the disbursement for Focus Plus in FY 2024-25?",
                             question="give me beneficiaries",
                             allowed_text="scheme=CM Elevate Legacy metric=disbursement FY 2024-25 Focus Plus",
                             denied_text="", allowed_schemes=["Focus Plus", "CM Elevate Legacy"])
    assert v == "metric"


def test_followup_after_another_schemes_knowledge_answer_picks_its_thread():
    st = ConversationState(scheme="CM Elevate Legacy", year=2024, metric="disbursement")
    kprev = Turn(question="what is Focus Plus", raw_question="what is Focus Plus", route="knowledge",
                 schemes=["Focus Plus"])
    # names nothing -> the antecedent's scheme, none of the old thread's filters
    t = p._followup_thread_state("give me beneficiaries", kprev, st)
    assert t is not st and t.scheme == "Focus Plus" and t.year is None
    # the digression is left intact in the session (test_context_manager.py §6)
    assert st.year == 2024
    # its own words pick the earlier thread -> that thread, with its filters
    mg = ConversationState(scheme="MGNREGA", district="West Garo Hills", year=2023)
    pprev = Turn(question="who is eligible for PMAY-G?", raw_question="", route="knowledge", schemes=["PMAY-G"])
    assert p._followup_thread_state("and person-days?", pprev, mg) is mg


def test_committing_another_scheme_drops_the_old_schemes_filters():
    st = ConversationState(scheme="CM Elevate Legacy", year=2024, district="EAST KHASI HILLS")
    plan = cp.plan_state_merge("give me beneficiaries", ConversationState(scheme="Focus Plus")).to_dict()
    st = _commit(st, plan, {"year_key": 2025}, "give me beneficiaries under Focus Plus, 2025-26")
    assert (st.scheme, st.year, st.district) == ("Focus Plus", 2025, None)
    # a substitution that carries a filter resolves it, so it is kept
    st2 = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024)
    plan2 = cp.plan_state_merge("give me same for MGNREGA", st2).to_dict()
    st2 = _commit(st2, plan2, {"district": "WEST GARO HILLS", "year_key": 2024}, "…", schemes=("MGNREGA",))
    assert (st2.scheme, st2.district, st2.year) == ("MGNREGA", "WEST GARO HILLS", 2024)


def test_measure_after_knowledge_is_data_with_the_scheme_and_no_model_rewrite():
    prev = Turn(question="what is cm elevate legacy", raw_question="what is cm elevate legacy",
                route="knowledge", schemes=["CM Elevate Legacy"])
    seen = _run("give me beneficiaries", prev, ConversationState(scheme="CM Elevate Legacy"))
    assert seen["route"] == "data" and "rewrite" not in seen and "kb" not in seen
    assert seen["data"] == "give me beneficiaries under CM Elevate Legacy?"


def test_typed_words_decide_intent():
    assert p._typed_intent("give me beneficiaries") == "DATA"
    assert p._typed_intent("eligibility?") == "KNOWLEDGE"
    assert p._typed_intent("and Dalu?") is None


def test_a_paused_followup_is_remembered_in_full():
    from app.routers import query as router
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.turn_context = {"standalone_question": "give me beneficiaries under CM Elevate Legacy?",
                      "resumed_question": None}
    assert router.pause_question(s, "give me beneficiaries") == "give me beneficiaries under CM Elevate Legacy?"
    s.turn_context = {"resumed_question": "Focus Plus bank question"}
    assert router.pause_question(s, "Focus Plus") == "Focus Plus bank question"


def test_all_of_them_clears_the_dimension_just_varied():
    st = ConversationState(scheme="Focus Plus", year=2022, last_dimension="year")
    for q in ("and all of them combined?", "both together?", "in total?"):
        assert cp.plan_state_merge(q, st).action("year") == cp.CLEAR, q
    assert cp.plan_state_merge("what about 2023-24 combined?", st).action("year") == cp.REPLACE
    assert cp.plan_state_merge("and all of them combined?",
                               ConversationState(scheme="Focus Plus", year=2022)).action("year") == cp.KEEP
    assert p._all_years_rewrite("give me beneficiaries under Focus Plus, 2022-23") == \
        "give me beneficiaries under Focus Plus across all financial years?"


def test_last_dimension_is_recorded_and_persisted():
    st = ConversationState(scheme="Focus Plus", year=2025)
    plan = cp.plan_state_merge("what about 2022-23?", st).to_dict()
    st = _commit(st, plan, {"year_key": 2022}, "give me beneficiaries under Focus Plus, 2022-23")
    assert st.last_dimension == "year"
    assert ConversationState.from_dict(st.to_dict()).last_dimension == "year"


def test_knowledge_question_drops_an_inherited_place():
    assert p._drop_inherited_place("Who is eligible for PMAY-G houses in East Khasi Hills?",
                                   "who is eligible?") == "Who is eligible for PMAY-G houses?"
    assert p._drop_inherited_place("What is the eligibility for Focus Plus in Dalu block?",
                                   "eligibility?") == "What is the eligibility for Focus Plus?"
    kept = "What documents are needed for PMAY-G houses in West Garo Hills?"
    assert p._drop_inherited_place(kept, "documents for west garo hills?") == kept


def test_place_change_after_knowledge_answers_continues_the_data_thread():
    q1 = "How many PMAY-G houses were completed in East Khasi Hills in FY 2023-24?"
    data = Turn(question=q1, raw_question=q1, route="data", schemes=["PMAY-G"],
                resolved_entities={"district": "EAST KHASI HILLS", "year_key": 2023})
    k1 = Turn(question="Who is eligible for PMAY-G houses?", raw_question="who is eligible?",
              route="knowledge", schemes=["PMAY-G"])
    k2 = Turn(question="What documents are needed for PMAY-G houses?", raw_question="what documents?",
              route="knowledge", schemes=["PMAY-G"])
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.turns = [data, k1, k2]
    st = ConversationState(scheme="PMAY-G", district="EAST KHASI HILLS", year=2023)
    plan = cp.plan_state_merge("and in West Garo Hills?", st)
    assert p._data_thread_antecedent("and in West Garo Hills?", k2, s, plan) is data
    # a how-it-works follow-up stays on the knowledge thread
    plan2 = cp.plan_state_merge("documents for West Garo Hills?", st)
    assert p._data_thread_antecedent("documents for West Garo Hills?", k2, s, plan2) is None


def test_focus_plus_amount_answer_guarantee_is_in_code():
    src = Path(p.__file__).read_text(encoding="utf-8")
    assert "Focus Plus is a cash benefit, not a loan; this covers only the" in src


def test_other_schemes_measure_after_an_aside_continues_that_schemes_thread():
    q1 = "What was MGNREGA expenditure in West Garo Hills in FY 2023-24?"
    data = Turn(question=q1, raw_question=q1, route="data", schemes=["MGNREGA"],
                resolved_entities={"district": "WEST GARO HILLS", "year_key": 2023})
    aside = Turn(question="who is eligible for PMAY-G?", raw_question="who is eligible for PMAY-G?",
                 route="knowledge", schemes=["PMAY-G"])
    # the shortcut must not pin "person-days" (MGNREGA-only) to PMAY-G
    assert not p._measure_after_knowledge("and person-days?", aside)
    assert p._measure_after_knowledge("give me beneficiaries", aside)
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.turns = [data, aside]
    st = ConversationState(scheme="MGNREGA", district="WEST GARO HILLS", year=2023)
    plan = cp.plan_state_merge("and person-days?", st)
    assert p._data_thread_antecedent("and person-days?", aside, s, plan) is data
    assert p._followup_thread_state("and person-days?", aside, st) is st


# ── "give me for pmay" after MGNREGA person-days (reported 2026-09-29) ───────
# The request verb was not an allowed opening of a scheme swap, so the message
# read as a self-contained PMAY-G question and got the scheme description. And
# a swap that carries a measure only the old scheme holds ("person-days" ->
# PMAY-G) ended in "couldn't build a working query".
_PD = "Total MGNREGA person-days in 2023-24"


def _pd_turn(q=_PD, resolved=None):
    return Turn(question=q, raw_question=q, route="data", schemes=["MGNREGA"],
                resolved_entities=resolved or {"year_key": 2023},
                answer="Total MGNREGA person-days in FY 2023-24 is 27264054.")


@pytest.mark.parametrize("q", ["give me for pmay", "give me for PMAY-G", "show me for pmay",
                               "show us the same for pmay", "can you give me for pmay?",
                               "get me the numbers for pmay", "pmay too", "for pmay also",
                               "and for pmay?", "now for pmay", "give me pmay",
                               "okay, for pmay", "under pmay"])
def test_request_verb_scheme_swap_is_a_followup(q):
    assert p._SCHEME_SWAP_FOLLOWUP.match(q)
    assert p.looks_like_followup(q)
    assert p._scheme_swap_rewrite(_PD, q) == "Total PMAY-G person-days in 2023-24"


@pytest.mark.parametrize("q", ["tell me about pmay", "what is pmay?", "give me pmay houses",
                               "give me pmay eligibility", "explain pmay"])
def test_a_question_about_the_scheme_is_not_a_swap(q):
    assert not p._SCHEME_SWAP_FOLLOWUP.match(q)


def test_please_after_a_request_verb_is_a_pure_substitution():
    # "please" is not part of the bare-name form: a bare "focus plus please"
    # stays cacheable (test_router_never_caches_a_pause_reply)
    assert not p._SCHEME_SWAP_FOLLOWUP.match("focus plus please")
    assert p._scheme_substitution("give me for pmay please") == ("PMAY-G", True)
    assert p.looks_like_followup("give me for pmay please")
    assert p._scheme_swap_rewrite(_PD, "give me for pmay please") == "Total PMAY-G person-days in 2023-24"


def test_swap_carrying_another_schemes_measure_offers_the_new_schemes_own():
    st = ConversationState(scheme="MGNREGA", year=2023, metric="person-days")
    gap = p._swap_measure_gap("give me for pmay", "Total PMAY-G person-days in 2023-24",
                              _pd_turn(), st)
    assert gap is not None and gap.rule == "swap-measure-unavailable"
    assert "PMAY-G doesn't record person-days" in gap.question
    qs = [o["question"] for o in gap.options]
    assert qs[0] == "How many PMAY-G houses were sanctioned in FY 2023-24?"
    assert all("PMAY-G" in q and "FY 2023-24" in q for q in qs)


def test_swap_gap_keeps_place_and_all_years_scope():
    st = ConversationState(scheme="MGNREGA", district="EAST KHASI HILLS", year_all=True)
    gap = p._swap_measure_gap("for pmay", "Total PMAY-G person-days in East Khasi Hills",
                              _pd_turn(), st)
    assert gap.options[0]["question"] == (
        "How many PMAY-G houses were sanctioned in East Khasi Hills across all financial years?")
    # no state: the previous turn's resolved filters give the scope
    gap = p._swap_measure_gap("for pmay", "Total PMAY-G person-days in 2023-24",
                              _pd_turn(resolved={"district": "WEST GARO HILLS", "year_key": 2023}),
                              None)
    assert gap.options[0]["question"].endswith("in West Garo Hills in FY 2023-24?")
    # CM Elevate records no year: the year is left out, never paused on again
    gap = p._swap_measure_gap("for cm elevate", "Total CM Elevate person-days in 2023-24",
                              _pd_turn(), ConversationState(scheme="MGNREGA", year=2023))
    assert gap.options[0]["question"] == "How many CM Elevate applications were received?"
    # PMAY-G houses swapped to MGNREGA
    q = "How many PMAY-G houses were sanctioned in FY 2023-24?"
    pm = Turn(question=q, raw_question=q, route="data", schemes=["PMAY-G"],
              resolved_entities={"year_key": 2023})
    gap = p._swap_measure_gap("same for mgnrega",
                              "How many MGNREGA houses were sanctioned in FY 2023-24?", pm,
                              ConversationState(scheme="PMAY-G", year=2023))
    assert "MGNREGA doesn't record houses" in gap.question


@pytest.mark.parametrize("followup,rewritten,route", [
    # money exists in every scheme: the generator answers it
    ("for pmay", "What is the total expenditure in 2023-24 in East Khasi Hills for PMAY-G?", "data"),
    # the user typed the measure themselves
    ("person-days for pmay", "Total PMAY-G person-days in 2023-24", "data"),
    # a knowledge swap is a question about the scheme, not a measure
    ("for pmay", "How are PMAY-G wages paid?", "knowledge"),
    # not a swap at all
    ("in East Khasi Hills", "Total MGNREGA person-days in East Khasi Hills in 2023-24", "data"),
])
def test_swap_gap_does_not_fire(followup, rewritten, route):
    prev = _pd_turn()
    prev.route = route
    assert p._swap_measure_gap(followup, rewritten, prev,
                               ConversationState(scheme="MGNREGA", year=2023)) is None


def test_give_me_for_pmay_end_to_end_pauses_with_pmay_offers_not_kb():
    seen = _run("give me for pmay", prev_turn=_pd_turn(),
                state=ConversationState(scheme="MGNREGA", year=2023, metric="person-days"))
    assert seen["route"] == "clarification" and seen["rule"] == "swap-measure-unavailable"
    assert "kb" not in seen and "data" not in seen and "rewrite" not in seen


# ── A typed reply to ANY chip pause resumes it (reported 2026-09-29) ─────────
# After "give me for pmay" paused with PMAY-G's measures, a typed "houses
# sanctioned" continued the last ANSWERED turn (MGNREGA): only the scope and
# scheme pauses were remembered.
from app.routers import query as router  # noqa: E402

_GAP_OPTS = [{"label": "Houses sanctioned", "question": "How many PMAY-G houses were sanctioned in FY 2023-24?"},
             {"label": "Houses completed", "question": "How many PMAY-G houses were completed in FY 2023-24?"},
             {"label": "Amount released", "question": "How much PMAY-G amount was released in FY 2023-24?"}]
_YEAR_OPTS = [{"label": "FY 2022-23", "question": "total for Focus Plus for FY 2022-23"},
              {"label": "FY 2025-26", "question": "total for Focus Plus for FY 2025-26"},
              {"label": "All available years combined",
               "question": "total for Focus Plus across all financial years"}]
_AMT_OPTS = [{"label": "₹5,000 payments (Tranch 1)", "question": "q for payments of ₹5,000"},
             {"label": "₹2,500 payments (Tranches 2–4)", "question": "q for payments of ₹2,500"},
             {"label": "All Focus Plus payments", "question": "q"}]
_SERI_OPTS = [{"label": "Spinning", "question": "x Sericulture spinning"},
              {"label": "Weaving", "question": "x Sericulture weaving"},
              {"label": "Both, shown separately", "question": "x both"}]


@pytest.mark.parametrize("opts,reply,idx", [
    (_GAP_OPTS, "houses sanctioned", 0), (_GAP_OPTS, "sanctioned", 0), (_GAP_OPTS, "completed", 1),
    (_GAP_OPTS, "the second one", 1), (_GAP_OPTS, "2", 1), (_GAP_OPTS, "last", 2),
    (_GAP_OPTS, "amount released please", 2), (_GAP_OPTS, "pmay houses sanctioned", 0),
    (_GAP_OPTS, "How many PMAY-G houses were completed in FY 2023-24?", 1),
    (_YEAR_OPTS, "2022-23", 0), (_YEAR_OPTS, "fy 2025-26", 1), (_YEAR_OPTS, "all years", 2),
    (_YEAR_OPTS, "all of them combined", 2),
    (_AMT_OPTS, "5000", 0), (_AMT_OPTS, "₹2,500", 1), (_AMT_OPTS, "tranch 1", 0), (_AMT_OPTS, "all payments", 2),
    (_SERI_OPTS, "weaving", 1), (_SERI_OPTS, "spinning one please", 0), (_SERI_OPTS, "both", 2),
])
def test_typed_reply_picks_the_one_matching_option(opts, reply, idx):
    assert p._resume_option_pause(reply, opts) == opts[idx]["question"]


@pytest.mark.parametrize("opts,reply", [
    (_GAP_OPTS, "houses"),                        # two options match: never guess
    (_GAP_OPTS, "yes"),                           # "yes" picks only a single option
    (_GAP_OPTS, "houses sanctioned in mgnrega"),  # names another scheme
    (_GAP_OPTS, "houses completed in west garo hills"),  # carries scope of its own
    (_GAP_OPTS, "thanks"), (_GAP_OPTS, "9"),
    (_YEAR_OPTS, "2023-24"),                      # a year that was not offered
])
def test_typed_reply_that_matches_no_single_option_picks_nothing(opts, reply):
    assert p._resume_option_pause(reply, opts) is None


def test_bare_yes_picks_a_single_option():
    one = [{"label": "Answer without a time filter", "question": "applications in EKH"}]
    assert p._resume_option_pause("yes please", one) == "applications in EKH"


def test_unmatched_reply_continues_the_paused_schemes_thread():
    paused = _GAP_OPTS[0]["question"]
    st = p._paused_thread_antecedent("houses completed in west garo hills", paused, _pd_turn())
    assert st is not None and st.schemes == ["PMAY-G"] and st.question == paused
    # its own words pick another scheme / name one / already that scheme: keep prev
    assert p._paused_thread_antecedent("person-days in EKH", paused, _pd_turn()) is None
    assert p._paused_thread_antecedent("houses in mgnrega", paused, _pd_turn()) is None
    pm = Turn(question=paused, raw_question=paused, route="data", schemes=["PMAY-G"])
    assert p._paused_thread_antecedent("houses completed", paused, pm) is None


def test_router_remembers_every_chip_pause_with_its_options():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    e = p.ClarificationNeeded("PMAY-G doesn't record person-days", options=_GAP_OPTS,
                              rule="swap-measure-unavailable")
    router.remember_pause(s, _GAP_OPTS[0]["question"], e)
    assert s.pending_scope_rule == "swap-measure-unavailable" and s.pending_scope_options == _GAP_OPTS
    assert not router.is_cacheable("houses sanctioned", s)
    # scope pauses are still merged, without options
    s2 = Session(session_id="t2", user_id="u", created=0.0, last_seen=0.0)
    router.remember_pause(s2, "q", p.ClarificationNeeded("which FY?", options=_YEAR_OPTS,
                                                         rule="year-not-specified"))
    assert s2.pending_scope_options is None and s2.pending_scope_rule in p.SCOPE_MERGE_RULES
    # a pause with no options (nothing to pick) is not remembered
    s3 = Session(session_id="t3", user_id="u", created=0.0, last_seen=0.0)
    router.remember_pause(s3, "q", p.ClarificationNeeded("Which one?", rule="reference-ambiguous"))
    assert s3.pending_scope_q is None


def test_measure_gap_pause_is_remembered_as_its_first_offer():
    s = _session(_pd_turn(), ConversationState(scheme="MGNREGA", year=2023, metric="person-days"))

    async def classifier(prompt, **_k):
        return '{"intent": "DATA"}'
    orig = llm.call_classifier
    llm.call_classifier = classifier
    try:
        with pytest.raises(p.ClarificationNeeded) as e:
            asyncio.run(p._run_pipeline("give me for pmay", session=s))
    finally:
        llm.call_classifier = orig
    assert router.pause_question(s, "give me for pmay") == _GAP_OPTS[0]["question"]
    assert e.value.options == _GAP_OPTS


def _resume(reply, rule, options, paused):
    s = _session(_pd_turn(), ConversationState(scheme="MGNREGA", year=2023, metric="person-days"))
    s.pending_scope_q, s.pending_scope_rule, s.pending_scope_options = paused, rule, list(options)
    seen = {}

    async def classifier(prompt, **_k):
        if "Standalone question:" in prompt:
            seen["rewrite_prompt"] = prompt
            return "How many PMAY-G houses were completed in West Garo Hills in FY 2023-24?"
        return '{"intent": "DATA"}'

    async def fake_answer_data(question, **kw):
        seen["data"] = question
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["PMAY-G"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}
    orig = (llm.call_classifier, p._answer_data)
    llm.call_classifier, p._answer_data = classifier, fake_answer_data
    try:
        try:
            asyncio.run(p._run_pipeline(reply, session=s))
        except p.ClarificationNeeded as e:
            seen["rule"] = e.rule
    finally:
        llm.call_classifier, p._answer_data = orig
    return seen, s


def test_typed_houses_sanctioned_after_the_measure_gap_runs_the_pmay_chip():
    seen, s = _resume("houses sanctioned", "swap-measure-unavailable", _GAP_OPTS, _GAP_OPTS[0]["question"])
    assert seen.get("data") == "How many PMAY-G houses were sanctioned in FY 2023-24?"
    assert "MGNREGA" not in seen["data"] and s.pending_scope_q is None


def test_typed_year_after_year_out_of_range_runs_that_year():
    seen, _ = _resume("2022-23", "year-out-of-range", _YEAR_OPTS, "total for Focus Plus in 2023-24")
    assert seen.get("data") == "total for Focus Plus for FY 2022-23"
