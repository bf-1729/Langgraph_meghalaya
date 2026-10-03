"""
Scheme substitution: "give me same for <another scheme>" is the previous
OPERATION on another scheme (reported live 2026-09-26).

The reported conversation:
    Q1  how many beneficiaries in focus + for all of Meghalaya across all financial years
        -> 105,813 beneficiaries / 385,671 payments (correct)
    Q2  give me same for mgnrega
        -> general MGNREGA information from the reference docs (wrong)

Traced live: looks_like_followup() returned False. The message names a scheme,
and _SCHEME_SWAP_FOLLOWUP only knew a fixed set of openings ("same for",
"what about", ...), not "give me same for". So no rewrite ran, the intent
classifier saw the bare words with no context, returned KNOWLEDGE, and RAG
answered.

Now pipeline._scheme_substitution / is_scheme_substitution recognise any
continuation wording plus exactly one scheme from _SCHEME_NAME_PATTERN (the
registry). A pure one reuses the deterministic _scheme_swap_rewrite on the
previous question, so intent, metric, geography, time and grouping carry over
verbatim. The merge plan labels it SCHEME_SUBSTITUTION, and the router keeps it
out of the shared cache.
Pure Python — the classifier is stubbed; no DB, model or KB.
    .venv/Scripts/python.exe -m pytest tests/test_scheme_substitution.py -q
"""
import asyncio
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import context_policy as cp  # noqa: E402
from app import entity_resolver, llm, pipeline as p  # noqa: E402
from app.routers import query as router  # noqa: E402
from app.session_store import ConversationState, Session, Turn  # noqa: E402

SCHEMES = list(p.SCHEME_CATALOG)
CUES = ["same for {s}", "do the same for {s}", "same thing for {s}", "give me same for {s}",
        "Give me the same for {s}.", "what about {s}?", "now for {s}", "same query for {s} please"]


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


def _other(scheme):
    return next(s for s in SCHEMES if s != scheme and not s.startswith(scheme.split()[0]))


# ── Detection is registry-driven and phrasing-generic ───────────────────────
@pytest.mark.parametrize("cue", CUES)
@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_cue_and_every_scheme_is_a_pure_substitution(cue, scheme):
    q = cue.format(s=scheme)
    assert p._scheme_substitution(q) == (scheme, True)
    assert p.looks_like_followup(q)
    prev = Turn(question="x", raw_question="x", route="data", schemes=[_other(scheme)])
    assert p.is_scheme_substitution(q, prev)


@pytest.mark.parametrize("q", [
    "Give me the same number by district.",            # grouping change, no scheme
    "Give me the same information but for last year.",  # time change
    "Give me the same for Dalu.",                       # geography change
    "What is MGNREGA?",                                 # informational
    "tell me about PMAY-G",
    "compare Focus Plus and MGNREGA",                   # two schemes: a comparison
    "how many beneficiaries in CM Elevate?",            # a new question naming a scheme
])
def test_negative_cases_are_not_substitutions(q):
    prev = Turn(question="How many beneficiaries under Focus Plus?", raw_question="", route="data",
                schemes=["Focus Plus"])
    assert not p.is_scheme_substitution(q, prev)


def test_weak_cue_with_extra_content_keeps_its_old_reading():
    prev = Turn(question="x", raw_question="x", route="data", schemes=["Focus Plus"])
    assert p._scheme_substitution("what about MGNREGA eligibility?") == ("MGNREGA", False)
    assert not p.is_scheme_substitution("what about MGNREGA eligibility?", prev)


def test_naming_the_current_scheme_is_not_a_substitution():
    prev = Turn(question="x", raw_question="x", route="data", schemes=["Focus Plus"])
    assert not p.is_scheme_substitution("same for Focus Plus", prev)


def test_no_antecedent_no_substitution():
    assert not p.is_scheme_substitution("give me same for MGNREGA", None)
    edge = Turn(question="hi", raw_question="hi", route="edge", schemes=[])
    assert not p.is_scheme_substitution("give me same for MGNREGA", edge)


# ── The deterministic rewrite keeps everything but the scheme ───────────────
PREVIOUS = [
    # (shape, previous question naming {s})
    ("metric", "how many beneficiaries in {s} for all of Meghalaya across all financial years"),
    ("money+geo+year", "How much was disbursed under {s} in West Garo Hills in FY 2024-25?"),
    ("grouped", "Show {s} beneficiaries by district in FY 2024-25."),
    ("aggregate", "What is the total amount disbursed under {s} across all financial years?"),
    ("result-reference", "How many beneficiaries were there under {s} in the district with the highest count?"),
]


@pytest.mark.parametrize("shape,prev_q", PREVIOUS)
@pytest.mark.parametrize("pair", [("Focus Plus", "MGNREGA"), ("MGNREGA", "PMAY-G"),
                                  ("PMAY-G", "CM Elevate Legacy"), ("Focus Legacy", "CM Elevate"),
                                  ("CM Elevate", "Focus Plus")])
def test_rewrite_swaps_only_the_scheme(shape, prev_q, pair):
    old, new = pair
    before = prev_q.format(s=old)
    out = p._scheme_swap_rewrite(before, f"give me same for {new}")
    assert out == prev_q.format(s=new)                      # every other word unchanged
    assert not p._exact_schemes(out) - {new}


def test_previous_question_without_a_scheme_name_gets_it_appended():
    out = p._scheme_swap_rewrite("How many beneficiaries in West Garo Hills?", "do the same for MGNREGA")
    assert out == "How many beneficiaries in West Garo Hills for MGNREGA"


def test_knowledge_antecedent_is_swapped_too():
    assert p._scheme_swap_rewrite("how to apply for cm elevate", "give me same for pmay-g") == \
        "how to apply for PMAY-G"


# ── Through the real pipeline ──────────────────────────────────────────────
def _run(prev_turn, state, followup, *, model_rewrite="unused"):
    seen = {}

    async def classifier(prompt, **_kw):
        if "Standalone question:" in prompt:
            return model_rewrite
        return '{"intent": "KNOWLEDGE"}'   # what the live classifier said for the bare words

    async def fake_answer_data(question, **kw):
        seen["q"], seen["prior"] = question, kw.get("prior_resolved") or {}
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["MGNREGA"],
                "sql": "S", "rows": [], "data": [], "resolved_entities": {}}

    async def kb(*_a, **_k):
        seen["kb"] = True
        return {"answer": "general info", "confidence": "medium", "sources": []}

    import app.rag as rag
    orig = (llm.call_classifier, p._answer_data, rag.answer_from_kb)
    llm.call_classifier, p._answer_data, rag.answer_from_kb = classifier, fake_answer_data, kb
    try:
        s = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
        s.turns, s.state = [prev_turn], state
        res = asyncio.run(p._run_pipeline(followup, session=s))
        seen["route"], seen["plan"] = res.get("route"), s.turn_context.get("plan") or {}
    finally:
        llm.call_classifier, p._answer_data, rag.answer_from_kb = orig
    return seen


def test_reported_conversation_stays_on_the_data_path():
    q1 = "how many beneficiaries in focus + for all of Meghalaya across all financial years"
    seen = _run(Turn(question=q1, raw_question=q1, route="data", schemes=["Focus Plus"]),
                ConversationState(scheme="Focus Plus", metric="beneficiaries"),
                "give me same for mgnrega")
    assert "kb" not in seen and seen["route"] == "data"
    assert seen["q"] == "how many beneficiaries in MGNREGA for all of Meghalaya across all financial years"
    assert seen["plan"]["kind"] == cp.SCHEME_SUBSTITUTION
    assert {k for k, v in seen["plan"]["actions"].items() if v != "KEEP"} == {"scheme"}


def test_geography_and_year_are_inherited():
    q1 = "How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?"
    seen = _run(Turn(question=q1, raw_question=q1, route="data", schemes=["Focus Plus"],
                     resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024}),
                ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024),
                "Give me same for MGNREGA.")
    assert seen["q"] == "How much was disbursed under MGNREGA in West Garo Hills in FY 2024-25?"
    assert seen["prior"]["district"] == "WEST GARO HILLS" and seen["prior"]["year_key"] == 2024


def test_grouping_is_kept():
    q1 = "Show Focus Plus beneficiaries by district in FY 2024-25."
    seen = _run(Turn(question=q1, raw_question=q1, route="data", schemes=["Focus Plus"],
                     resolved_entities={"year_key": 2024}),
                ConversationState(scheme="Focus Plus", year=2024), "Do the same for MGNREGA.")
    assert seen["q"] == "Show MGNREGA beneficiaries by district in FY 2024-25."
    assert seen["prior"]["year_key"] == 2024


def test_substitution_with_its_own_change_goes_through_the_checked_rewrite():
    q1 = "How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?"
    seen = _run(Turn(question=q1, raw_question=q1, route="data", schemes=["Focus Plus"],
                     resolved_entities={"district": "WEST GARO HILLS", "year_key": 2024}),
                ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024),
                "give me the same for mgnrega in 2023-24",
                model_rewrite="How much was disbursed under MGNREGA in West Garo Hills in FY 2023-24?")
    assert seen["q"] == "How much was disbursed under MGNREGA in West Garo Hills in FY 2023-24?"
    assert seen["plan"]["kind"] == cp.SCHEME_SUBSTITUTION
    assert seen["plan"]["actions"]["year"] == "REPLACE"
    assert "year_key" not in seen["prior"] and seen["prior"]["district"] == "WEST GARO HILLS"


def test_substitutions_are_never_cached():
    s = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    s.turns = [Turn(question="How many beneficiaries under Focus Plus?", raw_question="",
                    route="data", schemes=["Focus Plus"])]
    assert router.is_cacheable("give me same for mgnrega", s) is False
    assert router.is_cacheable("give me the same for mgnrega in 2023-24", s) is False
    assert router.is_cacheable("What is MGNREGA?", s) is True


def test_a_seventh_scheme_works_without_code_changes(monkeypatch):
    monkeypatch.setitem(p._SCHEME_NAME_PATTERN, "Test Scheme",
                        re.compile(r"\btest\s+scheme\b", re.IGNORECASE))
    monkeypatch.setitem(p.SCHEME_CATALOG, "Test Scheme", "synthetic")
    assert p._scheme_substitution("give me same for Test Scheme") == ("Test Scheme", True)
    assert p._scheme_swap_rewrite("How many beneficiaries under MGNREGA?", "give me same for test scheme") == \
        "How many beneficiaries under Test Scheme?"   # the registry's canonical name
