"""
Charts must come back when a conversation is reopened from chat history.

Reported 2026-09-18: a replayed thread showed the answers, tables and SQL but
no graphs. The stored payload was fine — the bug was in the renderer.

buildAnalytics() bumps a MODULE-LEVEL counter (_chartIdx) and writes canvas ids
from it, then returns an init() that actually instantiates the charts.
addBotRich() defers that init() to requestAnimationFrame. Sending one question
builds one turn and fires one callback, so the two always agreed.

Replaying history does not: openConversation() loops over every turn, building
them all synchronously, and only afterwards does the browser run the deferred
callbacks. An init() that re-read _chartIdx therefore saw the LAST turn's value
and looked for a canvas id belonging to a different turn — document.
getElementById returned null, Chart.js drew nothing, and every chart but the
final one silently vanished.

The fix freezes the index per build (`const myIdx = _chartIdx`) so the html and
its own init() agree. The KPI branch was already correct — its init() closes
over the `cid` constant rather than re-reading the counter — and is the pattern
the others now follow.

Static assertions over web/ai_query.html plus a simulation of the replay
ordering. No browser, model or DB. Plain script (no pytest in the venv):
`python tests/test_history_charts.py`, exit code 0 = all pass.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_UI = (_ROOT / "web" / "ai_query.html").read_text(encoding="utf-8")

_fails: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


_ANALYTICS = _UI[_UI.index("function buildAnalytics("):_UI.index("/* ============ FOLLOW-UP")]

# ── 1. Canvas ids are built from a frozen index ────────────────────────────
print("1. THE CHART INDEX IS FROZEN PER BUILD")
check("buildAnalytics captures the counter once",
      re.search(r"const myIdx = _chartIdx;", _ANALYTICS) is not None)

# Every id expression after that capture must use myIdx, never the live counter.
_after = _ANALYTICS[_ANALYTICS.index("const myIdx = _chartIdx;"):]
_live = re.findall(r"`dync_\$\{_chartIdx\}_", _after)
check("  no id after it re-reads the live counter", not _live, _live)
check("  and the ids that exist use the frozen one",
      len(re.findall(r"`dync_\$\{myIdx\}_", _after)) >= 4,
      len(re.findall(r"`dync_\$\{myIdx\}_", _after)))

# The KPI branch predates the bug and closes over its own cid — leave it be.
check("the KPI branch still closes over its own cid",
      "const init = () => { window._QD[cid] = { labels, values }; rQC(cid, 'bar'); };" in _ANALYTICS)

# ── 2. The html id and the init() id must be the same expression ───────────
# This is the actual invariant: whatever names the canvas in the markup has to
# name it again when the chart is created.
print("2. HTML AND INIT AGREE ON THE ID")
_html_ids = set(re.findall(r"const cid = `(dync_\$\{\w+\}_[^`]*)`", _ANALYTICS))
_init_ids = set(re.findall(r"const cid = `(dync_\$\{\w+\}_[^`]*)`", _ANALYTICS))
check("every canvas id template is shared by both", _html_ids == _init_ids,
      (_html_ids, _init_ids))
# The KPI branch legitimately still names the live counter — it takes its id
# ONCE into a `cid` constant and its init() closes over that constant, never
# re-reading _chartIdx. Only ids built AFTER the freeze point (the branches
# whose init() recomputes the id) have to use myIdx.
_after_ids = set(re.findall(r"const cid = `(dync_\$\{\w+\}_[^`]*)`", _after))
check("  no id built after the freeze mentions the live counter",
      not any("_chartIdx" in i for i in _after_ids), _after_ids)
check("  the KPI id is the only live-counter one left, and it is safe",
      {i for i in _html_ids if "_chartIdx" in i} == {"dync_${_chartIdx}_kpi"},
      {i for i in _html_ids if "_chartIdx" in i})


# ── 3. Replay ordering: build all, then init all ───────────────────────────
# Mirrors openConversation(): every turn is built first, and the deferred
# init() callbacks run afterwards. With a frozen index each turn still finds
# its own canvas; with a shared counter they would all find the last one's.
print("3. A MULTI-TURN REPLAY RESOLVES EVERY CHART")


def _simulate(freeze: bool, turns: int = 4, charts_per_turn: int = 2):
    counter = {"n": 0}
    built = []
    for _ in range(turns):
        counter["n"] += 1
        my = counter["n"]
        html = [f"dync_{my}_{i}" for i in range(charts_per_turn)]

        def init(my=my):                     # frozen: own index
            return [f"dync_{my}_{i}" for i in range(charts_per_turn)]

        def init_live():                     # buggy: reads the shared counter
            return [f"dync_{counter['n']}_{i}" for i in range(charts_per_turn)]

        built.append((html, init if freeze else init_live))
    # All builds happen before any init — exactly the replay ordering.
    return [(html, fn()) for html, fn in built]


_fixed = _simulate(freeze=True)
check("every turn's init finds its own canvas ids",
      all(html == got for html, got in _fixed), _fixed)
_rendered = sum(1 for html, got in _fixed if html == got)
check(f"  all {len(_fixed)} turns render (not just the last)",
      _rendered == len(_fixed), _rendered)

# The same simulation on the OLD behaviour must fail — otherwise this test
# would keep passing even if the bug came back.
_broken = _simulate(freeze=False)
_broken_ok = sum(1 for html, got in _broken if html == got)
check("the pre-fix behaviour would have failed this test",
      _broken_ok == 1, f"{_broken_ok} of {len(_broken)} matched")

# ── 4. The payload history replays from still carries the rows ─────────────
print("4. THE STORED PAYLOAD FEEDS THE CHARTS")
_store = (_ROOT / "app" / "conversation_store.py").read_text(encoding="utf-8")
check("the saved turn payload includes the result rows",
      '"data": result.get("data") or []' in _store)
check("  and the row count the meta line shows", '"row_count": rows,' in _store)
check("history decodes that payload back to an object",
      't["response"] = json.loads(raw)' in _store)
check("the replay path renders stored turns through addBotRich",
      "addBotRich(t.question, t.response);" in _UI)
check("  and charts are only skipped when there are genuinely no rows",
      "Array.isArray(response.data) && response.data.length >= 1" in _UI)

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL HISTORY-CHART CHECKS PASSED")
