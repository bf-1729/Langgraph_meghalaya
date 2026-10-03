"""
A COMPARISON across the FY2023-24 gap must stay a comparison.

Reported 2026-09-23, and caused by the previous gap fix. "Compare the total
remittance between FY 2023-24 and FY 2024-25" no longer dead-ends (good), but
answered with a single row:

    FY 2024-25: 114,990,000 amount disbursed.

The user asked to COMPARE. The gap fix stripped the unavailable year out of the
question before routing —

    '...between FY 2023-24 and FY 2024-25...'  ->  '...the total remittance FY 2024-25...'

— and with one year left there was nothing to compare, so the verb went unmet.

Few-shot retrieval was NOT at fault: the right exemplar ranks first ("Compare FY
2024-25 with the year before it", an IN-list two-year GROUP BY). It simply had
one operand to work with.

The scheme's own contract already prescribes the answer. The note on that very
exemplar in focuslegacy_few_shot.yaml:

    "The preceding year WITH PAYMENTS is FY2022-23, not FY2023-24. State that a
     year with no data sits between them."

and focuslegacy_response_template.yaml:

    fy_gap_comparison_note: "The preceding year with payments is {previous_year},
                             because FY2023-24 has none."

So on a comparison the absent year is SUBSTITUTED with the nearest year holding
data, not deleted. Verified live, that is the real answer the question wanted:

    2022-23   2,139 payments   141,790,000
    2024-25   2,653 payments   114,990,000
"""
import pytest

from app.pipeline import (
    _COMPARES_YEARS,
    _apply_year_gap,
    _nearest_available_year,
    _out_of_range_year_in,
    _years_in_question,
)

FL = ["Focus Legacy"]
REPORTED = '"Compare the total remittance between FY 2023-24 and FY 2024-25." for Focus Legacy'


def _gate(question, schemes=FL):
    """Calls the REAL gate the pipeline runs, so a regression in production code
    fails these tests.

    An earlier version of this helper re-implemented the logic locally. Proved
    worthless: disabling the substitution branch in pipeline.py left every test
    green, because they were exercising the copy. _apply_year_gap was extracted
    for exactly this reason.

    Returns (rewritten_question, swapped, dropped, paused) by reading the note
    the real function produces."""
    import re as _re

    before_ok, before_bad = _years_in_question(question, schemes)
    out, note, handled = _apply_year_gap(question, schemes)
    if not handled:
        return out, [], before_bad, True
    after_ok, _after_bad = _years_in_question(out, schemes)
    swapped = [
        (b, n) for b, n in _re.findall(
            r"FY (\S+) holds no data for this scheme, so FY (\S+) — the nearest",
            note or "")
    ]
    dropped = [y for y in before_bad if not any(y == b for b, _n in swapped)]
    return out, swapped, dropped, False


# ── the reported question ───────────────────────────────────────────────────
def test_the_comparison_keeps_two_years():
    out, swapped, dropped, paused = _gate(REPORTED)
    assert paused is False
    assert swapped == [("2023-24", "2022-23")]
    assert dropped == []
    # both operands survive, so the question is still a comparison
    assert "2022-23" in out and "2024-25" in out
    assert "2023-24" not in out


def test_the_comparison_verb_is_preserved():
    """Stripping used to leave "Compare the total remittance FY 2024-25" — the
    verb with nothing to apply it to."""
    out, _s, _d, _p = _gate(REPORTED)
    assert "ompare" in out
    assert "between" in out, "the comparison phrasing was mangled"


def test_the_substitute_is_the_preceding_year_with_payments():
    """The contract names FY2022-23 specifically, not "any other year"."""
    assert _nearest_available_year("2023-24", FL) == "2022-23"


@pytest.mark.parametrize(
    "question",
    [
        '"Compare the total remittance between FY 2023-24 and FY 2024-25." for Focus Legacy',
        "FY 2023-24 vs FY 2024-25 disbursement for Focus Legacy",
        "Compare FY 2023-24 against FY 2025-26 for Focus Legacy",
        "difference between FY 2023-24 and FY 2024-25 for Focus Legacy",
    ],
)
def test_every_comparison_phrasing_substitutes(question):
    _out, swapped, dropped, paused = _gate(question)
    assert paused is False
    assert swapped, f"{question!r} lost its comparison instead of substituting"
    assert dropped == []


# ── non-comparisons keep the strip-and-note behaviour ──────────────────────
def test_a_non_comparison_still_drops_the_gap_year():
    """There is no second operand to preserve, so substituting would invent a
    year the user never asked about."""
    out, swapped, dropped, paused = _gate("Focus Legacy total for FY 2023-24 and FY 2024-25")
    assert paused is False
    assert swapped == []
    assert dropped == ["2023-24"]
    assert "2023-24" not in out and "2024-25" in out


def test_nothing_answerable_still_pauses():
    _out, _s, _d, paused = _gate("Focus Legacy total for FY 2023-24")
    assert paused is True


def test_a_fully_valid_comparison_is_untouched():
    q = "Compare FY 2022-23 and FY 2024-25 for Focus Legacy"
    out, swapped, dropped, paused = _gate(q)
    assert (out, swapped, dropped, paused) == (q, [], [], False)


# ── the comparison cue ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "question,is_comparison",
    [
        ("Compare the total remittance between FY 2023-24 and FY 2024-25", True),
        ("FY 2023-24 vs FY 2024-25", True),
        ("difference between FY 2023-24 and FY 2024-25", True),
        ("Focus Legacy total for FY 2023-24 and FY 2024-25", False),
        ("How much was disbursed in FY 2024-25?", False),
    ],
)
def test_comparison_detection(question, is_comparison):
    assert bool(_COMPARES_YEARS.search(question)) is is_comparison


# ── the rewritten question must be answerable and not loop ────────────────
def test_the_rewritten_question_does_not_retrip_the_guard():
    out, _s, _d, _p = _gate(REPORTED)
    assert _out_of_range_year_in(out, FL) is None


def test_the_matching_exemplar_is_retrieved_for_the_rewritten_question():
    """The two-year IN-list shape is what makes this answer two rows."""
    from app import annotations

    annotations.load_all()
    out, _s, _d, _p = _gate(REPORTED)
    top = annotations.few_shot_examples(["Focus Legacy"], out, top_k=6)
    assert any(
        "financial_year_short IN (" in (e.get("sql") or "") for e in top
    ), "no two-year IN-list exemplar retrieved; the answer would be one row"
