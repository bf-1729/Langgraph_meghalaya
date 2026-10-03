"""
CM Elevate Legacy — the sanction-and-disbursement dataset (DB: CM Elevate
Disbursement, curated.v_cm_elevate_disbursement), wired as its own scheme beside
the CM Elevate APPLICATIONS dataset it shares a name (and no key) with.

Covers the prompt-layer v2 bank (data/cm_elevate_legacy/
cmelevatelegacy_prompt_few_shots.yaml), which dataset a "CM Elevate" question
lands on, the resolver, the not-held gate, the prompt assembly and the
registries a new scheme must appear in. Pure Python — no model or DB.
    python -m pytest tests/test_cm_elevate_legacy.py -q
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (annotations, auth, edge, entity_resolver, followups,  # noqa: E402
                 pipeline as p, prompt_builder as pb, schema_context, schema_introspect)

LEGACY = "CM Elevate Legacy"
_DATA = Path(__file__).resolve().parents[1] / "data" / "cm_elevate_legacy"
_BANK = yaml.safe_load((_DATA / "cmelevatelegacy_prompt_few_shots.yaml").read_text(encoding="utf-8"))
_VIEW_COLS = {c["name"] for c in yaml.safe_load(
    (_DATA / "cmelevatelegacy_schema_partitions.yaml").read_text(encoding="utf-8")
)["datasets"]["v_cm_elevate_disbursement"]["columns"]}


_AC_SHOTS = {"X07", "X08"}   # constituency through dim_geography


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()
    entity_resolver.load_all()


def _ids(question: str, k: int = 5) -> list[str]:
    by_q = {e["question"]: e["id"] for e in annotations._few_shot_cache[LEGACY]}
    return [by_q[x["question"]] for x in annotations.few_shot_examples([LEGACY], question, top_k=k)]


# ── 1. The bank matches the reviewed document ───────────────────────────────
def test_bank_counts_match_the_document():
    pool = _BANK["sql_generation_examples"]
    # X01 / X02 / X07 / X08 became SQL shots on 2026-09-25 (sanction share,
    # not-sanctioned count, constituency via dim_geography) - 110+4 / 22-4.
    assert sum(e.get("status") != "UNANSWERABLE" for e in pool) == 114
    assert sum(e.get("status") == "UNANSWERABLE" for e in pool) == 18
    assert len(_BANK["clarification_shots"]) == 8
    assert len(_BANK["common_mistakes"]) == 14
    assert len(_BANK["answer_shots"]) == 8


def test_bank_sql_parses_and_reads_only_the_view():
    sqlglot = pytest.importorskip("sqlglot")
    exp = sqlglot.exp
    for e in _BANK["sql_generation_examples"]:
        for key in ("sql", "caveat_sql"):
            if not e.get(key):
                continue
            tree = sqlglot.parse_one(e[key], read="postgres")
            aliases = {a.alias for a in tree.find_all(exp.Alias)}
            tables = {t.name for t in tree.find_all(exp.Table)}
            # The one permitted extra: the declared geography_key -> dim_geography
            # FK, used only by the constituency shots for ac_name.
            allowed = {"v_cm_elevate_disbursement"} | (
                {"dim_geography"} if e["id"] in _AC_SHOTS else set())
            assert tables == allowed, e["id"]
            extra = {"ac_name"} if e["id"] in _AC_SHOTS else set()
            unknown = {c.name for c in tree.find_all(exp.Column)} - _VIEW_COLS - aliases - extra
            assert not unknown, (e["id"], unknown)


def test_policy_gated_clarify_shots_stay_out_of_the_sql_pool():
    pool_ids = {e["id"] for e in annotations._few_shot_cache[LEGACY]}
    assert {"M01", "R01"} <= pool_ids          # default policy: answer with the split
    assert not pool_ids & {"K01", "K02", "K06"}


# ── 2. Which CM Elevate dataset a question lands on ─────────────────────────
@pytest.mark.parametrize("question", [
    "how many CM Elevate applications are on hold",
    "CM Elevate applications by district",
    "what is the gender split of CM Elevate applicants",
    "how many piggery applications are pending",   # "pending" = on hold there
])
def test_application_questions_stay_on_cm_elevate(question):
    q = p._pin_cm_elevate_dataset(question)
    assert q == question
    assert p._shortcut_scheme(q) == ["CM Elevate"]


@pytest.mark.parametrize("question", [
    "what is the total amount disbursed under CM Elevate",
    "CM-ELEVATE loans by lender",
    "how many CM Elevate records in FY 2024-25",
])
def test_money_year_lender_questions_move_to_legacy(question):
    q = p._pin_cm_elevate_dataset(question)
    assert "CM Elevate Legacy" in q
    assert p._shortcut_scheme(q) == [LEGACY]


@pytest.mark.parametrize("question", [
    "CM Elevate Legacy disbursement by district",
    "piggery disbursement in Ri Bhoi",
    "how many LIFCOM loans in Garo Hills",
])
def test_named_or_vocabulary_pins_legacy(question):
    assert p._shortcut_scheme(p._pin_cm_elevate_dataset(question)) == [LEGACY]


def test_legacy_name_is_not_also_read_as_the_applications_dataset():
    assert p._named_schemes("CM Elevate Legacy disbursement by district") == [LEGACY]
    assert p._named_schemes("CM Elevate Disbursement in Ri Bhoi") == [LEGACY]
    # the lone word "Elevate" must not be "corrected" into a second "CM"
    assert p._correct_scheme_spelling("CM Elevate Legacy disbursement") == \
        "CM Elevate Legacy disbursement"


def test_scheme_pause_offers_legacy():
    labels = [o["label"] for o in p._scheme_clarification("how many records").options]
    assert any(LEGACY in lbl for lbl in labels)


# ── 3. Resolver ──────────────────────────────────────────────────────────────
def test_resolver_loads_the_legacy_catalogue():
    cat = entity_resolver._catalog[LEGACY]
    assert len(cat["district"]) == 12 and len(cat["cm_scheme"]) == 13 and cat["block"]


@pytest.mark.parametrize("text,want", [
    ("sericulture spinning", ["Meghalaya Sericulture & Weaving Scheme (spinning)"]),
    ("weaving", ["Meghalaya Sericulture & Weaving Scheme(weaving)"]),
    ("compare piggery and poultry", ["Meghalaya Piggery Development Scheme",
                                     "Meghalaya Poultry Farming Scheme"]),
    ("PTV records", ["Prime Tourism Vehicle Scheme"]),
])
def test_legacy_scheme_literals(text, want):
    assert entity_resolver.resolve_cm_scheme(text, LEGACY).values == want


def test_bare_sericulture_is_asked_and_both_is_taken_at_its_word():
    assert p._cm_legacy_sericulture_choice("sericulture records by district") == "ask"
    both = p._cm_legacy_sericulture_choice("both sericulture schemes by district")
    assert sorted(both) == sorted(p._CM_LEGACY_SERICULTURE)
    assert p._cm_legacy_sericulture_choice("sericulture spinning records") is None


# ── 4. Not-held gate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("question", [
    "names of piggery beneficiaries in Umling",
    "who got the maximum money",
    "month wise disbursement for 2024-25",
    "how many women entrepreneurs got support",
    "how many SHG group applicants",
    "loan repayment status",
    "what percentage did the bank contribute",
    "how many jobs created by poultry units",
])
def test_not_held_questions_are_refused(question):
    assert p._cm_legacy_not_held(question) is not None


@pytest.mark.parametrize("question", [
    "how many beneficiaries who took a loan in poultry",        # relative "who"
    "list individual records of goat farming in Ri Bhoi",       # "individual"
    "give me an overview of disbursement by district",          # "overview"
    "we want to focus on piggery - how many records in each district",
    "how much subsidy was paid between April and December 2024",
])
def test_answerable_questions_are_not_refused(question):
    assert p._cm_legacy_not_held(question) is None


def test_lender_categories_are_queryable_but_branches_are_not():
    assert p._bank_clarification("CM Elevate Legacy loans by Bank and LIFCOM") is None
    assert p._bank_clarification("which bank branch gave most CM Elevate Legacy loans") is not None


# ── 5. Prompt assembly ───────────────────────────────────────────────────────
def test_legacy_prompt_is_scoped_and_carries_the_prompt_layer():
    prompt = pb.build_sql_prompt("total disbursement by district", [LEGACY],
                                 {"resolved": {}, "notes": [], "display": {}})
    assert "CM ELEVATE LEGACY RULES" in prompt and "COMMON MISTAKES" in prompt
    assert "Plan:" in prompt
    assert "CM ELEVATE RULES (" not in prompt and "FOCUS LEGACY RULES" not in prompt


def test_other_schemes_prompts_carry_no_legacy_text():
    for scheme in ("MGNREGA", "CM Elevate", "Focus Legacy"):
        prompt = pb.build_sql_prompt("total", [scheme], {"resolved": {}, "notes": [], "display": {}})
        assert "CM ELEVATE LEGACY" not in prompt and "COMMON MISTAKES" not in prompt


def test_legacy_tables_are_not_filed_under_cm_elevate():
    assert pb._scheme_of("v_cm_elevate_disbursement") == LEGACY
    assert pb._scheme_of("dim_cm_elevate_disb_scheme") == LEGACY
    assert pb._scheme_of("v_cm_elevate") == "CM Elevate"
    assert schema_introspect._table_matches_scheme("v_cm_elevate_disbursement", [LEGACY])
    assert not schema_introspect._table_matches_scheme("v_cm_elevate_disbursement", ["CM Elevate"])


def test_retrieval_ignores_the_scheme_name_and_caps_refusals():
    ids = _ids("What is the total amount disbursed under CM Elevate Legacy by district?")
    assert ids[0] == "M10"
    assert sum(i.startswith("X") for i in ids) <= 1
    assert _ids("kitne records hai piggery me")[0] == "C02"      # via a variant
    assert "X09" in _ids("who received the largest disbursement")


def test_other_schemes_keep_the_neutral_refusal_policy():
    for scheme in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy"):
        assert annotations._refusal_policy[scheme] == (1.0, None)
        assert not annotations._common_mistakes[scheme]


# ── 6. Caveats routed by code ────────────────────────────────────────────────
def test_caveat_notes_follow_the_sql():
    sql = ("SELECT COALESCE(financial_year_short, '(no financial year)') AS financial_year, "
           "ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
           "FROM curated.v_cm_elevate_disbursement GROUP BY financial_year_short")
    notes = " ".join(p._cm_legacy_answer_notes(sql, [{"financial_year": "2024-25",
                                                      "total_disbursed_cr": 1.0}]))
    assert "crore" in notes and "subsidy and loan" in notes and "Sericulture" in notes
    assert not p._cm_legacy_answer_notes("SELECT COUNT(*) AS records FROM x", [{"records": 1}])


# ── 7. Registries ────────────────────────────────────────────────────────────
def test_registered_everywhere_a_scheme_must_be():
    assert LEGACY in schema_context.SCHEME_CATALOG and LEGACY in schema_context.SCHEME_METRICS
    assert p._SCHEME_DATA_YEARS[LEGACY] == ["2024-25", "2025-26"]
    assert all(LEGACY in r["schemes"] for r in auth.ROLE_PERMISSIONS.values())
    assert edge._named_scheme("can I get CM Elevate Legacy data?") == LEGACY
    assert edge._named_scheme("can I get CM Elevate data?") == "CM Elevate"
    opts = followups.build_followups("data", "CM Elevate Legacy disbursement", [LEGACY], {})
    assert opts and all("CM Elevate Legacy" in o["question"] or "Break that down" in o["question"]
                        for o in opts)
    assert "curated.v_cm_elevate_disbursement" in p._CROSS_SCHEME_MONEY_SQL


# ── 8. Knowledge (RAG): one knowledge base for both CM Elevate datasets ─────
# CM Elevate and CM Elevate Legacy are two DATASETS of one programme; its
# eligibility / benefits / process docs are the same for both.
def test_legacy_reads_the_cm_elevate_knowledge_base_and_others_are_unchanged():
    from app import rag
    assert rag.kb_scheme(LEGACY) == "CM Elevate"
    for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy", None):
        assert rag.kb_scheme(s) == s


def test_retrieval_filters_on_the_shared_tag(monkeypatch):
    import asyncio
    from app import rag
    seen = []

    async def fake_embed(q):
        return [[0.0]]

    async def fake_search(vec, top_k, scheme=None):
        seen.append(scheme)
        return []

    monkeypatch.setattr(rag.llm, "call_embedding", fake_embed)
    monkeypatch.setattr(rag.vectorstore, "search", fake_search)
    asyncio.run(rag.retrieve("who is eligible", scheme=LEGACY))
    asyncio.run(rag.retrieve("who is eligible", scheme="Focus Legacy"))
    assert seen == ["CM Elevate", "Focus Legacy"]


def test_naming_both_retrieves_the_shared_docs_once(monkeypatch):
    import asyncio
    from app import rag
    calls = []

    async def fake_retrieve(q, scheme=None):
        calls.append(scheme)
        return [{"text": f"{scheme} passage", "doc": "d", "heading": "h", "score": 0.9}]

    async def fake_compose(prompt):
        return "- **CM Elevate** — answer"

    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag.llm, "call_response_composer", fake_compose)
    out = asyncio.run(rag.answer_from_kb_multi("how to apply", ["CM Elevate", LEGACY, "MGNREGA"]))
    assert calls == ["CM Elevate", "MGNREGA"]          # shared KB searched once
    assert out and "I don't have reference material" not in out["answer"]


def test_knowledge_question_is_not_shown_a_legacy_rewrite():
    q = "how much subsidy does CM Elevate provide"
    pinned = p._pin_cm_elevate_dataset(q)
    assert pinned != q                                    # the DATA-side decision
    assert p._unpin_cm_elevate(pinned) == q               # undone on the KNOWLEDGE route


def test_legacy_tagged_docs_fold_into_the_shared_tag():
    from app import kb_ingest
    for tag in ("cm elevate legacy", "cmelevatelegacy", "cm elevate disbursement"):
        assert kb_ingest._CANONICAL_SCHEME[tag] == "CM Elevate"


def test_not_covered_reply_names_the_material_actually_searched():
    assert "CM Elevate reference material" in p._knowledge_not_covered_answer("x", LEGACY)
    assert "Focus Legacy reference material" in p._knowledge_not_covered_answer("x", "Focus Legacy")


# ── 9. "by scheme" inside CM Elevate Legacy means its own 13 sub-schemes ─────
@pytest.mark.parametrize("question", [
    "How many CM Elevate Legacy records are there, by scheme in Meghalaya?",
    "CM Elevate Legacy disbursement scheme-wise",
])
def test_by_scheme_stays_inside_legacy_and_offers_only_its_two_years(question):
    assert p._shortcut_scheme(question) == [LEGACY]
    labels = [o["label"] for o in p._year_clarification(question, [LEGACY]).options]
    assert labels == ["FY 2024-25", "FY 2025-26", "All financial years combined"]


def test_by_scheme_without_legacy_is_unchanged():
    assert p._shortcut_scheme("total disbursement across all schemes") == \
        list(schema_context.SCHEME_CATALOG)


# ── 10. "What schemes do you have?" — one programme, two datasets ───────────
def test_scheme_listing_shows_cm_elevate_once_and_counts_correctly():
    ans = p._scheme_listing_answer("what schemes do you have?")["answer"]
    bullets = [line for line in ans.splitlines() if line.startswith("- **")]
    assert ans.startswith(f"I cover {p._count_word(len(bullets))} ")
    assert len(bullets) == 5
    assert not any(b.startswith("- **CM Elevate Legacy**") for b in bullets)
    cm = next(b for b in bullets if b.startswith("- **CM Elevate**"))
    assert "CM Elevate Legacy" in cm and "applications" in cm
    # every other scheme's line is exactly its summary
    for name in ("MGNREGA", "PMAY-G", "Focus Plus", "Focus Legacy"):
        assert f"- **{name}** — {p._SCHEME_USER_SUMMARY[name]}" in bullets


# ── 2026-09-25 use-case QA fixes ─────────────────────────────────────────────
@pytest.mark.parametrize("question", [
    "What percentage of applications have been sanctioned in CM Elevate Legacy?",
    "How many applications have not been sanctioned in CM Elevate Legacy?",
    "How many CM Elevate Legacy applications are mapped to Mairang constituency?",
    "What is the total disbursement amount for Mairang constituency in CM Elevate Legacy?",
])
def test_answerable_questions_are_no_longer_refused(question):
    assert p._cm_legacy_not_held(question) is None


def test_constituency_is_available_for_cm_elevate_legacy():
    q = "What is the total disbursement amount for Mairang constituency?"
    assert p._ac_dimension_available(q, [LEGACY]) is True


def test_unresolved_filter_is_dropped_from_a_district_total():
    sql = ("SELECT lgd_district, ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
           "FROM curated.v_cm_elevate_disbursement WHERE entity_type <> 'Unresolved' "
           "GROUP BY lgd_district")
    out = p._cm_legacy_keep_unresolved_off_village(
        "What is the total disbursement amount for each district?", [LEGACY], sql)
    assert "Unresolved" not in out and "GROUP BY lgd_district" in out


def test_unresolved_filter_is_kept_on_a_village_answer():
    sql = ("SELECT lgd_village_name, village_code, COUNT(*) AS records FROM "
           "curated.v_cm_elevate_disbursement WHERE lgd_block = 'TIKRIKILLA' AND "
           "entity_type <> 'Unresolved' GROUP BY lgd_village_name, village_code")
    assert p._cm_legacy_keep_unresolved_off_village("records per village", [LEGACY], sql) == sql


@pytest.mark.parametrize("question", [
    "Who are the intended beneficiaries of CM-ELEVATE?",
    "What is the target number of entrepreneurs under CM-ELEVATE?",
])
def test_programme_design_questions_route_to_knowledge(question):
    assert p._PROGRAMME_DESIGN_CUE.search(question)


@pytest.mark.parametrize("question", [
    "How many beneficiaries in West Garo Hills?",
    "who are the beneficiaries of piggery scheme in Umling",
    "what is the total number of beneficiaries",
])
def test_beneficiary_counts_stay_off_the_design_cue(question):
    assert not p._PROGRAMME_DESIGN_CUE.search(question)


def test_sanctioned_shots_count_sanctioned_amount_not_rows():
    by_id = {e["id"]: e for e in _BANK["sql_generation_examples"]}
    for sid in ("C06", "C07", "C08", "G08"):
        assert "COUNT(sanctioned_amount)" in by_id[sid]["sql"], sid


def test_shared_geo_columns_are_qualified_on_the_dim_geography_join():
    sql = ("SELECT g.ac_name, lgd_district, COUNT(*) AS records "
           "FROM curated.v_cm_elevate_disbursement v "
           "JOIN curated.dim_geography g ON g.geography_key = v.geography_key "
           "WHERE UPPER(g.ac_name) = UPPER('Mairang') GROUP BY g.ac_name, lgd_district")
    out = p._cm_legacy_qualify_shared_geo_cols([LEGACY], sql)
    assert "v.lgd_district" in out and " lgd_district," not in out
    assert p._cm_legacy_qualify_shared_geo_cols(["Focus Legacy"], sql) == sql


def test_output_alias_is_not_qualified():
    sql = ("SELECT v.lgd_district AS lgd_district FROM curated.v_cm_elevate_disbursement v "
           "JOIN curated.dim_geography g ON g.geography_key = v.geography_key")
    assert "AS lgd_district" in p._cm_legacy_qualify_shared_geo_cols([LEGACY], sql)


def test_multi_row_fallback_is_readable_and_says_what_it_cut():
    rows = [{"lgd_district": f"D{i}", "records": i, "total_disbursed_cr": 1.5} for i in range(20)]
    out = p._deterministic_answer(rows)
    assert out.startswith("Here are the 20 results:")
    assert "- D0 — records: 0, total disbursed (₹ crore): 1.50" in out
    assert "…and 5 more rows in the table." in out


@pytest.mark.parametrize("question", [
    "What was the total disbursement in each financial year in CM Elevate Legacy?",
    "How many applications are recorded for each financial year in CM Elevate Legacy?",
    "What is the total disbursement made through each loan entity in CM Elevate Legacy?",
])
def test_per_group_questions_do_not_pause_for_scope(question):
    assert not p._needs_scope_clarification(question, {})


def test_a_bare_total_still_pauses_for_scope():
    assert p._needs_scope_clarification("How many total applications are recorded under CM Elevate Legacy?", {})


@pytest.mark.parametrize("question,expected", [
    ("applications mapped to Mairang constituency", None),
    ("applications in Mairang", None),
    ("records in EWKH", "EASTERN WEST KHASI HILLS"),
    ("records in Eastern West Khasi Hills district", "EASTERN WEST KHASI HILLS"),
    ("total in South West Garo Hills", "SOUTH WEST GARO HILLS"),
])
def test_hq_town_alias_does_not_scan_as_its_district(question, expected):
    r = entity_resolver.scan_dimension(question, LEGACY, "district")
    assert (r.canonical.upper() if r else None) == expected


_DISTRICT_SQL = (
    "SELECT lgd_district, COUNT(*) AS records,\n"
    "       ROUND(SUM(sanctioned_amount) / 1e7, 2) AS sanctioned_cr,\n"
    "       ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr\n"
    "FROM curated.v_cm_elevate_disbursement\n"
    "WHERE financial_year = '2024-25'\n"
    "GROUP BY lgd_district\nORDER BY total_disbursed_cr DESC\nLIMIT 100")
_TWO_ROWS = [{"lgd_district": "A", "total_disbursed_cr": 1.0},
             {"lgd_district": "B", "total_disbursed_cr": 2.0}]


def test_exact_total_is_requeried_without_the_group_by(monkeypatch):
    import asyncio
    sent = []

    async def fake_run(sql):
        sent.append(sql)
        return [{"sanctioned_cr": 142.74, "total_disbursed_cr": 82.9}]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    notes = asyncio.run(p._cm_legacy_exact_totals(_DISTRICT_SQL, _TWO_ROWS))
    assert sent == ["SELECT ROUND(SUM(sanctioned_amount) / 1e7, 2) AS sanctioned_cr, "
                    "ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
                    "FROM curated.v_cm_elevate_disbursement\nWHERE financial_year = '2024-25'"]
    assert "total_disbursed_cr = 82.90" in notes[0]


@pytest.mark.parametrize("sql,rows", [
    (_DISTRICT_SQL.replace("GROUP BY lgd_district", "GROUP BY ROLLUP(lgd_district)"), _TWO_ROWS),
    (_DISTRICT_SQL, _TWO_ROWS[:1]),
    (_DISTRICT_SQL, _TWO_ROWS + [{"lgd_district": "ALL DISTRICTS", "total_disbursed_cr": 3.0}]),
    ("SELECT COUNT(*) AS records FROM curated.v_cm_elevate_disbursement GROUP BY lgd_district", _TWO_ROWS),
])
def test_exact_total_skipped_when_not_needed(monkeypatch, sql, rows):
    import asyncio

    async def boom(sql):
        raise AssertionError("should not query")
    monkeypatch.setattr(p, "run_readonly", boom)
    assert asyncio.run(p._cm_legacy_exact_totals(sql, rows)) == []


def test_zero_crore_value_is_described_as_under_one_lakh():
    notes = p._cm_legacy_small_money_notes([{"scheme_name": "Spinning", "total_disbursed_cr": 0.0}])
    assert "under ₹0.01 crore" in notes[0]
    assert p._cm_legacy_small_money_notes([{"scheme_name": "X", "total_disbursed_cr": 0.5}]) == []


# ── Use-case re-test 2026-09-29 (KI-166 / 167 / 168) and all-villages run ─────
_V = "curated.v_cm_elevate_disbursement"


def test_sanctioned_count_star_is_rewritten_to_count_sanctioned_amount():
    # TC-14: "2,823 applications have been sanctioned" (true 2,820), 5 of 5 runs
    sql = f"SELECT COUNT(*) AS sanctioned_records\nFROM {_V}\nLIMIT 1"
    out = p._cm_legacy_sanctioned_count(
        "How many applications have been sanctioned under CM Elevate Legacy for all of Meghalaya "
        "across all financial years", [LEGACY], sql)
    assert out == f"SELECT COUNT(sanctioned_amount) AS sanctioned_records\nFROM {_V}\nLIMIT 1"


def test_sanctioned_question_with_a_plain_record_count_gets_the_sanctioned_count():
    sql = f"SELECT COUNT(*) AS records FROM {_V} WHERE lgd_block = 'TIKRIKILLA' ORDER BY records DESC"
    out = p._cm_legacy_sanctioned_count(
        "How many sanctioned CM Elevate Legacy applications are there in Tikrikilla block?", [LEGACY], sql)
    assert out == (f"SELECT COUNT(sanctioned_amount) AS sanctioned_records FROM {_V} "
                   "WHERE lgd_block = 'TIKRIKILLA' ORDER BY sanctioned_records DESC")


@pytest.mark.parametrize("question,sql", [
    ("How many applications have not been sanctioned?",
     f"SELECT COUNT(*) FILTER (WHERE sanctioned_amount IS NULL) AS not_sanctioned_records, COUNT(*) AS records FROM {_V}"),
    ("Give me a district-wise summary of applications, sanctioned cases and total disbursement",
     f"SELECT lgd_district, COUNT(*) AS records, COUNT(sanctioned_amount) AS sanctioned_records FROM {_V} GROUP BY 1"),
    ("What is the total sanctioned amount?", f"SELECT ROUND(SUM(sanctioned_amount) / 1e7, 2) AS sanctioned_cr FROM {_V}"),
    ("How many applications are recorded?", f"SELECT COUNT(*) AS records FROM {_V}"),
    ("How many desanctioned applications are there?", f"SELECT COUNT(*) AS desanctioned FROM {_V}"),
])
def test_sanctioned_count_guard_leaves_other_measures_alone(question, sql):
    assert p._cm_legacy_sanctioned_count(question, [LEGACY], sql) == sql


def test_sanctioned_count_guard_is_cm_elevate_legacy_only():
    sql = f"SELECT COUNT(*) AS sanctioned_records FROM {_V}"
    assert p._cm_legacy_sanctioned_count("How many sanctioned?", ["PMAY-G"], sql) == sql


def test_unrequested_limit_on_a_scheme_ranking_is_dropped():
    # TC-13: LIMIT 10 cut 3 of the 13 schemes; the answer called row 10 the lowest
    sql = (f"SELECT scheme_name, COUNT(*) AS records FROM {_V}\n"
           "GROUP BY scheme_name\nORDER BY records DESC\nLIMIT 10")
    out = p._cm_legacy_unrequested_limit(
        "Which schemes have the highest number of applications in CM Elevate Legacy?", [LEGACY], sql)
    assert out == sql[:sql.index("\nLIMIT 10")]


@pytest.mark.parametrize("question,sql", [
    ("Which scheme has the highest number of applications?",
     f"SELECT scheme_name, COUNT(*) r FROM {_V} GROUP BY 1 ORDER BY r DESC LIMIT 1"),
    ("Top 5 schemes by applications", f"SELECT scheme_name, COUNT(*) r FROM {_V} GROUP BY 1 ORDER BY r DESC LIMIT 5"),
    ("Show the 3 highest districts", f"SELECT lgd_district, COUNT(*) r FROM {_V} GROUP BY 1 ORDER BY r DESC LIMIT 3"),
    ("How many applications are recorded?", f"SELECT COUNT(*) AS records FROM {_V} LIMIT 1"),
])
def test_requested_or_ungrouped_limits_are_kept(question, sql):
    assert p._cm_legacy_unrequested_limit(question, [LEGACY], sql) == sql


_TIK_ROWS = ([{"lgd_village_name": f"V{i}", "village_code": 1000 + i, "records": 1} for i in range(24)]
             + [{"lgd_village_name": f"W{i}", "village_code": 2000 + i, "records": 2} for i in range(11)]
             + [{"lgd_village_name": "BOROBATAPARA", "village_code": 273257, "records": 13}])


@pytest.mark.parametrize("said,fixed", [
    ("while 20 villages each have 1 record and", "while 24 villages each have 1 record and"),
    ("Nine villages each have 2 records.", "11 villages each have 2 records."),
    ("24 villages each have 1 record.", "24 villages each have 1 record."),
])
def test_each_have_counts_are_recomputed_from_every_row(said, fixed):
    # TC-34: "20 villages each have 1 record" against a true 24, 5 of 5 runs
    assert p._cml_fix_each_have_counts(said, _TIK_ROWS) == fixed


def test_each_have_fix_needs_exactly_one_count_column():
    rows = [{"v": "A", "records": 1, "sanctioned_records": 1}, {"v": "B", "records": 2, "sanctioned_records": 1}]
    assert p._cml_fix_each_have_counts("5 villages each have 1 record", rows) == "5 villages each have 1 record"


def test_no_village_records_are_requeried_in_the_same_scope_and_stated(monkeypatch):
    import asyncio
    sent = []

    async def fake_run(sql):
        sent.append(sql)
        return [{"n": 6}] if "COUNT(*) AS n" in sql else [{"v": None}]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    sql = (f"SELECT lgd_village_name, village_code,\n       COUNT(*) AS records\nFROM {_V}\n"
           "WHERE lgd_block = 'TIKRIKILLA'\n  AND entity_type <> 'Unresolved'\n"
           "GROUP BY lgd_village_name, village_code\nORDER BY records DESC")
    ans = asyncio.run(p._cm_legacy_answer_guarantees(
        "Show me the no of applications mapped to each village in Tikrikilla block", sql, _TIK_ROWS,
        "Tikrikilla block has 89 applications in 42 villages.", {"block": "TIKRIKILLA"}))
    assert sent[0] == (f"SELECT COUNT(*) AS n FROM {_V}\nWHERE lgd_block = 'TIKRIKILLA'\n"
                       "  AND entity_type = 'Unresolved'")
    assert ans.endswith("A further 6 records in Tikrikilla block have no village code, so they are not in "
                        "the village list (counted in block and district totals).")


def test_exact_small_amount_replaces_under_one_lakh_wording(monkeypatch):
    import asyncio
    sent = []

    async def fake_run(sql):
        sent.append(sql)
        return [{"v": 0}]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    sql = (f"SELECT ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr FROM {_V} "
           "WHERE village_code = 277592 GROUP BY lgd_village_name ORDER BY total_disbursed_cr DESC LIMIT 1")
    ans = asyncio.run(p._cm_legacy_answer_guarantees(
        "What is the total disbursement amount for Umtham village?", sql, [{"total_disbursed_cr": 0.0}],
        "The total disbursement for Umtham village is under ₹0.01 crore.", {"village": "Umtham"}))
    assert sent == [f"SELECT SUM(total_disbursement) AS v FROM {_V} WHERE village_code = 277592"]
    assert ans == "The total disbursement for Umtham village is ₹0."


def test_exact_rupees_are_added_to_a_small_crore_figure(monkeypatch):
    import asyncio

    async def fake_run(sql):
        return [{"v": 125000}]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    ans = asyncio.run(p._cm_legacy_answer_guarantees(
        "What is the total disbursement for Nongthymmai village?",
        f"SELECT ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr FROM {_V} WHERE village_code = 277681",
        [{"total_disbursed_cr": 0.01}], "₹0.01 crore has been disbursed for Nongthymmai village.",
        {"village": "Nongthymmai"}))
    assert ans == "₹0.01 crore (₹1,25,000) has been disbursed for Nongthymmai village."


@pytest.mark.parametrize("v,s", [(0, "₹0"), (999, "₹999"), (62500, "₹62,500"), (125000, "₹1,25,000"),
                                 (1950500, "₹19,50,500"), (12345678.5, "₹1,23,45,678.50")])
def test_indian_rupee_grouping(v, s):
    assert p._cml_inr(v) == s


def test_place_name_misspelling_is_corrected_and_other_names_left():
    display = {"village": "Rongchigre", "block": "SELSELLA", "district": "West Garo Hills"}
    assert p._cml_fix_place_spelling("1 application in Rongchigre village, Sellsella block, West Garo Hills.",
                                     display) == "1 application in Rongchigre village, Selsella block, West Garo Hills."
    assert p._cml_fix_place_spelling("Tikrikilla block: BOROBATAPARA 13, TIKRIKILLA 9.", {"block": "TIKRIKILLA"}) \
        == "Tikrikilla block: BOROBATAPARA 13, TIKRIKILLA 9."


def test_twin_village_chips_rank_by_cm_elevate_legacy_records(monkeypatch):
    # all-villages run: "Nongthymmai" had 10 registry matches; the JIRANG one holds the records
    import asyncio
    queries = []

    async def fake_fetch(sql, params):
        queries.append(sql)
        return [{"village_code": 277681}]
    monkeypatch.setattr(p, "fetch_rows", fake_fetch)
    cands = [{"village_code": c, "name": "NONGTHYMMAI"} for c in (276411, 276536, 276691, 277052, 277681)]
    ranked = asyncio.run(p._fl_rank_villages([LEGACY], cands))
    assert ranked[0]["village_code"] == 277681
    assert "curated.v_cm_elevate_disbursement" in queries[0]
    assert asyncio.run(p._fl_rank_villages(["PMAY-G"], cands)) == cands


def test_chip_pin_keeps_the_same_block_twin_holding_cm_elevate_legacy_records(monkeypatch):
    import asyncio

    async def fake_fetch(sql, params):
        if "dim_geography" in sql:
            return [{"village_code": 1, "lgd_village_name": "DEWSAW", "lgd_block": "MAIRANG",
                     "lgd_district": "EASTERN WEST KHASI HILLS"},
                    {"village_code": 2, "lgd_village_name": "DEWSAW", "lgd_block": "MAIRANG",
                     "lgd_district": "EASTERN WEST KHASI HILLS"}]
        assert "v_cm_elevate_disbursement" in sql
        return [{"village_code": 2}]
    monkeypatch.setattr(p, "fetch_rows", fake_fetch)
    pin = asyncio.run(p._mgnrega_village_chip_pin(
        "How many CM Elevate Legacy applications are recorded in DEWSAW village, MAIRANG block, "
        "EASTERN WEST KHASI HILLS across all financial years", "CM Elevate Legacy"))
    assert pin["village_code"] == 2


def test_row_label_misspelling_is_corrected_in_a_block_breakdown():
    # all-blocks run: "Mawsynrut" for the MAWSHYNRUT row of West Khasi Hills
    rows = [{"lgd_block": b, "records": n} for b, n in
            [("NONGSTOIN", 135), ("MAWSHYNRUT", 31), ("SHALLANG", 26), ("RAMBRAI", 13), ("RI MULIANG", 12)]]
    out = p._cml_fix_place_spelling("Nongstoin, Mawsynrut, Shallang, Rambrai and Ri Muliang: 135, 31, 26, 13, 12.",
                                    {"district": "West Khasi Hills"}, rows)
    assert out == "Nongstoin, Mawshynrut, Shallang, Rambrai and Ri Muliang: 135, 31, 26, 13, 12."


def test_a_label_that_is_another_known_name_is_never_respelled():
    rows = [{"lgd_block": "UMSNING", "records": 3}, {"lgd_block": "UMLING", "records": 2}]
    assert p._cml_fix_place_spelling("UMSNING 3 and UMLING 2.", {}, rows) == "UMSNING 3 and UMLING 2."


def test_place_literals_beside_a_pinned_village_code_are_dropped():
    # all-villages run: the ward's block written into lgd_village_name -> 0 records for a ward with 1
    sql = (f"SELECT COUNT(*) AS records FROM {_V} WHERE lgd_village_name = 'WILLIAM NAGAR-MUNICIPAL BOARD' "
           "AND village_code = 70681 AND UPPER(lgd_block) = 'WILLIAM NAGAR-MUNICIPAL BOARD' LIMIT 1000")
    assert p._focus_legacy_village_code_only([LEGACY], {"village_code": 70681}, sql) == \
        f"SELECT COUNT(*) AS records FROM {_V} WHERE village_code = 70681 LIMIT 1000"


def test_focus_legacy_keeps_its_village_name_literal():
    sql = "SELECT 1 FROM v WHERE lgd_village_name = 'X' AND village_code = 7 AND lgd_block = 'B'"
    assert p._focus_legacy_village_code_only(["Focus Legacy"], {"village_code": 7}, sql) == \
        "SELECT 1 FROM v WHERE lgd_village_name = 'X' AND village_code = 7"


_UMSNING = [{"scheme_name": s, "records": n} for s, n in [
    ("Meghalaya Piggery Development Scheme", 27), ("Meghalaya Poultry Farming Scheme", 24),
    ("Meghalaya Dairy Development Scheme", 16), ("Meghalaya Sericulture & Weaving Scheme (spinning)", 15),
    ("Prime Agriculture Response Vehicle Scheme", 13), ("Meghalaya Sericulture & Weaving Scheme(weaving)", 6),
    ("Meghalaya Agriculture Warehouse Scheme", 3), ("Meghalaya Sports & Wellness Scheme", 1),
    ("Prime Tourism Vehicle Scheme", 1)]]


def test_breakdown_figures_without_their_names_are_rebuilt_under_the_headline():
    # all-blocks run: "The remaining schemes show 24, 16, 15, 13, 6, 3, 1, and 1 records respectively"
    ans = ("Umsning block has 106 total applications across nine schemes, with the Meghalaya Piggery "
           "Development Scheme holding the highest count at 27 records. The remaining schemes show 24, 16, "
           "15, 13, 6, 3, 1, and 1 records respectively.")
    out = p._cml_complete_list("How many CM Elevate Legacy applications are there under each scheme in "
                               "Umsning block?", ans, _UMSNING)
    assert out.startswith("Umsning block has 106 total applications across nine schemes, with the Meghalaya "
                          "Piggery Development Scheme holding the highest count at 27 records.\n\n"
                          "Applications by scheme (9): Piggery Development Scheme 27; Poultry Farming Scheme 24;")
    assert out.endswith("Sports & Wellness Scheme 1; Prime Tourism Vehicle Scheme 1. Total 106.")


def test_a_breakdown_that_names_every_figure_is_left_alone():
    rows = [{"lgd_block": "RONGRAM", "records": 206}, {"lgd_block": "TIKRIKILLA", "records": 95}]
    ans = "RONGRAM block holds 206 records, followed by TIKRIKILLA with 95."
    assert p._cml_complete_list("Show me the number of applications in each block of West Garo Hills.",
                                ans, rows) == ans


@pytest.mark.parametrize("rows,ans", [
    ([{"financial_year": "2024-2025", "records": 2291}, {"financial_year": "2025-2026", "records": 137}],
     "FY 2024-25 holds 2,291 records and FY 2025-26 holds 137."),
    ([{"scheme_name": "A", "total_disbursed_cr": 1.5}, {"scheme_name": "B", "total_disbursed_cr": 2.5}],
     "Figures are 1.50 and 2.50 respectively."),
])
def test_year_and_money_breakdowns_are_not_rebuilt(rows, ans):
    assert p._cml_complete_list("How many applications for each financial year / each scheme?", ans, rows) == ans


_PURAKHASIA = [{"lgd_village_name": n, "village_code": 273000 + i, "records": v} for i, (n, v) in enumerate([
    ("RAPANGPANGGIRI", 6), ("GOPINATHKILLA", 5), ("DINAPARA", 3), ("MARENGPARA", 3), ("SALMANPARA", 3),
    ("DINGAMPARA", 2), ("BALIJHORA", 1), ("DAMALGRE", 1), ("DARUGRE", 1), ("JARANGPARA", 1), ("KIDAPARA", 1),
    ("NACHILPARA", 1), ("RIMTANGPARA", 1), ("SONAJURI", 1)])]


def test_garbled_village_breakdown_with_a_false_claim_is_rebuilt():
    # all-blocks run: "1 record at each of the other 13 villages" (8 have 1), names missing, total 30 never stated
    ans = ("Purakhasia block shows 14 villages with CM Elevate Legacy applications, ranging from 6 records at "
           "Rapangpanggiri down to 1 record at each of the other 13 villages. The village-level record counts "
           "are 6, 5, 3, 3, 3, 2, 1, 1, 1, 1, 1, 1, 1, and 1 respectively.")
    out = p._cml_village_breakdown(ans, _PURAKHASIA, {"block": "PURAKHASIA"})
    assert out.startswith("30 applications are mapped to 14 villages in Purakhasia block.\n\n"
                          "Applications by village (14): Rapangpanggiri 6; Gopinathkilla 5; Dinapara 3;")
    assert out.endswith("Rimtangpara 1; Sonajuri 1.")


def test_village_breakdown_missing_only_its_total_gets_a_headline():
    ans = "Rapangpanggiri leads with 6 records, followed by Gopinathkilla with 5."
    assert p._cml_village_breakdown(ans, _PURAKHASIA, {"block": "PURAKHASIA"}) == \
        "30 applications are mapped to 14 villages in Purakhasia block. " + ans


def test_a_correct_village_breakdown_is_left_alone():
    ans = ("Purakhasia block has 30 applications in 14 villages. Rapangpanggiri holds the most with 6, "
           "and 8 villages each have 1 record.")
    assert p._cml_village_breakdown(ans, _PURAKHASIA, {"block": "PURAKHASIA"}) == ans


def test_raw_fallback_row_dump_becomes_a_sentence():
    # all-constituencies run: the officer got "0.58 total disbursed cr."
    row = {"total_disbursed_cr": 0.58}
    assert p._deterministic_answer([row]) == "0.58 total disbursed cr."
    assert p._cml_single_row_sentence(row, {"constituency": "MAWKYNREW"}) == \
        "Mawkynrew constituency: ₹0.58 crore disbursed under CM Elevate Legacy across all financial years."
    assert p._cml_single_row_sentence({"records": 95, "total_disbursed_cr": 2.8},
                                      {"block": "TIKRIKILLA", "year": "FY 2024-25"}) == \
        "Tikrikilla block: 95 applications; ₹2.80 crore disbursed under CM Elevate Legacy in FY 2024-25."


def test_disbursement_note_forbids_a_split_the_result_does_not_carry():
    sql = "SELECT ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr FROM v"
    notes = p._cm_legacy_answer_notes(sql, [{"total_disbursed_cr": 0.58}])
    assert any("do not mention subsidy or loan at all" in n for n in notes)
    both = p._cm_legacy_answer_notes(sql, [{"total_disbursed_cr": 1, "subsidy_cr": 0.6, "loan_cr": 0.4}])
    assert any("give all three" in n for n in both) and not any("do not mention subsidy" in n for n in both)


def test_urban_body_maps_to_the_cm_elevate_legacy_stored_block(monkeypatch):
    # all-blocks run: "Tura Municipal Board" refused as outside Meghalaya
    import asyncio
    monkeypatch.setattr(p, "_CML_BLOCKS", ["RONGRAM", "TIKRIKILLA", "TURA MUNICIPAL BOARD",
                                            "WILLIAM NAGAR-MUNICIPAL BOARD"])
    assert asyncio.run(p._cm_elevate_block_from_mention("Tura Municipal Board", [LEGACY])) == "TURA MUNICIPAL BOARD"
    resolved, display = {"block": "TURA MUNICIPAL BOARD-MUNICIPAL BOARD"}, {}
    asyncio.run(p._cm_elevate_blocks_to_data("How many applications in Tura Municipal Board?",
                                             resolved, display, LEGACY))
    assert resolved["block"] == "TURA MUNICIPAL BOARD"


def test_group_by_on_an_unselected_column_is_dropped():
    # final pass: CHIOKGRE — invalid per-year grouping repeated by the repair model 4 times
    sql = (f"SELECT COUNT(*) AS records FROM {_V} WHERE village_code = 275499 "
           "GROUP BY COALESCE(financial_year_short, '(no financial year)') ORDER BY financial_year_short NULLS LAST")
    assert p._cm_legacy_unselected_group_by([LEGACY], sql) == \
        f"SELECT COUNT(*) AS records FROM {_V} WHERE village_code = 275499"


@pytest.mark.parametrize("sql", [
    f"SELECT COALESCE(financial_year_short, '(none)') AS financial_year, COUNT(*) AS records FROM {_V} "
    "GROUP BY COALESCE(financial_year_short, '(none)') ORDER BY 1",
    f"SELECT lgd_block, COUNT(*) AS records FROM {_V} WHERE lgd_district = 'X' GROUP BY lgd_block ORDER BY records DESC",
    f"SELECT scheme_name, COUNT(*) r FROM {_V} GROUP BY 1 ORDER BY r DESC",
])
def test_real_breakdowns_keep_their_group_by(sql):
    assert p._cm_legacy_unselected_group_by([LEGACY], sql) == sql


def test_verifier_block_district_complaint_on_a_pinned_village_is_discarded():
    # final pass: DARUGRE / MARENGPARA, Purakhasia — rejected 4 times after the chip
    issue = ("Check 2: The RESOLVED ENTITIES block lists 'village_code = 273879' (DARUGRE), but the question "
             "explicitly names 'PURAKHASIA block' and 'SOUTH WEST GARO HILLS' as required geographic filters.")
    sql = f"SELECT COUNT(*) AS records FROM {_V} WHERE village_code = 273879"
    assert p._verifier_village_code_complaint_is_false(issue, [LEGACY], {"village_code": 273879}, sql)
    assert not p._verifier_village_code_complaint_is_false(issue, [LEGACY], {"village_code": 1}, sql)


# ── A village read as a programme, and the village dropped (all-villages run 2026-10-02) ──
_NOAGRE_ER = {"resolved": {"village_code": 273728}}
_NOAGRE_Q = "How many applications are there under CM Elevate Legacy in NOAGRE across all financial years"
_NOAGRE_BAD = ("SELECT COUNT(*) AS records FROM curated.v_cm_elevate_disbursement "
               "WHERE scheme_name = 'Meghalaya New Agriculture and Allied Activities Scheme' LIMIT 1")


def test_village_read_as_a_programme_is_corrected_to_the_village():
    sql = p._cm_unasked_programme_beside_village(_NOAGRE_Q, ["CM Elevate Legacy"], _NOAGRE_ER, _NOAGRE_BAD)
    sql = p._pin_missing_village_code(["CM Elevate Legacy"], _NOAGRE_ER, sql)
    assert "scheme_name" not in sql and "village_code = 273728" in sql


def test_village_guard_now_covers_every_scheme():
    for s in (["CM Elevate Legacy"], ["CM Elevate"], ["PMAY-G"], ["Focus Plus"], ["Focus Legacy"], ["MGNREGA"]):
        assert p._village_filter_missing(s, _NOAGRE_ER, "SELECT COUNT(*) FROM t WHERE lgd_block = 'X'") == 273728
        assert p._village_filter_missing(s, _NOAGRE_ER, "SELECT COUNT(*) FROM t WHERE village_code = 273728") is None
    assert p._village_filter_missing(["PMAY-G", "Focus Plus"], _NOAGRE_ER, "SELECT 1") is None


def test_named_programme_beside_a_village_is_kept():
    sql = ("SELECT COUNT(*) FROM curated.v_cm_elevate_disbursement "
           "WHERE scheme_name = 'Meghalaya Piggery Development Scheme'")
    assert p._cm_unasked_programme_beside_village(
        "applications under Piggery in NOAGRE", ["CM Elevate Legacy"], _NOAGRE_ER, sql) == sql


def test_village_pin_drops_block_and_district_beside_it_and_skips_complex_sql():
    out = p._pin_missing_village_code(["PMAY-G"], _NOAGRE_ER,
                                      "SELECT COUNT(*) FROM curated.v_pmay WHERE NOT is_placeholder "
                                      "AND lgd_block = 'X' AND lgd_district = 'Y'")
    assert out == "SELECT COUNT(*) FROM curated.v_pmay WHERE village_code = 273728 AND NOT is_placeholder"
    join = "SELECT 1 FROM a JOIN b ON a.x = b.x"
    assert p._pin_missing_village_code(["CM Elevate"], _NOAGRE_ER, join) == join


def test_year_chip_after_village_chip_pins_for_cm_elevate_legacy_too():
    # "MAWTNUM — UMLING block" then "All financial years combined" answered all of Umling (224, not 4)
    import inspect
    src = inspect.getsource(p.resolve_entities)
    assert "_year_after_tail = bool(_pin_scheme and _YEAR_AFTER_CHIP_TAIL_RE" in src
    assert p._YEAR_AFTER_CHIP_TAIL_RE.search(
        "How many applications are there under CM Elevate Legacy in MAWTNUM, UMLING block, RI BHOI "
        "across all financial years")


def test_village_name_written_as_a_programme_is_dropped():
    # "…WHERE village_code = 279360 AND scheme_name = 'AMLARI MODEL'" answered 0 (2026-10-02)
    from app import entity_resolver as er
    er.load_all()
    er_ = {"resolved": {"village_code": 279360}}
    sql = ("SELECT COUNT(*) AS records FROM curated.v_cm_elevate_disbursement "
           "WHERE village_code = 279360 AND scheme_name = 'AMLARI MODEL' LIMIT 1")
    out = p._cm_unasked_programme_beside_village(
        "How many applications are there under CM Elevate Legacy in AMLARI MODEL?", ["CM Elevate Legacy"], er_, sql)
    assert "scheme_name" not in out and "village_code = 279360" in out
    real = ("SELECT COUNT(*) FROM curated.v_cm_elevate_disbursement "
            "WHERE village_code = 279360 AND scheme_name = 'Meghalaya Piggery Development Scheme'")
    assert p._cm_unasked_programme_beside_village(
        "applications under Piggery in AMLARI MODEL", ["CM Elevate Legacy"], er_, real) == real


def test_cm_elevate_legacy_is_a_village_scheme():
    assert p._village_scheme(["CM Elevate Legacy"]) == "CM Elevate Legacy"


def test_village_named_by_truncated_text_is_replaced_by_its_code():
    # "TEPORPARA ( UPPER )" (273637) written as UPPER(lgd_village_name) = 'TEPORPARA'
    # matched another village: 2 applications for 1 (all-villages re-run 2026-10-02)
    sql = ("SELECT COUNT(*) AS records\nFROM curated.v_cm_elevate_disbursement\n"
           "WHERE UPPER(lgd_village_name) = 'TEPORPARA' AND financial_year_short = '2024-25'\nLIMIT 1")
    out = p._pin_missing_village_code(["CM Elevate Legacy"], {"resolved": {"village_code": 273637}}, sql)
    assert "lgd_village_name" not in out
    assert "WHERE village_code = 273637 AND financial_year_short = '2024-25'" in out
    # nothing resolved, or the SQL already filters the code: unchanged
    assert p._pin_missing_village_code(["CM Elevate Legacy"], {"resolved": {}}, sql) == sql
    coded = sql.replace("UPPER(lgd_village_name) = 'TEPORPARA'", "village_code = 273637")
    assert p._pin_missing_village_code(["CM Elevate Legacy"], {"resolved": {"village_code": 273637}},
                                       coded) == coded
    # two villages resolved: left to the generator
    assert p._pin_missing_village_code(["CM Elevate Legacy"], {"resolved": {"village_code": [1, 2]}},
                                       sql) == sql


def test_place_spelling_fix_keeps_a_name_that_ends_in_a_bracket():
    d = {"village": "TEPORPARA ( UPPER )"}
    ok = "There are 1 applications in Teporpara ( Upper ) across all financial years."
    assert p._cml_fix_place_spelling(ok, d) == ok        # was "( Upper ) )"
    assert p._cml_fix_place_spelling("in Teporpra ( Upper ) block", d) == "in Teporpara ( Upper ) block"
    assert p._cml_fix_place_spelling("the Sellsella block", {"block": "SELSELLA"}) == "the Selsella block"
