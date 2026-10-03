"""
"Pick any scheme and explain it" — the user hands the CHOICE of scheme to the
bot. Reported 2026-09-24: "pick any scheme out of these and explain clearly with
key points" got the "which scheme?" pause (asking the user for the very choice
they delegated), and "no, pick your self any scheme and explain" was rewritten
against the previous scheme and answered "not covered". The bot now picks — a
named scheme if there is one, else one this conversation has not covered — and
explains it from that scheme's reference docs. Pure Python; no model, DB or KB.
    python -m pytest tests/test_scheme_pick.py -q
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm, pipeline as p, rag  # noqa: E402
from app.session_store import Session, Turn  # noqa: E402

PICK = [
    "pick any scheme out of these and explain clearly with key points",
    "no, pick your self any scheme and explain",
    "pick any MGNREGA out of these and explain clearly with key points",
    "choose one of these and explain", "you choose a scheme and tell me about it",
    "explain any one scheme", "tell me about any scheme", "any one of them, explain",
    "select one scheme and describe it", "take a random scheme and explain",
    "your choice, explain one", "up to you, pick one", "surprise me with a scheme",
    "explain a scheme of your choice", "pick another scheme",
    "give me key points of any one scheme", "which one would you pick? explain it",
    "describe any programme", "pick one yourself", "just pick any",
    "choose a scheme for me and explain", "explain whichever scheme you like",
    "can you explain any one of these schemes?", "pick any one", "pick another one",
]
NOT_PICK = [
    "which scheme has the most applications", "tell me about MGNREGA", "what is CM Elevate",
    "pick top 5 districts by expenditure", "pick any district and show MGNREGA expenditure",
    "choose a district", "explain the scheme", "is there any scheme that can help my family?",
    "which scheme should I apply for?", "how many schemes are there", "what schemes do you have",
    "total amount disbursed under any scheme in Ri Bhoi", "list all schemes", "explain PMAY-G",
    "which scheme can help me build a house", "any scheme for farmers?",
    "pick any village in Ri Bhoi", "show any scheme data",
]


@pytest.mark.parametrize("q", PICK)
def test_delegation_phrasings_are_recognised(q):
    assert p._is_scheme_pick_request(q)
    assert p.looks_like_followup(q)          # never served from the shared cache


@pytest.mark.parametrize("q", NOT_PICK)
def test_other_questions_are_left_alone(q):
    assert not p._is_scheme_pick_request(q)


def test_cm_elevate_legacy_is_not_offered_as_a_separate_programme():
    assert "CM Elevate Legacy" not in p._pickable_schemes()
    assert p._pick_scheme("explain any CM Elevate Legacy scheme", None) == ("CM Elevate", False)


def _conversation(monkeypatch):
    async def classifier(prompt, guided=None):
        if '"intent"' in prompt:
            return '{"intent": "KNOWLEDGE"}'
        return "UNUSED REWRITE"

    async def kb(q, scheme=None):
        return {"answer": f"- key points from {scheme}", "confidence": "medium", "sources": []}

    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(rag, "answer_from_kb", kb)
    monkeypatch.setattr(p.settings, "CONTEXT_LAYER_ENABLED", False)
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())

    def ask(q):
        r = asyncio.run(p._run_pipeline(q, session=sess))
        sess.turns.append(Turn(question=r.get("rewritten_question") or q, raw_question=q,
                               route=r["route"], schemes=r.get("schemes", []), answer=r["answer"]))
        return r
    return ask


def test_the_reported_conversation(monkeypatch):
    ask = _conversation(monkeypatch)
    r = ask("pick any scheme out of these and explain clearly with key points")
    assert r["schemes"] == ["MGNREGA"] and r["answer"].startswith("I'll pick **MGNREGA**")
    r = ask("pick any MGNREGA out of these and explain clearly with key points")
    assert r["schemes"] == ["MGNREGA"] and r["answer"].startswith("Here are the key points of **MGNREGA**")
    r = ask("no, pick your self any scheme and explain")
    assert r["schemes"] == ["PMAY-G"]                        # moves on, no repeat
    assert "key points from PMAY-G" in r["answer"]


def test_it_keeps_moving_on_and_continuations_follow_a_pick(monkeypatch):
    ask = _conversation(monkeypatch)
    order = [ask(q)["schemes"][0] for q in
             ("pick any scheme", "pick another one", "one more", "next one", "another one")]
    assert order == p._pickable_schemes()                    # every programme once, in order
    assert ask("one more")["schemes"] == ["MGNREGA"]         # then cycles
