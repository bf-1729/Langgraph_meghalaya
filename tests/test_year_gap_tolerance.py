"""
A question naming BOTH an absent year and a valid one must be answered, not refused.

Reported 2026-09-23: "Compare the total remittance between FY 2023-24 and FY
2024-25" for Focus Legacy replied

    For Focus Legacy, data is available only for the financial years 2021-22,
    2022-23, 2024-25, 2025-26. No data is held for "2023-24". Please select one
    of the financial years listed above, or all of them combined.

The user had already named the years. FY2024-25 is one of the four the scheme
holds — only FY2023-24 is absent. Asking them to re-pick, from a list whose
whole purpose is to exclude the year they asked about, is a dead end.

Cause: `_out_of_range_year_in()` returns the FIRST unavailable token and stops;
the caller raised on it without asking whether the question also named an
available year.

    "...FY 2023-24 and FY 2024-25"
        '2023-24' -> in_range=False
        '2024-25' -> in_range=True      <-- never considered

This is the FY2023-24 gap — the SME layer's single most emphasised trap
(use_cases TC-24: "the single test case most worth running before this bot
ships"), and its response contract says the gap is REPORTED ALONGSIDE the answer
rather than turned into a pause:

    fy_gap_note: "FY2023-24 holds no Focus Legacy payments at all, so the series
                  has a gap between FY2022-23 and FY2024-25 rather than a zero."

Verified against the live view: FY2024-25 holds 114,990,000 — a real answer that
was being withheld.
"""
import pytest

from app.pipeline import (
    _out_of_range_year_in,
    _strip_year_tokens,
    _years_in_question,
)

FL = ["Focus Legacy"]


def _would_pause(question: str, schemes=FL) -> bool:
    """The gate's decision after the fix: refuse only when NOTHING is available."""
    if _out_of_range_year_in(question, schemes) is None:
        return False
    ok, _bad = _years_in_question(question, schemes)
    return not ok


# ── the reported question ───────────────────────────────────────────────────
def test_the_reported_question_is_answered_not_refused():
    q = '"Compare the total remittance between FY 2023-24 and FY 2024-25." for Focus Legacy'
    assert _would_pause(q) is False
    ok, bad = _years_in_question(q, FL)
    assert ok == ["2024-25"]
    assert bad == ["2023-24"]


@pytest.mark.parametrize(
    "question,expect_ok,expect_bad",
    [
        ("Compare the total remittance between FY 2023-24 and FY 2024-25",
         ["2024-25"], ["2023-24"]),
        ("Focus Legacy disbursement in FY 2023-24 and FY 2025-26",
         ["2025-26"], ["2023-24"]),
        ("Total for FY 2023-24, FY 2024-25 and FY 2025-26",
         ["2024-25", "2025-26"], ["2023-24"]),
    ],
)
def test_mixed_year_questions_keep_the_available_years(question, expect_ok, expect_bad):
    ok, bad = _years_in_question(question, FL)
    assert ok == expect_ok
    assert bad == expect_bad
    assert _would_pause(question) is False


# ── the guard must still fire when nothing is answerable ───────────────────
@pytest.mark.parametrize(
    "question",
    [
        "Focus Legacy total for FY 2023-24",      # the gap year alone
        "Focus Legacy in FY 1999-20",             # outside the window entirely
        "Focus Legacy total for FY 2019-20",
    ],
)
def test_a_question_with_no_available_year_still_pauses(question):
    """Nothing to answer with — the pause is the right behaviour here, and the
    fix must not weaken it."""
    assert _would_pause(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "Focus Legacy total for FY 2024-25",
        "Compare FY 2022-23 and FY 2024-25",
        "Focus Legacy disbursement by district",   # no year named at all
    ],
)
def test_fully_valid_questions_are_untouched(question):
    assert _would_pause(question) is False


# ── the rewritten question must read cleanly and not loop ──────────────────
@pytest.mark.parametrize(
    "question,must_keep,must_drop",
    [
        ('"Compare the total remittance between FY 2023-24 and FY 2024-25." for Focus Legacy',
         "2024-25", "2023-24"),
        ("Focus Legacy disbursement in FY 2023-24 and FY 2025-26", "2025-26", "2023-24"),
    ],
)
def test_stripping_keeps_the_valid_year_and_removes_the_gap_year(question, must_keep, must_drop):
    _ok, bad = _years_in_question(question, FL)
    out = _strip_year_tokens(question, bad)
    assert must_keep in out
    assert must_drop not in out


def test_stripped_question_does_not_retrip_the_guard():
    """Otherwise the next turn pauses on the same question — an infinite loop."""
    q = '"Compare the total remittance between FY 2023-24 and FY 2024-25." for Focus Legacy'
    _ok, bad = _years_in_question(q, FL)
    assert _out_of_range_year_in(_strip_year_tokens(q, bad), FL) is None


def test_stripping_leaves_no_dangling_connector():
    """"between X and Y" with X removed used to read "...remittance and FY
    2024-25", which is what the SQL generator and the composer both see."""
    q = "Compare the total remittance between FY 2023-24 and FY 2024-25"
    out = _strip_year_tokens(q, ["2023-24"])
    assert " and FY 2024-25" not in out or not out.strip().endswith("and")
    assert "  " not in out, f"double space left in {out!r}"
    assert not out.rstrip(' ."').endswith(("and", "between", "from", ","))


# ── other schemes keep their own windows ───────────────────────────────────
def test_the_split_is_scheme_specific():
    """FY2023-24 is absent for Focus Legacy but present for PMAY-G, so the same
    token must classify differently per scheme."""
    ok_fl, bad_fl = _years_in_question("total for FY 2023-24", FL)
    ok_pm, bad_pm = _years_in_question("total for FY 2023-24", ["PMAY-G"])
    assert bad_fl == ["2023-24"] and ok_fl == []
    assert ok_pm == ["2023-24"] and bad_pm == []
