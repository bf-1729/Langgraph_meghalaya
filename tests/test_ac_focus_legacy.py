"""
Focus Legacy must offer the assembly-constituency reading of an ambiguous place
name, like MGNREGA does.

Reported 2026-09-23: "How many Producer Groups are mapped to Amlarem?" offered
only "The Amlarem C&RD block" and "The Amlarem village". Amlarem is also an
assembly constituency, and the same name under MGNREGA is asked about at all
three levels.

The collision DETECTOR was never the problem — _collision_values already lends
Focus Legacy MGNREGA's 56-name AC catalogue (the _SHARED_ADMIN_DIMENSIONS
fallback), so:

    collides_across_dimensions("Amlarem", "Focus Legacy")
        -> {'block': 'Amlarem', 'assembly_constituency': 'AMLAREM'}

The AC arm was thrown away one line later by the suppression gate:

    if (schemes or []) != ["MGNREGA"]:
        return False

That hard-coding was right when written — AC lives directly on exactly one
fact, and PMAY-G / Focus Plus / CM Elevate have no route to it. Focus Legacy
does, and its own contract is explicit
(focuslegacy_schema_partitions.yaml semantic_rules.constituency_rule):

    status: ANSWERABLE_ONLY_BY_AN_EXPLICIT_DIMENSION_JOIN
    runtime_behavior: "Answer the question, emit the join... Do not silently refuse."

So the data supports the question; only the gate blocked it.
"""
import pytest

from app import entity_resolver
from app.pipeline import _AC_CAPABLE_SCHEMES, _ac_dimension_available


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


REPORTED = "How many Producer Groups are mapped to Amlarem?"


# ── the reported question ───────────────────────────────────────────────────
def test_focus_legacy_offers_the_constituency_reading():
    assert _ac_dimension_available(REPORTED, ["Focus Legacy"]) is True


def test_mgnrega_is_unchanged():
    assert _ac_dimension_available(REPORTED, ["MGNREGA"]) is True


@pytest.mark.parametrize("scheme", ["PMAY-G", "Focus Plus", "CM Elevate"])
def test_schemes_with_no_ac_column_still_refuse(scheme):
    """These genuinely have no route to a constituency, so offering the chip
    would invite a choice that can never be answered."""
    assert _ac_dimension_available(REPORTED, [scheme]) is False


def test_the_capable_set_is_exactly_the_schemes_with_a_route():
    # CM Elevate Legacy joined 2026-09-25: geography_key -> dim_geography.ac_name,
    # which reproduces the source's mapped_constituency_name exactly.
    assert set(_AC_CAPABLE_SCHEMES) == {"MGNREGA", "Focus Legacy", "CM Elevate Legacy"}


def test_a_cross_scheme_question_does_not_offer_ac():
    """With more than one scheme in play the answer would have to span a
    dimension only some of them carry."""
    assert _ac_dimension_available(REPORTED, ["MGNREGA", "Focus Legacy"]) is False
    assert _ac_dimension_available(REPORTED, []) is False


# ── the metric test still applies within Focus Legacy ──────────────────────
@pytest.mark.parametrize(
    "question,expected",
    [
        ("How many Producer Groups are mapped to Amlarem?", True),
        ("How many PG members were covered in Amlarem?", True),
        ("figures for Amlarem", True),          # no metric named — leave it open
        # expenditure-shaped: suppressed exactly as on MGNREGA
        ("Total amount disbursed in Amlarem", False),
        ("disbursement for Amlarem", False),
    ],
)
def test_the_metric_test_is_not_bypassed(question, expected):
    assert _ac_dimension_available(question, ["Focus Legacy"]) is expected


# ── the detector, which was already correct ────────────────────────────────
def test_focus_legacy_borrows_the_ac_catalogue():
    """_collision_values lends the shared administrative dimensions across
    schemes; without it the AC reading is structurally invisible."""
    assert len(entity_resolver._collision_values("Focus Legacy", "assembly_constituency")) > 0


@pytest.mark.parametrize("name", ["Amlarem", "Mylliem"])
def test_block_and_constituency_collision_is_detected(name):
    dims = entity_resolver.collides_across_dimensions(name, "Focus Legacy")
    assert "block" in dims
    assert "assembly_constituency" in dims


def test_an_unambiguous_name_still_asks_nothing():
    """The gate must not start pausing on names that are only one kind of
    place — that would interrupt the commonest question shape there is."""
    assert entity_resolver.collides_across_dimensions("Nongstoin", "Focus Legacy") == {}
    assert entity_resolver.collides_across_dimensions("Mairang", "Focus Legacy") == {}


# ── the end-to-end clarification ───────────────────────────────────────────
def test_the_clarification_lists_the_constituency_option():
    from app.pipeline import _dimension_collision_clarification

    dims = entity_resolver.collides_across_dimensions("Amlarem", "Focus Legacy")
    if "assembly_constituency" in dims and not _ac_dimension_available(
        REPORTED, ["Focus Legacy"]
    ):
        dims = {k: v for k, v in dims.items() if k != "assembly_constituency"}

    clar = _dimension_collision_clarification(REPORTED, "Amlarem", dims)
    labels = " ".join(o["label"] for o in clar.options).lower()
    assert "constituency" in labels, (
        "the constituency reading is still missing from the chips"
    )
    assert "block" in labels
    assert clar.rule == "entity-ambiguous"


def test_the_sql_generator_can_actually_answer_it():
    """The chip must not promise something the generator cannot emit.

    The AC join reaches the prompt through FEW-SHOT retrieval, not the
    hand-written schema block — neither MGNREGA's nor Focus Legacy's block
    mentions ac_name, so asserting on build_schema_context would test the wrong
    mechanism. Focus Legacy's corpus carries two worked ac_name examples
    (sanctioned_patterns.constituency in the join-graph YAML), and they are what
    the generator copies."""
    from app import annotations

    annotations.load_all()
    top = annotations.few_shot_examples(
        ["Focus Legacy"],
        "producer groups in the Amlarem assembly constituency",
        top_k=6,
    )
    with_ac = [e for e in top if "ac_name" in (e.get("sql") or "")]
    assert with_ac, "no ac_name exemplar retrieved; the AC chip would be unanswerable"
    assert any("dim_geography" in (e.get("sql") or "") for e in with_ac), (
        "the constituency join must go through dim_geography on geography_key"
    )
