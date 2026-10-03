"""
Follow-up context from STRUCTURED state, not a character slice of the previous
answer (2026-09-26).

Before: rewrite_followup put `prev.answer[:300]` into the classifier prompt on
every follow-up, and told the model it could take districts, years and schemes
from it. Measured with the real prompt code: every follow-up carried answer
text, and a long answer's first 300 characters carried four unrelated block
names (Dalu, Rongram, Tura, Phulbari) into "How many beneficiaries were there?".

Now (context_manager.build_rewrite_evidence + pipeline.rewrite_followup):
  Tier 1  PREVIOUS filters — the previous turn's schemes and resolved entities.
          Structured, always sent.
  Tier 2  PREVIOUS result — a deterministic summary of the previous result ROWS
          (context_manager.summarize_result). Sent only when the follow-up
          points into that result ("the top one", "that block").
  Tier 3  a sentence-bounded excerpt of the previous answer. Only when the
          follow-up points into the result AND there were no rows to summarise
          (a knowledge answer).
  Guard   a rewrite that introduces a scheme, district or block that no
          permitted source contains is discarded, and the original question
          goes on (carried by inject_scheme_hint + prior_resolved). A name can no
          longer enter the question just because earlier text mentioned it.

Also covered: a failed data turn does not overwrite the last good state; a
typed reply to the "which scheme?" / "which Focus?" pause resumes the paused
question; prompt make-up is logged per call.

Pure Python — the classifier is stubbed; no DB, model or KB.
    .venv/Scripts/python.exe -m pytest tests/test_context_semantic_state.py -q
"""
import asyncio
import inspect
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import context_manager as cm  # noqa: E402
from app import entity_resolver, llm, pipeline as p, prompt_builder  # noqa: E402
from app.session_store import ConversationState, Session, Turn  # noqa: E402

Q1 = "How much was disbursed under Focus Plus in West Garo Hills in FY 2024-25?"
LONG_ANSWER = ("Focus Plus disbursed Rs 12,34,567 in West Garo Hills in FY 2024-25. "
               + " ".join(f"Block {n} received Rs {1000 + i},000 across {i} beneficiaries."
                          for i, n in enumerate(["Dalu", "Rongram", "Tura", "Phulbari",
                                                 "Dadenggre", "Selsella"] * 30)))
ROWS = [{"block_name_raw": n, "amount_raw": 1000 * (20 - i)}
        for i, n in enumerate(["Dalu", "Rongram", "Tura", "Phulbari", "Dadenggre"])]


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


def _prev(answer=LONG_ANSWER, schemes=("Focus Plus",), resolved=None, question=Q1,
          route="data", result_summary=None):
    return Turn(question=question, raw_question=question, route=route, schemes=list(schemes),
                resolved_entities=({"district": "WEST GARO HILLS", "year_key": 2024}
                                   if resolved is None else resolved),
                answer=answer,
                result_summary=(cm.summarize_result({"route": "data", "rows": ROWS,
                                                     "sql": "SELECT 1"})
                                if result_summary is None else result_summary))


def _rewrite(monkeypatch, followup, prev, model_reply, extra_context=""):
    """Run the real rewrite_followup with the classifier stubbed; return
    (result, prompt the model saw)."""
    seen = []

    async def classifier(prompt, **_kw):
        seen.append(prompt)
        return model_reply

    monkeypatch.setattr(llm, "call_classifier", classifier)
    out = asyncio.run(p.rewrite_followup(followup, prev, extra_context=extra_context))
    return out, (seen[0] if seen else "")


# ── Tier 1: structured filters replace the answer slice ─────────────────────
def test_basic_continuation_uses_filters_not_the_answer(monkeypatch):
    reply = "How much was disbursed under Focus Plus in Dalu block of West Garo Hills in FY 2024-25?"
    out, prompt = _rewrite(monkeypatch, "What about Dalu block?", _prev(), reply)
    assert out == reply
    assert "PREVIOUS filters: scheme=Focus Plus; district=WEST GARO HILLS; year=FY 2024-25" in prompt
    assert "PREVIOUS answer" not in prompt
    assert "12,34,567" not in prompt and "Block Rongram" not in prompt


def test_metric_change_keeps_scope_and_sends_no_result(monkeypatch):
    reply = "How many beneficiaries were there under Focus Plus in West Garo Hills in FY 2024-25?"
    out, prompt = _rewrite(monkeypatch, "How many beneficiaries were there?", _prev(), reply)
    assert out == reply
    assert "PREVIOUS result" not in prompt


def test_time_change_last_year(monkeypatch):
    state = ConversationState(scheme="Focus Plus", district="WEST GARO HILLS", year=2024)
    followup = cm.substitute_references("What about last year?", state)
    assert followup == "What about FY 2023-24?"
    reply = "How much was disbursed under Focus Plus in West Garo Hills in FY 2023-24?"
    out, _ = _rewrite(monkeypatch, followup, _prev(), reply)
    assert out == reply     # a changed year is the follow-up's own change, never a violation


def test_geography_change_named_in_the_followup_is_accepted(monkeypatch):
    reply = "How much was disbursed under Focus Plus in South Garo Hills in FY 2024-25?"
    out, _ = _rewrite(monkeypatch, "What about South Garo Hills?", _prev(), reply)
    assert out == reply


def test_district_nobody_named_is_rejected(monkeypatch):
    bad = "How much was disbursed under Focus Plus in East Garo Hills in FY 2024-25?"
    out, _ = _rewrite(monkeypatch, "What about South Garo Hills?", _prev(), bad)
    assert out == "What about South Garo Hills?"


def test_district_abbreviation_in_the_followup_counts_as_naming_it(monkeypatch):
    reply = "How much was disbursed under Focus Plus in East Khasi Hills in FY 2024-25?"
    out, _ = _rewrite(monkeypatch, "what about ekh?", _prev(), reply)
    assert out == reply


def test_aggregation_change(monkeypatch):
    reply = "Show Focus Plus disbursement in FY 2024-25 by district"
    out, prompt = _rewrite(monkeypatch, "Show it by district.", _prev(), reply)
    assert out == reply
    assert "PREVIOUS result" not in prompt


# ── Long / irrelevant previous answers ─────────────────────────────────────
def test_long_previous_answer_is_never_sent(monkeypatch):
    prev = _prev(answer=LONG_ANSWER * 10)
    _, prompt = _rewrite(monkeypatch, "What about Dalu block?", prev,
                         "How much was disbursed under Focus Plus in Dalu block in FY 2024-25?")
    assert len(LONG_ANSWER * 10) > 30_000
    assert len(prompt) < 2_000
    assert "received Rs" not in prompt


def test_names_only_in_the_previous_answer_are_not_injected(monkeypatch):
    bad = "How many beneficiaries were there under Focus Plus in Rongram block in FY 2024-25?"
    out, _ = _rewrite(monkeypatch, "How many beneficiaries were there?", _prev(), bad)
    assert out == "How many beneficiaries were there?"


def test_pointing_into_the_result_sends_the_rows_and_allows_their_names(monkeypatch):
    reply = "How much was disbursed under Focus Plus in Dalu block in FY 2024-25?"
    out, prompt = _rewrite(monkeypatch, "What about the top one?", _prev(), reply)
    assert "PREVIOUS result: 5 rows by block_name_raw, in result order: Dalu (20000)" in prompt
    assert out == reply


def test_the_block_the_user_names_replaces_the_previous_block(monkeypatch):
    prev = _prev(resolved={"district": "WEST GARO HILLS", "block": "DADENGGRE", "year_key": 2024})
    reply = "How much was disbursed under Focus Plus in Rongram block in FY 2024-25?"
    out, _ = _rewrite(monkeypatch, "What about Rongram?", prev, reply)
    assert out == reply


def test_knowledge_answer_falls_back_to_a_sentence_bounded_excerpt(monkeypatch):
    ans = ("Focus Plus has three components. The first is a cash benefit. "
           + "The second is training for producer collectives. " * 40)
    prev = _prev(answer=ans, route="knowledge", resolved={}, result_summary="",
                 question="What are the components of Focus Plus?")
    reply = "Explain the second component of Focus Plus"
    out, prompt = _rewrite(monkeypatch, "explain the second one", prev, reply)
    assert out == reply
    assert "PREVIOUS answer (excerpt): \"Focus Plus has three components." in prompt
    excerpt = prompt.split("PREVIOUS answer (excerpt): \"", 1)[1].split("\"\n", 1)[0]
    assert excerpt.endswith(".")                        # whole sentences, never a mid-word cut
    assert len(excerpt) // 4 <= p.settings.CONTEXT_PREV_ANSWER_MAX_TOKENS


# ── Scheme boundaries ──────────────────────────────────────────────────────
@pytest.mark.parametrize("scheme", list(p.SCHEME_CATALOG))
def test_every_scheme_keeps_its_own_scope(monkeypatch, scheme):
    other = "MGNREGA" if scheme != "MGNREGA" else "PMAY-G"
    prev = _prev(schemes=(scheme,), question=f"Total amount under {scheme} in FY 2024-25?",
                 resolved={"year_key": 2024})
    ok = f"Total amount under {scheme} in FY 2024-25 by district"
    out, prompt = _rewrite(monkeypatch, "Show it by district.", prev, ok)
    assert f"scheme={scheme}" in prompt and out == ok
    leaked = f"Total amount under {other} in FY 2024-25 by district"
    out, _ = _rewrite(monkeypatch, "Show it by district.", prev, leaked)
    assert out == "Show it by district."


def test_deterministic_scheme_swap_is_unchanged(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("scheme swap must not call the model")
    monkeypatch.setattr(llm, "call_classifier", boom)
    out = asyncio.run(p.rewrite_followup("in MGNREGA?", _prev()))
    assert "MGNREGA" in out and "Focus Plus" not in out


def test_inherited_values_stay_inside_their_scheme():
    """merged_prior_resolved offers a Focus Plus tranche to every follow-up;
    resolve_entities only takes it for a Focus-Plus-only question."""
    state = ConversationState(scheme="Focus Plus", tranche="Tranch 2 - August", year=2024)
    merged = cm.merged_prior_resolved({}, state)
    assert merged["tranche_label"] == "Tranch 2 - August"
    src = inspect.getsource(p.resolve_entities)
    assert 'schemes == ["Focus Plus"] and "tranche_label" not in resolved' in src


# ── State commits only after a successful data turn ─────────────────────────
def test_failed_data_turn_does_not_corrupt_last_good_state():
    s = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    cm.update_state(s, Q1, Q1, {"route": "data", "schemes": ["Focus Plus"], "sql": "SELECT 1",
                               "resolved_entities": {"district": "WEST GARO HILLS",
                                                     "year_key": 2024}})
    assert s.state.metric == "disbursement"
    failed = {"route": "data", "intent": "DATA", "confidence": "low", "schemes": [],
              "resolved_entities": {}, "sql": None, "rows": [],
              "answer": "I understood the question but couldn't build a working query ..."}
    cm.update_state(s, "compare person-days between Ri Bhoi and East Khasi Hills",
                    "compare person-days between Ri Bhoi and East Khasi Hills", failed)
    assert s.state.scheme == "Focus Plus"
    assert s.state.district == "WEST GARO HILLS" and s.state.year == 2024
    assert s.state.metric == "disbursement"
    assert s.state.comparison_entities == []
    assert s.state.turn_count == 2            # the turn itself is still recorded


# ── Tier 2 summary ─────────────────────────────────────────────────────────
def test_summarize_result_shapes():
    one = cm.summarize_result({"route": "data", "sql": "S",
                               "rows": [{"amount_raw": 1234567, "beneficiaries": 88}]})
    assert one == "1 row: amount_raw=1234567; beneficiaries=88"
    many = cm.summarize_result({"route": "data", "sql": "S", "rows": ROWS}, max_tokens=20)
    assert many.startswith("5 rows by block_name_raw, in result order: Dalu (20000)")
    assert "more" in many and len(many) // 4 <= 20 + 5
    years = cm.summarize_result({"route": "data", "sql": "S",
                                 "rows": [{"year_key": 2023, "amount": 5}, {"year_key": 2024, "amount": 7}]})
    assert years == "2 rows by year_key, in result order: 2023 (5); 2024 (7)"
    assert cm.summarize_result({"route": "data", "sql": "S", "rows": []}) == ""
    assert cm.summarize_result({"route": "knowledge", "rows": ROWS}) == ""
    assert cm.summarize_result(None) == ""


@pytest.mark.parametrize("q,expected", [
    ("What about the top one?", True), ("and the second block?", True),
    ("that district", True), ("explain the second one", True),
    ("How many beneficiaries were there?", False), ("Show it by district.", False),
    ("What about Dalu block?", False), ("Which one had the highest?", False),
])
def test_result_reference_cue(q, expected):
    assert cm.references_previous_result(q) is expected


# ── Clarification: a typed reply resumes the paused question ────────────────
def test_typed_scheme_reply_resumes_the_scheme_pause():
    opts = p._scheme_clarification("Show beneficiaries").options
    assert p._resume_scheme_pause("Focus Plus", opts) == "Show beneficiaries for Focus Plus"
    assert p._resume_scheme_pause("focus plus please", opts) == "Show beneficiaries for Focus Plus"
    assert p._resume_scheme_pause("CM Elevate Legacy", opts) == "Show beneficiaries for CM Elevate Legacy"
    assert p._resume_scheme_pause("CM Elevate", opts) == "Show beneficiaries for CM Elevate"
    # a fresh question, or several schemes, is not a scheme pick — unchanged behaviour
    assert p._resume_scheme_pause("How many job cards in Ri Bhoi?", opts) is None
    assert p._resume_scheme_pause("MGNREGA and PMAY-G", opts) is None
    assert p._resume_scheme_pause("thanks", opts) is None


def test_typed_reply_resumes_the_which_focus_pause():
    opts = p._focus_ambiguity_clarification("total focus disbursement").options
    assert p._resume_scheme_pause("Focus Legacy", opts) == "total Focus Legacy disbursement"
    assert p._resume_scheme_pause("legacy", opts) == "total Focus Legacy disbursement"
    assert p._resume_scheme_pause("plus", opts) == "total Focus Plus disbursement"
    assert p._resume_scheme_pause("MGNREGA", opts) is None      # not one of the offered two


def test_scheme_pause_resume_end_to_end(monkeypatch):
    """Q1 "Show beneficiaries" -> "Which scheme?" -> Q2 "Focus Plus" answers
    exactly what the Focus Plus chip would have asked."""
    seen = {}

    async def fake_answer_data(question, **kw):
        seen["q"], seen["kw"] = question, kw
        return {"route": "data", "intent": "DATA", "answer": "ok", "schemes": ["Focus Plus"],
                "resolved_entities": {}, "sql": "SELECT 1", "rows": [], "data": []}

    async def classifier(prompt, **_kw):
        return '{"intent": "DATA"}'

    monkeypatch.setattr(p, "_answer_data", fake_answer_data)
    monkeypatch.setattr(llm, "call_classifier", classifier)
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    clar = p._scheme_clarification("Show beneficiaries")
    sess.pending_scope_q, sess.pending_scope_rule = "Show beneficiaries", clar.rule
    sess.pending_scope_options = clar.options
    asyncio.run(p._run_pipeline("Focus Plus", session=sess))
    assert seen["q"] == "Show beneficiaries for Focus Plus"
    assert seen["kw"]["skip_scope_clarify"] is False    # same as clicking the chip
    assert sess.pending_scope_q is None and sess.pending_scope_rule is None
    assert sess.pending_scope_options is None


def test_router_never_caches_a_pause_reply():
    """The real router helpers. "focus plus please" doesn't look like a
    follow-up, so it was cacheable on its own; while a pause is pending it
    must not be. The scheme pause is remembered with its options."""
    from app.routers import query as router
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    assert router.is_cacheable("focus plus please", sess) is True
    router.remember_pause(sess, "Show beneficiaries", p._scheme_clarification("Show beneficiaries"))
    assert sess.pending_scope_rule == "scheme-not-specified" and sess.pending_scope_options
    assert router.is_cacheable("focus plus please", sess) is False
    assert router.is_cacheable("for the district of West Garo Hills in the year 2023-24",
                               sess) is False


# ── Observability ──────────────────────────────────────────────────────────
def test_sql_prompt_is_unchanged_and_its_make_up_is_logged(caplog):
    er = {"resolved": {"district": "WEST GARO HILLS", "year_key": 2024}, "notes": []}
    schemes = ["Focus Plus"]
    q = "How much was disbursed in West Garo Hills in FY 2024-25?"
    catalog = prompt_builder.schema_introspect.catalog_block(schemes)
    expected = "".join([
        prompt_builder.build_schema_context(schemes), "\n\n",
        prompt_builder._live_schema_block(schemes),
        (catalog + "\n") if catalog else "",
        prompt_builder._prohibited_block(schemes),
        prompt_builder._fewshot_block(schemes, prompt_builder._fewshot_ranking_text(q, er)),
        prompt_builder._common_mistakes_block(schemes),
        prompt_builder._entities_block(er, q, schemes),
        f"\nThe user's question is about: {', '.join(schemes)}.\n",
        f'\nQuestion: "{q}"\nSQL:',
    ])
    with caplog.at_level(logging.INFO, logger="app.context_budget"):
        got = prompt_builder.build_sql_prompt(q, schemes, er)
    assert got == expected
    recs = [r.getMessage() for r in caplog.records if "prompt_context" in r.getMessage()]
    assert recs and '"kind": "sql"' in recs[-1]
    assert '"resolved_entities"' in recs[-1] and '"schema_backbone"' in recs[-1]
    assert "WEST GARO HILLS" not in recs[-1]            # sizes only, never prompt text


def test_rewrite_logs_its_tiers_and_guard(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="app.context_budget"):
        _rewrite(monkeypatch, "How many beneficiaries were there?", _prev(),
                 "How many beneficiaries were there under Focus Plus in Rongram block?")
    rec = [r.getMessage() for r in caplog.records if '"kind": "rewrite"' in r.getMessage()][-1]
    assert '"tiers": ["filters"]' in rec
    assert '"guard": "rejected:block"' in rec


# ── Backward compatibility ─────────────────────────────────────────────────
def test_old_turns_and_bare_prev_objects_still_work(monkeypatch):
    class _Bare:                      # no result_summary / resolved_entities / schemes
        question, route, answer = Q1, "data", "whatever"
    reply = "How much was disbursed under Focus Plus in Dalu block in FY 2024-25?"
    out, prompt = _rewrite(monkeypatch, "What about Dalu block?", _Bare(), reply)
    assert out == reply and "PREVIOUS filters:" not in prompt   # no Tier-1 line to render
    t = Turn(question="q", raw_question="q", route="data")
    assert t.result_summary == ""
    s = Session(session_id="t", user_id=None, created=0.0, last_seen=0.0)
    assert s.pending_scope_rule is None and s.pending_scope_options is None
    assert ConversationState.from_dict({"scheme": "MGNREGA"}).scheme == "MGNREGA"


def test_first_turn_never_calls_the_model(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("no previous turn -> no rewrite call")
    monkeypatch.setattr(llm, "call_classifier", boom)
    assert asyncio.run(p.rewrite_followup("What about Dalu block?", None)) == "What about Dalu block?"
