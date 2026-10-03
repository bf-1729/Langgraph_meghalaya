"""
MGNREGA use-case QA fixes (Test_Case_Results_21st_Sep_26_MGNREGA, 2026-09-26).

The live QA found these bot defects (KNOWN_ISSUES KI-041 … KI-048); each test
pins the fix to the real production function, and the neighbouring behaviour
it must NOT change (other schemes are deliberately untouched).

  DATA-003/016/020/029  "…, the block, not the village" (the bot's own chip)
         resolved the same-named VILLAGE: 273 households instead of 11,590.
  DATA-004/014  SOUTH TURA (an assembly constituency tagged as a district) was
         refused as "outside Meghalaya".
  DATA-011/014/017/019/023/030  "which scheme?" for MGNREGA-only measures.
  DATA-021  person-days per household 38.00 instead of 38.16 (integer division).
  DATA-014  "which villages …" answered with codes, counting zero villages.
  DATA-023  wages vs materials answered with no figures.
  DATA-028/029  spend + person-days for a village/block never got working SQL.
  DATA-013  administrative expenditure refused instead of reported with a caveat.
  DATA-009/020  "100 days" flagged as a misquoted number → row-dump fallback.

No model or DB: the extractor, the village lookup and the DB are stubbed.
    python -m pytest tests/test_mgnrega_usecase_fixes.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import annotations, entity_resolver  # noqa: E402
from app import pipeline as p  # noqa: E402

MG = ["MGNREGA"]


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()
    entity_resolver.load_all()


def _stub_resolution(monkeypatch, mentions):
    """Make resolve_entities deterministic: fixed extractor output, no village in
    the DB, no constituency drill-down."""
    async def _extract(q):
        return dict(mentions)

    async def _no_village(*a, **k):
        return entity_resolver.Resolved("not_found", "village", str(a[0]) if a else "")

    async def _no_scan(*a, **k):
        return None

    async def _no_contents(*a, **k):
        return {"districts": [], "blocks": [], "villages": 0}

    async def _no_names():
        return {}

    async def _no_exact(cands):
        return {}

    monkeypatch.setattr(p, "extract_entity_mentions", _extract)
    monkeypatch.setattr(p, "resolve_village", _no_village)
    monkeypatch.setattr(p, "_scan_village_in_question", _no_scan)
    monkeypatch.setattr(p, "constituency_contents", _no_contents)
    # Offline by default: tests that need names stub these again afterwards.
    monkeypatch.setattr(p, "_mgnrega_village_names", _no_names)
    monkeypatch.setattr(p, "village_names_exact", _no_exact)


# ── KI-041: the negated level in the block chip ─────────────────────────────
CHIP_Q = "How many households received employment in MAWKYRWAT during 2024-25, the block, not the village"


def test_negated_village_is_not_the_stated_level_for_mgnrega():
    assert p._explicit_level_in(CHIP_Q, MG) == "block"
    assert p._explicit_level_in("x in MAWKYRWAT, the village, not the block", MG) == "village"


def test_other_schemes_keep_the_old_level_reading():
    # The user asked that non-MGNREGA behaviour stay exactly as it was. Focus
    # Plus was brought under the fix on 2026-09-27 (its all-villages run hit the
    # same chip bug; KNOWN_ISSUES KI-079), PMAY-G on 2026-09-28 (KI-096) and CM
    # Elevate on 2026-09-28 (KI-106); every other scheme is unchanged.
    assert p._explicit_level_in(CHIP_Q) == "village"
    # CM Elevate joined the village gate on 2026-09-28 (KI-106), CM Elevate Legacy on 2026-10-02
    assert p._explicit_level_in(CHIP_Q, ["CM Elevate"]) == "block"
    assert p._explicit_level_in(CHIP_Q, ["CM Elevate Legacy"]) == "block"
    assert p._explicit_level_in(CHIP_Q, ["Focus Plus"]) == "block"
    assert p._explicit_level_in(CHIP_Q, ["PMAY-G"]) == "block"


def test_admin_level_chip_wording_unchanged():
    assert p._explicit_level_in("person-days in Sohra, the block, not another area type", MG) == "block"
    assert p._explicit_level_in("houses in Nongchram, the village, not another area type", MG) == "village"


def test_block_chip_resolves_the_block_not_the_village(monkeypatch):
    _stub_resolution(monkeypatch, {"block": "MAWKYRWAT", "year": "2024-25"})
    out = asyncio.run(p.resolve_entities(CHIP_Q, MG))
    assert out["resolved"].get("block") == "MAWKYRWAT"
    assert "village_code" not in out["resolved"]


# ── KI-042: SOUTH TURA is a constituency, not an out-of-scope place ─────────
def test_exact_ac_name_is_recognised_for_mgnrega_only():
    assert p._mgnrega_ac_reading("SOUTH TURA", MG) == "SOUTH TURA"
    assert p._mgnrega_ac_reading("south  tura", MG) == "SOUTH TURA"
    assert p._mgnrega_ac_reading("SOUTH TURA", ["PMAY-G"]) is None
    assert p._mgnrega_ac_reading("Guwahati", MG) is None
    assert p._mgnrega_ac_reading("SOUTH TUR", MG) is None       # exact only, never fuzzy


def test_south_tura_mistagged_as_district_resolves_to_the_constituency(monkeypatch):
    _stub_resolution(monkeypatch, {"district": "SOUTH TURA", "year": "2024-25"})
    out = asyncio.run(p.resolve_entities(
        "How many persons received employment in SOUTH TURA during 2024-25?", MG))
    assert out["resolved"].get("assembly_constituency") == "SOUTH TURA"
    assert "district" not in out["resolved"]


def test_unknown_place_is_still_out_of_scope(monkeypatch):
    _stub_resolution(monkeypatch, {"district": "Guwahati"})
    with pytest.raises(p.OutOfScope):
        asyncio.run(p.resolve_entities("How many persons received employment in Guwahati?", MG))


# ── KI-043: MGNREGA-only vocabulary pins the scheme ─────────────────────────
@pytest.mark.parametrize("q", [
    "How much was spent on materials in JAKREM during 2024-25?",
    "Which villages in SOUTH TURA received employment during 2024-25?",
    "How many women received employment in West Khasi Hills during 2023-24?",
    "What percentage of employment persons were women in West Garo Hills during 2024-25?",
    "What percentage of employment households completed 100 days in Dadenggiri block?",
    "Was more spent on wages or materials in JAKREM during 2024-25?",
    "Compare total expenditure and employment generated in West Garo Hills during 2024-25.",
    "What was the administrative expenditure in MAWRYNGKNENG during 2024-25?",
])
def test_mgnrega_only_measures_need_no_scheme_pause(q):
    assert p._infer_scheme_from_terms(q) == MG


@pytest.mark.parametrize("q, want", [
    ("What was the total expenditure in DAMASH during 2024-25?", []),   # generic money word: still asks
    ("how many houses sanctioned in Tura", ["PMAY-G"]),
    ("What is the PMAY administrative expenditure?", ["PMAY-G"]),       # a named scheme still wins
])
def test_generic_or_named_questions_unchanged(q, want):
    assert p._named_or_inferred_schemes(q) == want


# ── KI-044: integer division ────────────────────────────────────────────────
def test_integer_ratio_numerator_is_cast_for_mgnrega():
    sql = ("SELECT ROUND(SUM(person_days) / NULLIF(SUM(households_employed), 0), 2) AS a "
           "FROM curated.v_employment")
    out = p._mgnrega_numeric_division(MG, sql)
    assert "SUM(person_days)::numeric /" in out
    assert p._mgnrega_numeric_division(MG, out) == out               # idempotent
    assert p._mgnrega_numeric_division(["PMAY-G"], sql) == sql


# ── KI-045: village lists ───────────────────────────────────────────────────
VQ = "Which villages in NONGKREM constituency received employment during 2024-25?"


def test_village_list_needs_names_and_a_positive_filter():
    assert "no village names" in p._mgnrega_village_list_issue(
        VQ, MG, "SELECT DISTINCT village_code FROM curated.v_employment")
    assert "HAVING" in p._mgnrega_village_list_issue(
        VQ, MG, "SELECT lgd_village_name, village_code, SUM(persons_employed) FROM v GROUP BY 1, 2")
    assert p._mgnrega_village_list_issue(
        VQ, MG, "SELECT lgd_village_name, SUM(persons_employed) FROM v GROUP BY 1 "
                "HAVING SUM(persons_employed) > 0") is None
    assert p._mgnrega_village_list_issue(VQ, ["Focus Legacy"], "SELECT village_code FROM v") is None


# ── KI-047: comparisons must carry figures ──────────────────────────────────
def test_comparison_with_only_a_label_is_rejected():
    q = "Was more spent on wages or materials in JAKREM during 2024-25?"
    assert p._mgnrega_comparison_without_figures(q, MG, [{"higher_spending_category": "wages"}])
    assert not p._mgnrega_comparison_without_figures(q, MG, [{"wage_exp_lakh": 219.48,
                                                              "material_exp_lakh": 113.89}])
    assert not p._mgnrega_comparison_without_figures(q, ["PMAY-G"], [{"x": "wages"}])


# ── KI-048 / DATA-029: spend + person-days is built deterministically ───────
def test_combined_query_for_a_village_is_parameter_bound():
    sql, params, shown = p._mgnrega_combined_facts_query(
        "How much was spent and how many person-days were generated in JAKREM during 2024-25?",
        MG, {"village_code": 277391, "year_key": 2024})
    assert params == [277391, 2024]
    assert "village_code = $1 AND year_key = $2" in sql and "277391" not in sql
    assert "curated.v_expenditure" in sql and "curated.v_employment" in sql
    assert " JOIN curated" not in sql                                 # never the fan-out join
    assert "persons_employed" not in sql                              # "person-days" is not "persons"
    assert "village_code = 277391" in shown


def test_combined_query_for_a_block():
    sql, params, _ = p._mgnrega_combined_facts_query(
        "Compare expenditure and person-days in MAWRYNGKNENG during 2024-25, the block, not another area type",
        MG, {"block": "MAWRYNGKNENG", "year_key": 2024})
    assert params == ["MAWRYNGKNENG", 2024] and "UPPER(lgd_block) = UPPER($1)" in sql


@pytest.mark.parametrize("q, resolved", [
    ("Which district had the highest expenditure and person-days?", {}),            # ranking
    ("Compare expenditure and person-days by block in Ri Bhoi", {"district": "RI BHOI"}),
    ("Compare wages and person-days in JAKREM", {"village_code": 277391}),
    ("Compare expenditure and person-days in SOUTH TURA", {"assembly_constituency": "SOUTH TURA"}),
    ("How many person-days in JAKREM?", {"village_code": 277391}),                 # one fact only
])
def test_combined_query_leaves_other_shapes_to_the_generator(q, resolved):
    assert p._mgnrega_combined_facts_query(q, MG, resolved) is None


def test_combined_query_is_mgnrega_only():
    assert p._mgnrega_combined_facts_query(
        "How much was spent and how many person-days in JAKREM?", ["PMAY-G"], {"village_code": 1}) is None


# ── KI-046: administrative expenditure ──────────────────────────────────────
def test_admin_query_and_answer_carry_the_caveat(monkeypatch):
    sql, params, _ = p._mgnrega_admin_expenditure_query(
        "What was the administrative expenditure in MAWRYNGKNENG during 2024-25?",
        MG, {"block": "MAWRYNGKNENG", "year_key": 2024})
    assert "admin_total_exp" in sql and params == ["MAWRYNGKNENG", 2024]

    async def _state(*a, **k):
        return [{"total": 0.9, "nonzero": 1, "n": 18818}]
    monkeypatch.setattr(p, "fetch_rows", _state)
    ans = asyncio.run(p._mgnrega_admin_expenditure_answer(
        [{"admin_expenditure_lakh": 0, "source_rows": 38}],
        {"block": "MAWRYNGKNENG", "year": "FY 2024-25"}))
    assert "₹0.00 lakh" in ans and "MAWRYNGKNENG block" in ans
    assert "never populated" in ans and "1 of 18,818" in ans


def test_pmay_admin_expenditure_still_refused():
    assert p._named_or_inferred_schemes("What is the PMAY-G administrative expenditure?") != MG


# ── DATA-009/020: "100 days" is the measure's name, not a misquote ──────────
def test_hundred_days_sentence_is_kept(monkeypatch):
    good = "In MAWKYRWAT during FY 2024-25, 3.39% of employment households completed 100 days."

    async def _compose(prompt):
        return good
    monkeypatch.setattr(p.llm, "call_response_composer", _compose)
    ans = asyncio.run(p.compose_response(
        "What percentage of employment households completed 100 days in MAWKYRWAT during 2024-25?",
        "SELECT 1", [{"completion_100_day_pct": 3.39}], notes=[],
        entities={"block": "MAWKYRWAT"}, schemes=MG))
    assert ans.startswith(good)


def test_a_real_misquote_is_still_caught(monkeypatch):
    async def _compose(prompt):
        return "In MAWKYRWAT, 4.12% of employment households completed 100 days."
    monkeypatch.setattr(p.llm, "call_response_composer", _compose)
    ans = asyncio.run(p.compose_response(
        "What percentage of employment households completed 100 days in MAWKYRWAT during 2024-25?",
        "SELECT 1", [{"completion_100_day_pct": 3.39}], notes=[],
        entities={"block": "MAWKYRWAT"}, schemes=MG))
    assert "4.12" not in ans and "3.39" in ans


# ── DATA-025/026/027: top-1 ranking wording ─────────────────────────────────
def test_statewide_top_row_gets_the_ranking_note():
    sql = ("SELECT lgd_district, lgd_block, SUM(person_days) AS person_days FROM curated.v_employment "
           "WHERE year_key = 2024 GROUP BY 1, 2 ORDER BY person_days DESC LIMIT 1")
    notes = p._mgnrega_answer_notes("Which block generated the highest person-days during 2024-25?", sql,
                                    [{"lgd_district": "WEST GARO HILLS", "lgd_block": "TIKRIKILLA",
                                      "person_days": 773491}])
    assert any("highest in Meghalaya" in n for n in notes)
    assert any("column names" in n for n in notes)


def test_no_ranking_note_for_a_scoped_or_multi_row_result():
    scoped = ("SELECT lgd_block, SUM(person_days) AS pd FROM curated.v_employment "
              "WHERE lgd_district = 'RI BHOI' GROUP BY 1 ORDER BY pd DESC LIMIT 1")
    assert not any("Meghalaya" in n for n in p._mgnrega_answer_notes("q", scoped, [{"pd": 1}]))
    assert p._mgnrega_answer_notes("q", "SELECT SUM(person_days) AS pd FROM v", [{"pd": 1}]) == []


_TOP_SQL = ("SELECT lgd_district, SUM(women_employment_provided) AS w FROM curated.v_employment "
            "WHERE year_key = 2024 GROUP BY lgd_district ORDER BY w DESC LIMIT 1")


def test_only_one_sentence_is_replaced_for_a_statewide_top_row():
    ans = ("East Khasi Hills district had 34,537 women employed during FY 2024-25. "
           "This is the only district recorded for that financial year in the result.")
    out = p._mgnrega_top_one_wording(ans, _TOP_SQL, [{"lgd_district": "EAST KHASI HILLS", "w": 34537}])
    assert "only district" not in out and "34,537" in out and out.endswith("highest in Meghalaya.")


def test_top_one_wording_leaves_other_answers_alone():
    ans = "East Khasi Hills district had 34,537 women employed during FY 2024-25."
    assert p._mgnrega_top_one_wording(ans, _TOP_SQL, [{"w": 34537}]) == ans
    only = "SANGSANGGRE is the only village in SOUTH TURA that received employment."
    assert p._mgnrega_top_one_wording(only, "SELECT lgd_village_name FROM v", [{"v": "S"}]) == only


# ── DATA-023 run 2: lakh money divided by 100 but still labelled lakh ───────
def test_lakh_alias_drops_a_crore_division():
    sql = ("SELECT ROUND(SUM(unskilled_wage_exp + semi_skilled_wage_exp) / 100.0, 2) AS wages_lakh, "
           "ROUND(SUM(material_exp) / 100.0, 2) AS materials_lakh FROM curated.v_expenditure")
    out = p._mgnrega_lakh_not_divided(MG, sql)
    assert "/ 100" not in out and "AS wages_lakh" in out and "AS materials_lakh" in out


def test_real_crore_conversion_and_other_schemes_untouched():
    crore = "SELECT ROUND(SUM(total_exp) / 100, 2) AS total_expenditure_crore FROM curated.v_expenditure"
    assert p._mgnrega_lakh_not_divided(MG, crore) == crore
    lakh = "SELECT ROUND(SUM(total_exp) / 100.0, 2) AS total_lakh FROM curated.v_expenditure"
    assert p._mgnrega_lakh_not_divided(["PMAY-G"], lakh) == lakh


# ── DATA-023: money stated without a unit ───────────────────────────────────
_WAGE_SQL = ("SELECT SUM(unskilled_wage_exp + semi_skilled_wage_exp) AS wages_lakh, "
             "SUM(material_exp) AS materials_lakh FROM curated.v_expenditure")
_WAGE_ROWS = [{"wages_lakh": 219.48, "materials_lakh": 113.89}]


def test_money_without_unit_gets_lakh():
    out = p._mgnrega_money_units(
        "Wages spending of 219.48 exceeded materials spending of 113.89 in Jakrem.", _WAGE_SQL, _WAGE_ROWS)
    assert out == "Wages spending of 219.48 lakh exceeded materials spending of 113.89 lakh in Jakrem."


def test_money_units_left_alone_when_present_or_not_money():
    ok = "Wages were 219.48 lakh and materials 113.89 lakh."
    assert p._mgnrega_money_units(ok, _WAGE_SQL, _WAGE_ROWS) == ok
    crore = "West Garo Hills spent 257.01 crore."
    assert p._mgnrega_money_units(crore, "SELECT SUM(total_exp)/100 AS total_exp_crore FROM x",
                                  [{"total_exp_crore": 257.01}]) == crore
    pd = "Jakrem generated 57,391 person-days."
    assert p._mgnrega_money_units(pd, "SELECT SUM(person_days) AS person_days FROM curated.v_employment",
                                  [{"person_days": 57391}]) == pd


# ── all-blocks QA: a bare block/constituency name tagged as a village ──────
def _stub_exact_villages(monkeypatch, names):
    async def _exact(cands):
        return {c.upper(): [{"village_code": 1}] for c in cands if c.upper() in names}
    monkeypatch.setattr(p, "village_names_exact", _exact)


def test_block_only_name_is_reread_as_the_block(monkeypatch):
    _stub_exact_villages(monkeypatch, set())
    assert asyncio.run(p._mgnrega_admin_reading_of_village_mention("KHATARSHNONG LAITKROH")) == \
        {"block": "KHATARSHNONG LAITKROH"}


def test_block_and_constituency_name_is_both(monkeypatch):
    _stub_exact_villages(monkeypatch, set())
    assert set(asyncio.run(p._mgnrega_admin_reading_of_village_mention("MAIRANG"))) == \
        {"block", "assembly_constituency"}


def test_a_real_village_name_is_left_to_the_village_branch(monkeypatch):
    _stub_exact_villages(monkeypatch, {"MAWKYRWAT"})
    assert asyncio.run(p._mgnrega_admin_reading_of_village_mention("MAWKYRWAT")) == {}
    _stub_exact_villages(monkeypatch, set())
    assert asyncio.run(p._mgnrega_admin_reading_of_village_mention("Damash")) == {}


def test_bare_block_mention_resolves_to_the_block(monkeypatch):
    _stub_resolution(monkeypatch, {"village": "KHATARSHNONG LAITKROH", "year": "2024-25"})
    _stub_exact_villages(monkeypatch, set())
    out = asyncio.run(p.resolve_entities(
        "What was the total expenditure in KHATARSHNONG LAITKROH during 2024-25 for MGNREGA", MG))
    assert out["resolved"].get("block") == "KHATARSHNONG LAITKROH" and "village_code" not in out["resolved"]


def test_bare_block_and_ac_mention_asks_which(monkeypatch):
    _stub_resolution(monkeypatch, {"village": "MAIRANG", "year": "2024-25"})
    _stub_exact_villages(monkeypatch, set())
    with pytest.raises(p.ClarificationNeeded) as e:
        asyncio.run(p.resolve_entities("How many households received employment in MAIRANG during 2024-25?", MG))
    labels = " ".join(o["label"] for o in e.value.options)
    assert "block" in labels and "constituency" in labels.lower()


# ── all-blocks QA: empty results and genuine zeros ──────────────────────────
def test_no_rows_is_said_plainly(monkeypatch):
    async def _count(sql, params):
        return [{"n": 0}]
    monkeypatch.setattr(p, "fetch_rows", _count)
    ans = asyncio.run(p._mgnrega_empty_answer(
        "SELECT ROUND(SUM(total_exp) / 100, 2) AS total_expenditure_crore FROM curated.v_expenditure "
        "WHERE lgd_block = 'KHATARSHNONG LAITKROH' AND year_key = 2024",
        [{"total_expenditure_crore": None}],
        {"block": "KHATARSHNONG LAITKROH", "year": "FY 2024-25"},
        {"block": "KHATARSHNONG LAITKROH", "year_key": 2024}))
    assert ans.startswith("No MGNREGA expenditure is recorded for Khatarshnong Laitkroh block in FY 2024-25")
    assert "null" not in ans.lower()


def test_zero_base_ratio_is_not_called_missing(monkeypatch):
    async def _count(sql, params):
        return [{"n": 35}]
    monkeypatch.setattr(p, "fetch_rows", _count)
    ans = asyncio.run(p._mgnrega_empty_answer(
        "SELECT ROUND(100.0 * SUM(households_completed_100_days) / NULLIF(SUM(households_employed), 0), 2) "
        "AS pct FROM curated.v_employment WHERE lgd_block = 'DEMDEMA'",
        [{"pct": None}], {"block": "DEMDEMA", "year": "FY 2024-25"}, {"block": "DEMDEMA", "year_key": 2024}))
    assert ans.startswith("This cannot be calculated for Demdema block")


def test_non_empty_rows_are_left_to_the_composer():
    assert asyncio.run(p._mgnrega_empty_answer("SELECT 1", [{"a": 0}], {}, {})) is None


def test_hedge_over_a_genuine_zero_is_replaced():
    rows = [{"households_employed": 0}]
    hedge = ("The data available doesn't cover the number of households that received employment "
             "for the Demdema block in FY 2024-25.")
    out = p._mgnrega_zero_backstop(hedge, rows, {"block": "DEMDEMA", "year": "FY 2024-25"})
    assert out.startswith("0 households employed for Demdema block in FY 2024-25")
    ok = "0 households received employment in Demdema block during FY 2024-25."
    assert p._mgnrega_zero_backstop(ok, rows, {}) == ok
    assert any("genuine recorded ZERO" in n for n in p._mgnrega_null_zero_notes(rows))
    assert any("never write" in n for n in p._mgnrega_null_zero_notes(
        [{"total_expenditure_lakh": None, "person_days": 134161}]))


# ── all-villages QA: the village-disambiguation chip resume ─────────────────
def _stub_chip_pin_db(monkeypatch, villages):
    """villages: list of (code, name, block, district) rows in dim_geography."""
    async def _rows(sql, params):
        if "dim_geography" in sql:
            b, d = params
            return [{"village_code": c, "lgd_village_name": n, "lgd_block": bb, "lgd_district": dd}
                    for c, n, bb, dd in villages if bb.upper() == b.upper() and dd.upper() == d.upper()]
        return []
    monkeypatch.setattr(p, "fetch_rows", _rows)


GARO = [(272807, "Belbari", "DEMDEMA", "WEST GARO HILLS"),
        (272900, "Belbari", "SELSELLA", "WEST GARO HILLS"),
        (272808, "Demdema", "DEMDEMA", "WEST GARO HILLS"),
        (273667, "KATALBARI", "RERAPARA", "SOUTH WEST GARO HILLS"),
        (273783, "KATALBARI", "BETASING", "SOUTH WEST GARO HILLS")]


def test_village_chip_names_one_village(monkeypatch):
    _stub_chip_pin_db(monkeypatch, GARO)
    pin = asyncio.run(p._mgnrega_village_chip_pin(
        "What was the total expenditure in Belbari during 2024-25 for MGNREGA, DEMDEMA block, WEST GARO HILLS"))
    assert pin["village_code"] == 272807          # the DEMDEMA Belbari, not the SELSELLA one
    pin = asyncio.run(p._mgnrega_village_chip_pin(
        "person-days in KATALBARI during 2024-25, the village, not another area type, "
        "RERAPARA block, SOUTH WEST GARO HILLS"))
    assert pin["village_code"] == 273667


def test_no_chip_tail_no_pin(monkeypatch):
    _stub_chip_pin_db(monkeypatch, GARO)
    assert asyncio.run(p._mgnrega_village_chip_pin("person-days in Belbari during 2024-25")) is None


def test_village_chip_resume_resolves_the_village_not_the_block(monkeypatch):
    # Before the fix the chip tail's "block" was read as the stated level: the
    # village was cleared and the whole DEMDEMA block was reported as Belbari.
    _stub_resolution(monkeypatch, {"village": "Belbari", "year": "2024-25",
                                   "block": "DEMDEMA", "district": "WEST GARO HILLS"})
    _stub_chip_pin_db(monkeypatch, GARO)
    out = asyncio.run(p.resolve_entities(
        "What was the total expenditure in Belbari during 2024-25 for MGNREGA, DEMDEMA block, WEST GARO HILLS",
        MG, village_hint="Belbari"))
    assert out["resolved"]["village_code"] == 272807
    assert "block" not in out["resolved"]          # village_code alone goes to SQL


def test_typed_block_tail_without_a_chip_stays_a_block_question():
    # 2026-09-18 BATABARI: a user-typed ", X block, DISTRICT" (no village_hint) means the block.
    q = "How many households in BATABARI, BATABARI block, WEST GARO HILLS"
    assert p._explicit_level_in(q, MG) == "block"


def test_more_than_five_same_name_villages_can_all_be_offered():
    import inspect
    src = inspect.getsource(p.resolve_entities)
    # MGNREGA and (since 2026-09-27) Focus Plus — see pipeline._village_scheme
    assert "_vcap = 10 if _village_scheme(schemes) else 5" in src
    assert "candidates[:5]" not in src and "_vhits[:5]" not in src


# ── all-villages QA: the SQL must filter the resolved village ───────────────
ER_V = {"resolved": {"village_code": 277449, "year_key": 2024}}


def test_missing_village_filter_is_caught():
    assert p._mgnrega_village_filter_missing(
        MG, ER_V, "SELECT SUM(total_exp) FROM curated.v_expenditure WHERE year_key = 2024") == 277449
    assert p._mgnrega_village_filter_missing(
        MG, ER_V, "SELECT SUM(total_exp) FROM curated.v_expenditure "
                  "WHERE lgd_block = 'RONGRAM' AND year_key = 2024") == 277449
    assert p._mgnrega_village_filter_missing(
        MG, ER_V, "SELECT SUM(total_exp) FROM curated.v_expenditure WHERE village_code = 277449") is None
    assert p._mgnrega_village_filter_missing(["PMAY-G"], ER_V, "SELECT 1") is None


def test_wrong_village_code_is_replaced_by_the_resolved_one():
    er = {"resolved": {"village_code": 273749, "year_key": 2024}}
    sql = "SELECT SUM(person_days) FROM curated.v_employment WHERE village_code = 276411 AND year_key = 2024"
    assert "village_code = 273749" in p._mgnrega_pin_village_code(MG, er, sql)
    ok = sql.replace("276411", "273749")
    assert p._mgnrega_pin_village_code(MG, er, ok) == ok
    assert p._mgnrega_pin_village_code(["PMAY-G"], er, sql) == sql


def test_verifier_complaint_on_correct_village_sql_is_discarded():
    resolved = {"village_code": 273990, "year_key": 2024}
    good = "SELECT SUM(person_days) FROM curated.v_employment WHERE village_code = 273990 AND year_key = 2024"
    issue = ("Check 2: The RESOLVED ENTITIES block specifies village_code = 273990, but the question asks "
             "for 'MALIPARA'. The SQL uses 273990, which is the code for a different village.")
    assert p._verifier_village_code_complaint_is_false(issue, MG, resolved, good)
    # a real mismatch still stands
    assert not p._verifier_village_code_complaint_is_false(issue, MG, resolved, good.replace("273990", "111"))
    assert not p._verifier_village_code_complaint_is_false(issue, MG, resolved, good.replace("2024", "2023"))
    # Focus Legacy joined this guard on 2026-09-29 (KI-161: the "district dropped"
    # complaint beside a pinned village_code sent answers to the KB fallback);
    # CM Elevate Legacy joined it the same night (KI-173, live 2,614/2,614). A
    # multi-scheme question that does not start with MGNREGA is still outside it.
    assert p._verifier_village_code_complaint_is_false(issue, ["CM Elevate Legacy"], resolved, good)
    assert not p._verifier_village_code_complaint_is_false(issue, ["PMAY-G", "Focus Plus"], resolved, good)


# ── all-villages QA: qualified names and "Garo" inside a village name ───────
def test_qualified_village_name_is_not_truncated(monkeypatch):
    _stub_resolution(monkeypatch, {"block": "LASKARPARA", "district": "HAJONG", "year": "2024-25"})

    async def _scan(q, scheme):
        return ("LASKARPARA (HAJONG)", [{"village_code": 273379, "name": "LASKARPARA (HAJONG)",
                                         "district": "WEST GARO HILLS", "block": "TIKRIKILLA"}])

    async def _village(text, **k):
        if text == "LASKARPARA (HAJONG)":
            return entity_resolver.Resolved("resolved", "village", text, canonical=273379)
        return entity_resolver.Resolved("not_found", "village", text)
    monkeypatch.setattr(p, "_scan_village_in_question", _scan)
    monkeypatch.setattr(p, "resolve_village", _village)
    out = asyncio.run(p.resolve_entities(
        "What was the total expenditure in LASKARPARA (HAJONG) during 2024-25 for MGNREGA", MG))
    assert out["resolved"].get("village_code") == 273379
    assert "district" not in out["resolved"]


def test_garo_region_check_is_skipped_once_a_village_is_resolved():
    import inspect
    # The region check moved, verbatim, from _answer_data into its clarification
    # stage when the DATA path was split into stages (D-032).
    src = inspect.getsource(p._data_clarification_stage)
    assert "_mg_place_settled" in src


def test_village_chip_text_does_not_double_a_qualifier():
    c = {"name": "LOWER NALBARI - I", "block": "DEMDEMA", "district": "WEST GARO HILLS"}
    q = "What was the total expenditure in LOWER NALBARI - I during 2024-25 for MGNREGA"
    assert p._village_chip_question(q, "LOWER NALBARI", c, MG) == \
        q + ", DEMDEMA block, WEST GARO HILLS"
    # other schemes keep the old substitution
    assert "- I - I" in p._village_chip_question(q, "LOWER NALBARI", c)


# ── all-villages QA: "BLOCK CAMPUS" and Unresolved placeholder candidates ──
def test_level_word_inside_a_village_name_is_not_a_stated_level(monkeypatch):
    _stub_resolution(monkeypatch, {"block": "BLOCK CAMPUS", "year": "2024-25"})

    async def _scan(q, scheme):
        return ("BLOCK CAMPUS", [{"village_code": 274903, "name": "BLOCK CAMPUS",
                                  "district": "EAST GARO HILLS", "block": "DAMBO RONGJENG"}])

    async def _village(text, **k):
        return entity_resolver.Resolved("resolved", "village", text, canonical=274903)
    monkeypatch.setattr(p, "_scan_village_in_question", _scan)
    monkeypatch.setattr(p, "resolve_village", _village)
    out = asyncio.run(p.resolve_entities("How many person-days were generated in BLOCK CAMPUS during 2024-25?", MG))
    assert out["resolved"].get("village_code") == 274903
    assert not any("not a C&RD block" in n for n in out.get("notes") or [])


def test_unresolved_placeholder_is_not_a_village_choice_for_mgnrega(monkeypatch):
    async def _village(text, **k):
        return entity_resolver.Resolved("ambiguous", "village", text, candidates=[
            {"village_code": 274903, "name": "BLOCK CAMPUS", "block": "DAMBO RONGJENG", "district": "EAST GARO HILLS"},
            {"village_code": 999, "name": "Unresolved / Not Yet Mapped", "block": None, "district": "SOUTH GARO HILLS"}])
    monkeypatch.setattr(p, "resolve_village", _village)
    r = asyncio.run(p._resolve_village_for(MG, "BLOCK CAMPUS"))
    assert r.status == "resolved" and r.canonical == 274903
    # Focus Legacy became a village scheme on 2026-10-02 and drops the placeholder too;
    # CM Elevate Legacy became a village scheme on 2026-10-02 and drops the placeholder too
    r = asyncio.run(p._resolve_village_for(["CM Elevate Legacy"], "BLOCK CAMPUS"))
    assert r.status == "resolved" and r.canonical == 274903


# ── all-villages QA: a village in the block slot, and place words in village names ─
def test_village_in_the_block_slot_moves_to_the_village(monkeypatch):
    _stub_resolution(monkeypatch, {"block": "RONGRA", "year": "2024-25"})

    async def _scan(q, scheme):
        return ("RONGRA", [{"village_code": 275928, "name": "RONGRA", "district": "SOUTH GARO HILLS",
                            "block": "GASUAPARA"}])

    async def _village(text, **k):
        return entity_resolver.Resolved("resolved", "village", text, canonical=275928)
    monkeypatch.setattr(p, "_scan_village_in_question", _scan)
    monkeypatch.setattr(p, "resolve_village", _village)
    out = asyncio.run(p.resolve_entities("What was the total expenditure in RONGRA during 2024-25 for MGNREGA", MG))
    assert out["resolved"].get("village_code") == 275928


def _stub_scan(monkeypatch, name, block="UMLING", district="RI BHOI"):
    async def _scan(q, scheme):
        return (name, [{"village_code": 1, "name": name, "block": block, "district": district}])
    monkeypatch.setattr(p, "_scan_village_in_question", _scan)


def test_longer_village_name_with_a_place_word_is_not_out_of_area(monkeypatch):
    _stub_scan(monkeypatch, "COAL INDIA COLONY")
    q = "How many person-days were generated in COAL INDIA COLONY during 2024-25?"
    assert asyncio.run(p._mgnrega_village_not_out_of_area(q, p.edge.detect_edge_case(q)))


def test_bare_state_named_village_asks_village_or_state(monkeypatch):
    _stub_scan(monkeypatch, "MANIPUR")
    q = "How many person-days were generated in MANIPUR during 2024-25?"
    with pytest.raises(p.ClarificationNeeded) as e:
        asyncio.run(p._mgnrega_village_not_out_of_area(q, p.edge.detect_edge_case(q)))
    village_q, state_q = (o["question"] for o in e.value.options)
    assert asyncio.run(p._mgnrega_village_not_out_of_area(village_q, p.edge.detect_edge_case(village_q)))
    hit = p.edge.detect_edge_case(state_q)
    assert hit and not asyncio.run(p._mgnrega_village_not_out_of_area(state_q, hit))   # still refused


def test_real_out_of_area_and_other_schemes_unchanged(monkeypatch):
    _stub_scan(monkeypatch, "MANIPUR")
    # PMAY-G joined on 2026-09-28: its village MANIPUR (277554) got the same refusal
    # in the PMAY-G all-villages run (KI-096), so a bare PMAY-G "Manipur" now ASKS —
    # see test_pmay_bare_manipur_asks. Schemes outside the change are unchanged.
    # CM Elevate joined too on 2026-09-28 (its MANIPUR village, KI-118) — see
    # test_cm_elevate_bare_manipur_asks. CM Elevate Legacy joined on 2026-10-02 (its
    # village MANIPUR) — a bare Legacy "Manipur" now asks too.
    for q in ("What is MGNREGA expenditure in Assam?",):
        assert not asyncio.run(p._mgnrega_village_not_out_of_area(q, p.edge.detect_edge_case(q)))


def test_cm_elevate_bare_manipur_asks(monkeypatch):
    _stub_scan(monkeypatch, "MANIPUR")
    q = "How many CM Elevate applicants in Manipur?"
    with pytest.raises(p.ClarificationNeeded):
        asyncio.run(p._mgnrega_village_not_out_of_area(q, p.edge.detect_edge_case(q)))


def test_pmay_bare_manipur_asks(monkeypatch):
    _stub_scan(monkeypatch, "MANIPUR")
    q = "How many PMAY houses in Manipur?"
    with pytest.raises(p.ClarificationNeeded):
        asyncio.run(p._mgnrega_village_not_out_of_area(q, p.edge.detect_edge_case(q)))


# ── all-villages QA: "&" / "INCL" names, stray geo filters, IN-lists ────────
def _stub_village_names(monkeypatch, names):
    table = {n.upper(): [{"village_code": c, "name": n, "block": b, "district": d}] for c, n, b, d in names}

    async def _names():
        return table
    monkeypatch.setattr(p, "_mgnrega_village_names", _names)


EWKH = [(277073, "MAWKOHMIT & MAWKYNSAH", "MAIRANG", "EASTERN WEST KHASI HILLS"),
        (277069, "LAWRIAT INCL DOMMUSUR", "MAIRANG", "EASTERN WEST KHASI HILLS"),
        (277106, "UMTHLONG KYRSEN & KHARJANA", "MAIRANG", "EASTERN WEST KHASI HILLS"),
        (279378, "Kharkhana", "MAIRANG", "EASTERN WEST KHASI HILLS")]


def test_longest_whole_village_name_is_found(monkeypatch):
    _stub_village_names(monkeypatch, EWKH)
    for q, want in (("What was the total expenditure in MAWKOHMIT & MAWKYNSAH during 2024-25?", 277073),
                    ("person-days in LAWRIAT INCL DOMMUSUR during 2024-25?", 277069),
                    ("expenditure in UMTHLONG KYRSEN & KHARJANA during 2024-25", 277106)):
        name, cands = asyncio.run(p._mgnrega_longest_village_in(q))
        assert cands[0]["village_code"] == want


@pytest.mark.parametrize("mentions", [
    {"year": "2024-25"},                                              # extractor found nothing
    {"block": "KHARJANA", "year": "2024-25"},                         # a fragment in the block slot
    {"blocks": ["LAWRIAT", "DOMMUSUR"], "year": "2024-25"},           # split into two "blocks"
])
def test_fragments_are_replaced_by_the_whole_village(monkeypatch, mentions):
    _stub_resolution(monkeypatch, mentions)
    _stub_village_names(monkeypatch, EWKH)
    q = {"KHARJANA": "What was the total expenditure in UMTHLONG KYRSEN & KHARJANA during 2024-25 for MGNREGA",
         "blocks": "How many person-days were generated in LAWRIAT INCL DOMMUSUR during 2024-25?",
         }.get(mentions.get("block") or ("blocks" if "blocks" in mentions else ""),
               "What was the total expenditure in MAWKOHMIT & MAWKYNSAH during 2024-25 for MGNREGA")
    want = {"UMTHLONG": 277106, "LAWRIAT": 277069, "MAWKOHMIT": 277073}[q.split(" in ")[1].split()[0]]

    async def _village(text, **k):
        hit = [c for c, n, b, d in EWKH if n.upper() == str(text).upper()]
        return (entity_resolver.Resolved("resolved", "village", text, canonical=hit[0]) if hit
                else entity_resolver.Resolved("not_found", "village", text))
    monkeypatch.setattr(p, "resolve_village", _village)
    out = asyncio.run(p.resolve_entities(q, MG))
    assert out["resolved"].get("village_code") == want
    assert "village_code_list" not in out["resolved"]


def test_block_and_district_filters_beside_the_village_are_dropped():
    er = {"resolved": {"village_code": 277048, "year_key": 2024}}
    sql = ("SELECT SUM(total_exp) AS t FROM curated.v_expenditure WHERE year_key = 2024   AND village_code = 277048"
           "   AND lgd_block = 'MAIRANG'   AND lgd_district = 'EAST KHASI HILLS' LIMIT 1")
    out = p._mgnrega_drop_geo_beside_village(MG, er, sql)
    assert "lgd_" not in out and "village_code = 277048 LIMIT 1" in out
    block_q = "SELECT SUM(total_exp) FROM curated.v_expenditure WHERE lgd_block = 'MAIRANG'"
    assert p._mgnrega_drop_geo_beside_village(MG, {"resolved": {"block": "MAIRANG"}}, block_q) == block_q


def test_extra_codes_in_an_in_list_are_pinned_to_the_resolved_village():
    er = {"resolved": {"village_code": 277073, "year_key": 2024}}
    sql = "SELECT SUM(person_days) FROM curated.v_employment WHERE village_code IN (277073, 277074)"
    assert p._mgnrega_pin_village_code(MG, er, sql).endswith("village_code = 277073")
    multi = "SELECT 1 WHERE village_code IN (1, 2)"
    assert p._mgnrega_pin_village_code(MG, {"resolved": {"village_code_list": [1, 2]}}, multi) == multi


def test_own_name_beats_an_alias_hit(monkeypatch):
    async def _village(text, **k):
        return entity_resolver.Resolved("ambiguous", "village", text, candidates=[
            {"village_code": 277263, "name": "EAST RANGASORA", "block": "RANIKOR", "district": "SWKH"},
            {"village_code": 277262, "name": "WEST RANGASORA", "block": "RANIKOR", "district": "SWKH"}])
    monkeypatch.setattr(p, "resolve_village", _village)
    r = asyncio.run(p._resolve_village_for(MG, "WEST RANGASORA"))
    assert r.status == "resolved" and r.canonical == 277262


def test_true_same_name_villages_still_ask(monkeypatch):
    async def _village(text, **k):
        return entity_resolver.Resolved("ambiguous", "village", text, candidates=[
            {"village_code": 1, "name": "ASIMGRE", "block": "DALU", "district": "WGH"},
            {"village_code": 2, "name": "ASIMGRE", "block": "SELSELLA", "district": "WGH"}])
    monkeypatch.setattr(p, "resolve_village", _village)
    assert asyncio.run(p._resolve_village_for(MG, "ASIMGRE")).status == "ambiguous"


def test_scheme_word_inside_a_village_name_is_not_a_scheme_signal(monkeypatch):
    _stub_village_names(monkeypatch, [(277913, "UMRAN DAIRY", "UMLING", "RI BHOI")])
    q = "What was the total expenditure in UMRAN DAIRY during 2024-25?"
    masked = asyncio.run(p._mask_scheme_words_in_village_name(q))
    assert "UMRAN DAIRY" not in masked and p._infer_scheme_from_terms(masked) is None
    # an explicit dairy-scheme question keeps its meaning
    q2 = "How many dairy scheme applicants in UMRAN DAIRY?"
    assert p._infer_scheme_from_terms(asyncio.run(p._mask_scheme_words_in_village_name(q2))) == ["CM Elevate"]
    # a village name without scheme words is left alone
    _stub_village_names(monkeypatch, [(274695, "DAMASH", "RESUBELPARA", "NORTH GARO HILLS")])
    q3 = "What was the total expenditure in DAMASH during 2024-25?"
    assert asyncio.run(p._mask_scheme_words_in_village_name(q3)) == q3


def test_chosen_village_level_resolves_a_name_that_is_also_a_block(monkeypatch):
    # SELSELLA is a block and a village; the officer chose "the village" and the
    # extractor returned no place at all.
    _stub_resolution(monkeypatch, {"year": "2024-25"})
    _stub_village_names(monkeypatch, [(272951, "SELSELLA", "SELSELLA", "WEST GARO HILLS")])

    async def _village(text, **k):
        return entity_resolver.Resolved("resolved", "village", text, canonical=272951)
    monkeypatch.setattr(p, "resolve_village", _village)
    out = asyncio.run(p.resolve_entities(
        "What was the total expenditure in SELSELLA during 2024-25 for MGNREGA, the village, not another area type", MG))
    assert out["resolved"].get("village_code") == 272951


def test_three_letter_village_needs_a_place_preposition(monkeypatch):
    _stub_village_names(monkeypatch, [(278836, "MOT", "X", "Y"), (1, "KUT", "X", "Y")])
    assert asyncio.run(p._mgnrega_longest_village_in("person-days in MOT during 2024-25"))[0] == "MOT"
    assert asyncio.run(p._mgnrega_longest_village_in("what is the KUT total")) is None


# ── women employment: FY 2025-26 is unrecorded at source (user report 2026-09-27) ─
YEAR_WOMEN = [{"year_key": 2022, "fy": "2022-23", "women": 309225.0},
              {"year_key": 2023, "fy": "2023-24", "women": 314863.0},
              {"year_key": 2024, "fy": "2024-25", "women": 210328.0},
              {"year_key": 2025, "fy": "2025-26", "women": 0.0}]


@pytest.fixture
def _women_years(monkeypatch):
    monkeypatch.setattr(p, "_MGNREGA_YEAR_WOMEN", [dict(y) for y in YEAR_WOMEN])


def test_percentage_or_women_question_without_a_year_asks_the_year():
    r = {"district": "EAST KHASI HILLS"}
    assert p._needs_year_clarification("What percentage of employment persons were women in ekh?", MG, r)
    assert p._needs_year_clarification("What is the women share in ekh?", MG, r)
    assert not p._needs_year_clarification("What percentage of employment persons were women in ekh?",
                                           MG, {**r, "year_key": 2024})
    # other schemes unchanged
    assert not p._needs_year_clarification("What is the women share in ekh?", ["Focus Plus"], r)


def test_women_year_chips_offer_only_years_with_data(_women_years):
    e = asyncio.run(p._mgnrega_women_year_clarification("What percentage of employment persons were women in ekh?"))
    labels = [o["label"] for o in e.options]
    assert "FY 2025-26" not in labels and "FY 2024-25" in labels
    assert labels[-1].startswith("All years with women data (FY 2022-23 to FY 2024-25)")
    assert "not recorded for FY 2025-26" in e.question


def test_women_query_without_a_year_uses_only_recorded_years(_women_years):
    info = asyncio.run(p._mgnrega_women_query(
        "What percentage of employment persons were women in ekh across all financial years", MG,
        {"district": "EAST KHASI HILLS"}))
    assert info["kind"] == "query" and info["all_years"]
    assert info["params"][-1] == [2022, 2023, 2024]
    assert "women_share_pct" in info["sql"] and "NULLIF" in info["sql"]


def test_women_query_for_an_unrecorded_year_is_not_a_zero(_women_years):
    info = asyncio.run(p._mgnrega_women_query(
        "What percentage of employment persons were women in ekh during 2025-26?", MG,
        {"district": "EAST KHASI HILLS", "year_key": 2025}))
    assert info["kind"] == "unrecorded" and info["year"]["fy"] == "2025-26"

    async def _latest(sql, params):
        return [{"w": 34537, "p": 44450, "pct": 77.70}]
    import pytest as _pt  # noqa: F401
    p_fetch = p.fetch_rows
    try:
        p.fetch_rows = _latest
        ans = asyncio.run(p._mgnrega_women_unrecorded_answer(info, {"district": "East Khasi Hills"}))
    finally:
        p.fetch_rows = p_fetch
    assert "not recorded" in ans and "77.70%" in ans and "FY 2024-25" in ans and "0.00" not in ans


def test_women_rankings_stay_with_the_generator(_women_years):
    assert asyncio.run(p._mgnrega_women_query(
        "Which district had the highest women employment during 2024-25?", MG, {"year_key": 2024})) is None


def test_generator_women_sql_over_all_years_is_restricted(_women_years):
    sql = ("SELECT ROUND(100.0 * SUM(women_employment_provided)::numeric / NULLIF(SUM(persons_employed), 0), 2) "
           "FROM curated.v_employment WHERE lgd_district = 'EAST KHASI HILLS'")
    out = p._mgnrega_women_years_only(MG, sql)
    assert "year_key IN (2022, 2023, 2024)) AS v_employment WHERE" in out
    aliased = "SELECT SUM(e.women_employment_provided) FROM curated.v_employment e GROUP BY e.lgd_district"
    assert "IN (2022, 2023, 2024)) AS e GROUP BY" in p._mgnrega_women_years_only(MG, aliased)
    with_year = sql + " AND year_key = 2024"
    assert p._mgnrega_women_years_only(MG, with_year) == with_year
    assert p._mgnrega_women_years_only(["PMAY-G"], sql) == sql


# ── Verifier false positive: a resolved block "not listed" (live 2026-10-02) ──
_DALU_ISSUE = ("Check 2: The RESOLVED ENTITIES block explicitly lists 'lgd_district = WEST GARO HILLS' "
               "as a required filter. The SQL includes this filter, which is correct. However, the SQL "
               "also includes 'lgd_block = DALU'. While the question asks for Dalu block, the RESOLVED "
               "ENTITIES block does NOT list 'lgd_block = DALU'.")
_DALU_RESOLVED = {"district": "WEST GARO HILLS", "block": "DALU", "year_key": 2024}
_DALU_SQL = ("SELECT SUM(person_days) AS person_days FROM curated.v_employment "
             "WHERE lgd_district = 'WEST GARO HILLS' AND lgd_block = 'DALU' AND year_key = 2024")


def test_verifier_unlisted_resolved_block_on_correct_sql_is_discarded():
    assert p._verifier_unlisted_geo_is_false(_DALU_ISSUE, _DALU_RESOLVED, _DALU_SQL)


@pytest.mark.parametrize("sql", [
    _DALU_SQL.replace(" AND lgd_block = 'DALU'", ""),                         # block really missing
    _DALU_SQL.replace("AND year_key", "AND lgd_block = 'GAMBEGRE' AND year_key"),  # another place too
])
def test_verifier_unlisted_complaint_stands_when_the_sql_is_wrong(sql):
    assert not p._verifier_unlisted_geo_is_false(_DALU_ISSUE, _DALU_RESOLVED, sql)


def test_verifier_unlisted_rule_needs_a_resolved_place_named():
    assert not p._verifier_unlisted_geo_is_false("the query sums across years", _DALU_RESOLVED, _DALU_SQL)
    assert not p._verifier_unlisted_geo_is_false(_DALU_ISSUE, {"year_key": 2024}, _DALU_SQL)


def test_verify_sql_discards_the_unlisted_block_complaint(monkeypatch):
    async def fake(prompt, **_k):
        import json
        return json.dumps({"ok": False, "issue": _DALU_ISSUE})
    monkeypatch.setattr(p.llm, "call_sql_verifier", fake)
    out = asyncio.run(p._verify_sql("person-days in Dalu block of West Garo Hills in FY 2024-25",
                                    ["MGNREGA"], {"resolved": _DALU_RESOLVED}, _DALU_SQL))
    assert out is None
