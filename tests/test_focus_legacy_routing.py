"""
Focus Legacy wiring — the routing and scoping facts that must hold for the fifth
scheme, and the three that were WRONG before it was added.

None of these touch megh_db: they exercise the registries and gates that decide
which scheme a question belongs to and which contract text the SQL generator is
shown. That is deliberate — the three real bugs this scheme surfaced were all
routing bugs, and all three were invisible to a DB-backed test.

The bugs, for the record:

1. "producer group" was claimed by _FOCUSPLUS_ONLY_TERMS, so every
   producer-group question routed to Focus Plus — the ONE scheme that holds no
   producer-group column and whose own classification rules refuse the question
   (focusplus_classification_rules.yaml: producer_group_requested).
2. The bare alias "focus" sat in _SCHEME_FUZZY_ALIASES["Focus Plus"], so
   "focus legacy payments" fuzzy-matched Focus Plus and answered from the wrong
   partition.
3. followups._KNOWLEDGE_LADDER offered "What is a Producer Group under FOCUS+?"
   — a question about data Focus Plus does not have.
"""
import pytest

from app import followups
from app.pipeline import (
    _SCHEME_DATA_YEARS,
    _SCHEME_NAME_PATTERN,
    _focus_ambiguity_clarification,
    _fuzzy_named_schemes,
    _infer_scheme_from_terms,
    _is_ambiguous_focus,
)
from app.prompt_builder import _scheme_of
from app.schema_context import SCHEME_CATALOG, SCHEME_METRICS, build_schema_context


def _named(question: str) -> list[str]:
    return [s for s, p in _SCHEME_NAME_PATTERN.items() if p.search(question)]


# ── registration ────────────────────────────────────────────────────────────
def test_focus_legacy_is_a_registered_scheme():
    assert "Focus Legacy" in SCHEME_CATALOG
    assert "Focus Legacy" in SCHEME_METRICS
    assert "Focus Legacy" in _SCHEME_DATA_YEARS


def test_annotation_layer_loads():
    """The seven SME YAMLs must parse and register, or every downstream stage
    silently falls back to a generic prompt."""
    from app import annotations, entity_resolver

    annotations.load_all()
    entity_resolver.load_all()

    assert len(annotations.few_shot_examples(["Focus Legacy"], top_k=99)) > 0
    assert "curated.fact_focus_legacy_disbursement" in annotations.prohibited_joins_text(
        ["Focus Legacy"]
    )
    # 12 Meghalaya districts, same as every other scheme's catalogue.
    assert len(entity_resolver.all_districts("Focus Legacy")) == 12


# ── bug 1: producer-group vocabulary belongs to Focus Legacy ────────────────
@pytest.mark.parametrize(
    "question",
    [
        "How many producer groups received money?",
        "How many PG members are registered overall?",
        "List the producer groups in Ri Bhoi",
        "What is the total PG member count?",
    ],
)
def test_producer_group_vocabulary_routes_to_focus_legacy(question):
    assert _infer_scheme_from_terms(question) == ["Focus Legacy"]


@pytest.mark.parametrize(
    "question",
    [
        "How many Focus Plus beneficiaries are there?",
        "Focus Plus disbursement by tranche",
        "Show the 12.5K cohort by gender",
    ],
)
def test_focus_plus_vocabulary_still_routes_to_focus_plus(question):
    """Regression guard: moving "producer group" out of Focus Plus must not
    have taken its own vocabulary with it."""
    assert _infer_scheme_from_terms(question) == ["Focus Plus"]


# ── bug 2: the two Focus schemes are told apart ─────────────────────────────
@pytest.mark.parametrize(
    "question,expected",
    [
        ("focus legacy payments", "Focus Legacy"),
        ("old focus disbursement", "Focus Legacy"),
        ("legacy focus totals", "Focus Legacy"),
        ("Focus Plus beneficiaries", "Focus Plus"),
        ("focus+ payments", "Focus Plus"),
        ("focusplus amount", "Focus Plus"),
    ],
)
def test_qualified_focus_names_resolve_to_one_scheme(question, expected):
    assert _named(question) == [expected]


@pytest.mark.parametrize(
    "question",
    ["total focus disbursement", "what is focus?", "tell me about the FOCUS scheme"],
)
def test_bare_focus_is_ambiguous_and_never_guessed(question):
    """A bare "Focus" names neither scheme. It must reach the two-way ask, and
    must NOT fuzzy-match its way into Focus Plus (bug 2)."""
    assert _named(question) == []
    assert _fuzzy_named_schemes(question) == []
    assert _is_ambiguous_focus(question) is True

    clar = _focus_ambiguity_clarification(question)
    assert clar.rule == "focus-scheme-ambiguous"
    labels = " ".join(o["label"] for o in clar.options)
    assert "Focus Legacy" in labels and "Focus Plus" in labels


@pytest.mark.parametrize(
    "question",
    [
        "How many producer groups under FOCUS?",   # forced to Legacy
        "FOCUS tranche 2 payments",                # forced to Plus
        "Focus Plus beneficiaries",                # named outright
        "focus legacy payments",                   # named outright
        "MGNREGA person-days",                     # no Focus at all
        "refocusing our efforts",                  # substring, not the word
    ],
)
def test_settled_questions_do_not_trigger_the_focus_ask(question):
    assert _is_ambiguous_focus(question) is False


# ── the FY2023-24 gap (the contract's own highest-value case) ───────────────
def test_fy_2023_24_is_absent_not_zero():
    """focuslegacy_schema_partitions.yaml semantic_rules.time_gap_rule: the
    scheme has FOUR years with a hole in the middle. FY2023-24 must never be
    offered as a year chip, or a user picks a year that returns nothing and
    reads it as "no money was paid" rather than "no data exists"."""
    years = _SCHEME_DATA_YEARS["Focus Legacy"]
    assert years == ["2021-22", "2022-23", "2024-25", "2025-26"]
    assert "2023-24" not in years


# ── schema context scoping ──────────────────────────────────────────────────
def test_schema_context_is_scoped_to_focus_legacy():
    ctx = build_schema_context(["Focus Legacy"])
    assert "FOCUS LEGACY TABLES" in ctx
    assert "curated.v_focus_legacy" in ctx
    # No other scheme's block may leak in — that is the whole point of scoping.
    for foreign in ("FOCUS PLUS TABLES", "CM ELEVATE TABLES", "MGNREGA TABLES",
                    "PMAY-G TABLES"):
        assert foreign not in ctx


def test_schema_context_carries_the_load_bearing_rules():
    """The four facts that make an answer wrong rather than ugly if dropped."""
    ctx = build_schema_context(["Focus Legacy"])
    assert "no_of_pg_members * 5000" in ctx          # the entitlement identity
    assert "COUNT(DISTINCT pg_id)" in ctx            # groups are never counted by name
    assert "never_traverse" in ctx or "NEVER QUERY, NEVER JOIN" in ctx   # the PII boundary
    assert "2023-24" in ctx                          # the year gap is stated


def test_other_schemes_context_unchanged_by_the_addition():
    for scheme, marker in [("Focus Plus", "FOCUS PLUS TABLES"),
                           ("CM Elevate", "CM ELEVATE TABLES"),
                           ("MGNREGA", "MGNREGA TABLES"),
                           ("PMAY-G", "PMAY-G TABLES")]:
        ctx = build_schema_context([scheme])
        assert marker in ctx
        assert "FOCUS LEGACY TABLES" not in ctx


# ── table ownership ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "table,owner",
    [
        ("v_focus_legacy", "Focus Legacy"),
        ("fact_focus_legacy_disbursement", "Focus Legacy"),
        ("dim_producer_group", "Focus Legacy"),
        ("dim_pg_entity_type", "Focus Legacy"),
        ("bridge_pg_bank_history", "Focus Legacy"),
        # unchanged neighbours
        ("v_focus_plus", "Focus Plus"),
        ("fact_focus_plus_disbursement", "Focus Plus"),
        ("v_cm_elevate", "CM Elevate"),
        ("v_pmay", "PMAY-G"),
        ("v_employment", "MGNREGA"),
        ("dim_geography", "shared"),
    ],
)
def test_live_schema_block_attributes_tables_correctly(table, owner):
    """v_focus_legacy contains "focus" but not "focus_plus"; before the explicit
    rule it fell through to "shared" and was shown to every scheme."""
    assert _scheme_of(table) == owner


# ── bug 3 + follow-up chips ─────────────────────────────────────────────────
def test_focus_plus_knowledge_ladder_no_longer_claims_producer_groups():
    ladder = followups._KNOWLEDGE_LADDER["Focus Plus"]
    assert not any("Producer Group" in q for _needles, q in ladder)


def test_focus_legacy_has_data_and_knowledge_followups():
    data = followups.build_followups("data", "How much was disbursed to producer groups?",
                                     ["Focus Legacy"], {})
    assert data, "a Focus Legacy data answer must offer next steps"

    know = followups.build_followups("knowledge", "What is the FOCUS scheme?",
                                     ["Focus Legacy"], {})
    assert know


def test_followups_never_restate_the_question():
    """A chip that just re-asks the question is a dead end for the user."""
    for q in ["How many producer groups were paid?",
              "How many PG members were covered?",
              "Which products do Focus Legacy groups work on?"]:
        for opt in followups.build_followups("data", q, ["Focus Legacy"], {}):
            assert not followups._norm(opt["question"]).startswith(followups._norm(q)), (
                f"{q!r} -> chip {opt['question']!r} restates the question"
            )


# ── access control ──────────────────────────────────────────────────────────
def test_every_role_can_reach_focus_legacy():
    from app.auth import ROLE_PERMISSIONS

    for role, perms in ROLE_PERMISSIONS.items():
        assert "Focus Legacy" in perms["schemes"], f"{role} cannot see Focus Legacy"


# ── knowledge base ──────────────────────────────────────────────────────────
def test_focus_legacy_reference_docs_are_ingested_and_chunked():
    """The FAQ arrived using bold "**1. Q**" lines instead of the house "### Q"
    convention, which collapsed all 18 Q&As into ONE 7.2k chunk and buried every
    specific answer. It is normalised now; this guards the regression."""
    from app.kb_ingest import _collect_chunks

    chunks = [c for c in _collect_chunks() if c["scheme"] == "Focus Legacy"]
    assert len(chunks) >= 20, f"only {len(chunks)} Focus Legacy chunks"

    faq = [c for c in chunks if "FAQ" in c["doc"]]
    assert len(faq) >= 15, f"FAQ did not split into questions ({len(faq)} chunks)"
