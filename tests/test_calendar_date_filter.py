"""
A calendar date in a question is a date filter, not a financial year (2026-09-27).

"How many PMAY houses were sanctioned on 2017-11-28?" ran the right SQL
(sanction_date = '2017-11-28' -> 504), but:
  * _backfill_explicit_year read "2017-11" as FY 2017-18, and the answer said
    the 504 "applies to FY 2017-18";
  * premise_check read the 11 and 28 as asserted figures, and the answer led
    with "not the 11 or 28 figures assumed in the question".
With the FY back-fill gone, _needs_scope_clarification would then have asked
"which area and time period?" — a date already pins the time.

Each test calls the real production function. Pure Python — no model or DB.
    python -m pytest tests/test_calendar_date_filter.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app import premise_check  # noqa: E402
from app.config import settings  # noqa: E402

PM = ["PMAY-G"]
DATE_QS = [
    "How many PMAY houses were sanctioned on 2017-11-28?",
    "How many PMAY houses were sanctioned on 2017/11/28?",
    "How many PMAY houses were sanctioned on 28/11/2017?",
    "How many PMAY houses were sanctioned on 28-11-2017?",
]


@pytest.fixture
def _all_years_in_range(monkeypatch):
    monkeypatch.setattr(p, "_year_in_data_range", lambda yk, schemes=None: True)


# ── the FY back-fill ignores a calendar date ─────────────────────────────────
@pytest.mark.parametrize("q", DATE_QS)
def test_date_is_not_backfilled_as_fy(q, _all_years_in_range):
    assert p._backfill_explicit_year(q, PM, {}) == {}


@pytest.mark.parametrize("q,fy", [
    ("How many PMAY-G houses were sanctioned in 2017-18?", "2017-18"),
    ("How many PMAY-G houses were sanctioned in FY 2019-2020?", "2019-20"),
    ("Houses sanctioned in 2017/18", "2017-18"),
])
def test_real_fy_range_still_backfilled(q, fy, _all_years_in_range):
    assert p._backfill_explicit_year(q, PM, {}) == {"year": fy}


# ── premise extraction ignores the parts of a date ───────────────────────────
@pytest.mark.parametrize("q", DATE_QS)
def test_date_parts_are_not_premises(q):
    assert premise_check.extract_premises(q) == []


def test_real_premise_next_to_a_date_is_kept():
    ps = premise_check.extract_premises(
        "Of the 900 houses sanctioned on 2017-11-28, how many were completed?")
    assert [x.text for x in ps] == ["900"]


def test_real_premise_still_extracted():
    ps = premise_check.extract_premises(
        "How is construction progressing on the 1.71 L sanctioned PMAY-G houses?")
    assert [x.text for x in ps] == ["1.71 L"]


# ── a date pins the time, so the scope gate does not ask ─────────────────────
@pytest.mark.parametrize("q", DATE_QS)
def test_date_question_skips_scope_clarification(q, monkeypatch):
    monkeypatch.setattr(settings, "SCOPE_CLARIFY_ENABLED", True)
    assert p._needs_scope_clarification(q, {}) is False


def test_undated_question_still_asks_for_scope(monkeypatch):
    monkeypatch.setattr(settings, "SCOPE_CLARIFY_ENABLED", True)
    assert p._needs_scope_clarification("How many PMAY-G houses were sanctioned?", {}) is True
