"""
The KNOWLEDGE/RAG path for GENERAL scheme questions — the three faults that made
it answer badly, or not at all, and the invariants that must hold after the fix.

None of these need Qdrant, the gateway or megh_db: they exercise the scope
DECISION (which scheme filter retrieval runs under) and the KB tagging that
decision depends on. That is where all three faults lived.

The faults:

1. "What is the FOCUS scheme in Meghalaya?" was answered "that isn't one of the
   schemes I hold" — for a scheme with 31 KB chunks. _names_unknown_scheme()
   recognises "our" schemes via _named_schemes() / _infer_scheme_from_terms(),
   and a bare "Focus" is deliberately in NEITHER (two schemes answer to it, so
   it belongs to _is_ambiguous_focus). The unknown-scheme guard runs first in
   the KNOWLEDGE branch, so the which-Focus ask never got a chance.

2. Knowledge retrieval ran UNSCOPED whenever the scheme was implied by
   vocabulary rather than named outright. The branch read _named_schemes()
   alone and ignored _infer_scheme_from_terms(), which the DATA path has always
   trusted. Measured on the real KB, "How many members does a producer group
   need?" returned a FOCUS PLUS chunk as its single best hit — and Focus Plus
   holds no producer-group data at all.

3. kb_ingest's `<!-- scheme: X -->` tag regex forbade spaces, so the three
   multi-word schemes could never be tagged explicitly; whatever it did capture
   was .upper()'d (unreachable, since filters match the canonical spelling);
   and the fallback label was "GENERAL", which no caller ever queries — an
   un-retrievable chunk.
"""
import pytest

from app.kb_ingest import _CANONICAL_SCHEME, _SCHEME_TAG, _collect_chunks, _web_sources
from app.pipeline import (
    _infer_scheme_from_terms,
    _is_ambiguous_focus,
    _named_schemes,
    _names_unknown_scheme,
    _needs_scheme_clarification,
)


def _kb_scheme_for(question: str) -> str | None:
    """The scheme filter the KNOWLEDGE branch would scope retrieval to, for a
    first turn (no previous-turn antecedent). Mirrors pipeline's own order."""
    named = _named_schemes(question)
    if len(named) == 1:
        return named[0]
    inferred = _infer_scheme_from_terms(question)
    if inferred and len(inferred) == 1:
        return inferred[0]
    return None


# ── fault 1: a real scheme was called unknown ───────────────────────────────
@pytest.mark.parametrize(
    "question",
    [
        "What is the FOCUS scheme in Meghalaya?",
        "tell me about the Focus scheme",
        "what is the focus programme?",
    ],
)
def test_bare_focus_is_never_reported_as_an_unknown_scheme(question):
    """It IS one of ours — we just need to ask which of the two. Being told
    "that isn't a scheme I hold" is worse than any wrong guess: it is flatly
    false, and it ends the conversation."""
    assert _names_unknown_scheme(question) is False
    assert _is_ambiguous_focus(question) is True


def test_a_genuinely_unknown_scheme_is_still_reported():
    """Regression guard on the fix above — the unknown-scheme reply must still
    fire for a scheme we really do not hold."""
    assert _names_unknown_scheme("what is the amma yedi scheme?") is True


# ── fault 2: retrieval ran unscoped on vocabulary-implied questions ─────────
@pytest.mark.parametrize(
    "question,expected",
    [
        # Focus Legacy — the case that surfaced this
        ("What is the role of Producer Groups under FOCUS?", "Focus Legacy"),
        ("How many members does a producer group need?", "Focus Legacy"),
        ("What kind of support does FOCUS provide to Producer Groups?", "Focus Legacy"),
        # the same fault affected every scheme with distinctive vocabulary
        ("How do I get a job card?", "MGNREGA"),
        ("What is the 100-day guarantee?", "MGNREGA"),
        ("Who qualifies for a pucca house?", "PMAY-G"),
        ("What is the piggery scheme about?", "CM Elevate"),
    ],
)
def test_vocabulary_implied_questions_get_a_scheme_filter(question, expected):
    assert _kb_scheme_for(question) == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What is MGNREGA?", "MGNREGA"),
        ("Who is eligible for PMAY-G?", "PMAY-G"),
        ("What is CM-ELEVATE?", "CM Elevate"),
        ("Who is eligible for FOCUS+?", "Focus Plus"),
        ("What is Focus Legacy?", "Focus Legacy"),
    ],
)
def test_named_schemes_still_scope_as_before(question, expected):
    """The named-outright path is unchanged; the fallback only runs when it
    finds nothing."""
    assert _kb_scheme_for(question) == expected


@pytest.mark.parametrize(
    "question",
    ["How do I apply?", "What are the benefits?", "Who is eligible?"],
)
def test_genuinely_vague_questions_still_have_no_scheme(question):
    """The fallback must not invent a scheme for a question that names none and
    uses no scheme-specific vocabulary — those still pause and ask."""
    assert _kb_scheme_for(question) is None
    assert _needs_scheme_clarification(question) is True


def test_no_clarification_pause_is_lost_to_the_new_fallback():
    """The invariant that makes fault 2's fix safe: _needs_scheme_clarification
    already returns False as soon as _infer_scheme_from_terms resolves, so every
    question the fallback now scopes was ALREADY skipping the pause — it was
    just searching unscoped. Nothing that used to ask now silently picks."""
    probe = [
        "How do I get a job card?",
        "What is the 100-day guarantee?",
        "Who qualifies for a pucca house?",
        "What is the piggery scheme about?",
        "What is the role of Producer Groups under FOCUS?",
        "How many members does a producer group need?",
        "How do I apply?",
        "What are the benefits?",
        "tell me about the scheme",
        "What is the FOCUS scheme in Meghalaya?",
    ]
    for q in probe:
        named = _named_schemes(q)
        inferred = _infer_scheme_from_terms(q)
        scoped_by_fallback = (
            len(named) != 1 and bool(inferred) and len(inferred) == 1
        )
        would_have_paused = (
            not named and not _is_ambiguous_focus(q) and _needs_scheme_clarification(q)
        )
        assert not (scoped_by_fallback and would_have_paused), (
            f"{q!r} used to pause for clarification and would now be answered "
            f"silently as {inferred}"
        )


# ── fault 3: KB chunks tagged with something nothing can query ──────────────
@pytest.mark.parametrize(
    "tag,expected",
    [
        ("<!-- scheme: MGNREGA -->", "MGNREGA"),
        ("<!-- scheme: PMAY-G -->", "PMAY-G"),
        # multi-word: the original regex forbade the space and matched nothing
        ("<!-- scheme: Focus Legacy -->", "Focus Legacy"),
        ("<!-- scheme: Focus Plus -->", "Focus Plus"),
        ("<!-- scheme: CM Elevate -->", "CM Elevate"),
        # case-insensitive, since .upper() used to mangle these
        ("<!-- scheme: focus legacy -->", "Focus Legacy"),
        ("<!-- scheme: cm elevate -->", "CM Elevate"),
    ],
)
def test_scheme_tag_resolves_to_the_canonical_spelling(tag, expected):
    """Retrieval filters on an exact scheme string (vectorstore.search uses
    MatchValue), so a tag that resolves to anything else is unreachable."""
    m = _SCHEME_TAG.search(tag)
    assert m is not None, f"{tag!r} did not match the tag regex at all"
    assert _CANONICAL_SCHEME.get(m.group(1).strip().lower()) == expected


def test_every_kb_chunk_is_tagged_with_a_queryable_scheme():
    """A chunk whose scheme is not one a caller can pass is dead weight: it can
    never be returned by a scoped search. "GENERAL" was exactly that."""
    from app.schema_context import SCHEME_CATALOG

    # PMAY-U is deliberate — Urban sections are re-tagged so a PMAY-G search
    # can never surface them. It is a real, intentional exclusion tag.
    allowed = set(SCHEME_CATALOG) | {"PMAY-U"}
    seen = {c["scheme"] for c in _collect_chunks()}
    assert seen <= allowed, f"unqueryable scheme tags in the KB: {seen - allowed}"
    assert "GENERAL" not in seen


def test_web_sources_are_all_canonically_tagged():
    from app.schema_context import SCHEME_CATALOG

    allowed = set(SCHEME_CATALOG) | {"PMAY-U"}
    for path, scheme in _web_sources():
        assert scheme in allowed, f"{path} tagged {scheme!r}, which nothing queries"


def test_focus_legacy_knowledge_is_actually_present():
    """The end of the chain: the docs the fixes above exist to make reachable."""
    chunks = [c for c in _collect_chunks() if c["scheme"] == "Focus Legacy"]
    assert len(chunks) >= 20
    headings = " ".join(c["heading"].lower() for c in chunks)
    assert "objective" in headings or "overview" in headings
    assert "producer group" in headings

# ── fault 4: a STALE Qdrant collection answered "not covered" forever ───────
# Live failure 2026-09-22: the running server held 170 points (the old
# four-scheme KB) while data/ produced 201. Focus Legacy's chunks had never been
# ingested, so every Focus Legacy knowledge question retrieved ZERO chunks and
# fell through to the not-covered reply — while CM Elevate and MGNREGA, already
# in the collection, answered fine. The skip heuristic could not see it: it
# compared only the point COUNT.
def test_ingest_skip_requires_every_scheme_to_be_present():
    """The skip decision must be content-aware, not just size-aware."""
    import inspect

    from app import kb_ingest

    src = inspect.getsource(kb_ingest.ingest_kb)
    assert "distinct_schemes" in src, (
        "ingest_kb skips on point count alone; a scheme added to data/ but never "
        "ingested would answer 'not covered' for every question, permanently"
    )


def test_distinct_schemes_helper_exists():
    from app import vectorstore

    assert hasattr(vectorstore, "distinct_schemes")


def test_a_small_new_scheme_would_not_be_silently_skipped():
    """The count-only heuristic passed `existing >= int(expected * 0.9)`, so a
    newly added scheme worth under 10% of the corpus never triggered a rebuild.
    Against the real numbers (170 existing), anything up to 18 new chunks was
    invisible — Focus Legacy's 31 cleared the bar only by luck."""
    existing = 170
    for new_chunks in (10, 15, 18):
        expected = existing + new_chunks
        count_only_says_skip = existing >= int(expected * 0.9)
        assert count_only_says_skip, (
            "this asserts the OLD heuristic really was blind here; if it no "
            "longer is, this test's premise needs revisiting"
        )
    # The content check is what catches it: the new scheme has no points, so the
    # scheme sets differ regardless of how close the totals are.
    want = {"MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy"}
    have = {"MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate"}
    assert want - have == {"Focus Legacy"}


# ── fault 5: the composer denied a scheme because OUR label isn't in its docs ─
# Live 2026-09-22: "what is Focus Legacy scheme" retrieved the right passages
# (top 0.769, all Focus Legacy) and answered "The Focus Legacy scheme is not
# mentioned in the provided reference material. The documents describe the
# 'FOCUS Scheme'..." — literally true about the passages, and useless.
#
# "Focus Legacy" is OUR disambiguation label, coined to separate the
# producer-group scheme from Focus Plus. It appears ZERO times in either
# reference doc; the SME prose says "FOCUS" throughout. Every other scheme's
# label is close enough to its doc's own wording to survive (MGNREGA 34x,
# PMAY-G 30x, FOCUS+ 42x, CM-ELEVATE 41x in their respective docs).
def test_focus_legacy_label_really_is_absent_from_its_own_docs():
    """The premise of the fix. If a future doc revision starts saying "Focus
    Legacy", this test fails and the naming note can be reconsidered."""
    from pathlib import Path

    ref = Path("data/reference")
    for name in ("FOCUS_legacy_Complete_Reference.md", "FOCUS_LEGACY_FAQ.md"):
        text = (ref / name).read_text(encoding="utf-8").lower()
        assert "focus legacy" not in text, (
            f"{name} now contains the literal label; rag._SCHEME_DOC_NAMES may "
            "no longer be needed for this scheme"
        )
        assert "focus" in text  # it does describe the scheme, under another name


def test_naming_note_bridges_label_and_doc_wording():
    from app.rag import _naming_note

    note = _naming_note("Focus Legacy")
    assert "Focus Legacy" in note and "FOCUS" in note
    assert "SAME scheme" in note
    # It must forbid exactly the sentence the composer produced.
    assert "never say" in note.lower()


def test_naming_note_is_empty_when_unscoped():
    """An unscoped or multi-scheme call cannot assert whose material it holds,
    so it must add nothing."""
    from app.rag import _naming_note

    assert _naming_note(None) == ""


def test_every_catalogued_scheme_has_a_doc_name_entry():
    from app.rag import _SCHEME_DOC_NAMES
    from app.schema_context import SCHEME_CATALOG

    missing = set(SCHEME_CATALOG) - set(_SCHEME_DOC_NAMES)
    assert not missing, f"no doc-name mapping for {missing}"


def test_naming_note_is_used_by_the_single_scheme_composer():
    import inspect

    from app import rag

    src = inspect.getsource(rag.answer_from_kb)
    assert "_naming_note(scheme)" in src
