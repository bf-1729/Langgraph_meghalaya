"""
"PG" is the scheme's everyday shorthand and must be recognised everywhere
"producer group" is.

Reported 2026-09-23: "is there any pg group with name sakania?" was answered

    "No — that's outside what I cover. I'm Megh One AI, the assistant for
     Meghalaya's MGNREGA, PMAY-G, Focus Plus, CM Elevate and Focus Legacy
     (producer groups) schemes..."

i.e. it named "producer groups" as covered in the same sentence that refused a
producer-group question. The group is real: PG-FOCUS-EKH-2245, "Sakania Pg",
EAST KHASI HILLS, 12 members.

Only the ABBREVIATION failed — the spelled-out form always worked:

    "is there any pg group with name sakania?"        -> off_topic   <-- reported
    "is there any producer group with name sakania?"  -> fine
    "Is there any Producer Group named Sakania PG?"   -> fine

Two registries carried the spelled-out forms but not a standalone "pg"/"pgs":

  * edge._DOMAIN_WORDS — the off-topic whitelist had "producer group", "pg id",
    "pg member", "pg-focus", "pg-lamp". With no domain word matched, the gate
    bounced the question before routing ever ran.
  * pipeline._DATA_HINTS — the name-lookup cue spelled out "producer group", so
    "pg group ... with name" was not recognised as a lookup either.

The pattern must be WORD-BOUNDED. _DOMAIN_WORDS entries are used as unanchored
regexes (`re.search(w, ql)`), so a bare "pg" matches inside "upgrade", "mpg" and
"upgrading" and would let genuinely off-topic questions through.
"""
import re

import pytest

from app import edge
from app.pipeline import _DATA_HINTS, _KNOWLEDGE_HINTS, _infer_scheme_from_terms


def _fast_path_route(question: str) -> str:
    """What the pipeline decides before any model call."""
    hit = edge.detect_edge_case(question)
    if hit:
        return f"EDGE:{hit['type']}"
    data = bool(_DATA_HINTS.search(question))
    know = bool(_KNOWLEDGE_HINTS.search(question))
    if data and not know:
        return "DATA"
    if know and not data:
        return "KNOWLEDGE"
    return "classifier"


# ── the reported question ───────────────────────────────────────────────────
def test_the_reported_question_is_not_off_topic():
    q = "is there any pg group with name sakania?"
    assert edge.detect_edge_case(q) is None, (
        "the edge whitelist bounced a producer-group question while its own "
        "refusal text lists producer groups as covered"
    )
    assert _fast_path_route(q) == "DATA"


@pytest.mark.parametrize(
    "question",
    [
        "is there any pg group with name sakania?",
        "is there any pg with name sakania?",
        "is there a pg named Muskan?",
        "pg called Sakania",
        "how many pgs are there?",
        "top 5 pgs by members",
        "List top 5 PGs which has more than 10 members",
    ],
)
def test_pg_abbreviation_is_on_topic(question):
    assert edge.detect_edge_case(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "is there any pg group with name sakania?",
        "is there any pg with name sakania?",
        "is there any producer group with name sakania?",
        "Is there any Producer Group named Sakania PG?",
    ],
)
def test_abbreviated_and_spelled_out_lookups_both_route_to_data(question):
    """The two phrasings are the same question and must route identically."""
    assert _fast_path_route(question) == "DATA"
    assert _infer_scheme_from_terms(question) == ["Focus Legacy"]


# ── the guard: a bare "pg" substring must not open the gate ────────────────
@pytest.mark.parametrize(
    "question",
    [
        "how do i upgrade my laptop",
        "the upgrade process",
        "mpg of a car",
        "what is gdp of india",
        "what is the weather today",
        "who won the cricket match",
        "what is bitcoin price",
    ],
)
def test_genuinely_off_topic_questions_are_still_bounced(question):
    """An unbounded "pg" would match inside upgrade/mpg and leak these through —
    _DOMAIN_WORDS entries are unanchored regexes."""
    assert edge.detect_edge_case(question) is not None


def test_the_whitelist_entry_is_word_bounded():
    from app.edge import _DOMAIN_WORDS

    pg_entries = [w for w in _DOMAIN_WORDS if re.fullmatch(r"\\?b?pgs?\\?b?|\\bpgs\?\\b", w)]
    # The entry exists in some bounded form...
    assert any("pg" in w and "\\b" in w for w in _DOMAIN_WORDS), (
        "no word-bounded pg entry; a bare 'pg' would false-match 'upgrade'"
    )
    # ...and no entry is a bare unanchored "pg".
    assert "pg" not in _DOMAIN_WORDS


def test_bounded_pattern_behaves_as_intended():
    pat = re.compile(r"\bpgs?\b")
    assert pat.search("is there any pg group with name sakania?")
    assert pat.search("top 5 pgs by members")
    assert not pat.search("how do i upgrade my job card")
    assert not pat.search("mpg rating")


# ── the SQL side (already fixed earlier) still applies to this phrasing ────
def test_the_sql_prompt_still_teaches_the_ilike_lookup():
    from app import annotations, entity_resolver
    from app.prompt_builder import build_sql_prompt

    annotations.load_all()
    entity_resolver.load_all()
    prompt = build_sql_prompt(
        "is there any pg group with name sakania?", ["Focus Legacy"], {"resolved": {}}
    )
    assert "never `pg_name = '...'`" in prompt
    assert "STRIP the group-type words" in prompt
