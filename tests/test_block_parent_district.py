"""
A block must be scoped to ITS OWN district, never to a district named in the
same question for a different block.

Regression for the 2026-09-18 report:

  "How many applicants are there in shallang under Agro Tourism Villa Scheme,
   PRIME Small Enterprise Empowerment and Development (SEED) and Meghalaya
   Poultry Farming Scheme, BATABARI block, WEST GARO HILLS"

SHALLANG is a WEST KHASI HILLS block; BATABARI is a WEST GARO HILLS one (the
district is named precisely because Batabari also exists in South Garo Hills).
The pipeline resolved both blocks into block_list and ALSO kept
district = WEST GARO HILLS as a standalone filter, so the generated SQL was
`lgd_district = 'WEST GARO HILLS' AND lgd_block IN ('SHALLANG','BATABARI')` —
a predicate SHALLANG can never satisfy. The bot reported no matching records
and offered every district instead of knowing which district Shallang is in.

Every block entry in every *_entity_resolver.yaml already carries `district:`;
nothing read it. The fix carries it on Resolved.parent_district, drops the
contradicting standalone district filter, and hands the generator each block's
own district via resolved["block_list_districts"].

Runs without megh_db and without the model gateway (resolve_village and the LLM
mention-extractor are stubbed). Plain script (no pytest in the venv) —
`python tests/test_block_parent_district.py`, exit code 0 = all pass.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_resolver as er  # noqa: E402
from app import pipeline as p  # noqa: E402
from app import prompt_builder  # noqa: E402
from app.entity_resolver import block_parent_district, load_all, resolve_dimension  # noqa: E402

load_all()

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# -- Stubs ------------------------------------------------------------------
async def _no_village(text, district=None, block=None):
    return er.Resolved("not_found", "village", text)


_MENTIONS: dict = {}


async def _fake_extract(question):
    return dict(_MENTIONS)


p.resolve_village = _no_village
er.resolve_village = _no_village
p.extract_entity_mentions = _fake_extract


def resolve(question: str, mentions: dict, schemes=("CM Elevate",)):
    """('asked', [labels]) or ('resolved', entity_result)."""
    global _MENTIONS
    _MENTIONS = mentions

    async def _go():
        try:
            out = await p.resolve_entities(question, list(schemes))
            return "resolved", out
        except p.ClarificationNeeded as c:
            return "asked", [o["label"] for o in c.options]

    return asyncio.run(_go())


# -- 1. The catalogues' district field is actually read ---------------------
print("\n1. Resolved.parent_district is populated for blocks")

for scheme in ("CM Elevate", "Focus Plus", "MGNREGA", "PMAY-G"):
    r = resolve_dimension("shallang", scheme, "block")
    check(f"{scheme}: SHALLANG -> WEST KHASI HILLS",
          r.status == "resolved" and r.parent_district == "WEST KHASI HILLS",
          f"{r.status} / {r.parent_district}")

check("BATABARI -> WEST GARO HILLS (CM Elevate)",
      resolve_dimension("batabari", "CM Elevate", "block").parent_district == "WEST GARO HILLS")
check("a DISTRICT resolve carries no parent_district",
      resolve_dimension("west garo hills", "CM Elevate", "district").parent_district is None)
check("block_parent_district() looks up an already-resolved canonical",
      block_parent_district("SHALLANG", "CM Elevate") == "WEST KHASI HILLS")
check("block_parent_district() falls back across schemes",
      block_parent_district("SHALLANG", "not-a-scheme") == "WEST KHASI HILLS")
check("block_parent_district() is None for an unknown block",
      block_parent_district("NOWHERE AT ALL", "CM Elevate") is None)


# -- 2. The reported question ----------------------------------------------
print("\n2. The 2026-09-18 question: blocks in two different districts")

Q = ("How many applicants are there in shallang under Agro Tourism Villa Scheme, "
     "PRIME Small Enterprise Empowerment and Development (SEED) and Meghalaya "
     "Poultry Farming Scheme, BATABARI block, WEST GARO HILLS")

status, out = resolve(Q, {"blocks": ["shallang", "BATABARI"], "district": "WEST GARO HILLS"})
check("resolves without pausing", status == "resolved", status)
if status == "resolved":
    res = out["resolved"]
    check("both blocks kept", res.get("block_list") == ["SHALLANG", "BATABARI"], res.get("block_list"))
    check("the contradicting standalone district filter is DROPPED",
          "district" not in res, res.get("district"))
    check("each block's own district is carried",
          res.get("block_list_districts") == {"SHALLANG": "WEST KHASI HILLS",
                                              "BATABARI": "WEST GARO HILLS"},
          res.get("block_list_districts"))
    check("the display no longer claims one district for both",
          "district" not in (out.get("display") or {}), (out.get("display") or {}).get("district"))


# -- 3. The district survives when it does NOT contradict the blocks --------
print("\n3. A district consistent with the blocks is left alone")

# BATABARI, TIKRIKILLA and RONGRAM are all genuine WEST GARO HILLS blocks
# (BETASING, despite the name, is SOUTH WEST Garo Hills - a good reminder that
# this has to come from the catalogue, not from the name).
status, out = resolve("compare batabari and tikrikilla blocks in West Garo Hills",
                      {"blocks": ["batabari", "tikrikilla"], "district": "West Garo Hills"})
check("resolves", status == "resolved", status)
if status == "resolved":
    res = out["resolved"]
    check("district kept (both blocks sit in it)",
          res.get("district") == "WEST GARO HILLS", res.get("district"))
    check("blocks kept", res.get("block_list") == ["BATABARI", "TIKRIKILLA"], res.get("block_list"))


# -- 4. Single block contradicting a named district ------------------------
print("\n4. One block, a district it does not belong to")

status, out = resolve("how many applicants in shallang block in West Garo Hills",
                      {"block": "shallang", "district": "West Garo Hills"})
check("resolves", status == "resolved", status)
if status == "resolved":
    res = out["resolved"]
    check("block kept", res.get("block") == "SHALLANG", res.get("block"))
    check("contradicting district dropped", "district" not in res, res.get("district"))

status, out = resolve("how many applicants in shallang block in West Khasi Hills",
                      {"block": "shallang", "district": "West Khasi Hills"})
if status == "resolved":
    res = out["resolved"]
    check("a MATCHING district is kept for a single block",
          res.get("district") == "WEST KHASI HILLS", res.get("district"))


# -- 5. The generator is told which district each block is in ---------------
print("\n5. prompt_builder renders the per-block districts")

block = prompt_builder._entities_block(
    {"resolved": {"block_list": ["SHALLANG", "BATABARI"],
                  "block_list_districts": {"SHALLANG": "WEST KHASI HILLS",
                                           "BATABARI": "WEST GARO HILLS"}},
     "display": {}},
    "",
    ["CM Elevate"],
)

check("names both blocks in the IN-list", "SHALLANG" in block and "BATABARI" in block)
check("states each block's district",
      "WEST KHASI HILLS" in block and "WEST GARO HILLS" in block, block)
check("warns against a single lgd_district filter",
      "do NOT add a single lgd_district" in block, block)
check("block_list_districts is not dumped as a raw key",
      "block_list_districts" not in block, block)


print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED: {_fails}"))
sys.exit(1 if _fails else 0)
