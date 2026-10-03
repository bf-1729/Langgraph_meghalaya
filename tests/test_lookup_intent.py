"""
Existence / name-lookup questions must route to DATA, not RAG.

Reported 2026-09-22: "Is there any Producer Group named as Sakania PG?" was
answered from the reference docs —

    "No, there is no mention of a specific Producer Group named 'Sakania PG' in
     the provided reference material."

— which is true of the PROSE and beside the point. The group exists: querying
curated.v_focus_legacy for `pg_name ILIKE '%sakania%'` returns
PG-FOCUS-EKH-2245, "Sakania Pg", EAST KHASI HILLS. The SME use-case bank lists
this exact shape (TC-13, "Is there any Producer Group named Nongstoin PG?") as
answer_route: sql.

Cause: classify_intent's keyword fast-path had no cue for a lookup. Every DATA
cue was a counting/aggregation word (how many, total, by district, ...), and a
question asking whether one named record exists has none of them —

    _DATA_HINTS      -> None
    _KNOWLEDGE_HINTS -> None

so it fell through to the LLM classifier, which guessed KNOWLEDGE.

Everything downstream was already correct — the schema context carries the
`pg_name ILIKE` rule and few-shot retrieval returns "Find the producer group
called Muskan" as the top exemplar for this question. Only the route was wrong.
"""
import pytest

from app.pipeline import _DATA_HINTS, _KNOWLEDGE_HINTS


def _fast_path_routes_to_data(question: str) -> bool:
    """The `_DATA_HINTS and not _KNOWLEDGE_HINTS` branch of classify_intent —
    the one that decides without a model call."""
    return bool(_DATA_HINTS.search(question)) and not bool(
        _KNOWLEDGE_HINTS.search(question)
    )


# ── the reported question, and its siblings ────────────────────────────────
@pytest.mark.parametrize(
    "question",
    [
        "Is there any Producer Group named as Sakania PG?",
        "Is there any Producer Group named Nongstoin PG?",      # TC-13 verbatim
        "Is there a producer group called Muskan?",
        "Are there any producer groups named Bak 15 Banana?",
        "Find the producer group called Muskan",                # few-shot verbatim
        "Search for the producer group Iainehlang",
        "Look up the producer group Nongprat Lynti",
    ],
)
def test_producer_group_lookups_route_to_data(question):
    assert _fast_path_routes_to_data(question), (
        "a lookup for a named record is a database question; answering it from "
        "the reference docs reports the DOCS' silence as the scheme's"
    )


def test_the_generic_existence_shape_is_covered():
    """Not PG-specific — the same shape applies to any stored value."""
    assert _fast_path_routes_to_data("is there a village called Umsaw?")
    assert _fast_path_routes_to_data("Is there any block named Mairang?")


# ── the guard: a naming word is required, so prose questions are untouched ──
@pytest.mark.parametrize(
    "question",
    [
        "What is a Producer Group?",
        "What is the role of Producer Groups under FOCUS?",
        "Who is eligible for FOCUS?",
        "What documents are required?",
        "is there any eligibility criteria?",
        "Is there any age limit?",
    ],
)
def test_knowledge_questions_do_not_become_data(question):
    """The new cues all require "named"/"called"/"by the name of". A question
    that names nothing keeps its old routing."""
    assert not _fast_path_routes_to_data(question)


def test_existence_cue_needs_a_naming_word():
    """"is there any X" alone must NOT be enough — that shape is common in
    genuine eligibility/rules questions."""
    assert not _DATA_HINTS.search("is there any upper age limit")
    assert _DATA_HINTS.search("is there any group named Sakania")


# ── end-to-end through the real fast path ──────────────────────────────────
@pytest.mark.asyncio
async def test_classify_intent_returns_data_for_the_reported_question():
    from app.pipeline import classify_intent

    # Resolved by the keyword fast path, so no model call is made.
    assert await classify_intent(
        "Is there any Producer Group named as Sakania PG?"
    ) == "DATA"


@pytest.mark.asyncio
async def test_classify_intent_still_returns_knowledge_for_prose():
    from app.pipeline import classify_intent

    assert await classify_intent("Who is eligible for FOCUS?") == "KNOWLEDGE"


# ── the SQL side was already right; keep it that way ───────────────────────
def test_sql_guidance_for_a_name_lookup_is_present():
    from app.schema_context import build_schema_context

    ctx = build_schema_context(["Focus Legacy"])
    assert "pg_name ILIKE" in ctx, "no guidance on how to search a group name"
    assert "COUNT(DISTINCT pg_name)" in ctx or "NEVER GROUP BY pg_name" in ctx


def test_the_name_lookup_exemplar_is_retrieved_for_this_question():
    from app import annotations

    annotations.load_all()
    top = annotations.few_shot_examples(
        ["Focus Legacy"], "Is there any Producer Group named as Sakania PG?", top_k=3
    )
    assert any("producer group called" in e["question"].lower() for e in top), (
        "the pg_name ILIKE exemplar is not retrieved for a name-lookup question"
    )
