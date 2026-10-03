"""
Conversational handling of meta questions — "can you answer?", "what should I
do?" — must get a plain, helpful reply, never the off-topic bounce and never a
"which scheme?" pause.

Regression for the 2026-09-17 report. Two distinct failures, same root cause
(no bank recognised a question ABOUT the assistant's capability):

  "are you able to give answers?"
      -> no domain vocabulary -> the step-6 whitelist gate -> "I can only
         answer questions about those schemes", which reads as a refusal to a
         question that was asking about exactly that capability.

  "if i ask questions on meghalaya schemes, will you be able to give answers?"
      -> the words "meghalaya"/"schemes" cleared the whitelist -> the DATA path
         -> "Which scheme does your question concern?", asking the user to pick
         a scheme for a question that requests no scheme's data at all.

Both now answer "Yes — that's exactly what I'm here for", then say what is
covered. Pure regex, no model or DB — plain script (no pytest in the venv):
`python tests/test_edge_conversational.py`, exit code 0 = all pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import edge  # noqa: E402
from app import pipeline as pl  # noqa: E402

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def kind(question: str) -> "str | None":
    hit = edge.detect_edge_case(question)
    return hit["type"] if hit else None


# ── 1. The two reported questions ──────────────────────────────────────────
print("1. REPORTED QUESTIONS")
for q in ("are you able to give answers?",
          "if i ask questions on meghalaya schemes, will you be able to give answers?"):
    check(f"{q[:52]!r} -> capability", kind(q) == "capability", kind(q))

_reply = edge.detect_edge_case("are you able to give answers?")["response"]
check("  the reply opens with a plain yes", _reply.lower().lstrip().startswith("yes"), _reply[:60])
check("  and names all four schemes",
      all(s in _reply for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate")))
check("  and is NOT the off-topic bounce", "I can only answer" not in _reply)
check("  and carries starter suggestions",
      len(edge.detect_edge_case("are you able to give answers?").get("suggestions", [])) > 0)

# ── 2. Other capability phrasings ──────────────────────────────────────────
print("2. OTHER CAPABILITY PHRASINGS")
for q in ("can you answer my questions?",
          "do you know about focus plus?",
          "can i ask about pmay?",
          "what kind of questions can i ask?",
          "is it possible for you to answer scheme questions",
          "are you capable of answering data questions"):
    check(f"{q[:48]!r} -> capability", kind(q) == "capability", kind(q))

# ── 3. "I'm lost, point me somewhere" ──────────────────────────────────────
# Guidance requests, not off-topic questions. The _CONFUSED reply already says
# what to try and carries the starter chips.
print("3. GUIDANCE OPENERS")
for q in ("what should i do?", "what should i ask?", "where do i start",
          "what next", "give me some examples"):
    check(f"{q[:40]!r} -> confused", kind(q) == "confused", kind(q))
check("  the guidance reply is not the off-topic bounce",
      "I can only answer" not in edge.detect_edge_case("what should i do?")["response"])

# ── 4. Real questions must still reach the pipeline ────────────────────────
# The capability patterns match loosely ("can you...", "do you have..."), so
# every shape that wraps a capability phrase around an ACTUAL request has to
# fall through untouched.
print("4. REAL QUESTIONS STILL ROUTE NORMALLY")
for q in ("can you show me PMAY-G houses completed by district",
          "could you give me the total expenditure for 2024-25",
          "will you be able to compare MGNREGA and PMAY-G spending",
          "can you list the top 5 districts by person-days",
          "do you have data on Focus Plus disbursements",
          "do you know how many CM Elevate applications are pending",
          "can i ask about eligibility for PMAY-G",
          "are you able to break it down by block",
          "can you tell me about MGNREGA",
          "can you explain PMAY-G",
          "who is eligible for PMAY-G?",
          "total MGNREGA expenditure in West Garo Hills"):
    check(f"{q[:50]!r} -> pipeline", kind(q) is None, kind(q))

# ── 5. Existing banks unaffected ───────────────────────────────────────────
print("5. EXISTING BANKS UNAFFECTED")
for q, want in (("hello", "greeting"), ("who are you?", "identity"),
                ("thanks", "thanks"), ("bye", "goodbye"),
                ("what is elon musk?", "off_topic"),
                ("weather in shillong", "off_topic"),
                ("PMAY-G houses in Assam", "off_topic"),
                ("tell me a joke", "silly")):
    check(f"{q[:34]!r} -> {want}", kind(q) == want, kind(q))

# ── 6. A capability question that NAMES one scheme stays on that scheme ────
# "can i get the response from for mgnrega data?" named MGNREGA, but the reply
# recited all four schemes and suggested PMAY-G / Focus Plus / CM Elevate
# starters — it read as if the question had not been listened to (reported
# 2026-09-17). One named scheme now narrows both the sentence and the chips.
print("6. CAPABILITY REPLY NARROWS TO A NAMED SCHEME")
_by_scheme = {
    "MGNREGA": "can i get the response from for mgnrega data?",
    "PMAY-G": "can you give me pmay-g data?",
    "Focus Plus": "do you know about focus plus?",
    "CM Elevate": "can i ask about cm elevate?",
}
for _scheme, _q in _by_scheme.items():
    _hit = edge.detect_edge_case(_q)
    check(f"{_scheme}: still a capability reply", _hit and _hit["type"] == "capability")
    if not _hit:
        continue
    _other = [s for s in _by_scheme if s != _scheme]
    check(f"  {_scheme}: the reply names it", _scheme in _hit["response"], _hit["response"][:70])
    check(f"  {_scheme}: and no other scheme",
          not any(o in _hit["response"] for o in _other), _hit["response"][:90])
    _chips = _hit.get("suggestions", [])
    check(f"  {_scheme}: every chip is about it",
          bool(_chips) and all(_scheme.split()[0].lower() in c.lower() for c in _chips), _chips)

# Naming none, or more than one, keeps the general four-scheme reply.
for _q in ("are you able to give answers?", "can you answer questions on mgnrega and pmay?"):
    _hit = edge.detect_edge_case(_q)
    check(f"{_q[:46]!r} keeps the general reply",
          all(s in _hit["response"] for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate")),
          _hit["response"][:70])
    check("  and the general starter chips",
          _hit.get("suggestions") == list(edge.STARTERS))


# ── 7. A refusal must answer the question it was asked ─────────────────────
# "will you answers for west bengal data?" got a statement of scope that never
# said "no" and never named West Bengal — it read as evasive rather than as an
# answer (reported 2026-09-17). A yes/no question now gets a yes/no answer, and
# an out-of-area question names the place it asked about.
print("7. REFUSALS ARE CONTEXTUAL")
_wb = edge.detect_edge_case("will you answers for west bengal data?")
check("an out-of-area yes/no question opens with 'No'",
      _wb["response"].lstrip().startswith("No —"), _wb["response"][:60])
check("  and names the place asked about", "West Bengal" in _wb["response"], _wb["response"][:80])
check("  and still says what IS covered",
      all(s in _wb["response"] for s in ("Meghalaya", "MGNREGA", "PMAY-G")))

for _q, _place in (("can you give assam data?", "Assam"),
                   ("do you cover delhi?", "Delhi"),
                   ("mgnrega data for kerala", "Kerala")):
    _r = edge.detect_edge_case(_q)["response"]
    check(f"{_q[:34]!r} names {_place}", _place in _r, _r[:70])

# A statement (not a question) says what is missing rather than "No".
check("a non-question out-of-area reply does not force a 'No'",
      edge.detect_edge_case("mgnrega data for kerala")["response"].lstrip().startswith("I don't hold"))

# "all India" is a coverage, not a state — it must not read as a missing place.
_ai = edge.detect_edge_case("all india mgnrega figures")["response"]
check("'all india' is phrased as a scope, not a place",
      "all-India figures" in _ai and "data for All India" not in _ai, _ai[:70])

# Acronym countries keep their spelling.
check("'usa' renders as 'the USA'",
      "the USA" in edge.detect_edge_case("what about usa schemes")["response"])

# Any other yes/no refusal answers as one too.
for _q in ("will you answer questions about cricket?",
           "can you tell me the weather?",
           "do you know about bitcoin?",
           "can you sing a song?"):
    _r = edge.detect_edge_case(_q)["response"]
    check(f"{_q[:40]!r} opens with 'No'", _r.lstrip().startswith("No —"), _r[:50])

# An off-topic SUBJECT beats the conversational "can you help me" opener —
# answering "yes, that's what I'm here for" to a flight booking would be wrong.
check("'can you help me book a flight?' is refused, not welcomed",
      kind("can you help me book a flight?") == "off_topic",
      kind("can you help me book a flight?"))
check("  and a bare 'can you help me' is still welcomed",
      kind("can you help me") in ("identity", "capability"))


# ── 8. A scheme we don't hold gets a real answer, not a dead end ───────────
# "what is PM kisan yojana?" got "I don't have information about that for
# MGNREGA, PMAY-G, Focus Plus or CM Elevate" — a flat dead end that never says
# the subject IS a scheme, just not one of ours, and offers nowhere to go
# (reported 2026-09-18). The unsupported-scheme check existed but sat inside
# _answer_data, so only DATA-shaped asks reached it; a "what is ..." question
# routed to KNOWLEDGE and missed it entirely.
print("8. OUT-OF-SCHEME QUESTIONS")
for _q in ("what is PM kisan yojana?", "what is ujjwala scheme?",
           "tell me about jal jeevan mission", "what is saubhagya scheme"):
    check(f"{_q[:44]!r} reaches the pipeline, not the bounce",
          kind(_q) is None, kind(_q))
check("  a named unsupported scheme is recognised",
      pl._unsupported_scheme_named("what is PM kisan yojana?") is not None)

_reply = pl._unsupported_scheme_clarification("what is PM kisan yojana?", "PM kisan").question
check("the reply names the scheme asked about", _reply.startswith("PM Kisan"), _reply[:50])
check("  and says plainly it is not covered", "isn't one of the schemes I cover" in _reply)
check("  and names all four that ARE covered",
      all(s in _reply for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate")))
check("  and is not the old dead-end wording",
      "I don't have information about that" not in _reply)

# A scheme named by name but absent from the unsupported catalogue must also
# not get the "which scheme does your question concern?" pause — the user was
# specific, not vague.
check("an unrecognised named scheme is not treated as vague",
      pl._names_unknown_scheme("what is amma yedi scheme?"))
for _vague in ("what is the scheme?", "what are the benefits of the scheme",
               "what documents are required for the scheme", "how do I apply?"):
    check(f"  but {_vague[:40]!r} still counts as vague",
          not pl._names_unknown_scheme(_vague))
check("and one of OUR schemes is never 'unknown'",
      not pl._names_unknown_scheme("what is MGNREGA?"))

# Genuine off-topic is still blocked by the whitelist.
for _q in ("what is elon musk?", "weather in shillong", "tell me a joke"):
    check(f"{_q[:34]!r} is still blocked", kind(_q) is not None, kind(_q))


# ── 9. Personal financial advice is declined, not routed ──────────────────
# "Where should I invest my money One lakh." got the "which scheme does your
# question concern?" picker (reported 2026-09-18). _SCHEME_STRONG counts a bare
# money unit — "lakh", "crore" — as scheme intent, so the question took the
# step-1 early exit straight to the pipeline before any off-topic bank ran.
print("9. PERSONAL FINANCIAL ADVICE")
for _q in ("Where should I invest my money One lakh.",
           "where should i invest 1 lakh",
           "best way to invest 50000 rupees",
           "how can I grow my money",
           "where to invest"):
    check(f"{_q[:44]!r} is declined", kind(_q) == "money_advice", kind(_q))
# Some phrasings ("where to put my savings") are already caught by the older
# off-topic banks — either refusal is correct, so assert only that they are
# declined rather than which bank claimed them.
check("'where to put my savings' is declined",
      kind("where to put my savings") in ("money_advice", "off_topic"),
      kind("where to put my savings"))

_ma = edge.detect_edge_case("Where should I invest my money One lakh.")["response"]
check("  the reply says it can't advise on investing",
      "can't advise on investing" in _ma, _ma[:70])
check("  and does not pretend to be a financial adviser",
      "not a financial adviser" in _ma)
check("  and still says what IS covered",
      all(s in _ma for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate")))
check("  and carries starter chips",
      len(edge.detect_edge_case("where to invest").get("suggestions", [])) > 0)

# The money UNITS are legitimate scheme vocabulary — every real question using
# them must still reach the pipeline untouched.
for _q in ("total MGNREGA expenditure in lakh",
           "how much in crore was released under PMAY-G",
           "total Focus Plus amount disbursed in lakh",
           "MGNREGA expenditure of 5 lakh in Ri Bhoi",
           "how much money was spent on MGNREGA in West Garo Hills",
           # Names a scheme outright — a scheme question however it is phrased.
           "can MGNREGA wages help me save money"):
    check(f"{_q[:48]!r} still routes normally", kind(_q) is None, kind(_q))


print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL CONVERSATIONAL-EDGE CHECKS PASSED")
