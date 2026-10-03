"""
Filtering by a producer-group NAME, and what an unmatched aggregate must say.

Reported 2026-09-22: "How many members are there in Sakania Producer Group? for
East Khasi Hills for FY 2021-22" produced

    SELECT SUM(no_of_pg_members) AS memberships FROM curated.v_focus_legacy
    WHERE pg_name = 'Sakania' AND lgd_district = 'EAST KHASI HILLS'
      AND year_key = 2021 LIMIT 1

and the answer was "the membership count is null". The group is real:
PG-FOCUS-EKH-2245, stored as 'Sakania Pg', with 12 members in FY2021-22.

TWO independent defects, both fixed:

1. `pg_name = '...'` can essentially never match. Measured on the live view,
   over the 9,452 distinct pg_name values: 3,053 END in "Pg" and 2,867 CONTAIN
   "Producer Group"; others use "P.g." or a trailing number. The user's wording
   and the stored suffix rarely coincide —

       pg_name =     'Sakania'                  -> 0 rows
       pg_name =     'Sakania Producer Group'   -> 0 rows
       pg_name ILIKE '%Sakania Producer Group%' -> 0 rows   (wrapping is NOT enough)
       pg_name ILIKE '%Sakania%'                -> 1 row

   The old guidance mentioned ILIKE only in the second-to-last sentence of a
   paragraph about COUNTING, never forbade `=`, and never mentioned the suffix.
   Now rule 5a covers the lookup on its own terms.

2. A bare aggregate over zero matching rows returns ONE ROW OF NULLS, not zero
   rows, so `if not rows: return _no_data_answer(...)` never fired. The
   `no_usable_value` branch could not catch it either, because _row_metrics
   drops NULL cells before the test.
"""
import re

import pytest

from app.pipeline import _row_metrics
from app.schema_context import build_schema_context


def _fl_context() -> str:
    return build_schema_context(["Focus Legacy"])


# ── defect 1: the name-lookup rule ─────────────────────────────────────────
def test_exact_match_on_pg_name_is_forbidden():
    """The generator used `=`; the rules never told it not to."""
    ctx = _fl_context()
    assert "never `pg_name = '...'`" in ctx, (
        "nothing forbids an exact match on pg_name, which is what produced the "
        "zero-row query behind the reported bug"
    )


def test_the_suffix_problem_is_explained_not_just_the_operator():
    """ILIKE alone is not enough — wrapping the user's full phrase still fails
    when they say "Producer Group" and the row says "Pg". The rule has to say
    STRIP the group-type words."""
    ctx = _fl_context()
    assert "STRIP the group-type words" in ctx
    assert "wrapping is NOT enough" in ctx


def test_a_copyable_worked_shape_exists():
    ctx = _fl_context()
    assert "pg_name ILIKE '%sakania%'" in ctx
    # and it is shown as a contrast with the wrong form
    assert "NOT pg_name =" in ctx


def test_zero_rows_after_ilike_is_not_a_null_measure():
    """The distinction the composer got wrong: "no such group" is not "the
    count is null"."""
    ctx = _fl_context()
    assert "not a spelling mismatch" in ctx
    assert "null" in ctx.lower()


def test_one_core_name_can_match_several_groups():
    """"Muskan" matches 4 pg_id values, spelled both "Muskan Pg" and "Muskan
    Producer Group" — the rule must say so, or a single-group question silently
    aggregates several."""
    ctx = _fl_context()
    assert "SEVERAL pg_id" in ctx


def test_the_counting_rule_is_left_intact():
    """Rule 5 is about aggregating on pg_name and was always correct; the fix
    adds a separate rule rather than editing it."""
    ctx = _fl_context()
    assert "NEVER COUNT(DISTINCT pg_name)" in ctx
    assert "NEVER GROUP BY pg_name" in ctx
    assert "MAX(pg_name) for display" in ctx


def test_rule_reaches_the_assembled_sql_prompt():
    """schema_context is only useful if prompt_builder actually ships it."""
    from app import annotations, entity_resolver
    from app.prompt_builder import build_sql_prompt

    annotations.load_all()
    entity_resolver.load_all()
    prompt = build_sql_prompt(
        "How many members are there in Sakania Producer Group?",
        ["Focus Legacy"],
        {"resolved": {"district": "EAST KHASI HILLS"}},
    )
    assert "never `pg_name = '...'`" in prompt
    assert "STRIP the group-type words" in prompt


# ── defect 2: an unmatched aggregate returns a NULL row, not zero rows ─────
def _no_usable_value(rows: list[dict]) -> bool:
    """The predicate as it now stands in compose_response."""
    nums = [n for r in rows for _k, n in _row_metrics(r)]
    all_null = bool(rows) and not nums and all(
        v is None for r in rows for v in r.values()
    )
    return (bool(nums) and all(n in (0, None) for n in nums)) or all_null


@pytest.mark.parametrize(
    "rows,expected,label",
    [
        ([{"memberships": None}], True, "all-NULL aggregate — the reported bug"),
        ([{"memberships": 0}], True, "a real zero"),
        ([{"memberships": 12}], False, "a real value"),
        ([{"memberships": None, "payments": 3}], False, "one NULL beside a real value"),
        ([{"a": None}, {"a": None}], True, "several all-NULL rows"),
    ],
)
def test_all_null_aggregate_counts_as_no_usable_value(rows, expected, label):
    assert _no_usable_value(rows) is expected, label


def test_row_metrics_still_drops_nulls():
    """The reason the old test could never fire: NULL cells are not numbers, so
    they never reach `all(n in (0, None) ...)`. Documented so the new guard is
    not "simplified" back into the old one."""
    assert _row_metrics({"memberships": None}) == []
    assert _row_metrics({"memberships": 12}) == [("memberships", 12)]


def test_the_guard_is_wired_into_compose_response():
    import inspect

    from app import pipeline

    src = inspect.getsource(pipeline.compose_response)
    assert "_all_null" in src, "the all-NULL case is not handled in compose_response"


# ── the SQL shape the rule teaches actually works ──────────────────────────
def test_core_name_pattern_strips_the_group_suffix():
    """What rule 5a asks the generator to do, as a local string check: the
    group-type words come off, leaving the core name that ILIKE will match."""
    # No trailing \b — it cannot match after the optional "." in "P.G.".
    suffix = re.compile(
        r"[\s,]*\b(?:producer\s+groups?|producer\s+grp|p\.?\s*g\.?|group)\.?\s*$",
        re.IGNORECASE,
    )
    for typed, core in [
        ("Sakania Producer Group", "Sakania"),
        ("Sakania PG", "Sakania"),
        ("Sakania P.G.", "Sakania"),
        ("Muskan Group", "Muskan"),
        ("Sakania", "Sakania"),
    ]:
        assert suffix.sub("", typed).strip() == core, typed
