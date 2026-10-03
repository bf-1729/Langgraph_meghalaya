"""
The constituency flow must work END TO END for Focus Legacy, as it does for MGNREGA.

Reported 2026-09-23: after picking "The AMLAREM assembly constituency" the bot
asked for a DISTRICT instead of answering. The chip appeared (fixed previously)
but nothing downstream could act on it.

Tracing MGNREGA's flow and comparing, layer by layer:

  1. collides_across_dimensions      OK  — borrows MGNREGA's AC catalogue
  2. _ac_dimension_available          OK  — fixed previously
  3. resolve_dimension(..., "assembly_constituency")   ** BROKEN **
         MGNREGA      -> resolved  'AMLAREM'
         Focus Legacy -> not_found
     It read the ASKING scheme's own catalogue, and only MGNREGA ships one. So
     resolved["assembly_constituency"] was never set, the filter was silently
     dropped, and the pipeline fell through to the geography branches — which
     ask for a district. That is the reported symptom.
  4. constituency_contents            read curated.v_employment (MGNREGA-only)
     for every scheme. Since KI-146 (2026-09-29) it reads the asking scheme's
     own rows (v_focus_legacy JOIN dim_geography for Focus Legacy).
  5. prompt_builder resolved-entity line             ** BROKEN **
     Emitted `UPPER(assembly_constituency_name) = ...`, a column that exists
     only on mgnrega_employment. v_focus_legacy has none — it reaches ac_name
     through the documented dim_geography join on geography_key.

Both fixes mirror patterns already in the codebase: resolve_dimension ALREADY
falls back across schemes for "block" (same real units, differing coverage), and
prompt_builder already branches per scheme elsewhere.

Verified live — the two readings are genuinely different areas, which is the
whole reason the clarification exists:
    AMLAREM constituency -> 204 producer groups
    AMLAREM block        -> 159
"""
import pytest

from app import entity_resolver
from app.pipeline import _ac_dimension_available
from app.prompt_builder import _focus_legacy_only


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


# ── layer 3: the name must resolve ─────────────────────────────────────────
def test_focus_legacy_resolves_a_constituency_name():
    """This is the step that broke the flow: an unresolved AC is a dropped
    filter, and the pipeline then asks for a district."""
    r = entity_resolver.resolve_dimension("Amlarem", "Focus Legacy", "assembly_constituency")
    assert r.status == "resolved"
    assert r.canonical == "AMLAREM"


def test_mgnrega_resolution_is_unchanged():
    r = entity_resolver.resolve_dimension("Amlarem", "MGNREGA", "assembly_constituency")
    assert r.status == "resolved"
    assert r.canonical == "AMLAREM"


@pytest.mark.parametrize("name", ["Amlarem", "Mylliem", "Sohra"])
def test_known_constituencies_resolve_for_focus_legacy(name):
    r = entity_resolver.resolve_dimension(name, "Focus Legacy", "assembly_constituency")
    assert r.status == "resolved", f"{name} did not resolve; the AC filter would be dropped"


def test_an_unknown_name_still_does_not_resolve():
    """The fallback must not turn the resolver into a rubber stamp."""
    r = entity_resolver.resolve_dimension(
        "Zzzznotaplace", "Focus Legacy", "assembly_constituency"
    )
    assert r.status == "not_found"


def test_the_block_fallback_still_works():
    """AC was added to the SAME mechanism that already served blocks; check the
    original behaviour is intact."""
    r = entity_resolver.resolve_dimension("Batabari", "CM Elevate", "block")
    assert r.status == "resolved"


def test_non_shared_dimensions_keep_the_strict_guard():
    """Only the shared ADMINISTRATIVE dimensions fall back. A scheme-specific
    dimension must still return not_found rather than borrowing."""
    r = entity_resolver.resolve_dimension("Tranch 1", "Focus Legacy", "tranche_label")
    assert r.status == "not_found"


# ── the upstream gate still protects schemes with no AC route ──────────────
@pytest.mark.parametrize("scheme", ["PMAY-G", "Focus Plus", "CM Elevate"])
def test_ac_less_schemes_never_reach_the_ac_branch(scheme):
    """resolve_dimension is now looser, so the safety net is the chip gate:
    these schemes are never offered the AC reading in the first place."""
    assert _ac_dimension_available("How many are mapped to Amlarem?", [scheme]) is False


# ── layer 5: the SQL filter must match the scheme's real shape ─────────────
def test_focus_legacy_only_helper():
    assert _focus_legacy_only(["Focus Legacy"]) is True
    assert _focus_legacy_only(["MGNREGA"]) is False
    assert _focus_legacy_only(["Focus Legacy", "MGNREGA"]) is False
    assert _focus_legacy_only([]) is False
    assert _focus_legacy_only(None) is False


def _ac_prompt(scheme: str) -> str:
    from app import annotations
    from app.prompt_builder import build_sql_prompt

    annotations.load_all()
    return build_sql_prompt(
        "How many Producer Groups are mapped to Amlarem, the assembly constituency?",
        [scheme],
        {"resolved": {"assembly_constituency": "AMLAREM"},
         "notes": [], "display": {"assembly_constituency": "Amlarem"}},
    )


def test_focus_legacy_is_told_to_join_dim_geography():
    """v_focus_legacy has no AC column; the MGNREGA filter would be unusable."""
    p = _ac_prompt("Focus Legacy")
    assert "dim_geography" in p
    assert "g.ac_name" in p
    assert "geography_key" in p
    # and it must NOT be handed the column that does not exist here
    assert "UPPER(assembly_constituency_name)" not in p


def test_focus_legacy_ac_answer_excludes_unresolved_placeholders():
    """A placeholder row has no real village, so no meaningful constituency —
    the scheme's own constituency_rule requires the exclusion."""
    assert "entity_type <> 'Unresolved'" in _ac_prompt("Focus Legacy")


def test_mgnrega_still_gets_its_own_column():
    p = _ac_prompt("MGNREGA")
    assert "UPPER(assembly_constituency_name)" in p


# ── the detector, unchanged ────────────────────────────────────────────────
def test_the_collision_still_offers_both_readings():
    dims = entity_resolver.collides_across_dimensions("Amlarem", "Focus Legacy")
    assert "block" in dims and "assembly_constituency" in dims
