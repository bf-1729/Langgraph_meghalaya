"""
Focus Plus use-case QA fixes (Focus +_Use_Cases.csv, 2026-09-27).

The live QA (19 / 30) found these bot defects (KNOWN_ISSUES KI-060 … KI-064);
each test pins the fix to the real production function.

  FOCUS-021b  "1,0263 female beneficiaries" — a mis-grouped 10,263 passed the
              faithfulness check, which strips commas before comparing (KI-062).
  FOCUS-029   "Which bank processed the highest total disbursement?" paused with
              NO chips; the typed reply "Focus Plus" became a new question and
              the bank question was lost. A Focus Legacy bank question raised
              KeyError (KI-063).
  FOCUS-008/011/012  status / gender / verification breakdowns had no shares.
  FOCUS-013/014/016  single status counts had no percentage (KI-060).
  FOCUS-026/027/028  comparisons gave the combined total, not the difference
              or the higher side (KI-061).
  FOCUS-006/007  "which area?" for a statewide batch / tranche total; the
              '12.5K' label read as an invented number → "31,317,500 amount raw."
              and amounts printed as 126197500.00 (KI-064).

No model or DB: the composer and fetch_rows are stubbed.
    python -m pytest tests/test_focusplus_usecase_fixes.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ── KI-062: mis-grouped digits ───────────────────────────────────────────────
def test_misgrouped_number_is_regrouped_when_it_is_a_data_value():
    assert p._fix_digit_grouping("1,0263 female beneficiaries received Focus+ assistance.",
                                 {"10263"}) == "10,263 female beneficiaries received Focus+ assistance."


@pytest.mark.parametrize("text", [
    "12,527 registrations", "12,34,567 rupees", "1,234.50 paid", "Tranches 1,2 and 3",
    "₹1,197,392,500.00 disbursed",
])
def test_valid_or_non_data_grouping_is_left_alone(text):
    assert p._fix_digit_grouping(text, {"12527", "1234567", "1234.5", "12", "1197392500"}) == text


def test_compose_response_repairs_misgrouped_number(monkeypatch):
    async def _composer(_prompt):
        return "1,0263 female beneficiaries received Focus+ assistance during FY 2025-26."
    monkeypatch.setattr(p.llm, "call_response_composer", _composer)
    sql = ("SELECT COUNT(*) AS female_beneficiaries FROM curated.v_focus_plus WHERE batch_label = '12.5K' "
           "AND gender = 'Female' AND financial_year_short = '2025-26' LIMIT 1")
    out = run(p.compose_response("How many female beneficiaries received Focus+ assistance during 2025-26?",
                                 sql, [{"female_beneficiaries": 10263}], schemes=["Focus Plus"]))
    assert out.startswith("10,263 female beneficiaries")


def test_numbers_inside_the_sql_filter_literals_are_not_misquotes():
    rows = [{"amount_raw": 31317500.0}]
    allowed = p._data_numbers(rows) | p._sql_literal_numbers(
        "SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE batch_label = '12.5K'")
    assert p._answer_numbers_faithful("31,317,500 was disbursed under the 12.5K batch.", allowed)
    # a genuinely invented figure is still caught
    assert not p._answer_numbers_faithful("41,317,500 was disbursed under the 12.5K batch.", allowed)


def test_compose_keeps_a_sentence_that_names_its_batch(monkeypatch):
    async def _composer(_prompt):
        return "31,317,500.00 was disbursed under the 12.5K batch across all of Meghalaya."
    monkeypatch.setattr(p.llm, "call_response_composer", _composer)
    out = run(p.compose_response(
        "How much was disbursed under the 12.5K batch?",
        "SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE batch_label = '12.5K' LIMIT 1",
        [{"amount_raw": 31317500.0}], schemes=["Focus Plus"]))
    assert "amount raw" not in out and "12.5K batch" in out


# ── KI-063: which bank? ──────────────────────────────────────────────────────
def test_bank_question_without_scheme_offers_the_two_bank_schemes():
    c = p._bank_clarification("Which bank processed the highest total disbursement?")
    assert c.rule == "bank-scheme-not-specified" and c.rule in p.SCHEME_PAUSE_RULES
    labels = [o["label"] for o in c.options]
    assert labels == ["Focus Plus (bank name)", "Focus Legacy (bank name)"]
    # each chip resumes as a question that is answerable (no second pause, no crash)
    for o in c.options:
        assert p._bank_clarification(o["question"]) is None


def test_typed_scheme_name_resumes_the_bank_question():
    c = p._bank_clarification("Which bank processed the highest total disbursement?")
    picked = p._resume_scheme_pause("Focus Plus", c.options)
    assert picked == "Which bank processed the highest total disbursement for Focus Plus"
    assert p._resume_scheme_pause("focus legacy please", c.options).endswith("for Focus Legacy")


def test_focus_legacy_bank_question_no_longer_raises():
    assert p._bank_clarification("Which banks handle Focus Legacy payments?") is None
    refusal = p._bank_clarification("Show Focus Legacy account numbers")
    assert refusal is not None and "IFSC" in refusal.question


def test_other_bank_behaviour_unchanged():
    assert p._bank_clarification("Which bank processed the highest total Focus+ disbursement?") is None
    assert p._bank_clarification("CM Elevate Legacy loans by Bank and LIFCOM") is None
    assert p._bank_clarification("which bank branch gave most CM Elevate Legacy loans") is not None
    assert p._bank_clarification("MGNREGA bank account details").rule == "column-not-held"


# ── KI-060: shares ───────────────────────────────────────────────────────────
def test_status_breakdown_gets_shares():
    rows = [{"focus_status": "Pending", "registrations": 11812},
            {"focus_status": "Approved", "registrations": 714},
            {"focus_status": "Rejected", "registrations": 1}]
    out = p._fp_breakdown_shares("Pending 11,812, Approved 714, Rejected 1.", rows)
    assert "Pending 94.29%, Approved 5.70%, Rejected 0.01%" in out
    assert "12.5K registration cohort" in out


def test_breakdown_shares_not_repeated_or_mixed():
    rows = [{"gender": "Female", "beneficiaries": 10263}, {"gender": "Male", "beneficiaries": 2264}]
    already = "Female 10,263 (81.93%), Male 2,264 (18.07%)."
    assert p._fp_breakdown_shares(already, rows) == already
    two_labels = [{"lgd_district": "A", "gender": "Female", "beneficiaries": 5},
                  {"lgd_district": "B", "gender": "Female", "beneficiaries": 7}]
    assert p._fp_breakdown_shares("x", two_labels) == "x"
    money = [{"gender": "Female", "amount_raw": 25657500.0}, {"gender": "Male", "amount_raw": 5660000.0}]
    assert p._fp_breakdown_shares("x", money) == "x"


def _stub_counts(monkeypatch, num, den):
    seen = []

    async def _fetch(sql, params=None):
        seen.append((sql, params))
        is_den = not any(v in ("Pending", "Approved", "Female", "Farmer") for v in params)
        return [{"n": den if is_den else num}]
    monkeypatch.setattr(p, "fetch_rows", _fetch)
    return seen


def test_single_status_count_gets_its_share(monkeypatch):
    seen = _stub_counts(monkeypatch, 11812, 12527)
    sql = ("SELECT COUNT(*) AS pending_beneficiaries FROM curated.v_focus_plus "
           "WHERE batch_label = '12.5K'   AND focus_status = 'Pending' LIMIT 1")
    out = run(p._fp_single_count_share("There are 11,812 beneficiaries with Pending status.", sql,
                                       [{"pending_beneficiaries": 11812}]))
    assert "94.29% of the 12,527 beneficiaries in the 12.5K registration cohort" in out
    # both recounts are parameter-bound, never the model's text
    assert all("$" in s and "'Pending'" not in s for s, _ in seen)


def test_share_is_scoped_to_the_same_filters(monkeypatch):
    _stub_counts(monkeypatch, 4460, 4670)
    sql = ("SELECT COUNT(DISTINCT beneficiary_key) AS n FROM curated.v_focus_plus WHERE "
           "lgd_district = 'WEST GARO HILLS' AND batch_label = '12.5K' AND focus_status = 'Pending'")
    out = run(p._fp_single_count_share("4,460 are pending.", sql, [{"n": 4460}]))
    assert "95.50% of the 4,670 beneficiaries in the 12.5K registration cohort in West Garo Hills" in out


def test_no_share_when_recount_disagrees_or_sql_is_unusual(monkeypatch):
    _stub_counts(monkeypatch, 999, 12527)
    sql = "SELECT COUNT(*) AS n FROM curated.v_focus_plus WHERE focus_status = 'Pending'"
    assert run(p._fp_single_count_share("11,812.", sql, [{"n": 11812}])) == "11,812."
    _stub_counts(monkeypatch, 11812, 12527)
    for odd in ("SELECT COUNT(*) AS n FROM curated.v_focus_plus WHERE focus_status = 'Pending' OR gender = 'Male'",
                "SELECT COUNT(*) AS n FROM curated.v_focus_plus WHERE focus_status IN ('Pending')",
                "SELECT COUNT(*) AS n FROM curated.v_focus_plus WHERE lgd_district = 'RI BHOI'"):
        assert run(p._fp_single_count_share("11,812.", odd, [{"n": 11812}])) == "11,812."


def test_status_literal_with_keyword_inside_is_parsed():
    assert p._fp_parse_where(
        "SELECT COUNT(*) FROM curated.v_focus_plus WHERE occupation = 'Others' AND batch_label = '12.5K'"
    ) == [("occupation", "Others", False), ("batch_label", "12.5K", False)]


# ── KI-061: comparisons ──────────────────────────────────────────────────────
def test_count_comparison_states_difference_and_drops_combined_total():
    rows = [{"lgd_district": "WEST GARO HILLS", "beneficiaries": 35039},
            {"lgd_district": "EAST KHASI HILLS", "beneficiaries": 13608}]
    out = p._fp_comparison(
        "Compare the number of Focus+ beneficiaries in West Garo Hills and East Khasi Hills.", rows,
        "West Garo Hills had 35,039 beneficiaries while East Khasi Hills had 13,608 beneficiaries. "
        "The combined total across both districts is 48,647 beneficiaries.")
    assert "West Garo Hills is higher by 21,431 beneficiaries" in out
    assert "48,647" not in out


def test_money_comparison_names_the_higher_district():
    rows = [{"lgd_district": "EAST GARO HILLS", "amount_raw": 126197500.0},
            {"lgd_district": "WEST KHASI HILLS", "amount_raw": 84425000.0}]
    out = p._fp_comparison("Compare the total Focus+ disbursement in East Garo Hills and West Khasi Hills.",
                           rows, "East Garo Hills received 126,197,500.00 and West Khasi Hills 84,425,000.00.")
    assert "East Garo Hills is higher by ₹41,772,500.00" in out


def test_tranche_comparison_picks_the_amount_column():
    rows = [{"tranche_label": "Tranch 1", "payments": 93286, "amount_raw": 466430000.0},
            {"tranche_label": "Tranch 3 - December", "payments": 93286, "amount_raw": 233215000.0}]
    out = p._fp_comparison("Compare disbursements between Tranche 1 and Tranche 3.", rows,
                           "Tranche 1 shows 466,430,000 while Tranche 3 shows 233,215,000.")
    assert "Tranche 1 is higher by ₹233,215,000.00" in out


def test_comparison_left_alone_when_already_stated_or_not_a_comparison():
    rows = [{"lgd_district": "A", "beneficiaries": 10}, {"lgd_district": "B", "beneficiaries": 4}]
    done = "A has 10 and B has 4; A is higher by 6."
    assert p._fp_comparison("Compare A and B", rows, done) == done
    assert p._fp_comparison("Beneficiaries in A and B", rows, "x") == "x"


# ── KI-064: formatting, fallback wording, scope pause ────────────────────────
def test_amounts_and_large_counts_are_formatted():
    rows = [{"amount_raw": 1197392500.0, "beneficiaries": 105813, "amount_per_beneficiary": 11316.12}]
    out = p._fp_format_numbers("Average 11316.12, from 1197392500.00 to 105813 beneficiaries in FY 2025-26.", rows)
    assert out == "Average ₹11,316.12, from ₹1,197,392,500.00 to 105,813 beneficiaries in FY 2025-26."


def test_digest_total_is_formatted_and_row_dump_reworded():
    rows = [{"gender": "Female", "amount_raw": 25657500.0}, {"gender": "Male", "amount_raw": 5660000.0}]
    assert "₹31,317,500.00" in p._fp_format_numbers("The total is 31,317,500.", rows)
    assert p._fp_format_numbers("31,317,500 amount raw.", [{"amount_raw": 31317500.0}]) == \
        "Amount disbursed: ₹31,317,500.00."


def test_percent_and_crore_figures_untouched():
    rows = [{"amount_raw": 94.29}]
    assert p._fp_format_numbers("94.29% share", rows) == "94.29% share"


def test_batch_or_tranche_is_a_scope():
    assert not p._needs_scope_clarification("How much was disbursed under the 12.5K batch?", {})
    assert not p._needs_scope_clarification("How much was disbursed under Tranche 2?",
                                            {"tranche_label": "Tranch 2 - August"})
    # an unscoped aggregate still asks, as before
    assert p._needs_scope_clarification("How much was disbursed?", {})


def test_guarantees_run_end_to_end(monkeypatch):
    _stub_counts(monkeypatch, 714, 12527)
    sql = ("SELECT COUNT(DISTINCT beneficiary_key) AS approved_beneficiaries FROM curated.v_focus_plus "
           "WHERE batch_label = '12.5K' AND focus_status = 'Approved' LIMIT 1")
    out = run(p._focusplus_answer_guarantees("How many beneficiaries have been approved?", sql,
                                             [{"approved_beneficiaries": 714}], "714 have been approved."))
    assert "5.70% of the 12,527" in out


# ── FOCUS-002: every category named ──────────────────────────────────────────
def test_list_answer_missing_names_is_rebuilt_from_rows():
    rows = [{"lgd_district": "WEST GARO HILLS", "beneficiaries": 35039},
            {"lgd_district": "SOUTH WEST GARO HILLS", "beneficiaries": 15294},
            {"lgd_district": "RI BHOI", "beneficiaries": 419},
            {"lgd_district": None, "beneficiaries": 1}]
    out = p._fp_complete_list("WEST GARO HILLS has 35,039. The remaining districts show 15,294, 419 and 1.", rows)
    assert out == ("Beneficiaries by district: West Garo Hills 35,039; South West Garo Hills 15,294; "
                   "Ri Bhoi 419; (no district recorded) 1.")


def test_complete_list_answer_is_kept():
    rows = [{"occupation": "Farmer", "beneficiaries": 8533}, {"occupation": "Student", "beneficiaries": 83},
            {"occupation": "Others", "beneficiaries": 904}]
    ok = "Farmer 8,533, Others 904 and Student 83."
    assert p._fp_complete_list(ok, rows) == ok


# ── FOCUS-029: a typed reply that pauses again keeps the whole question ─────
def test_typed_scheme_resume_is_remembered_whole_when_it_pauses_again(monkeypatch):
    import time
    from app import llm
    from app.routers import query as router
    from app.session_store import Session

    async def scope_pause(question, **_kw):
        raise p._scope_clarification(question, ["Focus Plus"])

    async def classifier(prompt, **_kw):
        return '{"intent": "DATA"}'
    monkeypatch.setattr(p, "_answer_data", scope_pause)
    monkeypatch.setattr(llm, "call_classifier", classifier)
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    bank_q = "Which bank processed the highest total disbursement?"
    router.remember_pause(sess, bank_q, p._bank_clarification(bank_q))
    with pytest.raises(p.ClarificationNeeded) as e:
        run(p._run_pipeline("Focus Plus", session=sess))
    remembered = router.pause_question(sess, "Focus Plus")
    assert remembered == "Which bank processed the highest total disbursement for Focus Plus"
    router.remember_pause(sess, remembered, e.value)
    assert sess.pending_scope_q == remembered


def test_pause_question_defaults_to_the_raw_question():
    class S:
        turn_context = {"resumed_question": None}
    from app.routers import query as router
    assert router.pause_question(S(), "How much was disbursed?") == "How much was disbursed?"
    assert router.pause_question(object(), "x") == "x"



# ── All-blocks / all-villages run (2026-09-27) ───────────────────────────────
# Blocks: 201/204 → 204/204 ("Nan"). Villages: 52/301 on the first sample →
# village resolution and single-village SQL were MGNREGA-only; now both schemes.
from app.entity_resolver import Resolved  # noqa: E402

_FP_NAMES = {
    "SALPARA - WARD NO.9": [{"village_code": 2533, "name": "Salpara - Ward No.9",
                             "block": "RESUBELPARA-MUNICIPAL BOARD", "district": "NORTH GARO HILLS"}],
    "ASIMGRE": [{"village_code": 1, "name": "ASIMGRE", "block": "A", "district": "EAST GARO HILLS"},
                {"village_code": 2, "name": "ASIMGRE", "block": "B", "district": "WEST GARO HILLS"}],
}


def _stub_fp_names(monkeypatch):
    async def names():
        return _FP_NAMES
    monkeypatch.setattr(p, "_focusplus_village_names", names)


def test_focusplus_village_prefers_its_own_exact_name(monkeypatch):
    _stub_fp_names(monkeypatch)

    async def generic(text, **kw):   # the statewide lookup picked another code
        return Resolved("resolved", "village", text, canonical=277769)
    monkeypatch.setattr(p, "resolve_village", generic)
    r = run(p._resolve_village_for(["Focus Plus"], "Salpara - Ward No.9"))
    assert r.status == "resolved" and r.canonical == 2533


def test_focusplus_same_name_villages_narrow_by_district(monkeypatch):
    _stub_fp_names(monkeypatch)

    async def generic(text, **kw):
        return Resolved("not_found", "village", text)
    monkeypatch.setattr(p, "resolve_village", generic)
    both = run(p._resolve_village_for(["Focus Plus"], "ASIMGRE"))
    assert both.status == "ambiguous" and {c["village_code"] for c in both.candidates} == {1, 2}
    one = run(p._resolve_village_for(["Focus Plus"], "ASIMGRE", district="EAST GARO HILLS"))
    assert one.status == "resolved" and one.canonical == 1


def test_focusplus_ambiguity_drops_villages_without_focus_plus_data(monkeypatch):
    _stub_fp_names(monkeypatch)

    async def generic(text, **kw):
        return Resolved("ambiguous", "village", text, candidates=[
            {"village_code": 2533, "name": "Salpara", "block": "X", "district": "Y"},
            {"village_code": 999, "name": "Salpara", "block": "Z", "district": "Y"}])
    monkeypatch.setattr(p, "resolve_village", generic)
    r = run(p._resolve_village_for(["Focus Plus"], "Salpara"))
    assert r.status == "resolved" and r.canonical == 2533


def test_village_scheme_is_single_village_grained_scheme():
    assert p._village_scheme(["Focus Plus"]) == "Focus Plus"
    assert p._village_scheme(["MGNREGA"]) == "MGNREGA"
    # PMAY-G joined the gate on 2026-09-28: its all-villages run failed the same
    # shapes (exact name paused against a fuzzy look-alike, chip tail answered as
    # the block, wrong code) — PMAY-G QA, KNOWN_ISSUES KI-096. Other schemes stay out.
    assert p._village_scheme(["PMAY-G"]) == "PMAY-G"
    # CM Elevate joined on 2026-09-28 (CM Elevate all-villages run, KI-106: block
    # total for a village, village name in the district slot, a neighbour's code).
    assert p._village_scheme(["CM Elevate"]) == "CM Elevate"
    assert p._village_scheme(["CM Elevate Legacy"]) == "CM Elevate Legacy"   # joined 2026-10-02
    # Focus Legacy joined on 2026-10-02 (bare village names, all-villages run)
    assert p._village_scheme(["Focus Legacy"]) == "Focus Legacy"
    assert p._village_scheme(["PMAY-G", "Focus Plus"]) is None   # several schemes: outside the gate
    # MGNREGA keeps its exact old gate (schemes[0] == "MGNREGA", any length)
    assert p._village_scheme(["MGNREGA", "PMAY-G"]) == "MGNREGA"
    assert p._village_scheme(["Focus Plus", "MGNREGA"]) is None
    assert p._village_scheme(["PMAY-G", "MGNREGA"]) is None


@pytest.mark.parametrize("sql,want", [
    ("SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE UPPER(district_name_raw) = 'CHOBAGOK' "
     "AND entity_type = 'Ward' AND village_code = 2527 LIMIT 1",
     "SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 2527 LIMIT 1"),
    ("SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE lgd_district = 'NORTH GARO HILLS' "
     "AND UPPER(block_name_raw) = 'RESUBELPARA-MUNICIPAL BOARD' AND village_code = 277769 LIMIT 1",
     "SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE village_code = 2527 LIMIT 1"),
    ("SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE lgd_village_name = 'WARD NO. 4' "
     "AND financial_year_short = '2025-26' GROUP BY tranche_label LIMIT 100",
     "SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE village_code = 2527 AND "
     "financial_year_short = '2025-26' GROUP BY tranche_label LIMIT 100"),
    ("SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus LIMIT 1",
     "SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 2527 LIMIT 1"),
])
def test_single_village_where_is_pinned_to_the_code(sql, want):
    assert p._focusplus_pin_village_where(["Focus Plus"], {"resolved": {"village_code": 2527}}, sql) == want


def test_village_pin_leaves_other_cases_alone():
    sql = "SELECT 1 FROM curated.v_focus_plus WHERE lgd_block = 'A' AND village_code = 1"
    assert p._focusplus_pin_village_where(["MGNREGA"], {"resolved": {"village_code": 5}}, sql) == sql
    assert p._focusplus_pin_village_where(["Focus Plus"], {"resolved": {}}, sql) == sql
    assert p._focusplus_pin_village_where(["Focus Plus"], {"resolved": {"village_code": [1, 2]}}, sql) == sql
    orq = "SELECT 1 FROM curated.v_focus_plus WHERE village_code = 1 OR village_code = 2"
    assert p._focusplus_pin_village_where(["Focus Plus"], {"resolved": {"village_code": 1}}, orq) == orq


def test_verifier_village_complaint_filter_covers_focus_plus_years():
    sql = "SELECT 1 FROM curated.v_focus_plus WHERE village_code = 2527 AND financial_year_short = '2025-26'"
    issue = "Check 2: RESOLVED ENTITIES specifies a different village"
    assert p._verifier_village_code_complaint_is_false(issue, ["Focus Plus"],
                                                       {"village_code": 2527, "year_key": 2025}, sql)
    assert not p._verifier_village_code_complaint_is_false(issue, ["Focus Plus"],
                                                           {"village_code": 2527, "year_key": 2022}, sql)


def test_nan_block_is_explained_not_refused():
    out = run(p._focusplus_answer_guarantees(
        "How much has been disbursed under Focus Plus in Nan block?",
        "SELECT SUM(amount_disbursed) AS amount_raw FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'NAN'",
        [{"amount_raw": 12500.0}], "Focus Plus disbursed 12500.00 in Nan block."))
    assert out.startswith("'Nan' is not a real block") and "₹12,500.00" in out


# ── Twin villages (same name AND block) and district-alias villages ─────────
_TWINS = [{"village_code": 272895, "name": "BOLDAMGRE", "block": "SELSELLA", "district": "WEST GARO HILLS"},
          {"village_code": 272933, "name": "BOLDAMGRE", "block": "SELSELLA", "district": "WEST GARO HILLS"},
          {"village_code": 273125, "name": "BOLDAMGRE", "block": "DADENGGIRI", "district": "WEST GARO HILLS"}]
_TWIN_Q = "How many Focus Plus beneficiaries are there in Boldamgre across all financial years?"


def test_twin_village_chips_carry_the_lgd_code():
    labels = [p._village_chip_label(c, _TWINS, ["Focus Plus"]) for c in _TWINS]
    assert labels == ["BOLDAMGRE (LGD 272895) — SELSELLA block, WEST GARO HILLS",
                      "BOLDAMGRE (LGD 272933) — SELSELLA block, WEST GARO HILLS",
                      "BOLDAMGRE — DADENGGIRI block, WEST GARO HILLS"]
    q = p._village_chip_question(_TWIN_Q, "Boldamgre", _TWINS[1], ["Focus Plus"], _TWINS)
    assert q == ("How many Focus Plus beneficiaries are there in Boldamgre (LGD 272933) across all "
                 "financial years, SELSELLA block, WEST GARO HILLS")
    # re-entry does not grow the text
    assert p._village_chip_question(q, "Boldamgre", _TWINS[1], ["Focus Plus"], _TWINS) == q


def test_twin_tag_only_for_village_grained_schemes_and_real_twins():
    # a question outside the village gate gets no tag (PMAY-G and CM Elevate are inside it
    # since 2026-09-28, Focus Legacy and CM Elevate Legacy since 2026-10-02)
    assert p._village_chip_label(_TWINS[0], _TWINS, ["PMAY-G", "Focus Plus"]) == "BOLDAMGRE — SELSELLA block, WEST GARO HILLS"
    assert "(LGD" in p._village_chip_label(_TWINS[0], _TWINS, ["CM Elevate Legacy"])
    assert "(LGD" in p._village_chip_label(_TWINS[0], _TWINS, ["CM Elevate"])
    assert "(LGD" in p._village_chip_label(_TWINS[0], _TWINS, ["PMAY-G"])
    assert p._village_chip_label(_TWINS[2], _TWINS, ["Focus Plus"]) == "BOLDAMGRE — DADENGGIRI block, WEST GARO HILLS"
    assert p._village_chip_question(_TWIN_Q, "Boldamgre", _TWINS[0], ["Focus Plus"]) == (
        "How many Focus Plus beneficiaries are there in Boldamgre across all financial years, "
        "SELSELLA block, WEST GARO HILLS")


def test_twin_chip_pins_its_lgd_code(monkeypatch):
    async def fetch(sql, params=None):
        assert "village_code = $1" in sql and params == [272933]
        return [{"village_code": 272933, "lgd_village_name": "BOLDAMGRE", "lgd_block": "SELSELLA",
                 "lgd_district": "WEST GARO HILLS"}]
    monkeypatch.setattr(p, "fetch_rows", fetch)
    q = p._village_chip_question(_TWIN_Q, "Boldamgre", _TWINS[1], ["Focus Plus"], _TWINS)
    assert run(p._mgnrega_village_chip_pin(q))["village_code"] == 272933


def test_lgd_code_is_not_a_misquote():
    q = "How many Focus Plus beneficiaries are there in Daren Agal (LGD 904712), GAMBEGRE block, WEST GARO HILLS"
    assert p._CHIP_LGD_CODE_RE.findall(q) == ["904712"]
    assert "904712" not in p._CHIP_LGD_CODE_RE.sub("", q)


def test_district_alias_that_is_also_a_village_asks(monkeypatch):
    async def names():
        return {"BAGHMARA": [{"village_code": 273089, "name": "BAGHMARA", "block": "BAGHMARA",
                              "district": "SOUTH GARO HILLS"}]}
    monkeypatch.setattr(p, "_focusplus_village_names", names)
    from app import entity_resolver
    entity_resolver.load_all()
    dims = run(p._focusplus_alias_district_collision("BAGHMARA"))
    assert list(dims) == ["district", "block", "village"] and dims["district"].upper() == "SOUTH GARO HILLS"
    for plain in ("West Garo Hills", "WGH", "Tura", "Dalu"):
        assert run(p._focusplus_alias_district_collision(plain)) == {}


def test_place_name_in_another_column_is_dropped_beside_the_village():
    from app import entity_resolver
    entity_resolver.load_all()
    er = {"resolved": {"village_code": 275847}}
    bad = ("SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 275847 "
           "AND tranche_label ILIKE '%GASUAPARA%' LIMIT 1")
    assert p._focusplus_pin_village_where(["Focus Plus"], er, bad) == (
        "SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 275847 LIMIT 1")
    good = ("SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 275847 "
            "AND tranche_label = 'Tranch 2 - August' AND focus_status = 'Pending' LIMIT 1")
    assert p._focusplus_pin_village_where(["Focus Plus"], er, good) == good


def test_standalone_roman_numeral_kept_in_place_names():
    assert p._place_title("NANDICHAR II") == "Nandichar II"
    assert p._place_title("IVY PARA") == "Ivy Para"


def test_one_figure_answer_must_state_that_figure(monkeypatch):
    calls = []

    async def composer(_prompt):
        calls.append(1)
        return "There is 1 Focus Plus beneficiary in Nonglang."     # the result is 11
    monkeypatch.setattr(p.llm, "call_response_composer", composer)
    out = run(p.compose_response(
        "How many Focus Plus beneficiaries are there in NONGLANG?",
        "SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries FROM curated.v_focus_plus WHERE village_code = 277400",
        [{"beneficiaries": 11}], schemes=["Focus Plus"]))
    assert len(calls) == 2 and "11" in out            # strict retry, then the exact row


def test_geography_key_subquery_becomes_the_village_code():
    sql = ("SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'RI MULIANG' "
           "AND geography_key IN (SELECT geography_key FROM curated.dim_geography WHERE lgd_village_name = "
           "'NONGRIM HILLS') LIMIT 1")
    assert p._focusplus_pin_village_where(["Focus Plus"], {"resolved": {"village_code": 276421}}, sql) == (
        "SELECT SUM(amount_disbursed) AS a FROM curated.v_focus_plus WHERE village_code = 276421 LIMIT 1")


def test_focus_plus_village_named_like_a_foreign_place_is_not_refused(monkeypatch):
    async def scan(question, scheme):
        assert scheme == "Focus Plus"
        return ("BURMA", [{"village_code": 278981, "name": "BURMA", "block": "B", "district": "D"}])
    monkeypatch.setattr(p, "_scan_village_in_question", scan)
    hit = {"type": "off_topic"}
    with pytest.raises(p.ClarificationNeeded) as e:
        run(p._mgnrega_village_not_out_of_area("How many Focus Plus beneficiaries are there in BURMA?", hit))
    assert e.value.rule == "village-or-outside-place"
    assert run(p._mgnrega_village_not_out_of_area(
        "How many Focus Plus beneficiaries are there in BURMA village?", hit)) is True
    assert run(p._mgnrega_village_not_out_of_area("How many PMAY-G houses in BURMA?", hit)) is False


# ── KI-025: a dropped database is not a SQL problem, and not a KB question ───
def test_connection_errors_are_classified():
    import asyncpg
    from app import db
    assert db.is_connection_error(OSError("[WinError 121] The semaphore timeout period has expired"))
    assert db.is_connection_error(ConnectionResetError())
    assert db.is_connection_error(asyncpg.exceptions.ConnectionDoesNotExistError("closed"))
    try:
        try:
            raise OSError("down")
        except OSError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert db.is_connection_error(outer)                 # found in the cause chain
    assert not db.is_connection_error(db.UnsafeSQLError("x"))
    assert not db.is_connection_error(ValueError("column does not exist"))
    assert not db.is_connection_error(asyncio.TimeoutError())  # slow query keeps its 504 path


def test_execute_with_repair_does_not_repair_a_dropped_connection(monkeypatch):
    repairs = []

    async def verify(*_a, **_k):
        return None

    async def down(_sql):
        raise OSError("[WinError 121] The semaphore timeout period has expired")

    async def generator(*_a, **_k):
        repairs.append(1)
        return "SELECT 1"
    monkeypatch.setattr(p, "_verify_sql", verify)
    monkeypatch.setattr(p, "run_readonly", down)
    monkeypatch.setattr(p.llm, "call_sql_generator", generator)
    with pytest.raises(p.DatabaseUnavailableError):
        run(p.execute_with_repair("How many beneficiaries in NANDICHAR II?", ["Focus Plus"],
                                  {"resolved": {"village_code": 273993}},
                                  initial_sql="SELECT COUNT(*) FROM curated.v_focus_plus WHERE village_code = 273993"))
    assert repairs == []                                      # no 30B repair spent


def test_run_pipeline_does_not_answer_a_database_outage_from_the_kb(monkeypatch):
    import time
    from app import llm
    from app.session_store import Session
    kb = []

    async def answer_data(question, **_kw):
        raise OSError("connection refused")

    async def fallback(question):
        kb.append(question)
        return {"route": "knowledge", "answer": "not in the reference material"}

    async def classifier(prompt, **_kw):
        return '{"intent": "DATA"}'
    monkeypatch.setattr(p, "_answer_data", answer_data)
    monkeypatch.setattr(p, "_data_path_kb_fallback", fallback)
    monkeypatch.setattr(llm, "call_classifier", classifier)
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())
    with pytest.raises(p.DatabaseUnavailableError):
        run(p._run_pipeline("How many Focus Plus beneficiaries are there in NANDICHAR II?", session=sess))
    assert kb == []


# ── Ten LGD blocks the Focus Plus source file never names (all-blocks run 2026-10-02) ──
@pytest.mark.parametrize("sql,expected", [
    ("SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'ADOKGRE' LIMIT 1",
     "SELECT COUNT(DISTINCT beneficiary_key) AS b FROM curated.v_focus_plus WHERE lgd_block = 'ADOKGRE' LIMIT 1"),
    ("SELECT SUM(amount_disbursed) FROM curated.v_focus_plus WHERE block_name_raw ILIKE 'Siju' AND financial_year_short = '2025-26'",
     "SELECT SUM(amount_disbursed) FROM curated.v_focus_plus WHERE lgd_block = 'SIJU' AND financial_year_short = '2025-26'"),
    # a block the source file does name keeps block_name_raw (lgd_block is NULL on 15% of rows)
    ("SELECT COUNT(*) FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'SONGSAK'",
     "SELECT COUNT(*) FROM curated.v_focus_plus WHERE UPPER(block_name_raw) = 'SONGSAK'"),
])
def test_focusplus_lgd_only_block_is_filtered_on_lgd_block(monkeypatch, sql, expected):
    monkeypatch.setattr(p, "_FOCUSPLUS_RAW_BLOCKS", {"SONGSAK", "KHARKUTTA", "BAGHMARA"})
    assert asyncio.run(p._focusplus_lgd_only_block(["Focus Plus"], sql)) == expected


def test_focusplus_block_rewrite_is_focus_plus_only(monkeypatch):
    monkeypatch.setattr(p, "_FOCUSPLUS_RAW_BLOCKS", {"SONGSAK"})
    sql = "SELECT 1 FROM t WHERE UPPER(block_name_raw) = 'ADOKGRE'"
    assert asyncio.run(p._focusplus_lgd_only_block(["Focus Legacy"], sql)) == sql


# ── A spelled-out figure becomes digits, every scheme (all-blocks run 2026-10-02) ──
@pytest.mark.parametrize("answer,rows,expected", [
    ("Eighteen producer groups received payments under Focus Legacy in Shella Bholaganj block.",
     [{"producer_groups": 18}], "18 producer groups received payments under Focus Legacy in Shella Bholaganj block."),
    ("Twelve houses were sanctioned in the village.", [{"houses": 12}], "12 houses were sanctioned in the village."),
    ("Two houses were sanctioned.", [{"houses": 3}], "Two houses were sanctioned."),     # not the result's value
])
def test_number_words_become_digits_for_every_scheme(answer, rows, expected):
    assert p._cme_digits_for_number_words(answer, rows) == expected
