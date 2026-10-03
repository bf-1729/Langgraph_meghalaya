"""
Two faults from one screenshot (2026-09-23).

FAULT 1 — a PRODUCER GROUP name was resolved as geography.
  "Is there any Producer Group named Nongstoin PG?" answered

      "Nongstoin" could mean either the block or a village with that name.
       Which did you mean?

  The user named a GROUP, not an area. Nongstoin is also a real block, which is
  why the geography branch grabbed it — Focus Legacy groups are routinely named
  after where they are. extract_entity_mentions' prompt already says "A PRODUCER
  GROUP name or id is NEVER a place", but that is a request to a model, not a
  constraint, and the model tagged it as a block anyway.
  (The honest answer is that no group matches: `pg_name ILIKE '%nongstoin%'`
  returns 0 rows.)

FAULT 2 — the NEXT question got the SAME stale prompt.
  "List top 5 PGs which has more than 10 of members." names no place at all, yet
  came back with the Nongstoin block-or-village pause. A clarification stores
  the question in session.pending_scope_q and the next turn is merged into it
  unless _reply_abandons_scope_pause() recognises a new question. That test only
  matched INTERROGATIVE openers ("how many", "what is the", "who is"), so an
  IMPERATIVE sailed through and was glued onto the stale stem. The 12-word
  escape did not help either — the reported question is 11 words.
"""
import pytest

from app import entity_resolver
from app.pipeline import _drop_producer_group_names, _reply_abandons_scope_pause


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    entity_resolver.load_all()


# ── fault 1: a group name is not a place ────────────────────────────────────
@pytest.mark.parametrize(
    "question,mentions",
    [
        ("Is there any Producer Group named Nongstoin PG?", {"block": "Nongstoin"}),
        ("Is there any Producer Group named as Sakania PG?", {"village": "Sakania"}),
        ("Find the PG called Mairang Producer Group", {"block": "Mairang"}),
        ("group named Umsaw", {"village": "Umsaw"}),
        ("Is there a producer group called Ri Bhoi PG?", {"district": "Ri Bhoi"}),
    ],
)
def test_a_name_introduced_as_a_group_is_not_geography(question, mentions):
    """Otherwise the block/village gate asks which AREA was meant, about a name
    the user offered as a GROUP."""
    assert _drop_producer_group_names(question, mentions) == {}


@pytest.mark.parametrize(
    "question,mentions",
    [
        ("How much was disbursed in Nongstoin?", {"block": "Nongstoin"}),
        ("Total amount for Mairang block", {"block": "Mairang"}),
        ("producer groups in Nongstoin", {"block": "Nongstoin"}),
        ("How many PGs are in Ri Bhoi?", {"district": "Ri Bhoi"}),
        ("villages in Umsaw", {"village": "Umsaw"}),
    ],
)
def test_real_geography_questions_are_untouched(question, mentions):
    """The guard is narrow by design: only a name the QUESTION labelled as a
    group is stripped. "disbursement in Nongstoin" is still a place."""
    assert _drop_producer_group_names(question, mentions) == mentions


def test_plural_mention_lists_are_filtered_too():
    out = _drop_producer_group_names(
        "Is there any Producer Group named Nongstoin PG?",
        {"blocks": ["Nongstoin"]},
    )
    assert "blocks" not in out


# ── fault 2: a fresh question must abandon the stale pause ──────────────────
@pytest.mark.parametrize(
    "reply",
    [
        "List top 5 PGs which has more than 10 of members.",   # the reported one
        "Total Focus Legacy amount disbursed by district",
        "Which banks handle Focus Legacy payments?",
        "Which products do Focus Legacy groups work on?",
        "How many producer groups are there?",
        "Show me payments by block",
        "Compare FY 2021-22 and FY 2024-25",
    ],
)
def test_a_new_question_abandons_the_pause(reply):
    """Merging it into the paused question re-answers the OLD question and the
    user sees the same prompt twice with no way forward."""
    assert _reply_abandons_scope_pause(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "West Garo Hills, 2023-24",     # the commonest scope reply shape
        "all of Meghalaya",
        "all of Meghalaya, all years",
        "Ri Bhoi",
        "East Khasi Hills",
        "Mairang",
        "2023-24",
        "FY 2021-22",
        "the block",
        "the village, not the block",
    ],
)
def test_a_genuine_scope_reply_still_merges(reply):
    """The regression risk on the other side: abandoning here throws away the
    question the user was answering. An earlier version of my fix did exactly
    that to "West Garo Hills, 2023-24" because it contains a year."""
    assert _reply_abandons_scope_pause(reply) is False


def test_place_plus_year_is_not_mistaken_for_a_metric_question():
    """The specific regression: _DATA_HINTS matches the year cue, so the metric
    test must run only AFTER place/time vocabulary is stripped."""
    assert _reply_abandons_scope_pause("West Garo Hills, 2023-24") is False
    assert _reply_abandons_scope_pause("Nongstoin, FY 2021-22") is False


def test_the_two_turn_sequence_from_the_screenshot():
    """Turn 1 pauses; turn 2 is a brand-new question. Turn 2 must NOT be merged
    into turn 1's pending question."""
    turn1 = "Is there any Producer Group named Nongstoin PG?"
    turn2 = "List top 5 PGs which has more than 10 of members."

    # turn 1: no geography mention survives, so no block/village gate fires
    assert _drop_producer_group_names(turn1, {"block": "Nongstoin"}) == {}
    # turn 2: abandons the pause rather than inheriting it
    assert _reply_abandons_scope_pause(turn2) is True


def test_known_place_names_are_available_for_stripping():
    """The metric test strips real place names before looking for a metric; if
    the catalogue is empty the test silently gets weaker."""
    from app.pipeline import _known_place_names

    names = _known_place_names()
    assert names, "no place names loaded — scope replies naming a place may misroute"
    assert any("garo" in n for n in names)
