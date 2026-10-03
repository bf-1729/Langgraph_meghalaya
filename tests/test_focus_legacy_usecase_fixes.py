"""
Focus Legacy use-case QA fixes (Use_Cases_-_Focus.csv, 2026-09-25).

Round 2 of the QA found these bot defects; each test pins the fix to the real
production function, and the neighbouring behaviour it must NOT change.

  TC-13  "Is there any Producer Group named Nongstoin PG?" went to the scheme
         recommender, then became a block-vs-village question.
  TC-14b / TC-15  group size SUMmed across a group's payments (20 -> 40).
  TC-18  entity_type <> 'Unresolved' dropped 11 no-village payments from a
         district total.
  TC-19  541 groups qualified; the answer listed 23 and never said so.
  TC-12  repeat-paid groups called "duplicates".
  TC-20 / TC-22 / TC-25  the SQL verifier rejected correct one-table SQL
         ("prohibited join", check 2 with nothing resolved).
  TC-23  the extractor dropped "FY 2024-25"; the scope chip then dropped the FY.
  TC-09  the briefing retrieved the institutions chunks, not the overview.

Pure Python — no model or DB.
    python -m pytest tests/test_focus_legacy_usecase_fixes.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import annotations  # noqa: E402
from app import pipeline as p  # noqa: E402

FL = ["Focus Legacy"]


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()


# ── TC-18: the Unresolved exclusion stays off non-village Focus Legacy SQL ───
def test_district_total_keeps_no_village_payments():
    sql = ("SELECT SUM(amount_disbursed) AS amount_disbursed FROM curated.v_focus_legacy "
           "WHERE lgd_district = 'EAST KHASI HILLS' AND entity_type <> 'Unresolved' LIMIT 1")
    out = p._cm_legacy_keep_unresolved_off_village(
        "What is the total amount disbursed for East Khasi Hills?", FL, sql)
    assert "Unresolved" not in out
    assert "lgd_district = 'EAST KHASI HILLS'" in out


def test_village_count_keeps_the_exclusion():
    sql = ("SELECT COUNT(DISTINCT village_code) FROM curated.v_focus_legacy "
           "WHERE entity_type <> 'Unresolved'")
    assert p._cm_legacy_keep_unresolved_off_village("How many villages?", FL, sql) == sql


def test_other_schemes_untouched():
    sql = "SELECT COUNT(*) FROM curated.v_pmay WHERE entity_type <> 'Unresolved'"
    assert p._cm_legacy_keep_unresolved_off_village("total houses", ["PMAY-G"], sql) == sql


# ── TC-23: an explicit FY the extractor dropped is back-filled ────────────────
def test_explicit_fy_backfilled():
    q = "What was the total amount remitted in FY 2024-25 for Focus Legacy"
    assert p._backfill_explicit_year(q, FL, {}) == {"year": "2024-25"}


@pytest.mark.parametrize("q", [
    "Compare FY 2022-23 and FY 2024-25",          # two years: not a single-year slot
    "Total remitted in FY 2023-24",                # absent year: the gap guard's job
    "Total remitted in 2024",                      # bare year: left to the extractor
])
def test_backfill_leaves_other_shapes_alone(q):
    assert p._backfill_explicit_year(q, FL, {}) == {}


def test_backfill_never_overrides_the_extractor():
    assert p._backfill_explicit_year("FY 2024-25", FL, {"year": "2022-23"}) == {"year": "2022-23"}


# ── TC-13: a PG name lookup is not a recommendation, nor a place ─────────────
def test_pg_name_lookup_is_not_a_recommendation():
    assert not p._is_recommendation_request("Is there any Producer Group named Nongstoin PG?")
    assert not p._is_recommendation_request("is there any pg called Sakania?")


@pytest.mark.parametrize("q", [
    "I am in a farmers' group, suggest a scheme",
    "my friend named Ram is a farmer, suggest me a scheme",
])
def test_real_recommendations_still_fire(q):
    assert p._is_recommendation_request(q)


def test_place_scans_do_not_see_the_group_name():
    assert "Nongstoin" not in p._question_without_pg_name(
        "Is there any Producer Group named Nongstoin PG?")
    q = "Total disbursement in Nongstoin block"
    assert p._question_without_pg_name(q) == q


# Reported live 2026-09-25: after a Betasing-block question, "is there any
# producer group named sakania pg?" was rewritten as a follow-up ("...in Betasing
# block") and answered "no such group" — the "there" of "is there" read as a
# back-reference. A name lookup is standalone unless it points back explicitly.
@pytest.mark.parametrize("q", [
    "is there any producer group named nongstoin pg?",
    "is there any producer group named sakania pg?",
    "is there any pg called sakania?",
    "Is there any Producer Group named Nongstoin PG in Betasing block?",
])
def test_name_lookup_is_not_a_followup(q):
    assert not p.looks_like_followup(q)


@pytest.mark.parametrize("q", [
    "is there any producer group named sakania pg there?",
    "is there a pg named sakania in that block?",
    "what about East Garo Hills?",
    "is there any data for it?",
])
def test_real_followups_unchanged(q):
    assert p.looks_like_followup(q)


def test_group_name_followed_by_a_place_is_still_a_group_name():
    q = "Is there any Producer Group named Nongstoin PG in Betasing block?"
    assert p._PG_NAMED_ENTITY.search(q).group("name") == "Nongstoin PG"
    assert "Betasing" in p._question_without_pg_name(q)
    assert "Nongstoin" not in p._question_without_pg_name(q)


# Bulk QA (290 sampled groups, 2026-09-25): group-name questions are answered
# deterministically. The parser must catch every group phrasing and leave
# place questions to the normal pipeline.
@pytest.mark.parametrize("q,kind,name", [
    ("How many members are there in Chelchak Pineapple P.g?", "size", "Chelchak Pineapple P.g"),
    ("How many members are there in Bak-15 Wachal Pg for all of Meghalaya, all years", "size", "Bak-15 Wachal Pg"),
    ("How many members are there in Teinam for Focus Legacy for all of Meghalaya, all years", "size", "Teinam"),
    ("How many members does Iatreilang have?", "size", "Iatreilang"),
    ("is there any producer group named sakania pg?", "exists", "sakania pg"),
    ("how many members are there in nongstoin pg?", "size", "nongstoin pg"),
])
def test_group_name_questions_are_parsed(q, kind, name):
    assert p._pg_name_question(q) == (kind, name)


@pytest.mark.parametrize("q", [
    "How many members are there in West Garo Hills?",
    "What is the total number of PG members in West Garo Hills?",
    "How many PG members were covered in Betasing block for Focus Legacy across all financial years?",
    "Is there any Producer Group named Sakania PG in Betasing block?",   # carries its own place
])
def test_place_questions_are_not_group_lookups(q):
    assert p._pg_name_question(q) is None


def test_group_name_words_ignore_suffix_and_punctuation():
    assert p._pg_name_tokens("Chelchak Pineapple P.g") == ["chelchak", "pineapple"]
    assert p._pg_name_tokens("Bak-15 Wachal Producer Group") == ["bak", "15", "wachal"]


def test_empty_raw_geo_columns_are_read_from_lgd():
    sql = ("SELECT COUNT(DISTINCT pg_id) FROM curated.v_focus_legacy "
           "WHERE UPPER(TRIM(block_name_raw)) = 'NONGSTOIN'")
    out = p._focus_legacy_geo_columns(FL, sql)
    assert "lgd_block" in out and "block_name_raw" not in out
    assert p._focus_legacy_geo_columns(["CM Elevate Legacy"], sql) == sql


# ── TC-14b / TC-15: group size is MAX per group, never a SUM of payments ─────
def test_group_size_sum_is_caught():
    q = "List top 5 PGs which has more than 15 members"
    sql = ("SELECT pg_id, MAX(pg_name), SUM(no_of_pg_members) AS memberships FROM "
           "curated.v_focus_legacy GROUP BY pg_id HAVING SUM(no_of_pg_members) > 15")
    assert p._focus_legacy_group_size_summed(q, FL, sql)
    sql1 = ("SELECT SUM(no_of_pg_members) AS memberships FROM curated.v_focus_legacy "
            "WHERE pg_name ILIKE '%umtyrkhow%'")
    assert p._focus_legacy_group_size_summed("How many members are there in Umtyrkhow Pg?", FL, sql1)


def test_membership_totals_keep_their_sum():
    # TC-16 / TC-17: memberships paid for, district or statewide — SUM is right.
    sql = ("SELECT SUM(no_of_pg_members) AS memberships FROM curated.v_focus_legacy "
           "WHERE lgd_district = 'WEST GARO HILLS'")
    assert not p._focus_legacy_group_size_summed(
        "What is the total number of PG members in West Garo Hills?", FL, sql)
    # A money question carries SUM(no_of_pg_members) beside the amount by rule.
    money = ("SELECT pg_id, SUM(no_of_pg_members), SUM(amount_disbursed) FROM "
             "curated.v_focus_legacy GROUP BY pg_id HAVING SUM(amount_disbursed) > 100000")
    assert not p._focus_legacy_group_size_summed(
        "Which Producer Groups received more than Rs 1,00,000?", FL, money)
    assert not p._focus_legacy_group_size_summed(
        "How many members are there in X?", ["Focus Plus"],
        "SELECT SUM(no_of_pg_members) FROM t WHERE pg_name ILIKE '%x%'")


def test_group_size_examples_in_the_bank():
    top = annotations.few_shot_examples(FL, "How many members are there in Bak 15 Banana Dijogre?",
                                        top_k=3)
    assert any("MAX(no_of_pg_members)" in e["sql"] for e in top)


# ── TC-12: duplicates wording ────────────────────────────────────────────────
def test_duplicate_question_gets_the_repeat_payment_note():
    notes = p._focus_legacy_answer_notes("Are there any duplicate Producer Groups there?")
    assert notes and "REPEAT PAYMENT" in notes[0]
    assert p._focus_legacy_answer_notes("How many producer groups are there?") == []


# ── TC-20 / TC-22 / TC-25 / TC-23: verifier false positives ──────────────────
def test_prohibited_join_on_a_one_table_query_is_discarded():
    issue = ("Check 1: The SQL joins directly to 'curated.v_focus_legacy', which is a prohibited "
             "join. The rules state: 'NEVER join any -> curated.v_focus_legacy directly.'")
    sql = ("SELECT financial_year_short, COUNT(*) AS records FROM curated.v_focus_legacy "
           "GROUP BY financial_year_short")
    assert p._verifier_join_complaint_is_false(issue, sql)
    absent = ("PROHIBITED JOIN violated. The SQL joins directly to "
              "'curated.fact_focus_legacy_disbursement' instead of curated.v_focus_legacy")
    assert p._verifier_join_complaint_is_false(absent, sql)


def test_a_real_prohibited_join_still_raises():
    issue = "Check 1: prohibited join to curated.fact_pmay_house"
    sql = ("SELECT * FROM curated.v_focus_legacy f JOIN curated.fact_pmay_house h "
           "ON h.geography_key = f.geography_key")
    assert not p._verifier_join_complaint_is_false(issue, sql)
    assert not p._verifier_join_complaint_is_false("Check 4: wrong metric column", "SELECT 1")


def test_check2_with_nothing_resolved_is_discarded():
    issue = ("Check 2: The RESOLVED ENTITIES block is empty ... the SQL is filtering/aggregating "
             "on a dimension (district) that was not resolved")
    assert p._verifier_check2_on_empty_entities(issue, {})
    assert not p._verifier_check2_on_empty_entities(issue, {"district": "EAST KHASI HILLS"})
    assert not p._verifier_check2_on_empty_entities("Check 4: wrong metric", {})


def test_fy_label_filter_matches_the_resolved_year():
    issue = ("Check 2: The RESOLVED ENTITIES block specifies year_key = 2024, but the SQL "
             "filters on financial_year_short = '2024-25'.")
    ok = "SELECT SUM(amount_disbursed) FROM curated.v_focus_legacy WHERE financial_year_short = '2024-25'"
    bad = "SELECT SUM(amount_disbursed) FROM curated.v_focus_legacy WHERE financial_year_short = '2022-23'"
    assert p._verifier_year_complaint_is_false(issue, {"year_key": 2024}, ok)
    assert not p._verifier_year_complaint_is_false(issue, {"year_key": 2024}, bad)


# ── TC-09: an overview/briefing request ─────────────────────────────────────
def test_overview_request_detected():
    assert p._OVERVIEW_REQUEST.search(
        "Give me a short overview of Focus Legacy that I can use for an official briefing")
    assert not p._OVERVIEW_REQUEST.search("Who can benefit under the Focus Legacy scheme")


# ═══ 2026-09-29 re-test + all blocks / villages / constituencies / PGs ═══════
# KI-145 rows a per-place breakdown left out; KI-146 the constituency drill-down
# read MGNREGA for every scheme; KI-147 month numbers and bare rupees;
# KI-148 a constituency alone pinned MGNREGA; composer breakdown errors.
import asyncio  # noqa: E402
import datetime  # noqa: E402
from decimal import Decimal  # noqa: E402

from app import entity_resolver as er  # noqa: E402

_S26 = ("SELECT lgd_block, COUNT(DISTINCT pg_id) AS producer_groups FROM curated.v_focus_legacy "
        "WHERE lgd_district = 'WEST KHASI HILLS' AND lgd_block IS NOT NULL GROUP BY lgd_block "
        "ORDER BY producer_groups DESC")
_R26 = [{"lgd_block": "NONGSTOIN", "producer_groups": 460}, {"lgd_block": "MAWSHYNRUT", "producer_groups": 286},
        {"lgd_block": "SHALLANG", "producer_groups": 78}, {"lgd_block": "RAMBRAI", "producer_groups": 56},
        {"lgd_block": "RI MULIANG", "producer_groups": 41}]


def test_ki147_month_numbers_become_month_names():
    sql = ("SELECT EXTRACT(MONTH FROM date_of_remittance) AS month, SUM(amount_disbursed) AS amount_disbursed "
           "FROM curated.v_focus_legacy WHERE financial_year_short = '2022-23' GROUP BY 1")
    rows = [{"month": Decimal("4"), "amount_disbursed": Decimal("52035000")},
            {"month": Decimal("5"), "amount_disbursed": Decimal("89755000")},
            {"month": Decimal("1"), "amount_disbursed": Decimal("5000")}]
    out = p._focus_legacy_answer_guarantees(
        "52,035,000.00 in month 4, 89,755,000.00 in month 5 and 5,000 in month 1; total 141,795,000.", sql, rows)
    assert "April 2022" in out and "May 2022" in out and "January 2023" in out
    assert "month 4" not in out
    assert "₹5,20,35,000" in out and "₹8,97,55,000" in out and "₹14,17,95,000" in out


def test_ki147_date_month_column_and_single_total():
    rows = [{"month": datetime.date(2025, 6, 1), "amount_disbursed": 95440000.0}]
    out = p._focus_legacy_answer_guarantees("In 2025-06-01 the total was 95440000.00.", "SELECT ...", rows)
    assert "June 2025" in out and "₹9,54,40,000 (₹9.54 crore)" in out


def test_ki147_counts_are_not_rupees():
    rows = [{"producer_groups": 11906, "memberships": 102021}]
    ans = "11,906 producer groups with 102,021 members."
    assert p._focus_legacy_answer_guarantees(ans, "SELECT ...", rows) == ans


def test_ki145_not_null_filter_is_dropped_for_the_left_out_query():
    loose = p._fl_drop_not_null(_S26, "lgd_block")
    assert "IS NOT NULL" not in loose and "lgd_district = 'WEST KHASI HILLS'" in loose
    assert p._fl_drop_not_null("SELECT lgd_block, COUNT(*) n FROM v WHERE lgd_block IS NOT NULL GROUP BY 1",
                               "lgd_block") == "SELECT lgd_block, COUNT(*) n FROM v GROUP BY 1"


def test_ki145_left_out_row_is_read_from_the_data(monkeypatch):
    seen = []

    async def fake(sql):
        seen.append(sql)
        return _R26 + [{"lgd_block": None, "producer_groups": 5}]
    monkeypatch.setattr(p, "run_readonly", fake)
    level, hit = asyncio.run(p._focus_legacy_unplaced_row(_S26, _R26))
    assert level == "block" and hit["producer_groups"] == 5
    assert "IS NOT NULL" not in seen[0]


def test_ki145_no_extra_query_when_nothing_was_filtered(monkeypatch):
    async def boom(sql):
        raise AssertionError("must not query")
    monkeypatch.setattr(p, "run_readonly", boom)
    sql = _S26.replace(" AND lgd_block IS NOT NULL", "")
    assert asyncio.run(p._focus_legacy_unplaced_row(sql, _R26)) is None


def test_ki145_guarantee_states_the_figure_once_in_digits():
    ans = ("East Jaintia Hills has 624 producer groups. WAPUNG has 380, SAIPUNG has 212. "
           "Two additional producer groups have no block recorded in the source data.")
    out = p._focus_legacy_unplaced_guarantee(ans, "block", {"lgd_block": None, "producer_groups": 2})
    assert out.count("no block recorded") == 1 and "Two additional" not in out
    assert "2 producer groups have no block recorded" in out and "212" in out


def test_breakdown_is_built_from_the_rows():
    out = p._focus_legacy_breakdown_answer(_S26, _R26, {"district": "West Khasi Hills"},
                                           ("block", {"lgd_block": None, "producer_groups": 5}))
    for name, n in (("Nongstoin", 460), ("Mawshynrut", 286), ("Shallang", 78), ("Rambrai", 56), ("Ri Muliang", 41)):
        assert f"- {name}: {n} producer groups" in out
    assert "5 blocks above account for 921 producer groups" in out
    assert "5 producer groups have no block recorded" in out and "the total is 926 producer groups" in out


def test_breakdown_null_row_in_result_is_not_listed_as_a_block():
    rows = _R26 + [{"lgd_block": None, "producer_groups": 5}]
    out = p._focus_legacy_breakdown_answer(_S26.replace(" AND lgd_block IS NOT NULL", ""), rows, {}, None)
    assert "- None" not in out and "5 producer groups have no block recorded" in out


def test_breakdown_top_n_has_no_total_and_money_is_rupees():
    sql = ("SELECT lgd_district, SUM(amount_disbursed) AS amount_disbursed FROM curated.v_focus_legacy "
           "GROUP BY lgd_district ORDER BY 2 DESC LIMIT 3")
    rows = [{"lgd_district": "WEST GARO HILLS", "amount_disbursed": Decimal("120000000")},
            {"lgd_district": "RI BHOI", "amount_disbursed": Decimal("60000000")},
            {"lgd_district": "EAST KHASI HILLS", "amount_disbursed": Decimal("54205000")}]
    out = p._focus_legacy_breakdown_answer(sql, rows, {}, None)
    assert out.startswith("Top 3 districts") and "Together" not in out
    assert "- East Khasi Hills: ₹5,42,05,000 amount disbursed" in out


def test_breakdown_leaves_other_shapes_to_the_composer():
    fy = [{"financial_year_short": "2021-22", "records": 965}, {"financial_year_short": "2022-23", "records": 2139}]
    assert p._focus_legacy_breakdown_answer(
        "SELECT financial_year_short, COUNT(*) records FROM v GROUP BY 1", fy, {}, None) is None
    one = [{"producer_groups": 11906}]
    assert p._focus_legacy_breakdown_answer("SELECT COUNT(DISTINCT pg_id) producer_groups FROM v", one, {}, None) is None


@pytest.mark.parametrize("q,expected", [
    ("What is the total amount disbursed for Baghmara assembly constituency?", None),
    ("How many Producer Groups are mapped to Baghmara assembly constituency?", ["Focus Legacy"]),
    ("Total person-days in Baghmara assembly constituency", ["MGNREGA"]),
    ("How many households were employed in AC 12?", ["MGNREGA"]),
])
def test_ki148_constituency_alone_does_not_pin_mgnrega(q, expected):
    assert p._infer_scheme_from_terms(q) == expected


def test_ki148_scheme_question_offers_only_constituency_schemes():
    c = p._scheme_clarification("What is the total amount disbursed for Baghmara assembly constituency?")
    assert [o["label"].split(" (")[0] for o in c.options] == ["MGNREGA", "Focus Legacy", "CM Elevate Legacy"]
    assert len(p._scheme_clarification("total amount disbursed in 2024").options) == 7


def test_ki146_drilldown_names_the_scheme_data():
    contents = {"districts": ["EAST GARO HILLS"], "blocks": ["SAMANDA", "SONGSAK"], "villages": 88}
    c = p._ac_drilldown_clarification("How many PGs are mapped to Songsak", "SONGSAK", contents, "Focus Legacy")
    assert "88 villages in the Focus Legacy data" in c.question and "employment" not in c.question
    m = p._ac_drilldown_clarification("person-days in Songsak", "SONGSAK", contents)
    assert "MGNREGA employment data" in m.question


@pytest.mark.parametrize("scheme,table", [("Focus Legacy", "v_focus_legacy"),
                                          ("CM Elevate Legacy", "v_cm_elevate_disbursement"),
                                          ("MGNREGA", "v_employment"), (None, "v_employment")])
def test_ki146_constituency_contents_reads_the_asking_scheme(monkeypatch, scheme, table):
    seen = []

    async def fake(sql, params):
        seen.append(sql)
        return [{"lgd_district": "EAST GARO HILLS", "lgd_block": "SONGSAK", "village_code": 1}]
    monkeypatch.setattr(er, "fetch_rows", fake)
    out = asyncio.run(er.constituency_contents("SONGSAK", scheme))
    assert table in seen[0] and out == {"districts": ["EAST GARO HILLS"], "blocks": ["SONGSAK"], "villages": 1}


# ── all-PG run 2026-09-29: names the group-name parser could not read ────────
@pytest.mark.parametrize("name", [
    "Bak -13 Mikdok Chiring", "Bak-15 Ginger Pg 7/2021", "Law ' Arliang Pg", "Bak - 4 Aski Piggery Pg",
    "Bak- 15 Ginger Pg-65/2021", "Badaka Reserve Lily Pg/spg Group", "Agronggre Pig Fattening Pg -c",
    "All In One Ginger Producer Group, Ladmukhla", "Green Hills Producer Group", "Umdang Dong Block Producer Group",
])
def test_every_stored_name_shape_parses(name):
    for q, kind in ((f"Is there any Producer Group named {name}?", "exists"),
                    (f"How many members are there in {name}?", "size")):
        got = p._pg_name_question(q, allow_weak=True)
        assert got is not None and got[0] in (kind, "size?"), q
        assert p._pg_name_tokens(got[1]) == p._pg_name_tokens(name), q


@pytest.fixture()
def _places(monkeypatch):
    monkeypatch.setattr(p, "_known_place_names", lambda: ("west garo hills", "nongstoin", "songsak", "betasing"))


@pytest.mark.parametrize("q", [
    "How many members are there in West Garo Hills?",
    "How many members are there in West Garo Hills producer groups?",
    "How many members are there in Songsak block?",
    "How many members are there in Nongstoin block producer groups?",
    "Is there any Producer Group named Nongstoin PG in Betasing block?",
    "Is there any Producer Group named Nongstoin PG in Nongstoin?",
])
def test_place_questions_stay_place_questions(_places, q):
    assert p._pg_name_question(q, allow_weak=True) is None


def test_place_word_name_without_group_word_is_only_a_candidate(_places):
    assert p._pg_name_question("How many members are there in Green Hills?", allow_weak=True) == ("size?", "Green Hills")
    # the scheme pin (no allow_weak) never treats it as a group
    assert p._pg_name_question("How many members are there in Green Hills?") is None


def test_weak_candidate_needs_an_exact_group(monkeypatch, _places):
    async def none(sql):
        return []
    monkeypatch.setattr(p, "run_readonly", none)
    assert asyncio.run(p._focus_legacy_pg_name_answer("How many members are there in Green Hills?")) is None

    async def one(sql):
        if "matched_alias FROM" in sql:     # no earlier spellings (KI-020 query)
            return []
        return [{"pg_id": "PG-X-1", "pg_name": "Green Hills", "district": "RI BHOI", "block": "UMSNING",
                 "group_size": 12, "smallest_recorded_size": 12, "payments": 1, "amount_disbursed": 60000}]
    monkeypatch.setattr(p, "run_readonly", one)
    out = asyncio.run(p._focus_legacy_pg_name_answer("How many members are there in Green Hills?"))
    assert "12 members" in out["answer"]


def test_breakdown_accepts_an_aliased_place_column():
    sql = ("SELECT lgd_block AS block, COUNT(DISTINCT pg_id) AS producer_groups FROM curated.v_focus_legacy "
           "WHERE lgd_district = 'RI BHOI' GROUP BY lgd_block ORDER BY producer_groups DESC")
    rows = [{"block": "UMSNING", "producer_groups": 380}, {"block": "UMLING", "producer_groups": 251},
            {"block": None, "producer_groups": 6}]
    out = p._focus_legacy_breakdown_answer(sql, rows, {"district": "Ri Bhoi"}, None)
    assert "- Umsning: 380 producer groups" in out and "6 producer groups have no block recorded" in out
    assert "the total is 637 producer groups" in out


# ── all-villages run 2026-09-29: "for PAKREGRE CHIKAMA village" -> CHIKAMA ───
def test_village_phrase_is_read_from_the_text():
    rx = p._VILLAGE_PHRASE_RE
    assert rx.findall("What is the total amount disbursed for PAKREGRE CHIKAMA village?") == ["PAKREGRE CHIKAMA"]
    assert rx.findall("amount for East Garo Hills in Abima village") == ["Abima"]
    assert rx.findall("Which Producer Groups are mapped to NONGCHRAM (II) village?") == ["NONGCHRAM (II)"]


def _stub_village(monkeypatch, extractor_village, exact_names):
    seen = []

    async def extract(q):
        return {"village": extractor_village}

    async def resolve(text, *a, **k):
        seen.append(text)
        return er.Resolved("resolved", "village", text, canonical="274886" if "PAKREGRE" in text.upper() else "274919")

    async def exact(cands):
        return {c.upper(): [{"village_code": 1}] for c in cands if c.upper() in exact_names}

    async def nothing(*a, **k):
        return None

    async def no_names():
        return {}
    monkeypatch.setattr(p, "extract_entity_mentions", extract)
    monkeypatch.setattr(p, "resolve_village", resolve)
    monkeypatch.setattr(p, "village_names_exact", exact)
    monkeypatch.setattr(p, "_scan_village_in_question", nothing)
    monkeypatch.setattr(p, "_mgnrega_village_names", no_names)
    # Focus Legacy is a village scheme since 2026-10-02 and has its own name list
    monkeypatch.setattr(p, "_focus_legacy_village_names", no_names)
    return seen


def test_fragment_from_the_extractor_is_replaced_by_the_whole_name(monkeypatch):
    seen = _stub_village(monkeypatch, "CHIKAMA", {"PAKREGRE CHIKAMA"})
    asyncio.run(p.resolve_entities("What is the total amount disbursed for PAKREGRE CHIKAMA village for Focus Legacy", FL))
    assert seen and all("PAKREGRE CHIKAMA" in s.upper() for s in seen)


def test_whole_name_used_only_when_the_db_holds_it(monkeypatch):
    seen = _stub_village(monkeypatch, "CHIKAMA", set())
    asyncio.run(p.resolve_entities("What is the total amount disbursed for PAKREGRE CHIKAMA village for Focus Legacy", FL))
    assert seen and seen[0].upper() == "CHIKAMA"


def test_group_name_list_is_copied_from_the_rows():
    rows = [{"pg_id": "PG-FOCUS-EGH-1", "pg_name": "Bak 13 Nalsa Pepper Producer Group."},
            {"pg_id": "PG-FOCUS-EGH-2", "pg_name": "Bak 13 Orange Producer Group"}]
    out = p._focus_legacy_group_list_answer(rows, {"village": "Nengkra Awe"}, None)
    assert out.startswith("2 producer groups are mapped to Nengkra Awe village")
    assert "- Bak 13 Nalsa Pepper Producer Group. (PG-FOCUS-EGH-1)" in out
    capped = p._focus_legacy_group_list_answer(rows, {}, 40)
    assert capped.startswith("40 producer groups are in the Focus Legacy data; the first 2 are listed")


def test_group_list_leaves_figures_to_the_composer():
    rows = [{"pg_id": "PG-1", "pg_name": "Doram Pg", "amount_disbursed": 200000}]
    assert p._focus_legacy_group_list_answer(rows, {}, None) is None


# ── all-PG run 2026-09-29: edge refusals and mis-encoded names ───────────────
from app import edge  # noqa: E402


@pytest.mark.parametrize("q", [
    "Is there any Producer Group named Rakkam China Banana Group?",
    "How many members are there in Rakkam China Banana Group?",
    "How many members are there in Umden Manipur Banana Pg?",
    "Is there any Producer Group named Manipur Nongtluh Ginger Pg-1?",
    "How many members are there in Umiong U.s.t?",
])
def test_group_name_holding_a_country_or_state_is_not_out_of_area(q):
    assert edge.detect_edge_case(q) is None


@pytest.mark.parametrize("q", [
    "How many producer groups are there in Assam?",
    "How many members are there in producer groups in Assam?",
    "PMAY-G houses in the U.S.",
    "How many beneficiaries in China?",
])
def test_real_out_of_area_questions_still_refused(q):
    assert edge.detect_edge_case(q) is not None


def test_mis_encoded_name_keeps_its_own_letters():
    garbled = "AÃ£Æ’Ã¦â€™Ã£Â¢Ã¢â€šÂ¬Ã¥Â¡Ã£Æ’Ã¢â‚¬Å¡Ã£â€šÃ¢Â·we Producer Group"
    assert p._pg_name_tokens(garbled) != p._pg_name_tokens("A.we")
    assert p._pg_name_tokens("Bak-15 Ginger Pg 7/2021") == ["bak", "15", "ginger", "7", "2021"]
    assert all("'" not in t and "\\" not in t for t in p._pg_name_tokens("Law ' Arliang Pg"))


# ── all-villages run 2026-09-29: duplicate-village chip looped (KI-156) ──────
@pytest.mark.parametrize("q", [
    "What is the total amount disbursed for RONGSIGRE village for Focus Legacy, GASUAPARA block, SOUTH GARO HILLS",
    "What is the total amount disbursed for RONGSIGRE village for Focus Legacy, GASUAPARA block, SOUTH GARO HILLS "
    "across all financial years",
    "What is the total amount disbursed for RONGSIGRE village for Focus Legacy, GASUAPARA block, SOUTH GARO HILLS "
    "for FY 2022-23?",
])
def test_chip_tail_survives_a_later_year_chip(q):
    m = p._CHIP_TAIL_PARSE_RE.search(q)
    assert m and m.group("block") == "GASUAPARA" and m.group("district") == "SOUTH GARO HILLS"


def test_focus_legacy_village_chip_is_pinned_after_the_year_chip(monkeypatch):
    seen = {}

    async def fetch(sql, params):
        seen["params"] = params
        return [{"village_code": 275850, "lgd_village_name": "RONGSIGRE", "lgd_block": "GASUAPARA",
                 "lgd_district": "SOUTH GARO HILLS"}]

    async def extract(q):
        return {"village": "RONGSIGRE"}

    async def boom(*a, **k):
        raise AssertionError("the pinned village must not be re-resolved")
    monkeypatch.setattr(p, "fetch_rows", fetch)
    monkeypatch.setattr(p, "extract_entity_mentions", extract)
    monkeypatch.setattr(p, "resolve_village", boom)
    q = ("What is the total amount disbursed for RONGSIGRE village for Focus Legacy, GASUAPARA block, "
         "SOUTH GARO HILLS across all financial years")
    out = asyncio.run(p.resolve_entities(q, FL))       # no village_hint: the year pause carries none
    assert out["resolved"].get("village_code") == 275850
    assert seen["params"] == ["GASUAPARA", "SOUTH GARO HILLS"]


# ── all-PG run 2026-09-29: "Focus" inside a group name (KI-157) ──────────────
@pytest.mark.parametrize("q", ["How many members are there in Focus Bibari?",
                               "Is there any Producer Group named Focus Bibari?",
                               "How many members are there in Bak-6 Focus-a Producer Group?"])
def test_focus_inside_a_group_name_is_not_the_bare_scheme(q):
    assert not p._is_ambiguous_focus(q)


@pytest.mark.parametrize("q", ["What is focus?", "How many beneficiaries under focus?", "focus disbursement in 2024-25"])
def test_bare_focus_still_asks_which_focus(q):
    assert p._is_ambiguous_focus(q)


# ── final pass 2026-09-29: KI-158 underscore names, KI-159 village-code SQL ──
def test_underscore_names_are_matched_as_words(monkeypatch):
    seen = []

    async def rows(sql):
        seen.append(sql)
        if "matched_alias FROM" in sql:     # no earlier spellings (KI-020 query)
            return []
        return [{"pg_id": "PG-FOCUS-RB-18311", "pg_name": "Erifa_25", "district": "RI BHOI", "block": "BHOIRYMBONG",
                 "group_size": 1, "smallest_recorded_size": 1, "payments": 1, "amount_disbursed": 5000}]
    monkeypatch.setattr(p, "run_readonly", rows)
    out = asyncio.run(p._focus_legacy_pg_name_answer("How many members are there in Erifa_25?"))
    assert any("translate(pg_name, '_', ' ')" in s for s in seen)
    assert "has **1 member**." in out["answer"]


def test_block_literal_beside_the_village_code_is_dropped():
    sql = ("SELECT SUM(amount_disbursed) AS amount_disbursed FROM curated.v_focus_legacy "
           "WHERE village_code = 276311 AND lgd_block = 'RONGBARA' AND lgd_district = 'SOUTH GARO HILLS'")
    out = p._focus_legacy_village_code_only(FL, {"village_code": 276311}, sql)
    assert out.endswith("WHERE village_code = 276311")
    # no resolved village, or another scheme: untouched
    assert p._focus_legacy_village_code_only(FL, {}, sql) == sql
    assert p._focus_legacy_village_code_only(["MGNREGA"], {"village_code": 276311}, sql) == sql


def test_group_name_mask_survives_the_scheme_chip_tail():
    q = "How many members are there in Rakkam China Banana Group for Focus Legacy"
    assert edge.detect_edge_case(q) is None
    assert edge.detect_edge_case("How many members are there in Rakkam China Banana Group for Focus Legacy "
                                 "across all financial years") is None


# ── final pass 2026-09-29: KI-020 older spellings, KI-160 village chip ranking ─
def _fake_pg_db(monkeypatch, alias_rows, name_rows):
    seen = []

    async def rows(sql):
        seen.append(sql)
        if "v_focus_legacy_pg_search WHERE NOT is_current" in sql and sql.startswith("SELECT DISTINCT pg_id, matched_alias"):
            return alias_rows
        return name_rows
    monkeypatch.setattr(p, "run_readonly", rows)
    return seen


def test_older_spelling_finds_the_group(monkeypatch):
    grp = {"pg_id": "PG-FOCUS-EGH-11214", "pg_name": "Dronpa", "district": "EAST GARO HILLS", "block": "SONGSAK",
           "group_size": 9, "smallest_recorded_size": 9, "payments": 1, "amount_disbursed": 45000}
    seen = _fake_pg_db(monkeypatch, [{"pg_id": "PG-FOCUS-EGH-11214", "matched_alias": "Drongpa Pg"}], [grp])
    out = asyncio.run(p._focus_legacy_pg_name_answer("Is there any Producer Group named Drongpa Pg?"))
    assert "PG-FOCUS-EGH-11214" in out["answer"] and "earlier recorded spelling" in out["answer"]
    # current names and earlier spellings are searched together
    assert any("OR pg_id IN (SELECT pg_id FROM curated.v_focus_legacy_pg_search" in s for s in seen)


def test_older_spelling_equal_to_another_groups_current_name_lists_both(monkeypatch):
    rows = [{"pg_id": "PG-FOCUS-WGH-18951", "pg_name": "Ten Star Pg", "district": "WEST GARO HILLS", "block": "SELSELLA",
             "group_size": 7, "smallest_recorded_size": 7, "payments": 1, "amount_disbursed": 35000},
            {"pg_id": "PG-FOCUS-EGH-16089", "pg_name": "Ten Stars", "district": "EAST GARO HILLS", "block": "SONGSAK",
             "group_size": 5, "smallest_recorded_size": 5, "payments": 1, "amount_disbursed": 25000}]
    _fake_pg_db(monkeypatch, [{"pg_id": "PG-FOCUS-EGH-16089", "matched_alias": "Ten Star Producer Group"}], rows)
    out = asyncio.run(p._focus_legacy_pg_name_answer("Is there any Producer Group named Ten Star Producer Group?"))
    assert "PG-FOCUS-WGH-18951" in out["answer"] and "PG-FOCUS-EGH-16089" in out["answer"]
    assert "(2 named exactly that)" in out["answer"]


def test_villages_with_focus_legacy_data_are_offered_first(monkeypatch):
    cands = [{"village_code": c, "name": "MAWLONG"} for c in (278828, 278001, 278002, 278003, 278004, 278264)]

    async def have(sql, params):
        return [{"village_code": 278264}]
    monkeypatch.setattr(p, "fetch_rows", have)
    ranked = asyncio.run(p._fl_rank_villages(FL, cands))
    assert ranked[0]["village_code"] == 278264 and len(ranked) == 6
    assert asyncio.run(p._fl_rank_villages(["MGNREGA"], cands)) == cands


# ── final pass 2026-09-29: KI-161 verifier, KI-162 "Garo" names, KI-163 twins ─
def test_verifier_district_complaint_beside_village_code_is_discarded():
    issue = ("Check 2: The RESOLVED ENTITIES block specifies lgd_district = 'WEST GARO HILLS', but the SQL "
             "WHERE clause filters on village_code = '272807' and omits the required lgd_district filter.")
    sql = "SELECT DISTINCT pg_id, MAX(pg_name) AS pg_name FROM curated.v_focus_legacy WHERE village_code = '272807' GROUP BY pg_id"
    assert p._verifier_village_code_complaint_is_false(issue, FL, {"village_code": 272807, "district": "WEST GARO HILLS"}, sql)
    # CM Elevate Legacy joined this guard on 2026-09-29 (KI-173, live 2,614/2,614);
    # a multi-scheme question that does not start with MGNREGA is still outside it.
    assert p._verifier_village_code_complaint_is_false(issue, ["CM Elevate Legacy"], {"village_code": 272807}, sql)
    assert not p._verifier_village_code_complaint_is_false(issue, ["PMAY-G", "Focus Plus"], {"village_code": 272807}, sql)


@pytest.mark.parametrize("q,name", [
    ("What is the total amount disbursed for MAWKOHMIT & MAWKYNSAH village?", "MAWKOHMIT & MAWKYNSAH"),
    ("Which Producer Groups are mapped to SIEJLIEH - MAWIABAN village?", "SIEJLIEH - MAWIABAN"),
    ("Which Producer Groups are mapped to Jong - U - Shen village?", "Jong - U - Shen"),
])
def test_village_phrase_keeps_ampersands_and_spaced_hyphens(q, name):
    assert p._VILLAGE_PHRASE_RE.findall(q) == [name]


def test_village_named_after_a_state_is_not_out_of_area():
    assert edge.detect_edge_case("What is the total amount disbursed for MANIPUR village for Focus Legacy") is None
    assert edge.detect_edge_case("PMAY-G houses in Manipur") is not None


def test_same_block_twins_with_focus_legacy_data_are_both_offered(monkeypatch):
    cands = [{"village_code": 276758, "name": "UMSAW", "block": "NONGSTOIN", "district": "WEST KHASI HILLS"},
             {"village_code": 277000, "name": "UMSAW", "block": "JIRANG", "district": "RI BHOI"}]

    async def twins(sql, params):
        return [{"village_code": 276898, "name": "Umsaw", "block": "NONGSTOIN", "district": "WEST KHASI HILLS"}]
    monkeypatch.setattr(p, "fetch_rows", twins)
    out = asyncio.run(p._fl_expand_twins(FL, cands))
    assert [c["village_code"] for c in out] == [276758, 277000, 276898]
    labels = [p._village_chip_label(c, out, FL) for c in out]
    assert "UMSAW (LGD 276758) — NONGSTOIN block, WEST KHASI HILLS" in labels
    assert "Umsaw (LGD 276898) — NONGSTOIN block, WEST KHASI HILLS" in labels
    assert "UMSAW — JIRANG block, RI BHOI" in labels
    assert asyncio.run(p._fl_expand_twins(["MGNREGA"], cands)) == cands


# ── final-code re-test 2026-09-29: DATE_TRUNC month shifted by the timezone (KI-164) ──
def test_date_trunc_month_is_cast_to_a_date():
    sql = ("SELECT DATE_TRUNC('month', date_of_remittance) AS payment_month, SUM(amount_disbursed) AS amount_disbursed "
           "FROM curated.v_focus_legacy GROUP BY DATE_TRUNC('month', f.date_of_remittance)")
    out = p._focus_legacy_date_trunc_as_date(FL, sql)
    assert out.count("::date") == 2
    assert p._focus_legacy_date_trunc_as_date(FL, out) == out            # idempotent
    assert p._focus_legacy_date_trunc_as_date(["PMAY-G"], sql) == sql


# ── final regression 2026-09-29: bracketed names, id-only lists (KI-165) ─────
def test_bracketed_village_name_is_read_whole():
    assert p._VILLAGE_PHRASE_RE.findall("Which Producer Groups are mapped to RAMJONGGRE ( R ) village?") == ["RAMJONGGRE ( R )"]


def test_id_only_group_list_gets_names(monkeypatch):
    async def names(sql, params):
        assert params == [["PG-FOCUS-WGH-13632", "PG-FOCUS-WGH-3700"]]
        return [{"pg_id": "PG-FOCUS-WGH-13632", "pg_name": "Life Producer Group"},
                {"pg_id": "PG-FOCUS-WGH-3700", "pg_name": "Tangkamgipa Pg"}]
    monkeypatch.setattr(p, "fetch_rows", names)
    rows = asyncio.run(p._fl_add_group_names([{"pg_id": "PG-FOCUS-WGH-13632"}, {"pg_id": "PG-FOCUS-WGH-3700"}]))
    out = p._focus_legacy_group_list_answer(rows, {"village": "Shyamding ( Garo )"}, None)
    assert "- Life Producer Group (PG-FOCUS-WGH-13632)" in out and "- Tangkamgipa Pg (PG-FOCUS-WGH-3700)" in out
    # a result that already has names, or carries figures, is left alone
    assert asyncio.run(p._fl_add_group_names([{"pg_id": "X", "pg_name": "Y"}])) == [{"pg_id": "X", "pg_name": "Y"}]
    assert asyncio.run(p._fl_add_group_names([{"pg_id": "X", "amount": 5}])) == [{"pg_id": "X", "amount": 5}]


# ── Bare village names (all-villages run 2026-10-02) ─────────────────────────
def test_focus_legacy_is_a_village_scheme():
    # "producer groups in BOLDAMGRE" (no word "village") was answered with Selsella block's 399
    assert p._village_scheme(["Focus Legacy"]) == "Focus Legacy"


def test_village_name_in_the_district_column_beside_its_code_is_dropped():
    er_ = {"resolved": {"village_code": 272897}}
    sql = ("SELECT COUNT(DISTINCT pg_id) AS producer_groups FROM curated.v_focus_legacy "
           "WHERE village_code = 272897 AND UPPER(TRIM(lgd_district)) = 'KASHARIPARA' LIMIT 1000")
    assert p._mgnrega_drop_geo_beside_village(["Focus Legacy"], er_, sql) == (
        "SELECT COUNT(DISTINCT pg_id) AS producer_groups FROM curated.v_focus_legacy "
        "WHERE village_code = 272897 LIMIT 1000")
    # a question outside the village guards (several schemes) keeps its SQL
    keep = "SELECT 1 FROM t WHERE village_code = 272897 AND lgd_block = 'X'"
    assert p._mgnrega_drop_geo_beside_village(["PMAY-G", "Focus Plus"], er_, keep) == keep


def test_pinned_village_drops_a_trimmed_district_literal():
    # "WHERE UPPER(TRIM(lgd_district)) = 'KASHARIPARA'": the code was pinned but the
    # village-name-as-district literal stayed -> 0 for a village with 11 (2026-10-02)
    sql = ("SELECT COUNT(DISTINCT pg_id) AS producer_groups\nFROM curated.v_focus_legacy\n"
           "WHERE UPPER(TRIM(lgd_district)) = 'KASHARIPARA'\nLIMIT 1")
    out = p._pin_missing_village_code(["Focus Legacy"], {"resolved": {"village_code": 272897}}, sql)
    assert "lgd_district" not in out and "WHERE village_code = 272897" in out
    blk = sql.replace("UPPER(TRIM(lgd_district))", "f.lgd_block")
    assert "lgd_block" not in p._pin_missing_village_code(["Focus Legacy"], {"resolved": {"village_code": 272897}}, blk)


def test_hyphenated_number_word_becomes_digits():
    # "Twenty-one producer groups received payments under Focus Legacy in Khliehriat East"
    # (all-villages run 2026-10-02): a compound number word was left spelled out
    f = p._cme_digits_for_number_words
    assert f("Twenty-one producer groups received payments.", [{"producer_groups": 21}]).startswith("21 producer")
    assert f("Forty two applicants", [{"n": 42}]) == "42 applicants"
    assert f("Twenty producer groups", [{"n": 20}]) == "20 producer groups"
    # a value not in the result stays spelled out, for the faithfulness checks to see
    assert f("Twenty-one producer groups", [{"n": 20}]) == "Twenty-one producer groups"


def test_lowered_district_literal_beside_village_is_dropped():
    # "village_code = 277038 AND LOWER(lgd_district) = 'nongthylep'" -> 0 for a village
    # with 1 (all-villages run 2026-10-02, 3 villages)
    er_ = {"resolved": {"village_code": 277038}}
    sql = ("SELECT COUNT(DISTINCT pg_id) AS producer_groups FROM curated.v_focus_legacy "
           "WHERE village_code = 277038 AND LOWER(lgd_district) = 'nongthylep' LIMIT 1")
    assert "lgd_district" not in p._mgnrega_drop_geo_beside_village(["Focus Legacy"], er_, sql)
    bare = ("SELECT COUNT(DISTINCT pg_id) FROM curated.v_focus_legacy "
            "WHERE LOWER(TRIM(lgd_district)) = LOWER('nongthylep') AND financial_year = '2024-25' LIMIT 1")
    out = p._pin_missing_village_code(["Focus Legacy"], er_, bare)
    assert "lgd_district" not in out and "village_code = 277038" in out and "financial_year = '2024-25'" in out
