"""
Schema context for the SQL generator, condensed from data/schema/schema_for_developers.md,
split per scheme so a query only pays for the schema/rules/vocabulary it actually
needs — matching how few_shot_examples() and prohibited_joins_text() are already
scheme-scoped. This matters more as scheme count grows: at 2 schemes the waste
from including everything is mild; it does not stay mild at 6.

The BUSINESS_VOCABULARY blocks are reconciled from the two source-of-truth
business documents — data/reference/{mgnrega,pmay}_kpi_use_cases.xlsx
(Glossary + Query Logic columns) — against the real curated schema. Only the
COLUMN NAMES and TERM DEFINITIONS were carried over; the Excel's numeric
"Expected Answer" values were deliberately NOT used as ground truth (several
are stale — see the MGNREGA total-expenditure discrepancy noted in the chat,
unresolved as of 2026-08-28) and do not appear here.

Re-derive this whenever a scheme is added or SCHEMA_FOR_DEVELOPERS.md changes;
do not let it drift.
"""

SCHEME_CATALOG = {
    "MGNREGA": "Rural employment guarantee — village-year grain, two facts "
               "(employment, expenditure), money in LAKH RUPEES.",
    "PMAY-G": "Rural housing scheme — one row per sanctioned house, money in RUPEES.",
    "Focus Plus": "Meghalaya STATE farmer cash-benefit / DBT scheme — one row per "
                  "disbursement (a payment to one member in one tranche), person-level, "
                  "split into a 93K legacy paid cohort and a 12.5K registration cohort. "
                  "Money UNIT IS UNVERIFIED — read curated.dim_scheme.money_unit.",
    "CM Elevate": "Meghalaya PROGRAMME covering 15 individual livelihood/enterprise "
                  "schemes under one scheme_code — one row per application. NO money "
                  "column and NO date column exist anywhere in this partition; "
                  "COUNT(*) is the entire aggregate vocabulary.",
    "Focus Legacy": "Meghalaya STATE legacy producer-group disbursement programme (the "
                    "'FOCUS' scheme) — one row per payment to a PRODUCER GROUP, the only "
                    "group-grained fact in megh_db (no individual exists in it at all). "
                    "amount_disbursed = no_of_pg_members x 5000 on every row. Money UNIT "
                    "IS UNVERIFIED — read curated.dim_scheme.money_unit. NOT Focus Plus: "
                    "the two share a name and no key.",
    "CM Elevate Legacy": "Meghalaya CM-ELEVATE SANCTION-AND-DISBURSEMENT records (the DB "
                         "calls it CM Elevate Disbursement) — one row per applicant's "
                         "sanction and disbursement under one of 13 schemes, with a "
                         "sanctioned amount, subsidy / loan / total disbursed (RUPEES), "
                         "lender (Bank / LIFCOM) and a financial year (FY2024-25, "
                         "FY2025-26). NOT the 15-scheme CM Elevate applications dataset: "
                         "the two share a name and no key.",
}

# The metrics each scheme actually carries in the curated data — the plain-English
# list to show a user who asks for something the data does not track ("MGNREGA
# loan defaults", "PMAY-G rent paid"). Reconciled from the *_VOCAB blocks below;
# keep the two in step when either changes.
SCHEME_METRICS = {
    "MGNREGA": [
        "person-days of work generated",
        "households employed / persons employed",
        "households that completed 100 days",
        "job cards issued (cumulative stock)",
        "wage expenditure (unskilled and semi-skilled) and material expenditure",
        "total expenditure (in lakh rupees)",
        "women employment provided, and its percentage share of persons employed",
        "cost per person-day",
    ],
    "PMAY-G": [
        "houses sanctioned, completed, and in progress",
        "house construction stage (Proposed Site, Existing site (Old House), "
        "House Sanctioned, Plinth, Roof Cast, Completed)",
        "sanctioned amount, amount released, amount pending (in rupees)",
        "installments / tranches paid",
        "utilisation rate and completion rate",
    ],
    "Focus Plus": [
        "disbursements / payments made (COUNT(*), one row per payment)",
        "amount disbursed (SUM — UNIT UNVERIFIED, read curated.dim_scheme.money_unit; "
        "only two values exist, 5,000 and 2,500)",
        "members with an id (COUNT(DISTINCT member_id) — 12.5K registration cohort only, "
        "~3.2% of rows; NOT the scheme total)",
        "batch split — 93K legacy paid cohort vs 12.5K newer registration cohort",
        "tranche breakdown (Tranch 1..4; categorical, never a time series)",
        "gender / occupation / application status — 12.5K cohort only",
        "distinct villages reached (COUNT(DISTINCT village_code))",
    ],
    "CM Elevate": [
        "applications received (COUNT(*), one row per application — a request, not an award)",
        "applications by scheme (15 sub-schemes under cm_scheme_key / scheme_name)",
        "applications by verification outcome (data_verified: Valid / On Hold / Wrong / Invalid)",
        "applications by applicant type (applicant_category: individual / registered / unregistered)",
        "applications by application mode (online / cmconnectcenter)",
        "applications by gender (scheme_specific ->> 'gender_id' — populated on every row)",
        "distinct villages / blocks / districts reached",
        "NO sanctioned amount, subsidy, loan, disbursement, or any rupee figure exists",
        "NO application date, month, or financial year exists",
    ],
    "Focus Legacy": [
        "payments / disbursements made (COUNT(*), one row per payment to a producer group)",
        "producer groups paid (COUNT(DISTINCT pg_id) — never by name; 11,906 groups carry "
        "only 10,678 distinct names)",
        "memberships paid for (SUM(no_of_pg_members) — memberships, NOT distinct people; "
        "there is no person record anywhere in this partition)",
        "amount disbursed (SUM — UNIT UNVERIFIED, read curated.dim_scheme.money_unit; it is "
        "exactly memberships x Rs 5,000 on every row, so it is a membership figure)",
        "producer group type (PG-FOCUS / PG-LAMP / PG-EXISTING, via pg_entity_type)",
        "programme variant ('FOCUS' vs 'Focus (Addnl)', the latter a single top-up day)",
        "product / commodity the group works on (FY2021-22 and FY2022-23 ONLY — 21% of rows)",
        "bank and IFSC the payment went to (bank-wise analysis IS legitimate here)",
        "remittance date — but only 30 distinct dates exist; these are treasury BATCHES",
        "distinct villages / blocks / districts reached",
        "NO individual beneficiary, name, EPIC, member id, gender, caste or household exists",
        "NO application, approval, rejection, status, outcome, target or budget exists",
    ],
    "CM Elevate Legacy": [
        "sanction-and-disbursement records (COUNT(*), one row per applicant per scheme) and "
        "sanctioned cases (records carrying a sanctioned amount), with the sanctioned share",
        "assembly constituency (through the geography registry)",
        "records by scheme (13 schemes: Piggery, Poultry, Dairy, Goat, Warehouse, the two "
        "Sericulture schemes, the two PRIME vehicle schemes and more)",
        "sanctioned amount (in rupees; shown in crore)",
        "subsidy disbursed, loan disbursed and total disbursed (subsidy + loan), and the "
        "share of the sanctioned amount paid out",
        "subsidy / loan instalments (tranches 1-3) and their dates",
        "lender category (Bank / LIFCOM) and loans with no lender recorded",
        "desanctioned records (Refused / Duplicate) and the refusal flag / written reason",
        "financial year (FY2024-25 and FY2025-26 only; the two Sericulture schemes carry "
        "no financial year)",
        "distinct villages / blocks / districts reached",
        "NO applicant name, gender, caste, applicant type, application status, repayment, "
        "target, budget or monthly figure exists",
    ],
}


def available_metrics_text(schemes: list[str]) -> str:
    """A bullet list of the metrics the given scheme(s) carry, for telling a user
    their question asked for one that isn't in the data."""
    lines: list[str] = []
    for s in schemes or list(SCHEME_METRICS):
        for m in SCHEME_METRICS.get(s, []):
            lines.append(f"  - {s}: {m}")
    return "\n".join(lines)

_PREAMBLE = """
Query only these curated/semantic objects (schema-qualified, e.g. curated.v_pmay).
Never query raw, staging or meta — the connection cannot see them anyway.
""".strip()

_SHARED_TABLES = """
SHARED TABLES
  curated.dim_scheme(scheme_key, scheme_code, scheme_name, money_unit, time_semantics)
  curated.dim_year(year_key, financial_year, financial_year_short, start_year, end_year, data_quality_note)
  curated.dim_geography(geography_key, village_code, lgd_village_name, lgd_block, lgd_district, entity_type)

WHERE scheme_key / scheme_code EXIST — read before adding any scheme filter:
  These two columns live on EXACTLY two objects: curated.dim_scheme and
  curated.v_cross_scheme_money_district_year. They are NOT on curated.v_pmay,
  curated.v_expenditure, curated.v_employment, curated.v_district_year_summary,
  curated.v_pmay_monthly_sanctions, curated.v_focus_plus or curated.v_cm_elevate —
  selecting or filtering scheme_key / scheme_code there fails with
  `column "scheme_key" does not exist`.
  Every per-scheme fact table and view is already ONE scheme's data. For a question
  about a single scheme (MGNREGA only, PMAY-G only, Focus Plus only, or CM Elevate
  only) do NOT add a scheme filter and do NOT join to curated.dim_scheme — just query
  that scheme's own object directly. A scheme filter is needed ONLY on
  curated.v_cross_scheme_money_district_year, i.e. only for a question that spans more
  than one scheme at once.
  (Exception: for Focus Plus or CM Elevate you MAY read curated.dim_scheme on its own —
  never joined per row — purely to look up money_unit, because both amount units are
  unverified/nonexistent and neither view exposes scheme_key. For CM Elevate,
  dim_scheme.money_unit describes NOTHING — the column is NOT NULL but there is no
  amount on the fact for it to describe.)

SCHEME_CODE LITERALS — when you DO filter curated.v_cross_scheme_money_district_year
  (a multi-scheme question), these are the values stored in scheme_code:
      MGNREGA    -> 'MGNREGA'
      PMAY-G     -> 'PMAY'      <- the code is the bare string 'PMAY'. It is NOT 'PMAY-G',
                                   'PMAY_G', 'PMAYG' or 'PMAY-Gramin'. Filtering
                                   scheme_code = 'PMAY-G' matches ZERO rows and returns a
                                   false 0 for the PMAY side while an unfiltered SUM still
                                   looks right — always write scheme_code = 'PMAY'.
      Focus Plus -> PRESENCE UNCONFIRMED. Run `SELECT DISTINCT scheme_code FROM
                                   curated.v_cross_scheme_money_district_year` first.
                                   If Focus Plus is absent, say the money comparison
                                   covers MGNREGA and PMAY-G only — do NOT guess a
                                   literal. If present, use the exact string that query
                                   returns.
      CM Elevate -> CONFIRMED ABSENT, not merely unconfirmed. The view definition is a
                                   two-branch UNION ALL over MGNREGA and PMAY only —
                                   verified from the live view definition, not inferred.
                                   CM Elevate has no amount column to contribute in any
                                   case. Never filter for it here; say plainly that the
                                   money comparison covers MGNREGA and PMAY-G only.
""".strip()

_SHARED_RULES = """
  * Count villages by village_code or geography_key, never by name (duplicate names exist).
  * The ENTIRE dataset is Meghalaya state — every row is already in Meghalaya. "in Meghalaya",
    "across Meghalaya", "state-wide", "overall", or naming no place at all means NO geographic
    filter whatsoever. Never emit lgd_district / lgd_block / lgd_village_name = 'Meghalaya'
    (or ILIKE '%meghalaya%'), and never add a dim_geography sub-select on entity_type = 'State'
    or lgd_village_name = 'MEGHALAYA' — no fact row's lgd_district matches that, so the filter
    silently returns 0 rows. Add a district/block filter ONLY when the question names a
    specific district or block.
  * NEVER put DISTINCT — or a nested aggregate — inside a window function
    (COUNT(DISTINCT x) OVER (...), SUM(DISTINCT x) OVER (...)). PostgreSQL rejects it
    ("DISTINCT is not implemented for window functions"). Do the distinct count in its own
    CTE first, then apply the window function to that.
  * "What share / % of <metric> is concentrated in the top N% (or top decile / quartile) of
    <districts | blocks | villages>" — a concentration / Pareto question. Aggregate per unit,
    rank, then divide — do NOT try to do it in one windowed expression:
      WITH per_unit AS (
        SELECT <unit>, SUM(<metric>) AS m FROM <curated object> [WHERE ...] GROUP BY <unit>
      ), ranked AS (
        SELECT m, NTILE(<100 / N>) OVER (ORDER BY m DESC) AS bucket FROM per_unit
      )
      SELECT ROUND(100.0 * SUM(m) FILTER (WHERE bucket = 1) / NULLIF(SUM(m), 0), 1)
             AS pct_in_top_slice
      FROM ranked
      LIMIT 1;
    top 10% -> NTILE(10); top 25% / quartile -> NTILE(4); top 5% -> NTILE(20). For "top N
    <units>" (a count, not a percentage) rank with ROW_NUMBER() OVER (ORDER BY m DESC) and
    divide the sum of rows <= N by the grand total instead.
""".strip()

_CLOSING = """
Every query MUST end with a LIMIT clause (the executor adds one if you omit it, but include it).
Generate a single read-only SELECT statement only — no comments, no explanation, SQL only.
""".strip()

_MGNREGA_TABLES = """
MGNREGA TABLES
  curated.fact_mgnrega_employment(year_key, geography_key, person_days, households_employed,
      persons_employed, households_completed_100_days, job_cards_issued_total)
  curated.fact_mgnrega_expenditure(year_key, geography_key, unskilled_wage_exp,
      semi_skilled_wage_exp, material_exp, total_exp)  -- LAKH RUPEES
  curated.v_employment / curated.v_expenditure  -- facts pre-joined to year+geography
  curated.v_district_year_summary  -- THE way to combine both MGNREGA facts (district x year)
""".strip()

_MGNREGA_RULES = """
MGNREGA RULES (breaking these produces a wrong number, not just an ugly query):
  1. NEVER join fact_mgnrega_employment to fact_mgnrega_expenditure directly (no shared grain).
     The two facts share NO measures: person_days / persons_employed /
     households_employed / households_completed_100_days / job_cards_issued_total are on
     curated.v_employment ONLY, while total_exp / unskilled_wage_exp /
     semi_skilled_wage_exp / material_exp are on curated.v_expenditure ONLY. A question
     wanting one from each ("compare expenditure and person-days in X") therefore cannot
     be answered from a single view.
       - At DISTRICT x YEAR grain: select both from curated.v_district_year_summary.
       - At BLOCK or VILLAGE grain: v_district_year_summary has no block/village column,
         so aggregate each fact in its own CTE with the SAME filters, then combine:
           WITH emp AS (SELECT SUM(person_days) AS person_days FROM curated.v_employment
                        WHERE lgd_block = '<BLOCK>' AND year_key = <YYYY>),
                exp AS (SELECT SUM(total_exp) AS total_exp_lakh FROM curated.v_expenditure
                        WHERE lgd_block = '<BLOCK>' AND year_key = <YYYY>)
           SELECT exp.total_exp_lakh, emp.person_days FROM emp, exp
         Use GROUP BY in each CTE plus a FULL OUTER JOIN on the grain columns only when a
         per-block / per-village / per-year breakdown is asked for.
  2. Both MGNREGA facts are at source-row grain — always SUM ... GROUP BY, never read a row raw.
  3. job_cards_issued_total is a cumulative STOCK. Report ONE financial year, never a sum
     across years. It is still at source-row grain, so the figure for an area is
     SUM(job_cards_issued_total) over that area for a single year_key. For a statewide
     "how many job cards" with no year given, use the latest year:
       SELECT SUM(job_cards_issued_total) AS job_card_stock
       FROM curated.v_employment
       WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment);
     A bare `SELECT job_cards_issued_total ... LIMIT 1` (no SUM, no GROUP BY) is ALWAYS
     wrong — it returns one village's number as if it were the whole total.
  4. Money unit: MGNREGA expenditure is LAKH RUPEES — report it in LAKH and state the unit; do
     NOT divide by 100 to convert to CRORE for a plain MGNREGA-only question. That /100
     conversion belongs only to the cross-scheme normalisation views (comparing against PMAY's
     rupee figures) — applied here it rounds a real small-village figure like 0.49 lakh to
     "0.00 crore" (2026-09-10 UAT: MGNREGA expenditure in a small village was reported as 0
     for exactly this reason), which reads as no expenditure at all. Never mix with PMAY's rupees.
  5. MGNREGA "dues" / "pending liabilities" / "unpaid amount" have NO column in curated —
     do not compute or estimate one from any other column. Answer that this is not available
     in the current source.
  5a. There is likewise NO "pending" / "unpaid" / "awaiting" count for beneficiaries,
      households, persons or job cards — the employment fact records what WAS provided,
      not a backlog or a waitlist. If the question asks for "pending beneficiaries",
      "pending applications", "paid vs pending", etc., return only the metric that exists
      (e.g. households_employed) and state that a pending/backlog figure is not tracked here.
      Do not emit a fabricated 0 for the pending side.
  6. `women_employment_provided` exists on fact_mgnrega_employment. A percentage share of
     women in employment is: ROUND(100.0 * SUM(women_employment_provided)
     / NULLIF(SUM(persons_employed), 0), 2) — women_employment_provided is a persons count,
     over the same persons_employed denominator used everywhere else in this schema.
  7. cost_per_person_day_rupees: use the precomputed column on v_district_year_summary, or,
     if computing it inline, SUM(total_exp) * 100000 / NULLIF(SUM(person_days), 0) — total_exp
     is LAKH and person_days is a raw count. NEVER divide the crore figure (total_exp / 100)
     by person_days: that is ~0.00004 and rounds to 0 for every row.
  8. Every employment measure is an INTEGER, and integer / integer truncates: person-days per
     household must be SUM(person_days)::numeric / NULLIF(SUM(households_employed), 0)
     (38.16, not 38). Cast the numerator of every ratio or average to numeric.
  9. "Which villages …" returns village NAMES: SELECT lgd_village_name, village_code,
     SUM(<metric>) … GROUP BY lgd_village_name, village_code ORDER BY 3 DESC. For "which
     villages received employment" add HAVING SUM(persons_employed) > 0 — villages with a
     zero row did not receive employment.
 10. A comparison ("more on wages or materials?", "X vs Y") returns BOTH figures as numeric
     columns, never only a CASE label: wages = SUM(unskilled_wage_exp + semi_skilled_wage_exp).
""".strip()

_MGNREGA_VOCAB = """
MGNREGA BUSINESS VOCABULARY (source: data/ KPI Use Cases workbook, Glossary sheet)
    "spending" / "expenditure" -> total_exp                "wages" -> unskilled_wage_exp + semi_skilled_wage_exp
    "material cost" -> material_exp                         "people employed" / "beneficiaries" -> households_employed or persons_employed (ask which if ambiguous)
    "work days" / "man-days" -> person_days                 "job cards" -> job_cards_issued_total (STOCK, rule 3)
    "100-day completion" -> households_completed_100_days   "wage compliance" / "60:40 ratio" -> unskilled_wage_exp / total_exp (statutory formula, NOT (unskilled+semi)/total)
""".strip()

_PMAY_TABLES = """
PMAY-G TABLES
  curated.dim_pmay_house_status(status_key, status_name, is_completed, is_in_progress)
      status_name is exactly one of (stage order low->high): 'Proposed Site',
      'Existing site(Old House)', 'House Sanctioned', 'Plinth', 'Roof Cast', 'Completed'
      (12 rows are NULL). For "completed" / "in progress" use the booleans (rule 2);
      for the other four stages filter status_name on the exact string above.
  curated.fact_pmay_house(scheme_key, year_key, geography_key, village_code, status_key,
      sanction_date, sanctioned_amount, amount_released, amount_pending, is_placeholder,
      completed_underfunded)  -- RUPEES
  curated.v_pmay  -- PMAY fact pre-joined to year/geography/status; one row = one house.
      Every row is PMAY-G, so it has NO scheme_key / scheme_code column and needs NO
      scheme filter and NO join to curated.dim_scheme — query it directly.
      Money columns: sanctioned_amount, amount_released, amount_pending (RUPEES). There is
      no single "total expenditure" column like MGNREGA's total_exp — "expenditure" /
      "spending" / "spent" for PMAY-G means amount_released (money actually disbursed).
      Carries lgd_district / lgd_block / lgd_village_name and financial_year /
      financial_year_short directly (no join needed) — this is THE object for any
      statewide, district- or block-level PMAY-G number.
  curated.v_pmay_monthly_sanctions  -- statewide monthly totals ONLY. Exactly four columns:
      sanction_month, houses_sanctioned, sanctioned_cr, released_cr (already CRORE, already
      aggregated). It has NO geography column — selecting lgd_district / lgd_block from it
      errors with `column "lgd_district" does not exist`. Never use it for a per-district,
      per-block or "top N districts" question.
""".strip()

_PMAY_RULES = """
PMAY-G RULES (breaking these produces a wrong number, not just an ugly query):
  1. curated.v_pmay: always filter `WHERE NOT is_placeholder` unless the question is explicitly
     about data completeness.
  2. "Completed" / "in progress" questions use status_key's booleans is_completed /
     is_in_progress, never `status_name = 'Completed'` as a string match.
  3. Exclude `sanctioned_amount = 0` rows (126 known rows) from any utilisation-rate or
     per-house-average calculation — they are a documented data gap, not real zeros.
  4. Money unit: PMAY amounts are RUPEES; v_pmay_monthly_sanctions is already CRORE. State
     the unit. Never mix with MGNREGA's lakhs.
  5. Financial-year filter: use `year_key` (smallint — 2023 means FY 2023-24; data spans
     year_key 2017 through 2023, plus 126 rows with year_key NULL). Do NOT filter on
     `financial_year` (a fixed-width CHAR stored as '2023-2024', NOT '2023-24') or on
     `financial_year_short` ('2023-24') unless you use that column's exact string form —
     `financial_year = '2023-24'` matches zero rows and returns a false 0.
  6. Per-district / per-block / "top N districts" / "share across districts" questions:
     SELECT from curated.v_pmay and GROUP BY lgd_district (or lgd_block) — it carries those
     columns directly. Do NOT use curated.v_pmay_monthly_sanctions for these; it has no
     geography column. For "share concentrated in the top N districts", aggregate the metric
     per district in a CTE, then divide the top-N subtotal by the grand total.
  7. A "financial summary", "performance" or "compare A and B" question with no year-wise /
     trend wording returns ONE row per area named (one row for one area) — never GROUP BY
     financial_year / year_key / sanction_date. Return sanctioned, released and
     remaining (SUM(sanctioned_amount) - SUM(COALESCE(amount_released, 0))) in RUPEES,
     unrounded; the answer states each (2026-09-28 QA: a per-year GROUP BY reported a sum of
     rounded yearly crore as the total and dropped the released and remaining amounts).
  8. Utilisation / "% of the sanctioned amount released" is ONE ratio of totals:
     100.0 * SUM(COALESCE(amount_released, 0)) / NULLIF(SUM(sanctioned_amount), 0). Never AVG()
     of amount_released or of per-house ratios. amount_released is NULL on 336 houses that
     have received nothing — COALESCE it to 0 in every count or sum of releases.
  9. Comparing named areas ("which has more: A or B", "compare A and B") returns a row for
     EACH area — never LIMIT 1 — and the answer names both figures and the difference.
""".strip()

_PMAY_VOCAB = """
PMAY-G BUSINESS VOCABULARY (source: data/ KPI Use Cases workbook, Glossary sheet)
    "sanctioned" -> sanctioned_amount                        "released" / "paid out" / "expenditure" / "spending" / "spent" / "outlay" / "disbursed" -> amount_released
    "pending" / "utilisation gap" -> sanctioned_amount - amount_released, or amount_pending directly
    "utilisation rate" -> amount_released / sanctioned_amount * 100
    "completion rate" -> COUNT(*) FILTER (WHERE is_completed) / COUNT(*) * 100
    "tranche" / "installment" -> installments_paid           "construction stage" -> status_name / is_completed / is_in_progress
    "sanction order" -> sanction_no                          "LGD code" -> village_code (district/block have no code column in curated, name only)
""".strip()

_FOCUSPLUS_TABLES = """
FOCUS PLUS TABLES
  curated.v_focus_plus  -- THE query surface, and for this scheme that is a PRIVACY
      boundary, not a convenience. One row = one disbursement (one payment, to one
      member, in one tranche) — the finest grain in megh_db. 25 columns:
        focus_plus_fact_id, source_row_id, source_sl_no, year_key, financial_year,
        financial_year_short, geography_key, village_code, lgd_village_name, lgd_block,
        block_name_raw, lgd_district, on_roster, has_geo_conflict, batch_label,
        tranche_label, amount_disbursed, gender, occupation, focus_status,
        verification_status, member_id, pincode, entity_type, bank_name_raw,
        beneficiary_key
      BLOCK COLUMN — for Focus Plus, blocks are mapped on block_name_raw. USE IT.
        block_name_raw  = THE block column for this scheme. Populated on all
                          385,671 rows. Filter and GROUP BY this for every
                          block-level figure (disbursement totals, beneficiary
                          counts, per-block breakdowns, "which block ...").
                          Stored Title Case ("Songsak", "Mylliem") — NOT
                          uppercase, so compare case-insensitively:
                          UPPER(block_name_raw) = 'SONGSAK'.
        lgd_block       = the LGD-registry-mapped name, and it is INCOMPLETE:
                          NULL on 57,649 rows (15%), hiding ₹18.01 crore of
                          disbursement. Mylliem loses 57% of its money this way,
                          Rongram 50%, Lawsohtun 66%. A block figure taken from
                          lgd_block silently under-reports and looks perfectly
                          plausible. Do NOT use it for a block filter or a block
                          GROUP BY, and do NOT AND it with block_name_raw — that
                          re-introduces the same 15% loss.
      lgd_district and lgd_village_name do NOT have this problem (4 and 0 nulls
      respectively) — this is specific to the block column.
      beneficiary_key is a GENERATED column (batch_label || ':' || source_sl_no) added
      2026-09-13 specifically to count beneficiaries correctly across both cohorts in one
      identifier space — see FOCUS PLUS RULES rule 6 below before writing any
      "how many beneficiaries" query.
      dim_year and dim_geography are PRE-JOINED — lgd_district / lgd_block /
      lgd_village_name and financial_year / financial_year_short are on the row, so no
      join is needed for any statewide, district-, block- or village-level number.
      Prefer this view over curated.fact_focus_plus_disbursement directly — it carries
      the same bank_name_raw plus the geography/time joins already done.
  curated.dim_scheme  -- read ON ITS OWN (never joined per row) ONLY to look up
      money_unit for the Focus Plus row. amount_disbursed's unit is UNVERIFIED and
      v_focus_plus does not expose scheme_key.
""".strip()

_FOCUSPLUS_RULES = """
FOCUS PLUS RULES (breaking these produces a wrong number, not just an ugly query):
  1. Query curated.v_focus_plus, NEVER curated.fact_focus_plus_disbursement directly —
     the view pre-joins financial_year and district/block/village names and adds the
     has_geo_conflict flag; the fact table alone lacks those.
  2. THERE IS NO MANDATORY PREDICATE. Do NOT copy PMAY's `WHERE NOT is_placeholder`.
     is_placeholder / is_completed / is_in_progress / mapping_category / sanction_date /
     installments_paid DO NOT EXIST on this partition — a query that references one errors.
  3. One row is one PAYMENT. `COUNT(*)` is a payment / disbursement count — never a
     person count and never a household count. Alias it so the answer says so
     (e.g. AS payments). A legacy member appears on four rows, one per tranche.
  4. batch_label splits the data into two structurally different cohorts and almost
     every question needs to know which:
       '93K'   — legacy PAID cohort. 4 rows per beneficiary (one per tranche). Every
                 person-level column (gender, occupation, focus_status,
                 verification_status, member_id, pincode) is BLANK on all of them.
       '12.5K' — newer REGISTRATION cohort. 1 row per person, Tranch 4 only, person
                 columns populated. ~3.2% of all rows.
     ANY breakdown by gender / occupation / focus_status / verification_status /
     member_id / pincode describes the 12.5K cohort ONLY — add
     `WHERE batch_label = '12.5K'` and state the cohort and its coverage in the answer.
     Never present such a split as a scheme-wide figure. Conversely, do NOT add
     `WHERE batch_label = '12.5K'` to a plain payments/beneficiary COUNT, a money
     total, or a geography (district/block/village) breakdown that does not itself
     group or filter by one of those person-level columns — those are read over the
     WHOLE table, both cohorts together (see "How many Focus Plus beneficiaries are
     there in each district?" in the few-shot set). Confirmed live 2026-09-12: adding
     this filter to a bare per-district beneficiary count silently dropped the 373,144
     legacy-cohort rows and got the query rejected outright.
  5. member_id and pincode are PII. NEVER SELECT, GROUP BY or ORDER BY them as a bare
     column. `COUNT(DISTINCT member_id)` is the ONLY permitted use of member_id — and it
     counts the 12.5K cohort only (~12,527), NOT the scheme total. Do not label it
     "total Focus Plus beneficiaries" — use beneficiary_key (rule 6) for that.
  6. "Beneficiaries" (a PERSON count) = `COUNT(DISTINCT beneficiary_key)`.
     beneficiary_key is a generated column (batch_label || ':' || source_sl_no —
     see semantic.column_catalog) that uniquely identifies a person across BOTH
     cohorts as one identifier space. Use it for EVERY beneficiary-count shape —
     bare statewide, one named district, "each district", or a multi-district
     comparison — there is no per-shape exception any more and no batch_label or
     has_geo_conflict filter is needed: beneficiary_key already spans both
     cohorts, and has_geo_conflict rows keep the roster-resolved district/block
     regardless of the flag (rule 9), so excluding them just drops real people.
     Confirmed live 2026-09-13 against megh_db: the old `COUNT(DISTINCT
     source_sl_no) WHERE batch_label = '93K' AND NOT has_geo_conflict` template
     this replaces, scoped to one district, silently dropped 4,670 twelve-point-
     five-K-cohort beneficiaries and 136 has_geo_conflict beneficiaries in West
     Garo Hills alone. Also, the use-case workbook's 93,286 figure — previously
     believed lost when epic_id was dropped at ingest — turns out to be exactly
     `COUNT(DISTINCT beneficiary_key) WHERE batch_label = '93K'`: beneficiary_key
     is the epic_id replacement for that cohort, not merely a rough proxy for it.
     `COUNT(*)` remains a separate, correct reading for "payments"/"disbursements"
     (rule 3) — a legacy member appears on up to four payment rows, so payments
     and beneficiaries are genuinely different numbers; return both, labelled,
     when the question is ambiguous between them. Do NOT use
     `COUNT(DISTINCT member_id)` or a bare `COUNT(DISTINCT source_sl_no)` as a
     beneficiary count — member_id only exists for the 12.5K cohort (rule 5) and
     source_sl_no repeats across the two cohorts and needs a batch_label scope
     to mean anything on its own (rule 7 covers the one place that scoped reading
     is still needed: a per-tranche/per-batch breakdown).
  7. amount_disbursed: UNIT IS UNVERIFIED. Read curated.dim_scheme.money_unit for the
     Focus Plus row before formatting or converting; if it does not resolve, say the
     unit is unconfirmed. Do NOT divide by 1e7 / 1e5 on an assumption and NEVER add a
     Focus Plus amount to a MGNREGA (lakh) or PMAY (rupee) figure. Only two values exist
     — 5,000 (Tranch 1) and 2,500 (later tranches) — so AVG(amount_disbursed) is a mix
     ratio, not an entitlement: a "per beneficiary" figure divides SUM(amount_disbursed)
     by a beneficiary count — `SUM(amount_disbursed) / COUNT(DISTINCT beneficiary_key)`
     (rule 6), a plain ratio with NO leading `100.0 *` multiplier. That multiplier
     belongs ONLY to a percentage-of-state-total ranking (e.g. "rank districts by
     disbursement and show their share"), never to a per-beneficiary average — copying
     it into an average query inflates the figure 100x (confirmed live 2026-09-12:
     "average disbursement per beneficiary in West Garo Hills" came back as 1,250,000
     instead of ~11,167). Do NOT scope this to `batch_label = '93K'` or exclude
     has_geo_conflict rows — beneficiary_key is already a correct cross-cohort
     denominator on its own (rule 6); adding either filter only removes real
     beneficiaries from both the numerator and denominator for no benefit. (Superseded
     2026-09-13: an earlier version of this rule required `COUNT(DISTINCT
     source_sl_no) WHERE batch_label = '93K'` here, because a bare unscoped
     COUNT(DISTINCT source_sl_no) mixed the two cohorts' file-serial numbering into a
     meaningless denominator — 11,316 statewide, confirmed live 2026-09-12. That number
     was an artifact of the missing identity column, not a real answer; beneficiary_key
     now gives 11,316 as the correct, whole-scheme average, and ~11,167 for West Garo
     Hills — both computed the same way with no per-cohort scoping needed.)
     amount_disbursed is NOT NULL (the only money column in megh_db that is) — no
     COALESCE needed.
  8. TIME: financial_year / financial_year_short is the FINEST time grain — THERE IS NO
     DATE COLUMN. Only FY 2022-23 and FY 2025-26 hold data (a three-year gap between).
     No monthly, quarterly, weekly, calendar-year or exact-date analysis is possible —
     say so, do not approximate. tranche_label embeds month words ("August",
     "Feb-March") but is a LABEL: NEVER `ORDER BY tranche_label` as a time series. A
     tranche breakdown is categorical; GROUP BY batch_label as well because Tranch 4
     mixes both cohorts.
  9. GEOGRAPHY: district and block are NAMES only, stored UPPERCASE in dim_geography —
     emit the uppercase literal; there is no district or block code. Count villages with
     COUNT(DISTINCT village_code), never by name. 102,923 source rows carry no village
     code — flag any village- or block-level figure as provisional. dim_geography also
     holds wards (entity_type = 'Ward'); do not silently mix them into a "top villages"
     ranking.
     BLOCKS ARE MAPPED ON block_name_raw FOR THIS SCHEME — use it for every
     block filter and every block GROUP BY, with UPPER(block_name_raw) = '<BLOCK>'
     (it is stored Title Case, so a bare equality against an uppercase literal
     matches zero rows). lgd_block is NULL on 57,649 of 385,671 rows (15%) and
     drops ₹18.01 crore of disbursement — Mylliem loses 57% of its money,
     Rongram 50% — so a block figure read from lgd_block silently under-reports.
     Do not AND the two columns together either; that re-creates the same loss.
     District and village are unaffected — lgd_district / lgd_village_name are
     effectively complete and stay the right columns for those levels.
     has_geo_conflict does NOT mean the stored lgd_district/lgd_block is wrong or
     unresolved — dim_geography.geo_conflict_note shows it flags rows where the Focus+
     source's own block name disagreed with the LGD roster; the roster's (authoritative)
     value is what v_focus_plus.lgd_district/lgd_block actually store either way. So a
     plain `WHERE lgd_district = '...'` already returns the correct, fully-resolved rows
     for that district whether or not they carry the flag — do NOT add `WHERE NOT
     has_geo_conflict` to a district- or block-scoped beneficiary/disbursement count, or
     to any other query, by default. Confirmed live 2026-09-13: adding it to a plain
     per-district beneficiary count silently dropped 136 real beneficiaries in West Garo
     Hills alone who were correctly attributed to that district. Only add the filter if
     the user explicitly asks for "reliably mapped" / "conflict-free" / "unambiguous"
     location data — and even then, say in the answer that some real records were
     excluded, don't silently narrow the population.
 10. NOT HELD — return "not available in this data", never borrow a column from PMAY or
     MGNREGA: producer groups / PG counts, EPIC-id lookups, beneficiary names, mobile
     numbers, account numbers / IFSC / bank-transfer or DBT status, eligible-population
     denominators (so no coverage %, density or per-capita), constituency / MLA,
     budget / target / forecast, any other scheme's data. bank_name_raw IS held and
     queryable (bank name only — no account/IFSC) but is dirty free-text: alongside real
     bank names it has a batch of rows where the raw value is a bare numeric code (e.g.
     '1', '6', '33') instead of a name — do not treat those as a distinct "bank"; if a
     ranking or breakdown surfaces one, flag it as an unresolved/raw code rather than
     presenting it as a bank.
 11. NEVER row-join v_focus_plus / fact_focus_plus_disbursement to a PMAY or MGNREGA
     fact or view — every such pair is prohibited (grain mismatch). For cross-scheme
     money use curated.v_cross_scheme_money_district_year (Focus Plus presence
     UNCONFIRMED — SELECT DISTINCT scheme_code first); for village coverage that view has
     NO Focus Plus column, so aggregate v_focus_plus to village_code in a CTE and FULL
     JOIN the aggregate.
 12. Every Focus Plus total is unverified against megh_db — state that a figure is
     provisional pending meta.v_reconciliation_focus_plus.
 13. UNQUALIFIED "status" / "status-wise" / "status breakdown" / "status distribution"
     — with NO other qualifier — ALWAYS means focus_status alone
     (`GROUP BY focus_status`, scoped `WHERE batch_label = '12.5K'` per rule 4). Do
     NOT also group by verification_status just because "What Focus Plus status
     values are recorded?" (the one-time audit example in the few-shot set) groups
     both columns together — that example is for enumerating every stored value
     before writing a filter, not the template for an ordinary status-breakdown
     question, and verification_status is a near-constant single value ('Approved'
     on every 12.5K row) that adds nothing but noise to a focus_status breakdown.
     Only group by verification_status when the question explicitly says
     "verification status" / "verification breakdown" / "verified" — see the
     FOCUS PLUS BUSINESS VOCABULARY entry below.
""".strip()

_FOCUSPLUS_VOCAB = """
FOCUS PLUS BUSINESS VOCABULARY (source: FOCUS+ NLP Use Cases workbook + focusplus_schema_partitions.yaml)
    "payments" / "disbursements" / "records" -> COUNT(*)              "amount" / "disbursed" / "paid" / "DBT amount" -> SUM(amount_disbursed)  (UNIT UNVERIFIED — read dim_scheme.money_unit)
    "members with an id" -> COUNT(DISTINCT member_id)  (12.5K cohort only, NOT a beneficiary count)   "beneficiaries" (any shape — statewide, one district, "each district", a comparison) -> COUNT(DISTINCT beneficiary_key), no batch_label or has_geo_conflict filter (rule 6)
    "legacy" / "paid cohort" -> batch_label = '93K'                  "newer" / "registration cohort" / "unpaid" -> batch_label = '12.5K'  (an inference from the batch, NOT a status column)
    "tranche" / "installment" / "tranch" -> tranche_label  (categorical, not time)   "batch" / "cohort" -> batch_label
    "farmers" -> occupation = 'Farmer'  (12.5K cohort only)          "women" / "female" -> gender = 'Female'  (12.5K cohort only)
    "pending" -> focus_status = 'Pending'  (12.5K cohort only; an application state, NOT a payment flag)   "distinct villages" -> COUNT(DISTINCT village_code)
    "status" / "status-wise" / "status breakdown" (unqualified, default) -> GROUP BY focus_status ONLY, never also verification_status   "verification status" / "verified" (explicit) -> verification_status
""".strip()

_CMELEVATE_TABLES = """
CM ELEVATE TABLES
  curated.v_cm_elevate  -- THE query surface. One row = one application (request_id)
      under one of 15 CM Elevate schemes. 22 columns:
        cm_elevate_fact_id, request_id, cm_scheme_key, scheme_name, endpoint,
        geography_key, village_code, lgd_village_name, lgd_block, lgd_district,
        on_roster, has_geo_conflict, entity_type, applicant_category, type_raw,
        current_file_status, current_level, application_mode, data_verified,
        onhold, is_withdraw, scheme_specific
      dim_cm_elevate_scheme and dim_geography are PRE-JOINED (both INNER, both keys
      NOT NULL — the view is lossless), so scheme_name/endpoint and
      lgd_district/lgd_block/lgd_village_name/entity_type are on the row directly.
      NO year_key, NO date column of any kind — see CM ELEVATE RULES rule 2.
      NO money column of any kind — see CM ELEVATE RULES rule 1.
  curated.dim_cm_elevate_scheme  -- 15 rows, one per individual scheme. Read this
      directly (never a DISTINCT over the fact) for "how many schemes" / "list the
      schemes" — a scheme with zero applications is invisible in the fact.
  curated.dim_scheme  -- read ON ITS OWN (never joined per row); constant within this
      partition (always the CM_ELEVATE row). Its money_unit describes nothing here.
  curated.fact_cm_elevate_application  -- underlying fact. Reach for it ONLY for a
      scheme_specific containment query (@>) that needs the GIN index
      (idx_cm_elevate_specific_gin) — the same predicate against the view cannot use it.
""".strip()

_CMELEVATE_RULES = """
CM ELEVATE RULES (breaking these produces a wrong number, not just an ugly query):
  1. THERE IS NO MEASURE. No sanctioned amount, subsidy, loan, disbursement, unit cost
     or tranche exists anywhere in this partition. COUNT(*) is the entire aggregate
     vocabulary. NEVER emit SUM/AVG over a v_cm_elevate column, and NEVER answer a
     money question with an application count — refuse and offer the count as an
     explicitly different question.
  2. THERE IS NO TIME. No year_key, no FK to dim_year, no application/sanction date of
     any kind. The only timestamp anywhere near this fact is ingested_at (audit-only,
     not exposed on the view), which describes when the row was LOADED, not the
     application — never substitute it. request_id's 4th segment resembles a date but
     is unpadded/ambiguous (a naive parse resolved only 3,038 of 8,436 source rows and
     produced dates after the export date) — NEVER parse it. Any year/month/FY/trend
     question is UNANSWERABLE; say so plainly.
  3. ON HOLD ROUTING — the single most dangerous trap in this partition: "on hold"
     ALWAYS means `data_verified = 'On Hold'`. The `onhold` boolean column is FALSE on
     every row and returns a confident-looking zero if used instead.
  4. CM Elevate is 15 schemes under ONE scheme_code, separated by cm_scheme_key /
     scheme_name. A programme-level total with no scheme breakdown mixes 15 unrelated
     schemes — offer the breakdown. Six schemes have fewer than 15 applications each;
     always show the count alongside any percentage from them. THIS STILL APPLIES
     when the question itself names two, three or more specific sub-schemes ("how
     many applicants under Piggery, Poultry and Dairy") — GROUP BY scheme_name and
     state each one's count plus the combined total; never collapse a multi-scheme
     IN-list into one bare merged COUNT(*)/COUNT(DISTINCT) with no breakdown just
     because three-or-more were named instead of two.
  5. entity_type = 'Unresolved' is a SYNTHETIC PLACEHOLDER, never a real village. Add
     `WHERE entity_type <> 'Unresolved'` to every VILLAGE count / village list, and to
     nothing else — district/block/scheme totals keep it so they reconcile to source.
  6. Group and filter applicant type on applicant_category (individual / registered /
     unregistered), NEVER on type_raw — the raw label is per-scheme and carries source
     typos preserved verbatim.
  7. LOWER() current_level and current_file_status before grouping/filtering — a
     Title-Case variant of a level (e.g. 'Level1' alongside 'level1') is not currently
     observed but the rule is defensive; 'sendback' and 'sendback to vdv/citizen' are
     two genuinely different stored values for a related idea and must be combined
     with a LIKE, not an exact match.
  8. request_id is NOT unique (about 36 numbers repeat, all inside Piggery) — never
     assume LIMIT 1 is safe on a request_id lookup. This also means a bare COUNT(*)
     overcounts "applicants"/"beneficiaries" by those ~36 duplicate rows: any question
     asking how many APPLICANTS (as opposed to how many APPLICATIONS/requests) must use
     COUNT(DISTINCT request_id), e.g.
       SELECT COUNT(DISTINCT request_id) AS applicants
       FROM curated.v_cm_elevate
       WHERE lgd_district = 'WEST GARO HILLS';
  9. gender lives in scheme_specific ->> 'gender_id' (text despite the _id suffix,
     populated on every row — the one scheme_specific key that needs no scheme scope).
     Every OTHER scheme_specific read MUST carry a cm_scheme_key or scheme_name filter
     — `->>` returns NULL for a missing key, indistinguishable from a genuinely empty
     value.
  9a. NEVER GUESS A scheme_specific KEY NAME. `->>` and `?` are silent on a missing
     key — a wrong guess returns 0 rows / NULL and reads exactly like "no data
     exists" (confirmed live 2026-09-11: a question about Poultry applicants "by
     sector" was answered "no matching records" because the generator wrote
     `scheme_specific ->> 'sector'`, when the real, populated key for Poultry is
     `sector_id` — one keystroke off, and 501 real rows silently became zero). Known
     real keys, verified live against megh_db, all holding TEXT despite the `_id`
     suffix: gender_id (all 15 schemes), sector_id (Poultry, Dairy, Goat only —
     'Poultry'/'Piggery'/etc., mostly NULL), occupation_id (uncoded, no lookup
     table), file_status (all 15 schemes, every row: 'Pending' 8,472 / 'Send Back'
     90 / 'Rejected' 64 / 'Approved' 1 — the application's decision state,
     verified live 2026-09-27). For any OTHER scheme_specific field the question names, either match it
     to a key already confirmed in this prompt, or run
     `SELECT DISTINCT jsonb_object_keys(scheme_specific)` scoped to that one scheme
     first and use exactly what comes back — never a plausible-sounding shortened or
     lengthened form of the word the user typed.
  9b. When the question's own wording explicitly names a scheme_specific dimension
     word ("sector", "gender", ...) ALONGSIDE a scheme it already resolved to (e.g.
     "poultry sector"), the user is asking about THAT SUB-FIELD, not just repeating
     the scheme name — answer with the sub-field, don't silently drop it and return
     the bare scheme total (confirmed live 2026-09-11: returning the plain 501
     scheme-wide count for "applicants associated with poultry sector" was reported
     back as ignoring "sector" entirely). But two traps stack here, and both must be
     handled in ONE query, not chosen between:
       - CASE: sector_id values are stored Title Case ('Poultry', 'Piggery'). Compare
         with `ILIKE` (or `LOWER(...) = LOWER(...)`), never an exact match on a
         lowercase guess — that was the original bug (0 rows on 'poultry').
       - SPARSITY: sector_id is recorded on only a small fraction of rows even for
         the schemes that have it (26 of 501 Poultry rows; 475 are NULL). A filtered
         count alone, with no denominator, reads as either "the whole scheme" or "a
         data error" depending which way it's wrong. Always return THREE numbers
         together so the answer is self-explanatory — the sector-matched count, how
         many rows have sector_id recorded at all, and the scheme's total applicant
         count — e.g.:
           SELECT COUNT(*) FILTER (WHERE scheme_specific ->> 'sector_id' ILIKE 'poultry')
                    AS poultry_sector_applicants,
                  COUNT(*) FILTER (WHERE scheme_specific ->> 'sector_id' IS NOT NULL)
                    AS sector_recorded,
                  COUNT(*) AS scheme_total
           FROM curated.v_cm_elevate
           WHERE scheme_name = 'Meghalaya Poultry Farming Scheme';
         THE "RECORDED" TEST MUST BE `->> 'key' IS NOT NULL`, NEVER `? 'key'`. The `?`
         (has-key) operator checks whether the JSON key is PRESENT, not whether its
         value is non-null — sector_id is present as a key on every one of the 501
         rows (with a JSON null value on 475 of them), so `scheme_specific ?
         'sector_id'` returns TRUE for all 501 and silently makes "sector_recorded"
         equal the scheme total (confirmed live 2026-09-11: this exact mistake was
         shipped once already, in this very rule, before being caught). `->>` returns
         SQL NULL for both a missing key and a JSON-null value, which is exactly the
         "not really recorded" test wanted here.
         State all three: "21 applicants have sector recorded as Poultry (out of 26
         rows where sector is recorded at all; 501 total applicants under the
         scheme)" — never just the 21, and never just the 501. When a MULTI-scheme
         question includes a scheme that never carries sector_id at all (anything
         other than Poultry/Dairy/Goat — e.g. Piggery), say plainly that sector
         isn't tracked for that scheme rather than reporting a bare "0" that reads
         as "checked and found none".
       - NEVER PUT `scheme_specific ->> 'sector_id' IS NOT NULL` (or any sector
         filter) IN THE OUTER WHERE CLAUSE of a multi-scheme/GROUP BY version of
         this query — it silently corrupts "scheme_total" into "sector-recorded
         count" AND drops every scheme with no sector_id at all (e.g. Piggery)
         from the result entirely, both at once (confirmed live 2026-09-12: "Piggery
         and Poultry ... poultry sector in Ri Bhoi" returned Poultry's scheme_total
         as 8 — its actual Ri Bhoi total is 238 — and Piggery vanished from the
         output instead of showing 0/0/1950). The sector test belongs ONLY inside
         the two COUNT(*) FILTER(...) clauses; the outer WHERE filters just
         scheme_name (+ any named geography), so every named scheme — sector-
         tracked or not — gets its own row with its real, unfiltered scheme_total:
           SELECT scheme_name,
                  COUNT(*) FILTER (WHERE scheme_specific ->> 'sector_id' ILIKE 'poultry')
                    AS poultry_sector_applicants,
                  COUNT(*) FILTER (WHERE scheme_specific ->> 'sector_id' IS NOT NULL)
                    AS sector_recorded,
                  COUNT(*) AS scheme_total
           FROM curated.v_cm_elevate
           WHERE scheme_name IN ('Meghalaya Piggery Development Scheme',
                                 'Meghalaya Poultry Farming Scheme')
             AND lgd_district = 'RI BHOI'
           GROUP BY scheme_name;
 10. Applicant names, mobile numbers, PAN and bank account fields were stripped at
     ingest — a name lookup returns "not held in this data", never a guessed join.
 11. NEVER row-join v_cm_elevate to a PMAY, MGNREGA or Focus Plus fact/view — all such
     pairs are prohibited (grain mismatch). CM Elevate is absent from BOTH cross-scheme
     views; the only sanctioned cross-scheme path is aggregating v_cm_elevate to
     village_code and joining that aggregate onto v_cross_scheme_village_coverage.
 12. The requirement workbook this scheme was scoped against describes a DIFFERENT
     dataset (2,847 applications, 13 schemes, four rupee measures, Bank/LIFCOM
     channels, Month/Year columns). Never adopt one of its figures — there are 15
     schemes here, not 13, and PRIME Small Enterprise Empowerment (the LARGEST scheme
     at ~43% of applications) is entirely absent from that workbook's list.
 13. UNQUALIFIED "status" / "status distribution" / "status-wise" / "current status" /
     "application status" / "status breakdown" ALWAYS means current_file_status
     (product decision 2026-09-28 — NOT data_verified). Its stored values: 'forward',
     'sendback', 'sendback to vdv/citizen', 'resubmit' (mixed case). Group them with
     exactly this expression, which merges the two send-back spellings (rule 7):
       CASE WHEN LOWER(current_file_status) LIKE 'sendback%' THEN 'Sent back'
            WHEN current_file_status IS NULL THEN '(not recorded)'
            ELSE INITCAP(LOWER(current_file_status)) END AS status
     A status question is a GROUP BY over ALL values, never a single filtered COUNT.
     data_verified stays the column for VERIFICATION words only: "verified" /
     "verification" / "valid" / "invalid" / "wrong" / "on hold" / "pending" (see
     rules 3 and 14 and the vocabulary) — its values are 'Valid', 'On Hold',
     'Invalid', 'Wrong' or NULL.
 14. "verified" / "verification completed" / "completed data verification" ->
     data_verified = 'Valid'. "not verified" / "verification pending" / "incomplete
     verification" -> data_verified IS DISTINCT FROM 'Valid' (covers On Hold, Invalid,
     Wrong and NULL together as one "not verified" bucket). NEVER filter for or invent
     a 'Completed' literal — it does not exist (confirmed live: this exact mistake
     silently zeroed out a real 2,605-row district's answer). A verification-count
     question must return the verified count AND the not-verified count together (and
     the verified % of total when useful) in one query — never just one side, per the
     same self-explanatory-numbers reasoning as rule 9b.
 15. "withdrawn" / "withdrawal" -> the is_withdraw boolean column
     (COUNT(*) FILTER (WHERE is_withdraw)). It is FALSE on every one of the 8,543 rows
     today — CM Elevate currently has zero recorded withdrawals at any scope. Answer
     with the real (zero) count from a live query — NEVER refuse this as "not
     tracked"/"not available"; the column exists and is queryable, it is simply
     uniformly false right now.
 16. A comparison across MULTIPLE schemes AND MULTIPLE geographies at once ("compare
     Piggery and Poultry across Ri Bhoi and East Khasi Hills") is a single GROUP BY
     scheme_name, lgd_district query (optionally pivoted), never two separate
     unrelated queries and never a refusal:
       SELECT scheme_name, lgd_district, COUNT(*) AS applications
       FROM curated.v_cm_elevate
       WHERE scheme_name IN ('Meghalaya Piggery Development Scheme','Meghalaya Poultry Farming Scheme')
         AND lgd_district IN ('RI BHOI','EAST KHASI HILLS')
       GROUP BY scheme_name, lgd_district
       ORDER BY scheme_name, lgd_district;
""".strip()

_CMELEVATE_VOCAB = """
CM ELEVATE BUSINESS VOCABULARY (source: cmelevate_schema_partitions.yaml + cmelevate_entity_resolver.yaml)
    "applications" / "records" / "requests" -> COUNT(*)   (an application is a REQUEST, not an award — say "applications")
    "applicants" / "beneficiaries" / "distinct applications" / "unique applications" -> COUNT(DISTINCT request_id)   (request_id is NOT unique — about 36 numbers repeat, all inside Piggery — so a bare COUNT(*) overcounts "applicants"/"beneficiaries" by those duplicates; only "applications"/"requests" itself means the raw row count)
    "on hold" / "pending" / "held" -> data_verified = 'On Hold'   (NEVER the onhold boolean)
    ONLY when the question itself names a level ("pending at level 2", "pending in level1"): -> LOWER(current_level) = 'levelN' alone — the level IS the scope; do NOT also add data_verified = 'On Hold' (no level-2 application is on hold, so that AND returns a false 0). A plain "pending" with NO level named is still data_verified = 'On Hold' — never a current_level filter
    "pending in each sector" / "<status> by sector" / "<status> by <dimension>" -> keep the status test INSIDE COUNT(*) FILTER (WHERE data_verified = '...') and GROUP BY the dimension with NO status test in the WHERE, so a group with none still shows 0
    "approved" / "rejected" / "decision" -> scheme_specific ->> 'file_status' = 'Approved' / 'Rejected' (exact Title Case) — current_file_status NEVER holds approved/rejected/pending (only forward / sendback / resubmit), so filtering it for those returns a false 0   "sanctioned" / "cleared" / "funded" -> NOT AVAILABLE here (sanction lives in CM Elevate Legacy)   "status" / "status distribution" / "status-wise" / "application status" (default) -> current_file_status, send-back spellings merged (rule 13; decided 2026-09-28)   "verification status" -> data_verified
    "villages" -> COUNT(DISTINCT village_code) FILTER (WHERE entity_type <> 'Unresolved')   "districts" / "blocks" -> COUNT(DISTINCT lgd_district) / COUNT(DISTINCT lgd_block)
    "amount" / "money" / "disbursed" / "subsidy" / "loan" / "sanctioned amount" -> NOT AVAILABLE, no money column exists   "gender" -> scheme_specific ->> 'gender_id'
    "type" (default) -> applicant_category   "mode" -> application_mode (online / cmconnectcenter)   "schemes" -> COUNT(*) FROM curated.dim_cm_elevate_scheme  (= 15)
    "sector" -> scheme_specific ->> 'sector_id'   (Poultry/Dairy/Goat only — NEVER 'sector', that key does not exist and silently returns zero rows)
    "verified" / "verification completed" -> data_verified = 'Valid'   "not verified" / "verification pending" -> data_verified IS DISTINCT FROM 'Valid'   (NEVER a 'Completed' literal — does not exist)
    "withdrawn" / "withdrawal" -> COUNT(*) FILTER (WHERE is_withdraw)   (boolean, FALSE on every row today — answer is 0, NEVER a refusal)
""".strip()

_FOCUSLEGACY_TABLES = """
FOCUS LEGACY TABLES
  curated.v_focus_legacy  -- THE query surface, and for this scheme that is a PRIVACY
      boundary, not a convenience: the underlying fact carries an UNMASKED account
      number and the account holder's name; the view masks the first and drops the
      second. One row = one disbursement (one payment, to one PRODUCER GROUP).
      This is the ONLY group-grained fact in megh_db — there is no individual
      anywhere in the partition. 28 columns:
        focus_legacy_fact_id, source_row_id, pg_id, pg_name, pg_entity_type,
        pg_entity_type_name, programme_variant, year_key, financial_year,
        financial_year_short, year_note, date_of_remittance, geography_key,
        village_code, lgd_village_name, lgd_block, lgd_district, entity_type,
        on_roster, has_geo_conflict, no_of_pg_members, amount_disbursed,
        product_raw, bank_name, ifsc_code, branch_raw, account_no_masked,
        bank_details_current
      dim_producer_group, dim_pg_entity_type, dim_geography, dim_year and
      bridge_pg_bank_history are ALL PRE-JOINED — pg_id / pg_name, lgd_district /
      lgd_block / lgd_village_name and financial_year / financial_year_short are on
      the row, so no join is needed for any statewide, district-, block- or
      village-level number.
      BLOCK/DISTRICT COLUMNS — lgd_district and lgd_block are stored UPPERCASE
      ('WEST GARO HILLS', 'MAIRANG'). A Title Case literal returns ZERO ROWS
      SILENTLY. lgd_village_name is NOT uppercased — do not fold all three alike.
  curated.dim_producer_group  -- 11,904 rows, one per producer group. Reach for it
      ONLY for first_payment_year_key / last_payment_year_key, which the view does
      NOT expose ("groups new this year", "groups paid in more than one year").
      Query it ALONE — joining it back to the view repeats those columns once per
      payment and double-counts every group paid more than once.
  curated.dim_pg_entity_type  -- 3 rows (type_code, type_name, source_id_prefix).
      Read it to ENUMERATE the three group types; the stored type_code strings are
      NOT documented, so never filter on a guessed 'FOCUS' / 'LAMP' / 'EXISTING'.
  curated.dim_scheme  -- read ON ITS OWN (never joined per row) ONLY to look up
      money_unit for the Focus Legacy row. amount_disbursed's unit is UNVERIFIED and
      v_focus_legacy does not expose scheme_key.
  NEVER QUERY, NEVER JOIN: curated.fact_focus_legacy_disbursement and
      curated.bridge_pg_bank_history. Both hold the unmasked account_no; the fact
      also holds name_on_the_account. Reaching either through a join is the same
      privacy breach as querying it directly, and there is no column on either
      worth it — everything legitimate is on the view.
""".strip()

_FOCUSLEGACY_RULES = """
FOCUS LEGACY RULES (breaking these produces a wrong number, not just an ugly query):
  1. Query curated.v_focus_legacy, NEVER curated.fact_focus_legacy_disbursement and
     NEVER curated.bridge_pg_bank_history — privacy boundary, see above.
  2. THERE IS NO MANDATORY PREDICATE. Do NOT copy PMAY's `WHERE NOT is_placeholder`.
     is_placeholder / is_completed / mapping_category / mapping_confidence_pct /
     sanction_date / installments_paid DO NOT EXIST here — a query referencing one
     ERRORS, it does not merely mislead.
  3. THE ENTITLEMENT IDENTITY — the single most important fact about this scheme:
         amount_disbursed = no_of_pg_members * 5000
     on every one of the 14,569 source rows, zero exceptions, confirmed twice. So:
       - SUM(amount_disbursed) is exactly 5000 * SUM(no_of_pg_members).
       - AVG(amount_disbursed) is NOT an average entitlement — it is 5000 x average
         GROUP SIZE, and it moves when group sizes move, never when policy does.
         Never describe it as an entitlement, a benefit level or a policy change.
       - "Amount per member" is the CONSTANT 5,000. State the rate; do not compute
         a ratio that can only return 5,000.
       - A ranking by amount and a ranking by members are the SAME ranking. Never
         present them as two independent findings.
     Therefore EVERY money SELECT must carry SUM(no_of_pg_members) beside it, so the
     relationship is visible in the result rather than buried in prose.
  4. THREE DIFFERENT COUNTING SUBJECTS, never interchangeable:
       COUNT(*)                  = PAYMENTS (~14,566). One row is one payment.
       COUNT(DISTINCT pg_id)     = PRODUCER GROUPS (~11,904).
       SUM(no_of_pg_members)     = MEMBERSHIPS (~102,021) — NOT people. A group paid
                                   in two years contributes twice, and its recorded
                                   size can differ between the two payments.
     2,655 groups were paid more than once, so COUNT(*) exceeds COUNT(DISTINCT pg_id)
     by about 2,663. Alias every count so the answer says which one it is. There is NO
     person-level count and none can be constructed — no name, EPIC or member id exists.
  5. GROUPS ARE COUNTED ON pg_id, NEVER ON pg_name. 11,906 groups carry only 10,678
     distinct names and 1,634 groups appear under more than one spelling, so a name
     both SPLITS one group and MERGES several. NEVER COUNT(DISTINCT pg_name) and
     NEVER GROUP BY pg_name. To show a name for a group, GROUP BY pg_id and take
     MAX(pg_name) for display.
 5a. FILTERING BY A GROUP NAME THE USER TYPED — never `pg_name = '...'`. An exact
     match on this column is almost always zero rows, because the stored name
     carries a group-type suffix the user does not reproduce. Of the 9,452 distinct
     names, 3,053 end in "Pg" ("Sakania Pg", "Iamyntoilang Pg") and 2,867 contain
     "Producer Group" ("Sunflower Producer Group"); others use "P.g." or a trailing
     number ("Nongtymmai Pg-5"). Measured:
         pg_name =     'Sakania'                  -> 0 rows
         pg_name =     'Sakania Producer Group'   -> 0 rows
         pg_name ILIKE '%Sakania Producer Group%' -> 0 rows   (wrapping is NOT enough)
         pg_name ILIKE '%Sakania%'                -> 1 row    (PG-FOCUS-EKH-2245)
     So: STRIP the group-type words from what the user typed — "Producer Group",
     "Producer Grp", "PG", "P.G.", "Group" — and ILIKE the CORE name only:
         WHERE pg_name ILIKE '%sakania%'
     "members in Sakania Producer Group" and "members in Sakania PG" must both
     become '%sakania%'. A zero-row result after this is a real "no such group",
     not a spelling mismatch — and it is NOT the same as a NULL measure: say the
     group was not found rather than reporting the count as null/unavailable.
     One core name can match SEVERAL pg_id values ("Muskan" matches 4, spelled
     both "Muskan Pg" and "Muskan Producer Group"). When the question is about one
     group, resolve to the pg_id(s) first and say which group(s) were matched.
 5b. "BY <DIMENSION>" MEANS GROUP BY THAT DIMENSION, AND SELECT IT. A question
     asking for a figure "by district" / "district-wise" / "for each district"
     (or by block, village, financial year, product, bank, group type) must put
     that column in BOTH the SELECT list and the GROUP BY. A bare
     `SELECT SUM(amount_disbursed) ... WHERE ...` answers a DIFFERENT question —
     the statewide total — and when it is run per-dimension it returns rows with
     no labels at all, which is worse than a wrong number because the user
     cannot even see what each row refers to (reported 2026-09-23: "amount
     disbursed by district for FY 2021-22" came back as ten unlabelled
     amount/membership pairs whose values were, in fact, the correct
     per-district totals).
     A filter is NOT a breakdown: "for FY 2021-22" restricts rows (WHERE), while
     "by district" splits them (GROUP BY). A question can carry both, and then
     it needs both clauses — the WHERE for the year and the GROUP BY for the
     district. Order the result by the measure descending unless asked otherwise.
  6. NEVER PARSE THE pg_id. It looks like PG-FOCUS-WGH-7089 and the three-letter token
     is the district AT ENROLMENT — stale on 1,075 of 14,569 rows because of district
     bifurcation (990 'WKH' rows are now EASTERN WEST KHASI HILLS, 85 'WGH' rows are
     now SOUTH WEST GARO HILLS). Geography comes from lgd_district, ALWAYS. Also: the
     trailing number is unique only within a prefix, so PG-FOCUS-WKH-5956 and
     PG-LAMP-WGH-5956 are different groups — never match on the number alone.
  7. FY 2023-24 IS ABSENT FROM THE DATA, NOT ZERO IN IT. The scheme's years are
     2021-22, 2022-23, 2024-25 and 2025-26. dim_year holds 2023-24, so a GROUP BY
     simply returns NO ROW for it. Never emit a zero column for it in a year grid,
     never draw a line through the gap, and when asked for "the previous year" before
     FY2024-25 use FY2022-23 — the preceding year that HOLDS payments.
  8. DATES ARE TREASURY BATCHES, NOT A FLOW. date_of_remittance spans 2021-08-31 to
     2026-03-31 and takes only 30 DISTINCT VALUES; three dates carry 9,193 of 14,569
     rows. A monthly or daily GROUP BY is valid SQL and misleading prose — label it a
     batch view. 171 rows have a NULL date (170 of them FY2025-26) and vanish from any
     date-grouped answer while surviving an FY grouping, so the two return different
     totals; prefer financial_year_short when completeness matters.
  9. product_raw IS RAW AND UNNORMALISED — 62 spellings collapse to 45 products
     (Piggery / PIGGERY / piggery). ALWAYS `GROUP BY UPPER(TRIM(product_raw))`; a bare
     GROUP BY product_raw splits one product into three rows. AND it is populated on
     only 3,112 of 14,569 rows (21%): 100% in FY2021-22 and FY2022-23, then 0 of 2,653
     in FY2024-25 and 8 of 8,812 in FY2025-26. EVERY product answer covers FY2021-22
     and FY2022-23 only and must say so. A product question about FY2024-25/FY2025-26
     is unanswerable because the field stopped being CAPTURED — not because the groups
     had no products.
 10. `entity_type <> 'Unresolved'` ON VILLAGE COUNTS AND VILLAGE LISTS ONLY. About
     1,101 rows (7.6%) carry a synthetic Unresolved placeholder instead of a real
     village. Those are REAL payments deliberately placed under their district so the
     totals reconcile — applying the exclusion to a money, district, block or
     programme total UNDERSTATES it. Count villages with COUNT(DISTINCT village_code),
     never by name (3,384 codes vs 3,270 names).
 11. NEVER row-join v_focus_legacy to a PMAY, MGNREGA, Focus Plus or CM Elevate
     fact/view — every fact pair in megh_db is prohibited. Focus Legacy x Focus Plus is
     the most tempting and the most wrong: the two schemes share a NAME and nothing
     else. Focus Plus holds no producer-group column at all, so there is no key, and a
     name match between a person and a group is not a match.
 12. BANKING. bank_name is clean and institution-level — bank-wise analysis IS
     legitimate here (unlike Focus Plus). 13 banks; two carry 92% of payments. 276 rows
     have a NULL bank_name but DO have an ifsc_code — render them 'Not recorded' with
     COALESCE, never merge them into another bank. One IFSC (SBIN0RRMEGB) covers 8,328
     rows because it is a SPONSOR-BANK code, not a branch — never present IFSC counts
     as branch counts. bank_details_current is TRUE or NULL and NEVER FALSE (the view's
     LEFT JOIN carries the is_current predicate), so "paid to an old account" is
     `bank_details_current IS NULL AND account_no_masked IS NOT NULL`.
 13. PII. NEVER select account_no or name_on_the_account — neither is on the view and
     both are out of scope. account_no_masked is displayable for ONE named group's
     payment history, never in a bulk export, and is NOT countable: masking is not
     collision-free, so never COUNT(DISTINCT account_no_masked).
 14. NO STATUS DIMENSION EXISTS. Every row is a payment that HAPPENED — there is no
     pending, approved, rejected or in-progress population, and none can be derived
     from a NULL date or a missing product.
 15. MONEY UNIT IS UNVERIFIED. Read curated.dim_scheme.money_unit for the Focus Legacy
     row before printing a currency symbol. Magnitudes (Rs 5,000/member, 950,000 max)
     corroborate rupees; nothing in the extract confirms it.
  Worked shapes:
    -- the three counts together (the default shape for a bare "how many")
    SELECT COUNT(*) AS payments, COUNT(DISTINCT pg_id) AS producer_groups,
           SUM(no_of_pg_members) AS memberships, SUM(amount_disbursed) AS amount_disbursed
    FROM curated.v_focus_legacy;
    -- a BREAKDOWN by district, scoped to one FY: the year is a WHERE, the
    -- district is a GROUP BY, and lgd_district is in the SELECT so the rows are
    -- labelled
    SELECT lgd_district,
           SUM(amount_disbursed) AS amount_disbursed,
           SUM(no_of_pg_members) AS memberships
    FROM curated.v_focus_legacy
    WHERE financial_year_short = '2021-22'
    GROUP BY lgd_district
    ORDER BY amount_disbursed DESC;
    -- district money (membership travels with it; district literal UPPERCASE)
    SELECT SUM(amount_disbursed) AS amount_disbursed, SUM(no_of_pg_members) AS memberships
    FROM curated.v_focus_legacy WHERE lgd_district = 'WEST GARO HILLS';
    -- find a group the user named (core name only, suffix stripped)
    SELECT pg_id, MAX(pg_name) AS pg_name, MAX(lgd_district) AS lgd_district,
           SUM(no_of_pg_members) AS memberships
    FROM curated.v_focus_legacy
    WHERE pg_name ILIKE '%sakania%'          -- NOT pg_name = 'Sakania Producer Group'
    GROUP BY pg_id;
    -- groups ranked, name shown safely
    SELECT pg_id, MAX(pg_name) AS pg_name, SUM(amount_disbursed) AS amount_disbursed,
           SUM(no_of_pg_members) AS memberships
    FROM curated.v_focus_legacy GROUP BY pg_id ORDER BY amount_disbursed DESC LIMIT 10;
    -- villages (the ONLY place the Unresolved filter belongs)
    SELECT COUNT(DISTINCT village_code) AS villages FROM curated.v_focus_legacy
    WHERE lgd_district = 'RI BHOI' AND entity_type <> 'Unresolved';
    -- products (fold case; state the two-year coverage)
    SELECT UPPER(TRIM(product_raw)) AS product, COUNT(*) AS payments
    FROM curated.v_focus_legacy WHERE product_raw IS NOT NULL
    GROUP BY UPPER(TRIM(product_raw)) ORDER BY payments DESC;
""".strip()

_FOCUSLEGACY_VOCAB = """
FOCUS LEGACY BUSINESS VOCABULARY (source: focuslegacy_schema_partitions.yaml + focuslegacy_entity_resolver.yaml)
  "payments" / "disbursements" / "records" / "transactions" -> COUNT(*)
  "producer groups" / "groups" / "PGs" / "how many groups"  -> COUNT(DISTINCT pg_id)   (NEVER pg_name)
  "members" / "membership" / "PG members"                   -> SUM(no_of_pg_members)   (memberships, NEVER "people")
  "beneficiaries"  -> AMBIGUOUS: payments vs groups vs memberships. There is no person-level
                     reading at all. Show all three rather than silently picking one.
  "amount" / "disbursed" / "remitted" / "money" / "funds"   -> SUM(amount_disbursed)   (= memberships x 5000)
  "rate" / "per member"                                     -> the CONSTANT 5000; state it, never compute it
  "villages"                                                -> COUNT(DISTINCT village_code) + entity_type <> 'Unresolved'
  "banks"                                                   -> COALESCE(bank_name, 'Not recorded')
  "branch"                                                  -> prefer ifsc_code (on every row); branch_raw is 22% populated
  "product" / "crop" / "commodity" / "activity"             -> UPPER(TRIM(product_raw)), FY2021-22 + FY2022-23 only
  "group type" / "PG type" / "entity type"                  -> pg_entity_type (+ pg_entity_type_name for display); ENUMERATE, never guess the code
  "programme" / "variant" / "additional" / "top-up"         -> programme_variant ('FOCUS' vs 'Focus (Addnl)'; the latter is ONE day in FY2025-26, not an era)
  "LAMP" / "FOCUS" / "EXISTING"                             -> pg_id prefixes behind pg_entity_type; the LAMP reading is an UNCONFIRMED hypothesis, do not harden it
  Financial years held: 2021-22, 2022-23, 2024-25, 2025-26 (FY2023-24 has NO rows — a gap, not a zero).
""".strip()

_CMELEVATELEGACY_TABLES = """
CM ELEVATE LEGACY TABLES (source: data/cm_elevate_legacy/*.yaml)
  curated.v_cm_elevate_disbursement  -- THE query surface, and ZERO JOINS for any ordinary
      question. One row = one applicant's sanction-and-disbursement record under one of
      13 schemes (2,823 rows - the full source; row 2392 was un-quarantined 2026-09-25). 38 columns:
        cm_elevate_disb_fact_id, source_row_id, application_number, scheme_name,
        year_key, financial_year, financial_year_short, year_note, geography_key,
        village_code, lgd_village_name, lgd_block, lgd_district, entity_type, on_roster,
        has_geo_conflict, sanctioned_amount, bank_sanctioned_amount,
        subsidy_disbursement_1, subsidy_disbursement_date_1, subsidy_disbursement_2,
        subsidy_disbursement_date_2, subsidy_disbursement_3, subsidy_disbursement_date_3,
        total_subsidy_disbursement, loan_disbursement_1, loan_disbursement_date_1,
        loan_disbursement_2, loan_disbursement_date_2, loan_disbursement_3,
        loan_disbursement_date_3, total_loan_disbursement, total_disbursement,
        loan_entity, loan_disbursed_status, desanctioned_reason_raw, refused_flag_raw,
        refused_reason_text
      dim_cm_elevate_disb_scheme, dim_geography and dim_year are PRE-JOINED (scheme_name,
      lgd_district / lgd_block / lgd_village_name / entity_type, financial_year /
      financial_year_short are on the row). Joining dim_year yourself with an INNER JOIN
      deletes both Sericulture schemes (they carry no year) — never do it.
  curated.dim_cm_elevate_disb_scheme  -- 13 rows. Already on the view as scheme_name.
      DISTINCT from curated.dim_cm_elevate_scheme (15 rows, the OTHER CM Elevate dataset).
  curated.dim_scheme  -- read ON ITS OWN (never joined per row) only to look up money_unit.
  NEVER QUERY, NEVER JOIN: curated.fact_cm_elevate_disbursement (it carries applicant
      first/middle/last names; the view drops them) and curated.v_cm_elevate /
      curated.fact_cm_elevate_application (a DIFFERENT scheme with no shared key — the
      fact-to-fact join is prohibited).
""".strip()

_CMELEVATELEGACY_RULES = """
CM ELEVATE LEGACY RULES (breaking these produces a wrong number, not just an ugly query):
  1. NOT THE CM ELEVATE APPLICATIONS DATASET. This is a separate scheme: 13 schemes (not 15),
     sanction and disbursement money in RUPEES, and a financial year. It has NO data_verified,
     onhold, is_withdraw, applicant_category, request_id, gender or application status —
     those live in curated.v_cm_elevate. Never reference them here.
  2. GRAIN AND COUNTING. One row = one applicant's sanction-and-disbursement record.
     "applications", "beneficiaries", "cases" and "records" mean COUNT(*) AS records.
     "SANCTIONED applications / cases" means COUNT(sanctioned_amount) AS
     sanctioned_records — 3 records (all Any Business Venture, marked Refused) carry no
     sanctioned amount and are NOT sanctioned, so COUNT(*) overstates it. Whenever a
     question asks for sanctioned cases, output sanctioned_records as its own column
     (alongside records when both are asked, e.g. a district summary). "Not sanctioned"
     = COUNT(*) FILTER (WHERE sanctioned_amount IS NULL); report desanctioned records
     (desanctioned_reason_raw Refused / Duplicate) beside it as withdrawn sanctions,
     never as the "not sanctioned" figure. There is NO mandatory row filter (no
     is_placeholder): do not invent one.
  3. MONEY IS RUPEES (unverified — dim_scheme.money_unit is authoritative). Totals in crore:
     ROUND(SUM(x) / 1e7, 2) AS <name>_cr. Per-record averages stay in rupees
     (ROUND(AVG(x)) AS avg_<name>_rupees). Never confuse with MGNREGA's lakh.
  4. STORED TOTALS ONLY. total_disbursement = subsidy + loan, total_subsidy_disbursement and
     total_loan_disbursement are verified stored totals — never re-add them by hand and
     never add the tranche columns (subsidy_disbursement_1..3, loan_disbursement_1..3) for a
     money total. A tranche column is right ONLY when the question names that instalment.
  5. "DISBURSED" IS THREE-WAY. A bare "disbursed" / "released" / "paid" returns subsidy_cr,
     loan_cr and total_disbursed_cr side by side, with sanctioned_cr beside them.
     "Subsidy" -> total_subsidy_disbursement; "loan" -> total_loan_disbursement; "total" /
     "subsidy and loan together" -> total_disbursement. SANCTIONED IS NOT DISBURSED.
     Desanctioned records stay IN money totals (the source file's totals include them)
     unless the user asks to exclude them.
  6. RATIOS. Utilisation / disbursed % = ROUND(100.0 * SUM(total_disbursement) /
     NULLIF(SUM(sanctioned_amount), 0), 1), always with numerator and denominator shown.
     "Pending" / "yet to be disbursed" = SUM(sanctioned_amount) - SUM(total_disbursement),
     a DERIVED figure — label it pending_cr and keep negatives as calculated.
  7. FINANCIAL YEAR is the STORED label: financial_year_short = '2024-25' / '2025-26' (only
     two years exist; a resolved year_key also works). 395 records (both Sericulture schemes,
     every row) have NO year: grouping by year MUST use
     COALESCE(financial_year_short, '(no financial year)') AS financial_year with
     GROUP BY financial_year_short ORDER BY financial_year_short NULLS LAST. NEVER derive a
     year from a date (EXTRACT on a date is for date questions only). A date-range question
     filters subsidy_disbursement_date_1 with a half-open range. Two years is a comparison,
     not a trend. There is NO monthly grain.
  8. GEOGRAPHY. lgd_district and lgd_block are UPPERCASE literals ('WEST GARO HILLS',
     'UMLING'); Title Case returns zero rows silently. Municipal Board areas are separate
     block values: 'RESUBELPARA-MUNICIPAL BOARD', 'WILLIAM NAGAR-MUNICIPAL BOARD',
     'TURA MUNICIPAL BOARD-MUNICIPAL BOARD'. Regions are not a column: Garo Hills = EAST,
     NORTH, SOUTH, SOUTH WEST and WEST GARO HILLS; Khasi Hills = EAST KHASI HILLS, EASTERN
     WEST KHASI HILLS, SOUTH WEST KHASI HILLS, WEST KHASI HILLS; Jaintia Hills = EAST and
     WEST JAINTIA HILLS; Ri Bhoi is its own group. "WKH" is WEST KHASI HILLS, not Eastern
     West Khasi Hills.
  9. VILLAGES. Count with COUNT(DISTINCT village_code) and add entity_type <> 'Unresolved'
     to every village count and village list (404 records have no village). Group a village
     list by lgd_village_name AND village_code (17 names are shared by different villages).
     District, block and scheme totals KEEP the Unresolved rows — their district is known.
 10. SCHEME NAMES are exact stored literals — use the RESOLVED ENTITIES value when given.
     The two Sericulture schemes differ by ONE SPACE: 'Meghalaya Sericulture & Weaving
     Scheme (spinning)' (space) and 'Meghalaya Sericulture & Weaving Scheme(weaving)' (no
     space). NEVER derive a scheme from the application_number prefix (MPDSI etc.).
     13 schemes / 12 districts: show all rows, no LIMIT, for "each scheme / each district".
 11. LENDER. loan_entity holds only 'Bank', 'LIFCOM' or NULL: group with
     COALESCE(loan_entity, '(not recorded)'). 'Bank' is a category, not a bank's name — no
     bank or branch name exists. "Took a loan" / "received a loan" = total_loan_disbursement
     > 0 (a recorded lender does not mean a loan was paid). Average loan size averages only
     records with a loan: AVG(...) FILTER (WHERE total_loan_disbursement > 0).
 12. REFUSAL FIELDS DISAGREE. desanctioned_reason_raw ('Refused' / 'Duplicate' / NULL,
     group with COALESCE(..., '(none recorded)')), refused_flag_raw (TRUE or NULL, NEVER
     FALSE: use IS TRUE / IS DISTINCT FROM TRUE, never NOT refused_flag_raw) and
     refused_reason_text (free text on ~7 rows: list it, never GROUP BY it). A bare
     "refused" counts all three side by side. "Duplicate" is a recorded outcome, not
     repeated rows (application_number is unique).
 13. ZERO IS NOT NULL. total_disbursement = 0 means nothing was paid — filter = 0, not IS
     NULL. NULL means missing. AVG ignores NULLs; never COALESCE a measure to 0 before AVG.
 14. FIXED ENTITLEMENT SCHEMES (Piggery Rs 1,25,000 each, Dairy Rs 3,00,000 each, ...):
     an average restates the constant — show the sanctioned_amount distribution instead
     (GROUP BY sanctioned_amount). A statewide average is a Piggery average — break
     averages down by scheme_name.
 15. RANKING. An explicit N -> LIMIT N. A plural "which districts / blocks ... most" with no
     N -> top 5. A singular "which district" -> LIMIT 1 with a name tie-break. Keep the
     record count beside any money ranking (the money leader is often a different scheme).
 16. bank_sanctioned_amount has NO confirmed meaning — never build a metric, share or
     total on it.
 17. SANCTION RATE = ROUND(100.0 * COUNT(sanctioned_amount) / NULLIF(COUNT(*), 0), 2) AS
     sanctioned_pct, with records and sanctioned_records beside it; the answer must say it
     is a share of THIS dataset (applications that never reached sanction are in the
     separate CM Elevate applications dataset). CONSTITUENCY: the view has no column —
     JOIN curated.dim_geography g ON g.geography_key = v.geography_key and use g.ac_name
     (filter UPPER(g.ac_name) = UPPER('<name>')); it is NOT the block of the same name.
 18. NOT HELD — answer that it is not held and offer the nearest real figure; never
     improvise a number: applicant names (identify a record by application_number),
     monthly figures, individual vs group applicants, loan repayment,
     gender / caste / age, bank branch names, targets / budgets, jobs or business outcomes,
     any link to the CM Elevate applications dataset, and any year other than FY2024-25 /
     FY2025-26.
""".strip()

_CMELEVATELEGACY_VOCAB = """
CM ELEVATE LEGACY BUSINESS VOCABULARY (source: cmelevatelegacy_schema_partitions.yaml + cmelevatelegacy_entity_resolver.yaml)
  "applications" / "records" / "beneficiaries" / "cases" / "sanctions" -> COUNT(*)   (every row is a sanction)
  "sanctioned" / "sanction amount" / "entitlement"  -> SUM(sanctioned_amount)   (NOT disbursed)
  "disbursed" / "released" / "paid" / "money"       -> subsidy + loan + total side by side (rule 5)
  "subsidy" / "grant"                               -> total_subsidy_disbursement
  "loan" / "credit"                                 -> total_loan_disbursement (presence: > 0)
  "lender" / "loan entity" / "LIFCOM" / "bank"      -> COALESCE(loan_entity, '(not recorded)')
  "utilisation" / "% paid"                          -> total_disbursement / NULLIF(sanctioned_amount, 0)
  "pending" / "outstanding"                         -> sanctioned - disbursed (derived, labelled)
  "desanctioned" / "withdrawn sanction" / "refused" / "duplicate" -> desanctioned_reason_raw (+ rule 12)
  "instalment" / "tranche" 1/2/3                    -> subsidy_disbursement_N / loan_disbursement_N (only when named)
  "villages"                                        -> COUNT(DISTINCT village_code) + entity_type <> 'Unresolved'
  "Piggery" / "Poultry" / "Dairy" / "Goat" / "Warehouse" / "PTV" / "PARV" / "spinning" / "weaving"
                                                    -> the resolved scheme_name literal
  "this year" / "current year" -> FY 2025-26 (say so);  "last year" -> FY 2024-25
  Financial years held: 2024-25 (2,291 records), 2025-26 (137 records; only Prime Tourism
  Vehicle, Agriculture Warehouse and Prime Agriculture Response Vehicle), none (395, Sericulture).
""".strip()

_CROSS_SCHEME = """
CROSS-SCHEME TABLES (only relevant when the question spans more than one scheme together)
  curated.v_cross_scheme_money_district_year  -- MGNREGA vs PMAY spend, both normalised to CRORE
  curated.v_cross_scheme_village_coverage  -- which villages have MGNREGA / PMAY / both

FOCUS PLUS IN A CROSS-SCHEME QUESTION — read first:
  Focus Plus may not be represented in either cross-scheme view.
  * curated.v_cross_scheme_village_coverage has NO Focus Plus column (its measures are
    pmay_houses and mgnrega_person_days only). To include Focus Plus, aggregate
    curated.v_focus_plus to village_code in a CTE and FULL JOIN that aggregate on
    village_code — NEVER join the fact.
  * curated.v_cross_scheme_money_district_year: run
    `SELECT DISTINCT scheme_code FROM curated.v_cross_scheme_money_district_year` FIRST.
    If Focus Plus is absent, say the money comparison covers MGNREGA and PMAY-G only.
  * Focus Plus amount_disbursed is a THIRD, different kind of number (money disbursed to
    individuals, UNIT UNVERIFIED). Never add it into a combined total with MGNREGA lakh
    or PMAY rupee figures, and never row-join v_focus_plus to a PMAY / MGNREGA object
    (all such fact pairs are prohibited — aggregate each side to a shared grain first).
  * FAMILY C metric (beneficiaries / persons / "how many benefited") including Focus Plus:
    the Focus Plus figure is its own single-scheme UNIQUE-person reading, NOT COUNT(*).
    COUNT(*) FROM curated.v_focus_plus is PAYMENTS — the legacy 93K cohort carries four
    rows per person. Use COUNT(DISTINCT beneficiary_key) (rule 6 — spans both cohorts as
    one identifier space, no batch_label or has_geo_conflict filter needed);
    COUNT(DISTINCT member_id) is the 12.5K registration cohort ONLY and is not a scheme
    total. Put it in its OWN subquery / bare SELECT and UNION ALL (or CROSS JOIN) the
    finished per-scheme figures — never wrap the union in an outer COUNT/SUM.

CM ELEVATE IN A CROSS-SCHEME QUESTION — read first:
  CM Elevate is CONFIRMED ABSENT from both cross-scheme views (verified from the live
  view definitions, not merely unconfirmed like Focus Plus).
  * curated.v_cross_scheme_money_district_year is a two-branch UNION ALL over MGNREGA
    and PMAY only. There is no scheme_code probe to run — CM Elevate cannot appear
    there, and it has no money column to contribute even if it could. A money
    comparison involving CM Elevate is IMPOSSIBLE, not merely awkward — refuse it and
    say the money comparison covers MGNREGA and PMAY-G only.
  * curated.v_cross_scheme_village_coverage has NO CM Elevate column (pmay_houses and
    mgnrega_person_days only). To include CM Elevate, aggregate curated.v_cm_elevate to
    village_code in a CTE (excluding entity_type = 'Unresolved') and LEFT/FULL JOIN
    that aggregate on village_code — NEVER join the fact, and never row-join
    v_cm_elevate to a PMAY/MGNREGA/Focus Plus fact or view.
  * FAMILY C metric (beneficiaries / persons / "how many benefited") including CM
    Elevate: the CM Elevate figure is COUNT(DISTINCT request_id) FROM
    curated.v_cm_elevate (request_id is NOT unique — dedupe it, don't COUNT(*)),
    labelled "applications" — it is a REQUEST count, not a count of people funded
    (there is no disbursement field to prove anyone received anything). Put it in its
    own bare SELECT and UNION ALL the finished per-scheme figures, same as the other
    schemes.

CROSS-SCHEME — GENERAL PROCEDURE. Apply this to EVERY question that spans MGNREGA and
PMAY-G, however it is worded — reworded, compound, negated, "twist and turn". The worked
examples further down are only instances of this procedure; if a question does not match
one closely, follow the procedure, do NOT pattern-match a near-miss example.

  STEP 1 — sort the question into exactly ONE of three families:

    FAMILY A — MONEY: any spend / expenditure / released / sanctioned / disbursed / outlay
      amount, compared, combined, ranked or shared across the two schemes.
        -> Use curated.v_cross_scheme_money_district_year (already CRORE; one row per
           scheme x district x year; PMAY's scheme_code literal is 'PMAY', never 'PMAY-G').
           Filter / GROUP BY / pivot with FILTER (WHERE scheme_code = ...). Carry
           measure_semantics into the answer.
        -> ONLY if the grain asked is finer than district x year (block, village) does this
           view not fit — then build it with FAMILY C's method applied to the money columns
           (MGNREGA total_exp is LAKH -> /100 for crore; PMAY amount_released is RUPEES
           -> /1e7).

    FAMILY B — COVERAGE / OVERLAP: which, or how many, districts | blocks | villages are
      covered by BOTH schemes / by ONLY one / by EITHER; "common to both", "present in
      both", "where both schemes operate", convergence footprint.
        -> Use curated.v_cross_scheme_village_coverage (one row per village; columns
           mgnrega_person_days and pmay_houses, each NULL where that scheme is absent).
           COALESCE(measure, 0) > 0 means "present", = 0 means "absent".
        -> Roll the village grain UP to the grain asked first (for blocks, JOIN
           curated.dim_geography on village_code to get lgd_block), THEN apply the > 0 / = 0
           tests, THEN COUNT. Never COUNT the raw view for a district/block question.

    FAMILY C — ANY OTHER METRIC, one figure per scheme, side by side: beneficiaries /
      households / persons / houses / person-days / job cards / completion / women /
      100-day / installments / "how many benefited", "how many did each scheme reach /
      help / assist / cover / provide for", etc. There is NO cross-scheme view for these,
      and it is NOT unanswerable.
        -> "beneficiaries" / "how many people/households" is a UNIQUE person-or-household
           count. It is NEVER COUNT(*) of payment / disbursement / source rows, and NEVER
           a SUM across financial years — the same household recurs in each scheme's fact
           every year, so a multi-year SUM double-counts. "across all financial years" does
           NOT mean "add every year together"; it means use each scheme's standing
           distinct-beneficiary reading over the whole data window.
        -> Compute EACH scheme's figure with EXACTLY the SQL its own single-scheme question
           would use: same view (curated.v_employment / curated.v_expenditure for MGNREGA,
           curated.v_pmay for PMAY-G, curated.v_focus_plus for Focus Plus), same filters
           (curated.v_pmay ALWAYS `WHERE NOT is_placeholder`; a single latest year_key for
           MGNREGA households_employed / persons_employed / job-card counts, since there is
           no household id to dedupe across years), same aggregate (Focus Plus beneficiaries
           = COUNT(DISTINCT beneficiary_key), rule 6 — NOT COUNT(*), which is payments) —
           each inside its OWN subquery / CTE.
        -> Then combine the FINISHED per-scheme sub-results:
             * one statewide number per scheme -> CROSS JOIN the one-row subqueries.
             * a per-district / block / village table -> FULL OUTER JOIN the pre-aggregated
               CTEs on the GRAIN columns only (COALESCE the grain in SELECT), never on a
               measure column.
        -> SELECT each sub-result's column straight through, aliased by scheme.

  UNIVERSAL INVARIANTS — a wrong cross-scheme number almost always means one of these was
  broken:
    * NEVER join or CROSS JOIN a MGNREGA object (curated.v_employment, curated.v_expenditure,
      curated.fact_mgnrega_*) to a PMAY object (curated.v_pmay, curated.fact_pmay_house) on
      geography_key, year_key, village_code, district or any other data column — they share
      no grain. The ONLY valid cross-scheme joins are (a) the two curated.v_cross_scheme_*
      views themselves, and (b) a FULL OUTER JOIN between two subqueries ALREADY aggregated
      to the same grain, joined on the grain columns.
    * NEVER wrap a finished per-scheme subquery in an outer COUNT(*) / SUM() / AVG(). The
      outer aggregate then counts the OTHER table's rows and the figure collapses — this is
      how "PMAY houses benefited" came back ~7k instead of ~171k.
    * For FAMILY C use ONE of exactly two shapes and nothing else:
        - WIDE: CROSS JOIN the one-row per-scheme subqueries; the final SELECT lists
          m.<col>, p.<col> straight through (no aggregate around them). One result row.
        - LONG: a bare `SELECT '<scheme>' AS scheme, <agg> AS value FROM <that scheme's
          view> [filters]` per scheme joined by UNION ALL, and that UNION ALL IS the whole
          query — one result row per scheme.
      NEVER UNION ALL the two schemes and then SELECT ... FROM (that union): the per-scheme
      columns do not line up and any outer aggregate counts union rows, not real records.
    * Each per-scheme figure MUST equal what that scheme's standalone query returns. If
      "houses benefited for PMAY-G" alone is `COUNT(*) FROM curated.v_pmay WHERE NOT
      is_placeholder`, the PMAY side of the cross-scheme answer is that identical expression.
    * The two schemes measure DIFFERENT things (MGNREGA person-days / households given work
      vs PMAY-G houses). Do NOT add them into one "combined total" unless both sides are the
      SAME unit (money, both in crore). Otherwise report one figure per scheme and state
      each unit — and, for MGNREGA yearly counts, the financial year.
    * curated.bridge_geography_source is MGNREGA-internal (source_system 'EMPLOYMENT' /
      'EXPENDITURE' only) — it has NO PMAY rows and can never answer a cross-scheme question.

  Worked examples — each is ONE instance of the procedure above. For anything not shown,
  follow the procedure; do not force the question onto the nearest example.

  Worked example (FAMILY C template) — "How many households/beneficiaries benefited from
  each scheme?" / "households benefited across MGNREGA and PMAY-G" / "how many people did
  the two schemes reach". One figure per scheme, different units, NO combined total.
  Aggregate each scheme in its OWN one-row subquery exactly as its single-scheme query
  would, CROSS JOIN, and SELECT each column straight through:
    SELECT m.mgnrega_households_employed, p.pmay_houses_sanctioned
    FROM (SELECT SUM(households_employed) AS mgnrega_households_employed
          FROM curated.v_employment
          WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment)) m
    CROSS JOIN (SELECT COUNT(*) AS pmay_houses_sanctioned
                FROM curated.v_pmay
                WHERE NOT is_placeholder) p
    LIMIT 1;
  The MGNREGA side is a single financial year (latest if none named), like every
  households_employed / person-days figure; the PMAY side is all sanctioned houses across
  the data window. State both units and the financial year in the answer. For a
  per-district version, GROUP BY lgd_district inside each subquery and FULL OUTER JOIN on
  lgd_district (COALESCE it in SELECT).

  Worked example (FAMILY C template, MGNREGA + Focus Plus) — "sum of beneficiaries in
  MGNREGA and Focus Plus" / "beneficiaries across MGNREGA and Focus Plus, all years". Same
  WIDE shape as the MGNREGA + PMAY-G example above — CROSS JOIN two one-row subqueries.
  The Focus Plus side is `COUNT(DISTINCT beneficiary_key)` (rule 6) over the WHOLE view —
  no batch_label filter, no GROUP BY, no has_geo_conflict exclusion. beneficiary_key
  already spans both cohorts as one identifier space, so a plain COUNT(DISTINCT ...)
  correctly includes both the legacy 93K cohort and the 12.5K cohort in one number:
    SELECT m.mgnrega_beneficiaries, f.focus_plus_beneficiaries
    FROM (SELECT SUM(households_employed) AS mgnrega_beneficiaries
          FROM curated.v_employment
          WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment)) m
    CROSS JOIN (SELECT COUNT(DISTINCT beneficiary_key) AS focus_plus_beneficiaries
                FROM curated.v_focus_plus) f
    LIMIT 1;
  State that MGNREGA's figure is one financial year while Focus Plus's spans the whole
  data window, and do not add the two into one combined total — they are different units.

  Worked example (FAMILY C, THREE schemes) — "compare beneficiaries across MGNREGA, PMAY-G
  and Focus Plus" / "how many beneficiaries does each of the three schemes have across all
  financial years". "Beneficiaries" is a UNIQUE household/person count per scheme — NOT
  COUNT(*) of rows, NOT a SUM across years. Each scheme keeps its own single-scheme
  distinct-beneficiary reading in a bare SELECT, joined by UNION ALL, and that UNION ALL
  IS the whole query (never wrap it in an outer COUNT/SUM):
    SELECT 'MGNREGA' AS scheme, SUM(households_employed) AS beneficiaries
    FROM curated.v_employment
    WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment)
  UNION ALL
    SELECT 'PMAY-G', COUNT(*)
    FROM curated.v_pmay
    WHERE NOT is_placeholder
  UNION ALL
    SELECT 'Focus Plus', COUNT(DISTINCT beneficiary_key)
    FROM curated.v_focus_plus
  LIMIT 10;
  Why each side is what it is:
    * MGNREGA has no household id, so a cross-year SUM double-counts the same household.
      Its unique reading is ONE financial year's SUM(households_employed) (latest if none
      named).
    * curated.v_pmay is already one row per house, so COUNT(*) WHERE NOT is_placeholder IS
      the distinct-house count.
    * curated.v_focus_plus COUNT(*) is PAYMENTS (the legacy 93K cohort has four rows per
      person). COUNT(DISTINCT beneficiary_key) (rule 6) is the correct unique-person count
      across both cohorts, no per-batch scoping needed. COUNT(DISTINCT member_id) counts
      the 12.5K registration cohort ONLY — never label it a Focus Plus total.
  The three figures measure different things (MGNREGA households given work in one FY,
  PMAY-G houses sanctioned across the window, Focus Plus distinct beneficiaries) — report
  one per scheme, state each unit and the MGNREGA financial year, and do NOT add them into
  a combined total.

  Worked example — "Compare MGNREGA and PMAY spending in West Garo Hills":
    SELECT scheme_code, amount_crore, measure_semantics
    FROM curated.v_cross_scheme_money_district_year
    WHERE lgd_district = 'WEST GARO HILLS'
    ORDER BY scheme_code
    LIMIT 10;

  Worked example — "Compare districts for MGNREGA and PMAY-G" / any per-district table with
  one MGNREGA column, one PMAY column and a combined column. The view is LONG (one row per
  scheme), so split the schemes with conditional aggregation — and the PMAY filter literal
  is 'PMAY', never 'PMAY-G' (see SCHEME_CODE LITERALS). A wrong PMAY literal is exactly what
  makes the PMAY column come back 0 while the combined column still looks right:
    SELECT lgd_district,
           ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'MGNREGA'), 2) AS mgnrega_total_crore,
           ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'PMAY'), 2)    AS pmay_total_crore,
           ROUND(SUM(amount_crore), 2)                                        AS combined_total_crore
    FROM curated.v_cross_scheme_money_district_year
    GROUP BY lgd_district
    ORDER BY combined_total_crore DESC
    LIMIT 50;

  Worked example — "Top 5 blocks by combined MGNREGA expenditure + PMAY-G amount released" /
  ANY block- or village-level combined-money question. There is NO block-grain cross-scheme
  view, and joining curated.v_expenditure to curated.v_pmay is prohibited. Build it the way
  the curated views build v_district_year_summary internally: aggregate EACH scheme to the
  target grain in its own CTE, convert both to CRORE (MGNREGA total_exp is LAKH -> /100;
  PMAY amount_released is RUPEES -> /1e7), then FULL OUTER JOIN the two pre-aggregated CTEs
  on the grain columns only — never on a fact column. Block names do not repeat across
  districts, but carry lgd_district through both sides and join on both:
    WITH mgnrega AS (
      SELECT lgd_district, lgd_block, SUM(total_exp) / 100 AS mgnrega_exp_cr
      FROM curated.v_expenditure
      GROUP BY lgd_district, lgd_block
    ), pmay AS (
      SELECT lgd_district, lgd_block, SUM(amount_released) / 1e7 AS pmay_released_cr
      FROM curated.v_pmay
      WHERE NOT is_placeholder
      GROUP BY lgd_district, lgd_block
    )
    SELECT COALESCE(m.lgd_district, p.lgd_district)  AS lgd_district,
           COALESCE(m.lgd_block, p.lgd_block)        AS lgd_block,
           ROUND(COALESCE(m.mgnrega_exp_cr, 0), 2)   AS mgnrega_exp_cr,
           ROUND(COALESCE(p.pmay_released_cr, 0), 2) AS pmay_released_cr,
           ROUND(COALESCE(m.mgnrega_exp_cr, 0) + COALESCE(p.pmay_released_cr, 0), 2) AS combined_cr
    FROM mgnrega m
    FULL OUTER JOIN pmay p
      ON m.lgd_district = p.lgd_district AND m.lgd_block = p.lgd_block
    ORDER BY combined_cr DESC
    LIMIT 5;
  For a per-village version, GROUP BY village_code (+ lgd_village_name) on each side and join
  on village_code. The two amounts stay different kinds of number — MGNREGA expenditure
  incurred vs PMAY money released — so say that in the answer, exactly as measure_semantics
  would for the district view.

  Worked example — "Which districts have BOTH MGNREGA activity and PMAY-G houses
  sanctioned?" (a coverage question — roll the village-grain coverage view up to
  district; "has activity" / "has houses" means the summed measure is > 0, with a
  NULL measure treated as 0):
    SELECT lgd_district
    FROM curated.v_cross_scheme_village_coverage
    GROUP BY lgd_district
    HAVING COALESCE(SUM(mgnrega_person_days), 0) > 0
       AND COALESCE(SUM(pmay_houses), 0) > 0
    ORDER BY lgd_district
    LIMIT 20;
  For "only MGNREGA" / "only PMAY-G", set the other side's HAVING term to = 0. For a
  per-village version, SELECT village_code, lgd_district and filter the two measures
  directly with no GROUP BY. NEVER answer a coverage question from a bare
  SELECT DISTINCT lgd_district — that lists every district and proves nothing about
  which have both.

  Worked example — "How many common districts are in both schemes?" / "how many
  districts have both MGNREGA and PMAY-G activity?" (the COUNT of that same
  district set — NOT a count of rows). The coverage view is one row per village,
  so counting it directly counts villages, not districts. Roll up to district
  first, then count the districts that qualify:
    SELECT COUNT(*) AS districts_with_both
    FROM (
      SELECT lgd_district
      FROM curated.v_cross_scheme_village_coverage
      GROUP BY lgd_district
      HAVING COALESCE(SUM(mgnrega_person_days), 0) > 0
         AND COALESCE(SUM(pmay_houses), 0) > 0
    ) d;
  Same shape for "how many villages are in both schemes" EXCEPT there GROUP BY /
  the subquery is not needed — the view is already one row per village, so
  SELECT COUNT(*) ... WHERE COALESCE(mgnrega_person_days,0) > 0 AND
  COALESCE(pmay_houses,0) > 0. NEVER COUNT(*) the raw view for a "how many
  districts" question — that returns the village count (a number in the
  thousands), not the ~12 districts asked for.

  Worked example — "How many BLOCKS are common to both schemes?" / "block-wise,
  which blocks are in both schemes?" / "how many blocks have both MGNREGA and
  PMAY-G activity?". curated.v_cross_scheme_village_coverage carries village_code
  and lgd_district but NOT lgd_block, so join it to curated.dim_geography on
  village_code to pick up the block, roll the village grain up to
  (lgd_district, lgd_block), keep the blocks where BOTH summed measures are > 0,
  then count them:
    SELECT COUNT(*) AS blocks_with_both
    FROM (
      SELECT g.lgd_district, g.lgd_block
      FROM curated.v_cross_scheme_village_coverage v
      JOIN curated.dim_geography g ON g.village_code = v.village_code
      GROUP BY g.lgd_district, g.lgd_block
      HAVING COALESCE(SUM(v.mgnrega_person_days), 0) > 0
         AND COALESCE(SUM(v.pmay_houses), 0) > 0
    ) b;
  To LIST the blocks instead, drop the COUNT(*) wrapper and finish with
  ORDER BY g.lgd_district, g.lgd_block LIMIT 50. For "blocks with only MGNREGA" /
  "only PMAY-G", set the other HAVING term to = 0. Always GROUP BY BOTH
  lgd_district and lgd_block (a bare block name can repeat across districts), and
  NEVER COUNT(*) the joined rows directly — that counts villages, not blocks.

  Worked example — "How many villages have MGNREGA activity but no PMAY-G house
  sanctioned?" (count of villages covered by ONE scheme and not the other). This is
  curated.v_cross_scheme_village_coverage as well — NOT curated.bridge_geography_source,
  NOT a join between curated.v_employment and curated.v_pmay. The view is already one
  row per village, so NO GROUP BY; each measure is NULL when that scheme did nothing
  there, and NULL means "no activity", so COALESCE it to 0 on BOTH sides:
    SELECT COUNT(*) AS villages
    FROM curated.v_cross_scheme_village_coverage
    WHERE COALESCE(mgnrega_person_days, 0) > 0
      AND COALESCE(pmay_houses, 0) = 0;
  Swap the two conditions for "PMAY-G house but no MGNREGA activity". Both sides > 0
  is "villages with BOTH"; OR the two is "villages with EITHER". To list the villages
  rather than count them, SELECT village_code, lgd_district and add LIMIT.

  Worked example — "Give me the full MGNREGA + PMAY-G convergence picture for Meghalaya —
  spend, coverage, completion, and where the two schemes overlap or don't" / "the whole
  convergence / overlap picture" / any SINGLE request that wants combined SPEND +
  COMPLETION + COVERAGE/OVERLAP across both schemes at once. No one view carries all of
  that, and it is NOT unanswerable — build a district-grain table by aggregating four
  independent CTEs to lgd_district and LEFT JOINing them onto the full district list:
  money (pivoted per scheme from the cross-scheme money view), village overlap (rolled
  up from the cross-scheme coverage view), MGNREGA completion and PMAY completion (each
  from that scheme's own detail view). Join ONLY on lgd_district, never on a fact column:
    WITH money AS (
      SELECT lgd_district,
             ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'MGNREGA'), 2) AS mgnrega_crore,
             ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'PMAY'), 2)    AS pmay_crore
      FROM curated.v_cross_scheme_money_district_year
      GROUP BY lgd_district
    ), coverage AS (
      SELECT lgd_district,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) > 0
                                AND COALESCE(pmay_houses,0) > 0) AS villages_both,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) > 0
                                AND COALESCE(pmay_houses,0) = 0) AS villages_mgnrega_only,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) = 0
                                AND COALESCE(pmay_houses,0) > 0) AS villages_pmay_only
      FROM curated.v_cross_scheme_village_coverage
      GROUP BY lgd_district
    ), mgnrega_done AS (
      SELECT lgd_district, SUM(households_completed_100_days) AS mgnrega_100day_households
      FROM curated.v_employment
      GROUP BY lgd_district
    ), pmay_done AS (
      SELECT lgd_district,
             COUNT(*)                             AS pmay_houses_sanctioned,
             COUNT(*) FILTER (WHERE is_completed) AS pmay_houses_completed
      FROM curated.v_pmay
      WHERE NOT is_placeholder
      GROUP BY lgd_district
    ), districts AS (
      SELECT lgd_district FROM money
      UNION SELECT lgd_district FROM coverage
      UNION SELECT lgd_district FROM mgnrega_done
      UNION SELECT lgd_district FROM pmay_done
    )
    SELECT d.lgd_district,
           COALESCE(mo.mgnrega_crore, 0)             AS mgnrega_crore,
           COALESCE(mo.pmay_crore, 0)                AS pmay_crore,
           COALESCE(md.mgnrega_100day_households, 0) AS mgnrega_100day_households,
           COALESCE(pd.pmay_houses_sanctioned, 0)    AS pmay_houses_sanctioned,
           COALESCE(pd.pmay_houses_completed, 0)     AS pmay_houses_completed,
           COALESCE(co.villages_both, 0)             AS villages_both,
           COALESCE(co.villages_mgnrega_only, 0)     AS villages_mgnrega_only,
           COALESCE(co.villages_pmay_only, 0)        AS villages_pmay_only
    FROM districts d
    LEFT JOIN money mo        USING (lgd_district)
    LEFT JOIN coverage co     USING (lgd_district)
    LEFT JOIN mgnrega_done md USING (lgd_district)
    LEFT JOIN pmay_done pd    USING (lgd_district)
    ORDER BY (COALESCE(mo.mgnrega_crore,0) + COALESCE(mo.pmay_crore,0)) DESC
    LIMIT 20;
  mgnrega_crore is MGNREGA expenditure incurred and pmay_crore is PMAY money released
  against sanctions — different kinds of number, so say that in the answer (exactly as
  measure_semantics would). villages_both / villages_mgnrega_only / villages_pmay_only
  ARE the "where they overlap or don't" finding. For a single statewide row, drop
  lgd_district from every CTE's SELECT and GROUP BY, drop the districts CTE, and
  cross join the four one-row CTEs: SELECT * FROM money, coverage, mgnrega_done, pmay_done.
""".strip()

_CMELEVATELEGACY_CROSS = """
CM ELEVATE LEGACY IN A CROSS-SCHEME QUESTION — read first:
  CM Elevate Legacy appears in NEITHER cross-scheme view (both are built from MGNREGA and
  PMAY only), and every fact-to-fact join involving curated.v_cm_elevate_disbursement is
  prohibited — including to curated.v_cm_elevate, the other CM Elevate dataset, with which
  it shares a name and no key.
  * MONEY compared or combined with another scheme (FAMILY A): do NOT build it. Return only
    the CM Elevate Legacy figure(s) from its own view, and say the like-for-like comparison
    is not available because no cross-scheme view covers this scheme.
  * Any other per-scheme metric (FAMILY C): the CM Elevate Legacy figure is COUNT(*) FROM
    curated.v_cm_elevate_disbursement, labelled "records" (sanction-and-disbursement
    records), in its own bare SELECT joined to the others by UNION ALL — never added into a
    combined total with another scheme's unit.
""".strip()

_SCHEME_BLOCKS = {
    "MGNREGA": (_MGNREGA_TABLES, _MGNREGA_RULES, _MGNREGA_VOCAB),
    "PMAY-G": (_PMAY_TABLES, _PMAY_RULES, _PMAY_VOCAB),
    "Focus Plus": (_FOCUSPLUS_TABLES, _FOCUSPLUS_RULES, _FOCUSPLUS_VOCAB),
    "CM Elevate": (_CMELEVATE_TABLES, _CMELEVATE_RULES, _CMELEVATE_VOCAB),
    "Focus Legacy": (_FOCUSLEGACY_TABLES, _FOCUSLEGACY_RULES, _FOCUSLEGACY_VOCAB),
    "CM Elevate Legacy": (_CMELEVATELEGACY_TABLES, _CMELEVATELEGACY_RULES, _CMELEVATELEGACY_VOCAB),
}


def build_schema_context(schemes: list[str]) -> str:
    """Assemble only the tables/rules/vocabulary the classified scheme(s) need.
    Shared dimensions and the LIMIT/read-only instructions are always included;
    cross-scheme views only appear when more than one scheme is in play."""
    parts = [_PREAMBLE, _SHARED_TABLES]

    for scheme in schemes:
        block = _SCHEME_BLOCKS.get(scheme)
        if block:
            parts.extend(block)

    if len(schemes) > 1:
        parts.append(_CROSS_SCHEME)
        if "CM Elevate Legacy" in schemes:
            parts.append(_CMELEVATELEGACY_CROSS)

    parts.append(f"SHARED RULES:\n{_SHARED_RULES}")
    parts.append(_CLOSING)
    return "\n\n".join(parts)


# Kept for any caller that still wants the full, unscoped context (e.g. a
# fallback if scheme classification produced nothing usable).
SCHEMA_CONTEXT = build_schema_context(list(SCHEME_CATALOG))
