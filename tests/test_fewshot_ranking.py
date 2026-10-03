"""
Few-shot retrieval must return a RIGHT-SHAPED exemplar for a breakdown question.

Reported 2026-09-23: "Total Focus Legacy amount disbursed by district for FY
2021-22" came back as ten unlabelled amount/membership pairs. The aggregates
were correct — the values match the real per-district totals exactly — but
lgd_district was never SELECTed or GROUPed, so the user got rows with nothing to
identify them by.

Cause: _fewshot_score was plain token overlap, so every token counted the same.
Across Focus Legacy's 81 examples the scheme's own vocabulary dominates —
"focus" 32%, "payment" 32%, "legacy" 28%, "expenditure" 25% — while "district",
the token that says what SHAPE the answer needs, is in 12%. The retrieved set was

    1. Compare Focus Legacy and PMAY disbursement by district  (cross-scheme CTE)
    2. What is the total amount disbursed under Focus Legacy?  <-- NO GROUP BY
    3. What money unit is Focus Legacy recorded in?
    ...

and the generator copied #2's bare-SUM shape. The corpus HAS 18 district-grouped
examples; none of the strong ones ranked.

The scheme name is noise BY CONSTRUCTION here: few_shot_examples() is called
per-scheme, so every candidate in the pool already shares it.

Fix, in two layers:
  * ranking — IDF-weight each shared token over that scheme's own corpus, and
    score a token appearing in >= 25% of it at a small residual instead of full
    weight. Measured effect on the suite below: 8/12 questions had no
    right-shaped exemplar before, 0/12 after.
  * contract — schema_context rule 5b states the breakdown invariant outright,
    so the shape does not depend on what the ranker happens to return.
"""
import pytest

from app import annotations
from app.annotations import (
    _FEWSHOT_UBIQUITOUS_DF,
    _FEWSHOT_UBIQUITOUS_WEIGHT,
    _build_idf,
    _build_scheme_stop,
    _fewshot_score,
    _fewshot_tokens,
)


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()


def _groups_by(sql: str, col: str) -> bool:
    return "GROUP BY" in (sql or "") and col in (sql or "").split("GROUP BY")[1][:120]


# ── the reported question ───────────────────────────────────────────────────
def test_by_district_question_retrieves_a_district_grouped_exemplar():
    ex = annotations.few_shot_examples(
        ["Focus Legacy"],
        "Total Focus Legacy amount disbursed by district for FY 2021-22",
        top_k=6,
    )
    assert any(_groups_by(e.get("sql") or "", "lgd_district") for e in ex), (
        "no district-grouped exemplar retrieved; the generator has only "
        "bare-SUM shapes to copy, which is what dropped lgd_district"
    )


def test_the_bare_sum_exemplar_no_longer_outranks_the_district_one():
    """The specific inversion behind the bug."""
    idf = annotations._fewshot_idf["Focus Legacy"]
    stop = annotations._fewshot_scheme_stop["Focus Legacy"]
    q = _fewshot_tokens("Total Focus Legacy amount disbursed by district for FY 2021-22")

    bare_sum = _fewshot_score(q, "What is the total amount disbursed under Focus Legacy?",
                              idf, stop)
    by_district = _fewshot_score(q, "Top 5 districts by amount disbursed", idf, stop)
    assert by_district > bare_sum, (
        f"bare SUM ({bare_sum:.3f}) still outranks the district example "
        f"({by_district:.3f})"
    )


# ── the property, across every scheme ───────────────────────────────────────
@pytest.mark.parametrize(
    "scheme,question,col",
    [
        ("Focus Legacy", "Total Focus Legacy amount disbursed by district for FY 2021-22", "lgd_district"),
        ("Focus Legacy", "Focus Legacy amount disbursed by district", "lgd_district"),
        ("Focus Legacy", "district-wise Focus Legacy disbursement", "lgd_district"),
        ("Focus Legacy", "How many producer groups in each district?", "lgd_district"),
        ("Focus Legacy", "Focus Legacy payments by block", "lgd_block"),
        ("Focus Legacy", "Which products do Focus Legacy groups work on?", "product_raw"),
        ("Focus Legacy", "Focus Legacy disbursement by financial year", "financial_year_short"),
        ("Focus Legacy", "Which banks handle Focus Legacy payments?", "bank_name"),
        # the same ranking flaw affected every scheme, so guard them too
        ("PMAY-G", "PMAY-G houses completed by district", "lgd_district"),
        ("MGNREGA", "MGNREGA expenditure by district", "lgd_district"),
        ("CM Elevate", "CM Elevate applications by district", "lgd_district"),
    ],
)
def test_breakdown_questions_retrieve_the_right_shape(scheme, question, col):
    ex = annotations.few_shot_examples([scheme], question, top_k=6)
    assert any(_groups_by(e.get("sql") or "", col) for e in ex), (
        f"{question!r} retrieved no exemplar grouping by {col}"
    )


# ── the mechanism ───────────────────────────────────────────────────────────
def test_scheme_vocabulary_is_identified_as_ubiquitous():
    """Derived from the corpus, not hand-maintained — so it stays correct as
    examples are added."""
    stop = annotations._fewshot_scheme_stop["Focus Legacy"]
    assert {"focus", "legacy"} <= stop, (
        "the scheme's own name is not treated as ubiquitous, so it keeps "
        "scoring as hard evidence in a pool where every candidate shares it"
    )
    # A genuinely topical token must NOT be stopworded.
    assert "district" not in stop
    assert "village" not in stop


def test_idf_ranks_a_rare_token_above_a_common_one():
    idf = annotations._fewshot_idf["Focus Legacy"]
    assert idf["district"] > idf["focus"]


def test_ubiquitous_tokens_score_a_residual_not_full_weight():
    """They must still break ties, but never outweigh a real topical match —
    my first attempt kept them at full weight and the bug survived."""
    assert 0 < _FEWSHOT_UBIQUITOUS_WEIGHT < 0.5
    examples = [{"question": "alpha common"}, {"question": "beta common"},
                {"question": "gamma common"}, {"question": "delta common"}]
    idf = _build_idf(examples)
    stop = _build_scheme_stop(examples)
    assert "common" in stop, "a token in 100% of examples is not ubiquitous?"
    # sharing only the ubiquitous token still scores > 0 (tie-breaking)
    assert _fewshot_score({"common"}, "alpha common", idf, stop) > 0


def test_an_empty_corpus_does_not_crash_the_ranker():
    assert _build_idf([]) == {}
    assert _build_scheme_stop([]) == set()


def test_threshold_is_a_fraction_not_a_count():
    """It must scale with corpus size, or it silently stops working as
    examples are added."""
    assert 0 < _FEWSHOT_UBIQUITOUS_DF < 1


# ── the contract rule, which holds whatever the ranker returns ─────────────
def test_schema_context_states_the_breakdown_rule():
    from app.schema_context import build_schema_context

    ctx = build_schema_context(["Focus Legacy"])
    assert '"BY <DIMENSION>" MEANS GROUP BY' in ctx
    assert "A filter is NOT a breakdown" in ctx, (
        "the WHERE-vs-GROUP BY distinction is the one the generator got wrong: "
        "it applied the FY filter and dropped the district grouping"
    )


def test_a_by_district_worked_shape_is_available():
    from app.schema_context import build_schema_context

    ctx = build_schema_context(["Focus Legacy"])
    assert "GROUP BY lgd_district" in ctx
    assert "financial_year_short = '2021-22'" in ctx
