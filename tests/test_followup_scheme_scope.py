"""
Follow-ups that change WHICH scheme the previous question is about:
  * "for focus"                        -> same question, Focus; a bare "Focus" names
                                          two schemes, so the "which Focus?" pause asks
  * "same for the remaining schemes"   -> same question, every scheme NOT asked yet
  * "same for all schemes"             -> same question, every scheme

Reported 2026-09-24: after "how to apply for cm elevate", "for focus" and "give
same like for remaining schemes" were both re-answered as CM Elevate — the first
because the knowledge route inherited the previous turn's scheme before the
"which Focus?" check, the second because the LLM rewrite read "remaining
schemes" as CM Elevate's own sub-schemes. Pure Python — no model, DB or KB.
    python -m pytest tests/test_followup_scheme_scope.py -q
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm, pipeline as p, rag  # noqa: E402
from app.session_store import Session, Turn  # noqa: E402


class _Prev:
    def __init__(self, question, route="knowledge"):
        self.question, self.route = question, route


def test_bare_focus_swap_keeps_the_word_focus():
    assert p.looks_like_followup("for focus")
    assert p._scheme_swap_rewrite("how to apply for cm elevate", "for focus") == \
        "how to apply for Focus"
    assert p._scheme_swap_rewrite("how to apply for cm elevate", "for focus legacy") == \
        "how to apply for Focus Legacy"
    # existing swaps unchanged
    assert p._scheme_swap_rewrite("who is eligible for PMAY-G", "for focus plus") == \
        "who is eligible for Focus Plus"


def test_remaining_schemes_excludes_the_previous_one_and_its_shared_kb_twin():
    out = p._rest_of_schemes_rewrite(_Prev("how to apply for cm elevate"),
                                     "give same like for remaining schemes")
    assert out == "how to apply for MGNREGA, PMAY-G, Focus Plus and Focus Legacy"


def test_remaining_schemes_on_a_data_turn_keeps_cm_elevate_legacy():
    out = p._rest_of_schemes_rewrite(_Prev("total amount disbursed under MGNREGA", "data"),
                                     "same for other schemes")
    assert "CM Elevate Legacy" in out and "MGNREGA" not in out


def test_all_schemes_lists_every_scheme_once_and_keeps_names_intact():
    out = p._rest_of_schemes_rewrite(_Prev("how to apply for cm elevate"), "same for all schemes")
    assert out == "how to apply for MGNREGA, PMAY-G, Focus Plus, CM Elevate and Focus Legacy"


@pytest.mark.parametrize("followup", ["what about Focus Plus", "for 2024-25", "same for Ri Bhoi"])
def test_other_followups_are_left_to_the_existing_paths(followup):
    assert p._rest_of_schemes_rewrite(_Prev("how to apply for cm elevate"), followup) is None


def test_the_reported_conversation(monkeypatch):
    async def classifier(prompt, guided=None):
        if '"intent"' in prompt:
            return '{"intent": "KNOWLEDGE"}'
        return "how to apply for the remaining CM Elevate schemes"   # the bad LLM rewrite

    async def one(q, scheme=None):
        return {"answer": f"KB:{scheme}", "confidence": "medium", "sources": []}

    async def multi(q, schemes):
        return {"answer": f"KB:{','.join(schemes)}", "confidence": "medium", "sources": []}

    monkeypatch.setattr(llm, "call_classifier", classifier)
    monkeypatch.setattr(rag, "answer_from_kb", one)
    monkeypatch.setattr(rag, "answer_from_kb_multi", multi)
    monkeypatch.setattr(p.settings, "CONTEXT_LAYER_ENABLED", False)
    sess = Session(session_id="t", user_id="u", created=time.time(), last_seen=time.time())

    def ask(q):
        r = asyncio.run(p._run_pipeline(q, session=sess))
        sess.turns.append(Turn(question=r.get("rewritten_question") or q, raw_question=q,
                               route=r["route"], schemes=r.get("schemes", []), answer=r["answer"]))
        return r

    assert ask("how to apply for cm elevate")["answer"] == "KB:CM Elevate"
    with pytest.raises(p.ClarificationNeeded) as pause:      # a pause records no turn
        asyncio.run(p._run_pipeline("for focus", session=sess))
    assert pause.value.rule == "focus-scheme-ambiguous"
    r = ask("give same like for remaining schemes")
    assert r["answer"] == "KB:MGNREGA,PMAY-G,Focus Plus,Focus Legacy"
