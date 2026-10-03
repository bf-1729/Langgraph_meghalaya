"""
Admin-level collision gate — a bare place name that could be a block, an
assembly constituency and/or a village must PAUSE and ask which level is meant,
never resolve one silently.

Regression for the 2026-09-15 report: "give me total beneficiaries in Sohra for
MGNREGA across all financial years" answered with 643 beneficiaries for the
VILLAGE Sohrarim. "Sohra" is assembly constituency 28 (East Khasi Hills); it is
not a village at all. The chain was: the LLM mention-extractor tags a bare name
as "block" by default -> no SOHRA block exists -> the block branch falls back to
resolve_village() -> its trigram fuzzy stage matched "Sohrarim" -> answered.
That branch never checked whether the same text also names a constituency.

Runs without the model gateway and without megh_db: resolve_village and the LLM
mention-extractor are stubbed, so every assertion exercises the real
resolve_entities / collides_across_dimensions logic against the real SME
catalogues. Plain script (no pytest in the venv) —
`python tests/test_admin_level_collision.py`, exit code 0 = all pass.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_resolver as er  # noqa: E402
from app import pipeline as p  # noqa: E402
from app import prompt_builder  # noqa: E402
from app.entity_resolver import collides_across_dimensions, load_all  # noqa: E402

load_all()

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ── Stubs: the only two things that would need the DB / the model gateway ───
# Names chosen to mirror the real data: "Sohra" is an AC whose nearest village
# is the DIFFERENT place "Sohrarim" (the exact confusion that caused the bug);
# "Mawlai" is a genuine three-way block/AC/village collision.
_VILLAGES = {
    "sohra": (277001, "Sohrarim"),
    "mawlai": (278067, "Mawlwai"),
    "malwai": (278067, "Mawlwai"),   # the typo also trigram-matches a village
    "nongthymmai": (277900, "Nongthymmai"),
}


async def _fake_resolve_village(text, district=None, block=None):
    hit = _VILLAGES.get(str(text).strip().lower())
    if not hit:
        return er.Resolved("not_found", "village", text)
    return er.Resolved("resolved", "village", text, canonical=hit[0], display=hit[1])


_MENTIONS: dict = {}


async def _fake_extract(question):
    return dict(_MENTIONS)


# What each constituency contains, as constituency_contents() would read it
# live from curated.v_employment. SELSELLA is one of the 8 real ACs that
# straddle two districts — the case where the district step is a genuine
# question rather than a formality.
_AC_CONTENTS = {
    "SOHRA": {"districts": ["EAST KHASI HILLS"], "blocks": ["SOHRA"], "villages": 94},
    "SELSELLA": {"districts": ["SOUTH WEST GARO HILLS", "WEST GARO HILLS"],
                 "blocks": ["SELSELLA", "BETASING"], "villages": 210},
}


async def _fake_constituency_contents(ac_name, scheme=None):
    return _AC_CONTENTS.get(str(ac_name).upper(),
                            {"districts": [], "blocks": [], "villages": 0})


p.resolve_village = _fake_resolve_village
er.resolve_village = _fake_resolve_village
p.extract_entity_mentions = _fake_extract
p.constituency_contents = _fake_constituency_contents


def resolve(question: str, mentions: dict, village_hint: str | None = None):
    """('asked', [chip labels]) or ('resolved', {resolved entities})."""
    global _MENTIONS
    _MENTIONS = mentions

    async def _go():
        try:
            out = await p.resolve_entities(question, ["MGNREGA"], village_hint=village_hint)
            return "resolved", out["resolved"]
        except p.ClarificationNeeded as c:
            return "asked", [o["label"] for o in c.options]

    return asyncio.run(_go())


def chips(question: str, mentions: dict) -> list[tuple[str, str]]:
    """[(label, resumed question)] for a clarification, else []."""
    global _MENTIONS
    _MENTIONS = mentions

    async def _go():
        try:
            await p.resolve_entities(question, ["MGNREGA"])
            return []
        except p.ClarificationNeeded as c:
            return [(o["label"], o["question"]) for o in c.options]

    return asyncio.run(_go())


SOHRA_Q = "give me total beneficiaries in Sohra for MGNREGA across all financial years"

# ── 1. The detector itself ─────────────────────────────────────────────────
print("1. COLLISION DETECTOR")
check("'Sohra' + a village match is a 2-way collision",
      list(collides_across_dimensions("Sohra", "MGNREGA", village_hit=True))
      == ["assembly_constituency", "village"])
check("'Sohra' with NO village match is not a collision",
      collides_across_dimensions("Sohra", "MGNREGA", village_hit=False) == {})
check("'Mawlai' is a 3-way block/AC/village collision",
      list(collides_across_dimensions("Mawlai", "MGNREGA", village_hit=True))
      == ["block", "assembly_constituency", "village"])
check("a district name never collides (zero-collision dimension)",
      collides_across_dimensions("West Garo Hills", "MGNREGA", village_hit=True) == {})
check("an unknown name never collides",
      collides_across_dimensions("Guwahati", "MGNREGA", village_hit=True) == {})

# A MISSPELLED name must collide too. resolve_village reaches the DB through a
# trigram fallback that tolerates typos, so an exact-only check on the
# catalogue side would be asymmetric and the typo would resolve silently to a
# village — the 2026-09-15 "malwai" report.
_typo = collides_across_dimensions("malwai", "MGNREGA", village_hit=True)
check("typo 'malwai' still collides across all three levels",
      list(_typo) == ["block", "assembly_constituency", "village"], _typo)
check("typo collision carries the CANONICAL name for the chips",
      _typo.get("block") == "MAWLAI" and _typo.get("assembly_constituency") == "MAWLAI", _typo)
check("a name merely near-equidistant from several entries is not a hit",
      collides_across_dimensions("tura", "MGNREGA", village_hit=True) == {})

# ── 2. The reported bug ────────────────────────────────────────────────────
print("2. REPORTED BUG — 'Sohra' must ask, not answer")
for slot in ("block", "village", "district"):
    status, payload = resolve(SOHRA_Q, {slot: "Sohra"})
    check(f"extractor tags Sohra as {slot!r} -> asks which level", status == "asked", payload)
    if status == "asked":
        check(f"  chips offered for {slot!r} name both levels",
              payload == ["The SOHRA assembly constituency", "The Sohra village"], payload)

status, payload = resolve("person-days in Mawlai", {"block": "Mawlai"})
check("'Mawlai' offers all three levels",
      status == "asked" and len(payload) == 3, payload)

# ── 3. Resuming the pause — each chip resolves at THAT level only ───────────
print("3. CHIP RESUME RESOLVES AT THE CHOSEN LEVEL ONLY")
# Picking the AC now leads to the step-2 narrowing prompt rather than a final
# answer (see section 5); confirming "the whole constituency" is what resolves.
status, payload = resolve(SOHRA_Q + ", the assembly constituency, not another area type",
                          {"block": "Sohra"}, village_hint="Sohra")
check("AC chip leads to the narrowing step", status == "asked", payload)
status, res = resolve(SOHRA_Q + ", the assembly constituency, not another area type"
                      + ", the whole constituency",
                      {"assembly_constituency": "Sohra"}, village_hint="Sohra")
check("AC chip resolves the constituency", res.get("assembly_constituency") == "SOHRA", res)
check("AC chip does NOT also filter a village", "village_code" not in res, res)

status, res = resolve(SOHRA_Q + ", the village, not another area type",
                      {"block": "Sohra"}, village_hint="Sohra")
check("village chip resolves the village", res.get("village_code") == 277001, res)
check("village chip does NOT also filter a constituency",
      "assembly_constituency" not in res, res)

# A MISSPELLED name must survive the whole round trip: pause, then each chip
# resolves at its OWN level. The chip rewrites the question to the canonical
# name, but the extractor keeps echoing the typo back — often into the very
# slot the chip names — so the level must be taken from the question text, not
# from the extractor (2026-09-15 "malwai" report).
MALWAI_Q = "give me total beneficiaries in malwai for MGNREGA across all financial years"
status, payload = resolve(MALWAI_Q, {"block": "malwai"})
check("typo 'malwai' pauses instead of answering", status == "asked", payload)
check("  typo chips name the canonical place, not the typo",
      payload == ["The MAWLAI C&RD block", "The MAWLAI assembly constituency",
                  "The malwai village"], payload)

_resumed = MALWAI_Q.replace("malwai", "MAWLAI")
status, res = resolve(_resumed + ", the block, not another area type",
                      {"block": "malwai"}, village_hint="malwai")
check("typo block chip resolves the BLOCK",
      res.get("block") == "MAWLAI" and "village_code" not in res, res)

status, res = resolve(_resumed + ", the assembly constituency, not another area type",
                      {"block": "malwai"}, village_hint="malwai")
check("typo AC chip resolves the CONSTITUENCY",
      res.get("assembly_constituency") == "MAWLAI" and "village_code" not in res, res)

status, res = resolve(MALWAI_Q + ", the village, not another area type",
                      {"block": "malwai"}, village_hint="malwai")
check("typo village chip resolves the VILLAGE",
      res.get("village_code") == 278067 and "block" not in res, res)


# ── 4. The gate must not fire where the level is already clear ──────────────
print("4. NO UNNECESSARY PAUSE (regressions)")
status, res = resolve("total beneficiaries in West Garo Hills for MGNREGA",
                      {"district": "West Garo Hills"})
check("a plain district question still answers straight through",
      status == "resolved" and res.get("district") == "WEST GARO HILLS", res)

status, res = resolve("person-days in Sohra constituency, the whole constituency",
                      {"assembly_constituency": "Sohra"})
check("user naming the level outright is not asked which level again",
      status == "resolved" and res.get("assembly_constituency") == "SOHRA", res)

status, res = resolve("person-days in Sohra village", {"village": "Sohra"})
check("'Sohra village' resolves the village without a pause",
      status == "resolved" and res.get("village_code") == 277001, res)

status, res = resolve("total MGNREGA expenditure", {})
check("a question naming no place is untouched", status == "resolved" and res == {}, res)

status, res = resolve("compare ekh and wgh", {"districts": ["ekh", "wgh"]})
check("a district comparison is untouched", status == "resolved" and res.get("district_list"), res)

status, res = resolve("person-days in Nongthymmai, East Khasi Hills",
                      {"village": "Nongthymmai", "district": "East Khasi Hills"})
check("a district-scoped village is untouched",
      status == "resolved" and res.get("village_code") == 277900, res)

# ── 5. Step 2 — narrowing INSIDE a chosen constituency ─────────────────────
# Picking "assembly constituency" is not the end of the conversation: the user
# is then offered the whole constituency or a part of it. An AC is an electoral
# boundary, not an administrative parent (8 of 56 straddle two districts), so
# the narrowing offered is built from what the constituency actually contains.
print("5. AC DRILL-DOWN (step 2 of the hierarchy)")
# Step 1: the level question. Follow its AC chip into step 2.
_step1 = chips("give me total person-days in Sohra for MGNREGA", {"block": "Sohra"})
_ac_q = [q for l, q in _step1 if "constituency" in l.lower()][0]
_step2 = chips(_ac_q, {"assembly_constituency": "Sohra"})
check("picking the AC offers a narrowing step",
      [l for l, _ in _step2] == ["The whole SOHRA constituency", "Sohra block"], _step2)

for _label, _q in _step2:
    _st, _res = resolve(_q, {"assembly_constituency": "Sohra"})
    check(f"  {_label!r} resolves without asking again", _st == "resolved", _res)
    check(f"  {_label!r} keeps the constituency filter",
          _res.get("assembly_constituency") == "SOHRA", _res)
_whole, _blk = _step2[0][1], _step2[1][1]
check("  'whole constituency' adds no area filter",
      "block" not in resolve(_whole, {"assembly_constituency": "Sohra"})[1])
check("  a block chip DOES add the block filter",
      resolve(_blk, {"assembly_constituency": "Sohra"})[1].get("block") == "SOHRA")

# A two-district constituency additionally offers each district part.
_s1 = chips("total person-days in Selsella", {"block": "Selsella"})
_split = chips([q for l, q in _s1 if "constituency" in l.lower()][0],
               {"assembly_constituency": "Selsella"})
check("a 2-district AC offers each district part",
      "Only the South West Garo Hills part" in [l for l, _ in _split], _split)
_dq = [q for l, q in _split if "South West Garo Hills" in l][0]
_st, _res = resolve(_dq, {"assembly_constituency": "Selsella"})
check("  picking a district part filters BOTH the AC and that district",
      _res.get("assembly_constituency") == "SELSELLA"
      and _res.get("district") == "SOUTH WEST GARO HILLS", _res)

# ── 6. The AC reading is only offered when it can be answered ──────────────
# assembly_constituency exists on ONE fact in the warehouse
# (curated.fact_mgnrega_employment). Offering it for an expenditure or housing
# question would invite a choice that can never be answered.
print("6. AC CHIP SUPPRESSED WHERE THERE IS NO AC DATA")
for _q in ("total expenditure in Sohra", "houses completed in Sohra",
           "amount released in Sohra"):
    _st, _res = resolve(_q, {"block": "Sohra"})
    check(f"{_q!r} does not offer a constituency", _st == "resolved", _res)
_st, _payload = resolve("person-days in Sohra", {"block": "Sohra"})
check("an employment question DOES still offer the constituency",
      _st == "asked" and "The SOHRA assembly constituency" in _payload, _payload)


# ── 7. AC + block together survive SQL generation and verification ─────────
# Both filters resolved is the DRILL-DOWN case (section 5). Two failures were
# found live on 2026-09-15 when that pair reached the SQL layer:
#   a) the verifier rejected correct SQL purely because the generator wrote
#      `assembly_constituency_name = 'X'` instead of the prompt's
#      `UPPER(...) = UPPER('X')` — identical rows, cosmetic difference;
#   b) the resulting repair prompt made the generator DROP one of the two
#      filters, so every later attempt was flagged for a genuinely missing
#      entity and the question fell through to the KB fallback.
print("7. AC + BLOCK PAIR (prompt + verifier guard)")
_pair = {"assembly_constituency": "MAWLAI", "block": "MAWLAI"}
_block_text = prompt_builder._entities_block({"resolved": _pair, "notes": []})
check("the prompt states BOTH filters are required",
      "BOTH REQUIRED" in _block_text, _block_text)
check("  and names the block alongside the constituency",
      "lgd_block = 'MAWLAI'" in _block_text and "assembly_constituency_name" in _block_text)

_cosmetic = ("Check 2: RESOLVED ENTITIES block lists UPPER(assembly_constituency_name) = "
             "UPPER('MAWLAI'), but the SQL filters on assembly_constituency_name = 'MAWLAI' "
             "(lowercase). The resolved entity value must be present verbatim.")
_sql_ok = ("SELECT SUM(persons_employed) FROM curated.v_employment "
           "WHERE assembly_constituency_name = 'MAWLAI' AND lgd_block = 'MAWLAI'")
_sql_missing = "SELECT SUM(persons_employed) FROM curated.v_employment WHERE lgd_block = 'MAWLAI'"
check("a cosmetic case complaint is discarded when the filter IS present",
      p._verifier_complaint_is_cosmetic(_cosmetic, _pair, _sql_ok))
check("  but a genuinely missing AC filter still raises",
      not p._verifier_complaint_is_cosmetic(_cosmetic, _pair, _sql_missing))
check("  and a non-cosmetic complaint is never discarded",
      not p._verifier_complaint_is_cosmetic(
          "Check 1: the query counts houses, not beneficiaries.", _pair, _sql_ok))


# ── 8. Village backstop + its verifier guard ───────────────────────────────
# The LLM mention-extractor drops a plainly-named village ("...in ASIMGRE for
# MGNREGA for East Khasi Hills" came back {"district": "East Khasi Hills"}),
# and with no village mention the filter silently vanished: the query counted
# the WHOLE district while the answer still said "for ASIMGRE" (2026-09-15).
# The scan that backstops it must be precise — a fuzzy scan over ~6,000
# villages would invent a filter for almost any question.
print("8. VILLAGE BACKSTOP (scan precision + verifier guard)")
# The scan proposes several overlapping phrases per preposition (each costs one
# exact-match lookup and nothing more) — what matters is that the real village
# name is among them, not that it is the only one.
check("a bare village name is a scan candidate",
      "ASIMGRE" in p._village_scan_candidates(
          "give me total beneficiaries in ASIMGRE for MGNREGA for East Khasi Hills",
          "MGNREGA"))
for _q in ("total MGNREGA expenditure in West Garo Hills",
           "How many job cards issued in Ri Bhoi",
           "What was the total MGNREGA expenditure in Meghalaya?",
           "compare person-days between EKH and WGH for FY 2023-24"):
    check(f"  no candidate from {_q[:38]!r}",
          p._village_scan_candidates(_q, "MGNREGA") == [],
          p._village_scan_candidates(_q, "MGNREGA"))

# When a village_code is resolved, _entities_block suppresses the district that
# merely scoped the lookup. The verifier sometimes "quotes" that suppressed
# entity anyway and rejects correct village-only SQL for omitting it.
_res = {"district": "EAST GARO HILLS", "village_code": 275373}
_sql_village = "SELECT SUM(persons_employed) FROM curated.v_employment WHERE village_code = 275373"
_sql_district = ("SELECT SUM(persons_employed) FROM curated.v_employment "
                 "WHERE lgd_district = 'EAST GARO HILLS'")
_hallucinated = ("Check 2: The RESOLVED ENTITIES block lists lgd_district = 'EAST GARO HILLS', "
                 "but the SQL WHERE clause filters on village_code = 275373 instead. The "
                 "resolved district value is missing from the query.")
check("a demand for the suppressed district is discarded",
      p._verifier_wants_suppressed_geography(_hallucinated, _res, _sql_village))
check("  but not when the village filter is genuinely absent",
      not p._verifier_wants_suppressed_geography(_hallucinated, _res, _sql_district))
check("  and never for an unrelated complaint",
      not p._verifier_wants_suppressed_geography(
          "Check 1: the query counts houses, not beneficiaries.", _res, _sql_village))
check("  and never when no village was resolved",
      not p._verifier_wants_suppressed_geography(
          _hallucinated, {"district": "EAST GARO HILLS"}, _sql_village))

# The district/village suppression itself must hold in the prompt.
_blk = prompt_builder._entities_block({"resolved": _res, "notes": []})
check("the entities block omits the district when a village is pinned",
      "lgd_district" not in _blk and "village_code = 275373" in _blk, _blk)


# ── 9. Village names whose PART-MARKER suffix is part of the name ──────────
# Meghalaya writes these two ways, and both distinguish real, separate villages
# in the same block: parenthesised ("NONGCHRAM (I)" / "(II)") and hyphenated
# ("NONGSPUNG - A" / "- B" / "- C", "Umshakait - B"). Two 2026-09-17 reports:
#   * _clean_mention stripped the CLOSING paren and left the opening one, so
#     "NONGCHRAM (I)" became "NONGCHRAM (I" — matching no stored name, staying
#     permanently ambiguous, and making the chip re-ask forever; and
#   * the village backstop's candidate scan stopped before the suffix, so
#     "NONGSPUNG - A" was probed as a bare "NONGSPUNG" (no exact match), the
#     village filter was lost, and the whole UMLING block was reported instead
#     (₹1.46 crore against a real ₹78,000).
print("9. PART-MARKER SUFFIXES SURVIVE")
for _raw, _want in (("NONGCHRAM (I)", "NONGCHRAM (I)"),
                    ("NONGCHRAM (I))))", "NONGCHRAM (I)"),
                    ("NONGCHRAM (II)", "NONGCHRAM (II)"),
                    ("Existing site(Old House)", "Existing site(Old House)"),
                    ("(Tura)", "Tura"),
                    ("Adugre)", "Adugre"),
                    ("West Garo Hills?", "West Garo Hills")):
    check(f"strip {_raw!r} -> {_want!r}",
          p._strip_mention_punctuation(_raw) == _want, p._strip_mention_punctuation(_raw))

_hyphen_q = ("how much sanctioned amount is still to be released in NONGSPUNG - A, "
             "UMLING block, RI BHOI")
_paren_q = ("how much sanctioned amount is still to be released in NONGCHRAM (I), "
            "DAMBO RONGJENG block, EAST GARO HILLS")
check("a hyphenated suffix is scanned as part of the name",
      p._village_scan_candidates(_hyphen_q, "PMAY-G")[0] == "NONGSPUNG - A",
      p._village_scan_candidates(_hyphen_q, "PMAY-G"))
check("a parenthesised suffix is too",
      p._village_scan_candidates(_paren_q, "PMAY-G")[0] == "NONGCHRAM (I)",
      p._village_scan_candidates(_paren_q, "PMAY-G"))
check("an ordinary question still yields no candidate",
      p._village_scan_candidates("total expenditure in West Garo Hills", "PMAY-G") == [])

# Dropping the village for the "All of <district>" chip must take the suffix
# with it — leaving "- A" behind turned the next turn into a broken question.
check("dropping the place takes its suffix along",
      p._drop_place_phrase(_hyphen_q, "NONGSPUNG")
      == "how much sanctioned amount is still to be released, UMLING block, RI BHOI",
      p._drop_place_phrase(_hyphen_q, "NONGSPUNG"))
check("  and leaves no doubled comma or stray fragment",
      "- A" not in p._drop_place_phrase(_hyphen_q, "NONGSPUNG")
      and ",," not in p._drop_place_phrase(_hyphen_q, "NONGSPUNG"))

# The display name must not mangle a roman numeral: .title() gave "(Ii)".
check("'NONGCHRAM (II)' displays as 'Nongchram (II)'",
      p._place_title("NONGCHRAM (II)") == "Nongchram (II)", p._place_title("NONGCHRAM (II)"))
check("  and ordinary names still title-case normally",
      p._place_title("west garo hills") == "West Garo Hills")


# ── 10. A resolved village must not be filtered at the WRONG LEVEL ─────────
# Entity resolution pinned village_code = 277735 for NONGLADEW and the prompt
# said so, but the generator wrote lgd_district = 'NONGLADEW' one run and
# lgd_block = 'NONGLADEW' the next — a VILLAGE name in a block/district column.
# Both match zero rows, so the same question answered "the data doesn't cover
# Nongladew" (0) for a village with 32 real CM Elevate records, and did it
# non-deterministically: district one time, block the next (reported
# 2026-09-17). The existing guard only caught the lgd_village_name variant.
print("10. A RESOLVED VILLAGE IS NOT FILTERED AS A BLOCK/DISTRICT")
_v = {"resolved": {"village_code": 277735}, "display": {"village": "Nongladew"}}
_vd = {"resolved": {"village_code": 277735, "district": "RI BHOI"},
       "display": {"village": "Nongladew", "district": "Ri Bhoi"}}

for _col in ("lgd_block", "lgd_district"):
    _hit = p._village_filtered_at_wrong_level(
        _v, f"SELECT COUNT(*) FROM curated.v_cm_elevate WHERE {_col} = 'NONGLADEW'")
    check(f"{_col} = the village's name is flagged", _hit is not None, _hit)
    if _hit:
        check(f"  and reports the resolved code to repair to", _hit[0] == 277735, _hit)

check("UPPER()-wrapped and table-aliased forms are caught too",
      p._village_filtered_at_wrong_level(
          _v, "SELECT 1 FROM curated.v_cm_elevate ce WHERE UPPER(ce.lgd_block) = 'NONGLADEW'")
      is not None)

check("correct village_code SQL passes",
      p._village_filtered_at_wrong_level(
          _v, "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE village_code = 277735") is None)
check("a GENUINE district scope alongside the village passes",
      p._village_filtered_at_wrong_level(
          _vd, "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE village_code = 277735 "
               "AND lgd_district = 'RI BHOI'") is None)
check("a district-only question is untouched",
      p._village_filtered_at_wrong_level(
          _vd, "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE lgd_district = 'RI BHOI'") is None)
check("nothing fires when no village was resolved",
      p._village_filtered_at_wrong_level(
          {"resolved": {"district": "RI BHOI"}, "display": {}},
          "SELECT 1 FROM curated.v_cm_elevate WHERE lgd_district = 'RI BHOI'") is None)


# ── 11. A TRUNCATED village mention must not re-ask forever ───────────────
# "how many applicants ... in william nagar(mb) - ward no.4" looped: the
# extractor returned only "william nagar(mb)", which is ambiguous across 12
# wards, so every chip re-asked and the question grew each round (reported
# 2026-09-17). The full name resolves to exactly one village, so when the raw
# text carries a LONGER name that resolves uniquely, the mention is a fragment
# of it and neither ambiguity gate should fire.
print("11. TRUNCATED VILLAGE MENTIONS")
_ward_q = ("how many applicants are there under goat farming scheme in "
           "william nagar(mb) - ward no.4 for CM Elevate")
_cands = p._village_scan_candidates(_ward_q, "CM Elevate")
check("the lowercase full ward name is a scan candidate",
      "william nagar(mb) - ward no.4" in [c.lower() for c in _cands], _cands)
check("  and so is the Title-case spelling",
      "William Nagar (MB) - Ward No.4" in p._village_scan_candidates(
          "how many applicants in William Nagar (MB) - Ward No.4 for CM Elevate", "CM Elevate"))

# Overlapping matches: a phrase opening at an earlier preposition ("under goat
# farming scheme in ...") must not swallow the "in" that introduces the place.
check("an earlier preposition does not hide a later one",
      any("william" in c.lower() for c in _cands), _cands)

# Multi-marker names: "(MB)" AND "- Ward No.4" both belong to the name.
check("a two-marker suffix chain is kept whole",
      any(c.lower().endswith("ward no.4") for c in _cands), _cands)

# The suffix must stay bounded — it must not run into the rest of the sentence.
check("  but the suffix does not run on past the name",
      not any("for cm elevate" in c.lower() for c in _cands), _cands)

# Ordinary questions still propose nothing.
for _q in ("total expenditure in West Garo Hills", "person-days in Ri Bhoi"):
    check(f"{_q[:38]!r} proposes no village",
          p._village_scan_candidates(_q, "MGNREGA") == [],
          p._village_scan_candidates(_q, "MGNREGA"))


# ── 12. An explicit "X block" must win, and X must resolve ────────────────
# "…in BATABARI under <3 schemes>, BATABARI block, WEST GARO HILLS" returned
# "no matching records" against a real 46 (reported 2026-09-18). Two faults:
#   a) the chip-scope stripper deleted ", BATABARI block, WEST GARO HILLS" from
#      a USER-typed question, so the word "block" vanished before level
#      detection and a same-named VILLAGE (1 row) beat the block (55 rows);
#   b) BATABARI is missing from CM Elevate's own block catalogue — 21 blocks
#      covering 1,519 rows are — so it resolved "not a C&RD block" and the
#      filter was dropped entirely.
print("12. EXPLICIT BLOCK WORDING AND CATALOGUE GAPS")
_bat_q = ("How many applicants are there in BATABARI under Agro Tourism Villa Scheme, "
          "PRIME Small Enterprise Empowerment and Development (SEED) and Meghalaya "
          "Poultry Farming Scheme, BATABARI block, WEST GARO HILLS")
check("a user-typed 'X block' is detected as the level",
      p._explicit_level_in(_bat_q) == "block", p._explicit_level_in(_bat_q))

# A real chip resume still gets its scope tail stripped, so the chip's own
# level phrase still decides.
check("a village chip resume still reads as 'village'",
      p._explicit_level_in("how many houses in Nongchram, the village, not another "
                           "area type, DAMBO RONGJENG block, EAST GARO HILLS") == "village")
check("a block chip resume still reads as 'block'",
      p._explicit_level_in("person-days in Sohra, the block, not another area type") == "block")
check("an ordinary question with no level word reads as None",
      p._explicit_level_in("how many applicants in NONGLADEW?") is None)

# Blocks are the same real units across schemes — a name any catalogue knows is
# a genuine block, so a per-scheme gap must not make it unresolvable.
for _blk in ("BATABARI", "SIJU", "MAWHATI"):
    _r = er.resolve_dimension(_blk, "CM Elevate", "block")
    check(f"{_blk} resolves for CM Elevate despite the catalogue gap",
          _r.status == "resolved" and str(_r.canonical).upper() == _blk, _r.status)
check("a block already in the scheme's own catalogue still resolves",
      er.resolve_dimension("Selsella", "CM Elevate", "block").status == "resolved")
check("and a non-block still does not resolve",
      er.resolve_dimension("Guwahati", "CM Elevate", "block").status == "not_found")


# ── 13. A breakdown phrase names the GROUPING, not the place ──────────────
# "how many applicants under piggery in each village of Betasing block" read
# "village" as the stated level, so the block branch preferred a village and
# resolved a same-named village (273707). The query then filtered ONE village
# while grouping by village — 0 rows against a real 3-village, 6-applicant
# breakdown (reported 2026-09-18).
print("13. BREAKDOWN PHRASES DO NOT SET THE LEVEL")
for _q, _want in (
        ("how many applicants under piggery in each village of Betasing block", "block"),
        ("CM Elevate applications by village in Betasing block", "block"),
        ("applications village-wise in Betasing block", "block"),
        ("PMAY-G houses per block in West Garo Hills district", "district"),
        # A real place question still reports its own level.
        ("person-days in Sohra village", "village"),
        ("applicants in BATABARI block, WEST GARO HILLS", "block"),
        # A grouping with no place named states no level at all.
        ("CM Elevate applications by district", None),
        ("how many applicants in NONGLADEW?", None)):
    check(f"{_q[:50]!r} -> {_want}", p._explicit_level_in(_q) == _want,
          p._explicit_level_in(_q))


print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL ADMIN-LEVEL COLLISION CHECKS PASSED")
