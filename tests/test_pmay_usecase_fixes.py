"""
PMAY-G use-case QA fixes (PMAY-G.csv, 2026-09-28).

The live QA (17 / 28) found these bot defects (KNOWN_ISSUES KI-089 … KI-095);
each test pins the fix to the real production function.

  KI-089  "financial summary" / comparisons grouped by financial year (row dumps,
          "sanctioned credit released 41.79 crore", invented "38,899 for both").
  KI-090  remaining amount / difference / sanctioned amount left out.
  KI-091  "received only part of their sanctioned amount" / "full sanctioned amount
          but their house" resolved to house_status 'House Sanctioned'.
  KI-092  utilisation as AVG(released)/AVG(sanctioned).
  KI-093  "which has more: A or B" answered from LIMIT 1.
  KI-094  "15 December 2020": premise check read "15" as an assumed figure.
  KI-095  a village's ₹9,40,000 printed "0.09 crore".

No model or DB: result rows are built by hand in the shape _pmay_facts_query returns.
    python -m pytest tests/test_pmay_usecase_fixes.py -q
"""
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app import premise_check  # noqa: E402
from app.entity_resolver import load_all as load_resolver, resolve_house_status  # noqa: E402

PM = ["PMAY-G"]
ALL = " across all financial years"


def row(key, name, houses, sanctioned, released, completed=0, **kw):
    r = {"entity_key": key, "name": name, "block": kw.pop("block", "MAWPHLANG"),
         "district": kw.pop("district", "EAST KHASI HILLS"), "houses": houses, "sanctioned": sanctioned,
         "released": released, "released_on_sanctioned": released, "sanctioned_positive": sanctioned,
         "completed": completed, "incomplete": houses - completed, "no_status": 0, "full_release": 0,
         "part_release": 0, "no_release": 0, "full_not_done": 0, "sanction_numbers": 0}
    for c in p._PMAY_STAGE_COL.values():
        r[c] = 0
        r["fnd_" + c] = 0
    r["st_completed"] = completed
    r.update(kw)
    return r


# ---------------------------------------------------------------- shape detection
@pytest.mark.parametrize("q,expected", [
    # exactly what the use case asks for — no house count / release % extras (2026-09-28)
    ("Give me the PMAY financial summary for Mawphlang block.", ["sanctioned", "released", "remaining"]),
    ("How much sanctioned amount is still to be released in Lempluh?", ["remaining"]),
    ("How many beneficiaries in Lempluh have received only part of their sanctioned amount?", ["part"]),
    ("How many beneficiaries in Lempluh have received their full sanctioned amount?", ["full"]),
    ("How many beneficiaries in Lempluh have not received any amount?", ["none"]),
    ("How many beneficiaries in Lempluh have received the full sanctioned amount but their house is still not completed?",
     ["full_not_done"]),
    ("How many houses were sanctioned and how much was released in Ri Bhoi during 2019-20?",
     ["houses", "sanctioned", "released"]),
    ("Give me the house status breakdown for Ri Bhoi.", ["status"]),
    ("What percentage of PMAY houses are completed in Ri Bhoi?", ["completion_pct"]),
    ("What percentage of the sanctioned amount has been released in West Garo Hills?", ["release_pct"]),
    ("How many unique sanction numbers are there in Lempluh?", ["sanction_numbers"]),
    ("How many houses are still incomplete in Lempluh?", ["incomplete"]),
])
def test_use_case_shapes_are_recognised(q, expected):
    assert p._pmay_metrics(q, False) == expected
    assert p._pmay_metrics(q + ALL, False) == expected       # the year chip's suffix changes nothing


def test_comparisons_are_recognised_for_named_areas():
    assert p._pmay_metrics("Which has more completed houses: Maweitnar or Rohonpara?", True) == ["completed"]
    assert p._pmay_metrics("Compare the sanctioned amounts of Umsning and Amlarem blocks.", True) == ["sanctioned"]
    assert p._pmay_metrics("Compare PMAY performance between East Khasi Hills and Ri Bhoi.", True) == \
        ["houses", "sanctioned", "released", "completed"]


@pytest.mark.parametrize("q", [
    "Which district has the most completed houses?",
    "How many houses were completed in each district?",
    "Show the trend of PMAY sanctions by year",
    "How many houses went to women in Ri Bhoi?",
    "What is the average sanctioned amount per house?",
    "How many villages in Ri Bhoi have PMAY houses?",
    "How many houses are in progress in Ri Bhoi?",
    "How many houses were sanctioned between 2019 and 2021?",
    "Top 5 blocks by sanctioned amount",
])
def test_other_shapes_stay_on_the_model_path(q):
    assert p._pmay_metrics(q, False) is None


def test_query_only_for_pmay_and_known_filters():
    q = "How many PMAY beneficiaries are there in Ri Bhoi?"
    assert p._pmay_facts_query(q, ["MGNREGA"], {"district": "RI BHOI"}) is None
    assert p._pmay_facts_query(q, PM, {"district": "RI BHOI", "assembly_constituency": "X"}) is None
    spec = p._pmay_facts_query(q, PM, {"district": "RI BHOI"})
    assert spec["dim"] == "district" and spec["params"][0] == ["RI BHOI"]
    assert "NOT is_placeholder" in spec["sql"] and "$1" in spec["sql"]         # parameter-bound
    assert "COALESCE(amount_released, 0)" in spec["sql"]                         # 336 NULL releases


def test_unresolved_place_is_never_answered_statewide():
    q = "How many PMAY beneficiaries are there in Nongxyz?"
    assert p._pmay_facts_query(q, PM, {}, ["'Nongxyz' is not a known village — say so."]) is None
    assert p._pmay_facts_query(q, PM, {}) is None
    assert p._pmay_facts_query("How many PMAY houses are there in Meghalaya?", PM, {})["dim"] == "state"


def test_date_and_year_filters():
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned on 15 December 2020?", PM, {})
    assert spec["date"] == datetime.date(2020, 12, 15) and "sanction_date = $1" in spec["sql"]
    assert p._pmay_date("on 26/10/2017") == datetime.date(2017, 10, 26)
    assert p._pmay_date("on 2020-03-03") == datetime.date(2020, 3, 3)
    spec = p._pmay_facts_query("How many houses were sanctioned and how much was released in Ri Bhoi during 2019-20?",
                               PM, {"district": "RI BHOI", "year_key": 2019})
    assert "year_key = $2" in spec["sql"] and spec["params"][1] == 2019


# ---------------------------------------------------------------- answers (KI-089/090/095)
def test_financial_summary_states_all_three_amounts_exactly():
    spec = p._pmay_facts_query("Give me the PMAY financial summary for Mawphlang block.", PM, {"block": "MAWPHLANG"})
    ans = p._pmay_facts_answer(spec, [row("MAWPHLANG", "MAWPHLANG", 3214, 417820000, 412692500)])
    assert "₹41,78,20,000" in ans and "₹41,26,92,500" in ans and "₹51,27,500" in ans
    assert "41.79" not in ans


def test_village_money_is_exact_not_rounded_crore():
    spec = p._pmay_facts_query("How much sanctioned amount is still to be released in Lempluh?", PM,
                               {"village_code": 278247})
    ans = p._pmay_facts_answer(spec, [row(278247, "LEMPLUH", 52, 6760000, 5820000)])
    assert "₹9,40,000" in ans and "0.09 crore" not in ans


def test_two_area_comparison_names_both_and_the_difference():
    spec = p._pmay_facts_query("Which has more completed houses: Maweitnar or Rohonpara?", PM,
                               {"village_code_list": [277852, 274237]})
    ans = p._pmay_facts_answer(spec, [row(277852, "MAWEITNAR", 52, 1, 1, completed=19),
                                      row(274237, "ROHONPARA", 52, 1, 1, completed=23)])
    assert "Maweitnar" in ans and "19" in ans and "Rohonpara is higher by 4" in ans


def test_status_breakdown_lists_each_stage_once():
    spec = p._pmay_facts_query("Give me the house status breakdown for Ri Bhoi.", PM, {"district": "RI BHOI"})
    r = row("RI BHOI", "RI BHOI", 17525, 1, 1, completed=12056, st_roof_cast=3415, st_plinth=1737,
            st_house_sanctioned=317)
    ans = p._pmay_facts_answer(spec, [r])
    assert "Completed 12,056, Roof Cast 3,415, Plinth 1,737, House Sanctioned 317" in ans
    assert ans.count("Roof Cast") == 1 and "2017-18 —" not in ans


def test_place_with_no_rows_falls_back_to_the_model_path():
    spec = p._pmay_facts_query("How many PMAY beneficiaries are there in Ri Bhoi?", PM, {"district": "RI BHOI"})
    assert p._pmay_facts_answer(spec, []) is None


def test_zero_on_a_date_is_a_genuine_zero():
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned on 1 January 2019?", PM, {})
    assert "No PMAY-G houses were sanctioned" in p._pmay_facts_answer(spec, [])


def test_indian_grouping_and_units():
    assert p._pmay_money(6760000) == "₹67,60,000 (₹67.60 lakh)"
    assert p._pmay_money(22227530000) == "₹22,22,75,30,000 (₹2,222.75 crore)"
    assert p._pmay_money(94500) == "₹94,500"


# ---------------------------------------------------------------- model-path guards (KI-089/092/093)
def test_year_group_by_on_a_whole_period_question_is_repaired():
    sql = ("SELECT financial_year_short, SUM(sanctioned_amount) FROM curated.v_pmay WHERE NOT is_placeholder "
           "AND lgd_block = 'MAWPHLANG' GROUP BY financial_year_short")
    assert p._pmay_sql_issue("Give me the PMAY financial summary for Mawphlang block" + ALL, PM, sql)
    assert p._pmay_sql_issue("Show year-wise sanctions for Mawphlang block", PM, sql) is None
    assert p._pmay_sql_issue("Give me the PMAY financial summary for Mawphlang block", ["MGNREGA"], sql) is None


def test_avg_based_utilisation_is_repaired():
    sql = ("SELECT ROUND(AVG(amount_released) / NULLIF(AVG(sanctioned_amount), 0) * 100, 2) FROM curated.v_pmay "
           "WHERE NOT is_placeholder AND lgd_district = 'EAST KHASI HILLS'")
    assert p._pmay_sql_issue("What is the utilisation rate in East Khasi Hills?", PM, sql)
    assert p._pmay_sql_issue("What is the average amount released per house?", PM, sql) is None


def test_comparison_limit_one_is_widened():
    sql = "SELECT lgd_village_name, COUNT(*) FROM curated.v_pmay GROUP BY 1 ORDER BY 2 DESC\nLIMIT 1"
    er = {"resolved": {"village_code_list": [277852, 274237]}}
    assert p._pmay_comparison_limit(PM, er, sql).endswith("LIMIT 2")
    assert p._pmay_comparison_limit(PM, {"resolved": {"village_code": 1}}, sql).endswith("LIMIT 1")


# ---------------------------------------------------------------- KI-091 resolver
@pytest.fixture(scope="module", autouse=True)
def _resolver():
    load_resolver()


@pytest.mark.parametrize("q", [
    "How many beneficiaries in Lempluh have received only part of their sanctioned amount?",
    "How many beneficiaries in Lempluh have received the full sanctioned amount but their house is still not completed?",
    "What is the total amount sanctioned in Lempluh?",
    "houses sanctioned in 2023",
])
def test_sanctioned_amount_is_not_the_house_sanctioned_stage(q):
    assert resolve_house_status(q, "PMAY-G") is None


@pytest.mark.parametrize("q", ["how many houses are at house sanctioned stage in Ri Bhoi",
                               "how many houses are at sanctioned stage", "houses at sanction stage"])
def test_real_stage_phrases_still_resolve(q):
    assert resolve_house_status(q, "PMAY-G").values == ["House Sanctioned"]


def test_other_stages_unchanged():
    assert resolve_house_status("how many houses at plinth level", "PMAY-G").values == ["Plinth"]
    assert resolve_house_status("roof cast houses in Ri Bhoi", "PMAY-G").values == ["Roof Cast"]


# ---------------------------------------------------------------- KI-094 written dates
def test_written_date_is_a_date_not_an_assumed_figure():
    q = "How many PMAY houses were sanctioned on 15 December 2020?"
    assert p._CALENDAR_DATE_RE.search(q)
    assert not premise_check.extract_premises(q)
    assert p._CALENDAR_DATE_RE.search("sanctioned on December 15, 2020")
    assert not p._CALENDAR_DATE_RE.search("15 houses in 2020")


# ---------------------------------------------------------------- extra taps found in the QA
@pytest.mark.parametrize("q", ["How much sactioned amount has been released in Lempluh?",
                               "What is the sancioned amount in Lempluh?"])
def test_misspelled_sanctioned_amount_still_means_pmay(q):
    assert p._PMAY_ONLY_TERMS.search(q)


def test_unrelated_words_do_not_pin_pmay():
    assert not p._PMAY_ONLY_TERMS.search("the sanctuary amount")


# ---------------------------------------------------------------- model-path money, genuine-zero village
def test_bare_rupee_cells_are_formatted_and_nothing_else():
    rows = [{"lgd_block": "DEMDEMA", "total_sanctioned": 795860000.0}, {"lgd_block": "SONGSAK", "total_sanctioned": 793390000.0}]
    out = p._pmay_rupee_format("DEMDEMA leads at 795,860,000.00, followed by SONGSAK at 793,390,000.00.", rows)
    assert "₹79,58,60,000 (₹79.59 crore), followed" in out and "₹79,33,90,000" in out
    assert p._pmay_rupee_format("The average is 130000.00 rupees.", [{"avg_sanctioned_amount": 130000.0}]) == \
        "The average is ₹1,30,000 (₹1.30 lakh)."
    assert p._pmay_rupee_format("Ri Bhoi has 17,525 houses.", [{"houses": 17525}]) == "Ri Bhoi has 17,525 houses."
    assert p._pmay_rupee_format("released 41.27 crore", [{"released_cr": 41.27}]) == "released 41.27 crore"


def test_placeholder_only_village_is_a_plain_zero():
    ans = p._pmay_no_houses_answer({"entities": [279069]}, {"placeholders": 1, "name": "RTIANG SANPHEW",
                                                           "block": "NAMDONG", "district": "WEST JAINTIA HILLS"}, {})
    assert ans.startswith("No PMAY-G houses are recorded for Rtiang Sanphew village (Namdong block")
    assert "placeholder" in ans and "doesn't cover" not in ans


# ---------------------------------------------------------------- KI-096 village resolution (all-villages run)
import asyncio  # noqa: E402

from app.entity_resolver import Resolved  # noqa: E402

_PM_NAMES = {
    "BHANGARPAR": [{"village_code": 272746, "name": "BHANGARPAR", "block": "DEMDEMA", "district": "WEST GARO HILLS"}],
    "BHOLARBHITA (W)": [{"village_code": 272776, "name": "BHOLARBHITA (W)", "block": "DEMDEMA",
                         "district": "WEST GARO HILLS"}],
}


def _stub_pm_names(monkeypatch):
    async def names():
        return _PM_NAMES
    monkeypatch.setattr(p, "_pmay_village_names", names)


def test_pmay_is_a_village_grained_scheme():
    assert p._village_scheme(["PMAY-G"]) == "PMAY-G"


def test_exact_pmay_name_beats_a_fuzzy_look_alike(monkeypatch):
    _stub_pm_names(monkeypatch)

    async def generic(text, **kw):   # statewide lookup: exact + a fuzzy ANGARIPARA -> "which one?"
        return Resolved("ambiguous", "village", text, candidates=[
            {"village_code": 272999, "name": "ANGARIPARA", "block": "TIKRIKILLA", "district": "WEST GARO HILLS"},
            {"village_code": 272746, "name": "Bhangarpar", "block": "DEMDEMA", "district": "WEST GARO HILLS"}])
    monkeypatch.setattr(p, "resolve_village", generic)
    r = asyncio.run(p._resolve_village_for(["PMAY-G"], "Bhangarpar"))
    assert r.status == "resolved" and r.canonical == 272746


def test_pmay_name_beats_a_code_with_no_pmay_rows(monkeypatch):
    _stub_pm_names(monkeypatch)

    async def generic(text, **kw):   # the statewide lookup picked 272775 (no PMAY-G record)
        return Resolved("resolved", "village", text, canonical=272775)
    monkeypatch.setattr(p, "resolve_village", generic)
    r = asyncio.run(p._resolve_village_for(["PMAY-G"], "Bholarbhita (W)"))
    assert r.canonical == 272776


def test_unknown_place_is_said_plainly_not_as_zero():
    ans = p._pmay_unknown_place_answer(PM, {}, ["'Nongxyzabc' is not a known village — say so, do not filter on it."])
    assert ans.startswith("“Nongxyzabc” is not a village") and "0" not in ans
    # a place WAS resolved, or another scheme: leave it to the normal path
    assert p._pmay_unknown_place_answer(PM, {"district": "RI BHOI"}, ["'X' is not a known village — say so."]) is None
    assert p._pmay_unknown_place_answer(["MGNREGA"], {}, ["'X' is not a known village — say so."]) is None



# ---------------------------------------------------------------- village named beside its own block (user report)
_PM_NAMES2 = {
    "NONGSOHRAM": [{"village_code": 276435, "name": "NONGSOHRAM", "block": "RI MULIANG", "district": "WEST KHASI HILLS"}],
    "RONGRAM BAZAR": [{"village_code": 273541, "name": "RONGRAM BAZAR", "block": "RONGRAM", "district": "WEST GARO HILLS"}],
    "UMSNING": [{"village_code": 277929, "name": "UMSNING", "block": "UMSNING", "district": "RI BHOI"}],
    "MAWLAI": [{"village_code": 276564, "name": "MAWLAI", "block": "SHALLANG", "district": "WEST KHASI HILLS"}],
}


def _stub_pm_names2(monkeypatch):
    async def names():
        return _PM_NAMES2
    monkeypatch.setattr(p, "_pmay_village_names", names)


def test_village_named_beside_its_block_is_the_village(monkeypatch):
    _stub_pm_names2(monkeypatch)
    q = ("How much sanctioned amount is still to be released in NONGSOHRAM across all financial years, "
         "RI MULIANG block, WEST KHASI HILLS")
    r = asyncio.run(p._pmay_village_beside_block(q, {"block": "RI MULIANG", "district": "WEST KHASI HILLS"}))
    assert r and r["village_code"] == 276435


def test_village_containing_the_block_name(monkeypatch):
    _stub_pm_names2(monkeypatch)
    q = "How many PMAY beneficiaries are there in Rongram Bazar, RONGRAM block, WEST GARO HILLS"
    r = asyncio.run(p._pmay_village_beside_block(q, {"block": "RONGRAM", "district": "WEST GARO HILLS"}))
    assert r and r["village_code"] == 273541


def test_village_named_like_another_block(monkeypatch):
    _stub_pm_names2(monkeypatch)
    q = "How many PMAY beneficiaries are there in Mawlai, SHALLANG block, WEST KHASI HILLS"
    r = asyncio.run(p._pmay_village_beside_block(q, {"block": "SHALLANG", "district": "WEST KHASI HILLS"}))
    assert r and r["village_code"] == 276564


def test_block_question_stays_a_block_question(monkeypatch):
    _stub_pm_names2(monkeypatch)
    # UMSNING is also a village in Umsning block — "Umsning block" names the block (2026-09-18 rule)
    for q in ("How many PMAY beneficiaries are there in Umsning block?",
              "Give me the house status breakdown for Umsning block., RI BHOI"):
        assert asyncio.run(p._pmay_village_beside_block(q, {"block": "UMSNING", "district": "RI BHOI"})) is None
    # a village of ANOTHER block is not taken
    q = "How many PMAY beneficiaries are there in Nongsohram, RONGRAM block"
    assert asyncio.run(p._pmay_village_beside_block(q, {"block": "RONGRAM"})) is None


def test_village_named_like_its_own_block_when_the_name_is_repeated(monkeypatch):
    names = {"DEMDEMA": [{"village_code": 272799, "name": "DEMDEMA", "block": "DEMDEMA", "district": "WEST GARO HILLS"}]}

    async def _n():
        return names
    monkeypatch.setattr(p, "_pmay_village_names", _n)
    q = "How much sanctioned amount is still to be released in Demdema across all financial years, DEMDEMA block, WEST GARO HILLS"
    r = asyncio.run(p._pmay_village_beside_block(q, {"block": "DEMDEMA", "district": "WEST GARO HILLS"}))
    assert r and r["village_code"] == 272799
    # one mention, written as a block (the 2026-09-18 BATABARI shape): the block
    q = "How many PMAY houses are there in DEMDEMA block, WEST GARO HILLS?"
    assert asyncio.run(p._pmay_village_beside_block(q, {"block": "DEMDEMA", "district": "WEST GARO HILLS"})) is None


# ---------------------------------------------------------------- scenario run (2026-09-28): old-house words, zero naming
@pytest.mark.parametrize("q,want", [
    ("How many beneficiaries in Old Bhaitbari, West Garo Hills have received the full sanctioned amount but their house "
     "is still not completed", None),
    ("how many houses are at old house stage in Ri Bhoi", ["Existing site(Old House)"]),
    ("how many existing site houses", ["Existing site(Old House)"]),
])
def test_old_house_words_only_as_a_phrase(q, want):
    r = resolve_house_status(q, "PMAY-G")
    assert (r.values if r else None) == want


def test_zero_answer_never_prints_a_bare_code():
    spec = p._pmay_facts_query("How many PMAY houses at plinth stage in Old Bhaitbari?", PM,
                               {"village_code": 272820, "house_status": "Plinth"})
    spec["display"] = {"village": "Old Bhaitbari"}
    ans = p._pmay_facts_answer(spec, [])
    assert "272820" not in ans and "Old Bhaitbari village" in ans and "0 houses" in ans


def test_year_key_printed_as_a_bare_year_becomes_an_fy():
    rows = [{"year_key": 2017, "houses": 2390}, {"year_key": 2018, "houses": 564}]
    out = p._pmay_fy_labels("2,390 houses were sanctioned in 2017 and 564 in 2018.", rows)
    assert out == "2,390 houses were sanctioned in FY 2017-18 and 564 in FY 2018-19."
    assert p._pmay_fy_labels("2,390 houses in 2017.", [{"houses": 1}]) == "2,390 houses in 2017."


def test_two_named_villages_become_a_comparison(monkeypatch):
    names = {"NONGTALANG MISSION": [{"village_code": 279343, "name": "NONGTALANG MISSION", "block": "AMLAREM",
                                     "district": "WEST JAINTIA HILLS"}],
             "SOHKHA MISSION": [{"village_code": 279336, "name": "SOHKHA MISSION", "block": "AMLAREM",
                                 "district": "WEST JAINTIA HILLS"}],
             "MISSION": [{"village_code": 1, "name": "MISSION", "block": "X", "district": "Y"}]}

    async def _n():
        return names
    monkeypatch.setattr(p, "_pmay_village_names", _n)
    r = asyncio.run(p._pmay_two_villages("Which has more completed houses: Nongtalang Mission or Sohkha Mission?"))
    assert [c["village_code"] for c in r] == [279343, 279336]


def test_model_path_crore_division_becomes_exact_rupees():
    sql = ("SELECT ROUND(SUM(sanctioned_amount) / NULLIF(COUNT(*), 0) / 10000000.0, 2) AS avg_sanctioned_amount_crore "
           "FROM curated.v_pmay WHERE NOT is_placeholder")
    out = p._pmay_crore_to_rupees(PM, sql)
    assert "10000000" not in out and "AS avg_sanctioned_amount_rupees" in out
    assert p._pmay_rupee_format("The average is 130000.00.", [{"avg_sanctioned_amount_rupees": 130000.0}]) == \
        "The average is ₹1,30,000 (₹1.30 lakh)."
    assert p._pmay_crore_to_rupees(["MGNREGA"], sql) == sql



def test_village_whose_name_ends_in_village_is_not_doubled():
    spec = p._pmay_facts_query("How many PMAY beneficiaries are there in Model Village?", PM, {"village_code": 277481})
    ans = p._pmay_facts_answer(spec, [row(277481, "MODEL VILLAGE", 26, 1, 1, block="UMLING", district="RI BHOI")])
    assert "Model Village (Umling block, Ri Bhoi)" in ans and "Village village" not in ans


# ---------------------------------------------------------------- 21 Sep sheet recheck: a typed year is never dropped
def test_bare_year_is_kept_and_flagged_for_the_calendar_reading():
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned in 2023?", PM, {})
    assert spec["year_key"] == 2023 and spec["bare_year"] == 2023 and "year_key = $1" in spec["sql"]
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned in FY 2023-24?", PM, {"year_key": 2023})
    assert spec["bare_year"] is None


def test_calendar_year_filters_on_sanction_date():
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned in 2023?", PM, {}, calendar_year=2023)
    assert spec["year_key"] is None and spec["params"] == [datetime.date(2023, 1, 1), datetime.date(2024, 1, 1)]
    assert "sanction_date >= $1" in spec["sql"] and "calendar year 2023" in p._pmay_period(spec)


# ---------------------------------------------------------------- no extra information (user, 2026-09-28)
def test_single_figures_carry_no_side_details():
    r = row(278247, "LEMPLUH", 52, 6760000, 5820000, completed=20, incomplete=32, st_roof_cast=22, sanction_numbers=5)
    for q, res, banned in [
        ("How much sanctioned amount is still to be released in Lempluh?", "₹9,40,000", ["sanctioned,", "released"]),
        ("How many PMAY houses have been completed in Lempluh?", ": 20.", ["of 52"]),
        ("How many houses are still incomplete in Lempluh?", ": 32.", ["Roof Cast"]),
        ("How many unique sanction numbers are there in Lempluh?", ": 5.", ["covering"]),
        ("How many PMAY beneficiaries are there in Lempluh?", ": 52.", ["houses sanctioned"]),
    ]:
        ans = p._pmay_facts_answer(p._pmay_facts_query(q, PM, {"village_code": 278247}), [r])
        assert res in ans and not any(b in ans.split(": ", 1)[1] for b in banned), (q, ans)


def test_summaries_list_only_the_asked_figures():
    r = row("RI BHOI", "RI BHOI", 17525, 2278250000, 2228533500, completed=12056)
    fin = p._pmay_facts_answer(p._pmay_facts_query("Give me the PMAY financial summary for Ri Bhoi.", PM,
                                                   {"district": "RI BHOI"}), [r])
    assert "sanctioned" in fin and "released" in fin and "still to be released" in fin
    assert "houses" not in fin.split(":", 1)[1] and "%" not in fin
    perf = p._pmay_facts_answer(p._pmay_facts_query("Give me the overall PMAY performance of Ri Bhoi.", PM,
                                                    {"district": "RI BHOI"}), [r])
    assert "17,525" in perf and "Completed houses: 12,056" in perf and "%" not in perf and "still to be" not in perf


def test_follow_up_answers_only_what_the_user_typed():
    tok = p._PMAY_TYPED_TURN.set("and how many received only part of it?")
    try:
        spec = p._pmay_facts_query("How many beneficiaries in Lempluh have not received any amount till date and how "
                                   "many received only part of it?", PM, {"village_code": 278247})
    finally:
        p._PMAY_TYPED_TURN.reset(tok)
    assert spec["metrics"] == ["part"]
    tok = p._PMAY_TYPED_TURN.set("and in East Khasi Hills?")      # no figure typed: keep the rewrite's
    try:
        spec = p._pmay_facts_query("How many PMAY beneficiaries are there in East Khasi Hills till date?", PM,
                                   {"district": "EAST KHASI HILLS"})
    finally:
        p._PMAY_TYPED_TURN.reset(tok)
    assert spec["metrics"] == ["houses"]



# ---------------------------------------------------------------- result table / Sources (user report 2026-09-29)
def test_result_table_holds_only_the_asked_figures():
    r = row("SELSELLA", "SELSELLA", 5028, 653640000, 647401500, completed=4457, st_roof_cast=412)
    spec = p._pmay_facts_query("How many PMAY beneficiaries are there in Selsella block?", PM, {"block": "SELSELLA"})
    assert p._pmay_display_rows(spec, [r]) == [{"block": "Selsella", "houses": 5028}]
    spec = p._pmay_facts_query("Give me the PMAY financial summary for Selsella block.", PM, {"block": "SELSELLA"})
    assert list(p._pmay_display_rows(spec, [r])[0]) == ["block", "sanctioned amount (Rs)", "amount released (Rs)",
                                                        "still to be released (Rs)"]


def test_facts_sql_has_no_from_true():
    spec = p._pmay_facts_query("How many houses are still incomplete in Lempluh?", PM, {"village_code": 278247})
    assert "FROM TRUE" not in spec["sql"].upper() and "FROM TRUE" not in spec["shown"].upper()


def test_calendar_year_zero_is_a_genuine_zero():
    spec = p._pmay_facts_query("How many PMAY houses were sanctioned in Ri Bhoi during 2016?", PM,
                               {"district": "RI BHOI"}, calendar_year=2016)
    assert "No PMAY-G houses were sanctioned" in p._pmay_facts_answer(spec, [])



# ── PMAY-G officer cases, every place (2026-10-02) ───────────────────────────
@pytest.mark.parametrize("q,expected", [
    ("How many PMAY-G beneficiaries in UMSAW have received their full sanctioned amount, JIRANG block, RI BHOI "
     "across all financial years", True),
    ("How many houses in UMSAW, JIRANG block, RI BHOI for FY 2022-23", True),
    ("How many houses in UMSAW, JIRANG block, RI BHOI", False),            # the village hint covers this one
    ("houses in Jirang block across all financial years", False),          # no village chip tail
])
def test_a_year_chip_after_a_village_chip_is_recognised(q, expected):
    # the year pause replaced the remembered village hint; without this the
    # village was re-asked forever (UMSAW, JIRANG block)
    assert bool(p._YEAR_AFTER_CHIP_TAIL_RE.search(q)) is expected


def test_two_village_comparison_settles_the_place():
    # "MALANG KHASI or SIDAKANDI" asked "which Khasi Hills district?"
    import inspect
    assert '"village_code_list"' in inspect.getsource(p._data_clarification_stage)


def test_burma_beside_another_village_is_the_village(monkeypatch):
    async def names(scheme):
        return {"BURMA": [{"village_code": 278981}], "JEWILGRE": [{"village_code": 1}]}

    async def scan(q, scheme):
        return ("JEWILGRE", [{"village_code": 1, "block": "X", "district": "Y"}])
    monkeypatch.setattr(p, "_scheme_village_names", names)
    monkeypatch.setattr(p, "_scan_village_in_question", scan)
    hit = {"type": "off_topic"}
    q = "Which has more completed PMAY-G houses: JEWILGRE or BURMA?"
    assert asyncio.run(p._mgnrega_village_not_out_of_area(q, hit)) is True


def test_chosen_village_reading_lets_a_block_named_village_compare(monkeypatch):
    async def names():
        return {"PECHUA": [{"village_code": 273011, "name": "PECHUA"}],
                "LASKEIN": [{"village_code": 279237, "name": "LASKEIN"}]}
    monkeypatch.setattr(p, "_pmay_village_names", names)
    monkeypatch.setattr(p, "canonical_names", lambda s, d: ["LASKEIN"] if d == "block" else [])
    bare = "Which has more completed PMAY-G houses: PECHUA or LASKEIN?"
    chosen = bare.rstrip("?") + ", the village, not the block across all financial years"
    assert asyncio.run(p._pmay_two_villages(bare)) is None           # LASKEIN is also a block: ask first
    assert {c["village_code"] for c in asyncio.run(p._pmay_two_villages(chosen))} == {273011, 279237}
