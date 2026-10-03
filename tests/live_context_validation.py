"""
LIVE multi-turn context validation against the real model gateway and megh_db.

Not collected by pytest (not named test_*.py). The offline counterparts are
tests/test_context_semantic_state.py and tests/test_context_hardening.py.
Needs the office VPN: the gateway and the DB are on 10.48.242.4 (Qdrant is on 115.124.102.167:6335).

    .venv/Scripts/python.exe tests/live_context_validation.py
    .venv/Scripts/python.exe tests/live_context_validation.py --legacy       # pre-2026-09-26 rewrite, for A/B
    .venv/Scripts/python.exe tests/live_context_validation.py --only A,E --no-db-sync
    .venv/Scripts/python.exe tests/live_context_validation.py --out logs/live_ctx.json

What it does, per scenario (A-G of the 2026-09-26 hardening brief):
  * runs every turn through pipeline.answer_question in-process, with the
    router's own helpers (routers.query.is_cacheable / remember_pause /
    build_turn), so the logic is the product's, not a copy;
  * unless --no-db-sync, rotates the turns across THREE simulated workers
    (independent SessionStores) that share state only through the real
    Postgres snapshot (session_sync with PostgresBackend). That is the KI-028
    path end to end. Its rows are written under session ids 'live-ctx-...' in
    app.conversations and deleted at the end;
  * answers an UNEXPECTED clarification the way a user would click it: the
    option labelled all / combined / overall, else the first one (TESTING.md);
  * records, per turn: the rewritten question, the resolved entities, the SQL,
    the row count, the answer, the structured state and its provenance, the
    merge plan and follow-up kind, the context tiers the rewrite received, and
    every model call (role, gateway prompt/completion tokens, latency);
  * checks the expected behaviour and prints PASS/FAIL per check, plus
    per-role token and latency figures (p50/p95).

Time to first token: generation is not streamed in this app, so TTFT is not
observable separately and is reported as equal to the call latency.

Exit code: 0 all checks passed, 1 a check failed, 3 not run (gateway or DB
unreachable).
"""
import argparse
import asyncio
import json
import logging
import re
import socket
import statistics
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import context_policy, llm, pipeline, session_sync  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import ClarificationNeeded  # noqa: E402
from app.routers import query as router  # noqa: E402
from app.session_store import SessionStore  # noqa: E402

# Focus Plus holds FY 2022-23 and FY 2025-26 only (scheme years loaded from megh_db,
# 2026-09-26). The first live run asked about FY 2024-25, which correctly paused as
# out of range, and every scenario built on it was testing the wrong thing.
Q_FP = "How much was disbursed under Focus Plus in West Garo Hills in FY 2025-26?"
Q_MG = "How many person-days were generated under MGNREGA in West Garo Hills in FY 2024-25?"
ANSWER_G = ("Across Focus Plus, MGNREGA and PMAY-G, East Khasi Hills, Ri Bhoi and Jaintia Hills "
            "led; blocks Rongram, Tura and Selsella were next; villages Nongthymmai, Maska and "
            "Adugre reported most; FY 2019-20 and FY 2021-22 were the peak years; women were 54% "
            "and SC/ST 30%; Piggery and Poultry applicants dominated; person-days reached 1.2 "
            "lakh for 4,500 beneficiaries.")
CONTAMINANTS = ["east khasi", "ri bhoi", "jaintia", "rongram", "tura", "selsella", "nongthymmai",
                "maska", "adugre", "2019", "2021", "women", "female", "piggery", "poultry",
                "mgnrega", "pmay", "person-days", "person_days"]

SCENARIOS = {
    "A": ("Geography continuation",
          [Q_FP, "What about Dalu block?", "How many beneficiaries were there?"]),
    "B": ("Time continuation (Focus Plus: previous year is not held)", [Q_FP, "What about last year?"]),
    "B2": ("Time continuation (MGNREGA: previous year is held)", [Q_MG, "What about last year?"]),
    "C": ("Aggregation continuation",
          ["How much was disbursed under Focus Plus in West Garo Hills?", "Show it by district."]),
    "D": ("Result reference",
          ["Show the districts with beneficiaries under Focus Plus.", "Which one had the highest?",
           "How much was it?"]),
    "E": ("Clarification resume", ["Show beneficiaries.", "Focus Plus"]),
    "F": ("Scheme switching",
          [Q_FP,
           "How many person-days were generated under MGNREGA in West Garo Hills in FY 2024-25?",
           "What about Dalu block?", "What about Focus Legacy?",
           # No FY: a financial-year word pins a bare "CM Elevate" to CM Elevate
           # Legacy by design (D-009), which is not what this step tests.
           "How many applications were received under CM Elevate?"]),
    "G": ("Contamination", [Q_FP, "What about Dalu block?", "How many beneficiaries were there?"]),
}

_AUTO_CHIP = re.compile(r"\ball\b|combined|overall|every", re.IGNORECASE)


# ── Reachability ────────────────────────────────────────────────────────────
def _reachable(url_or_host: str, port: int | None = None, timeout: float = 3.0) -> bool:
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    port = port or (urlparse(url_or_host).port or 443)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Legacy mode (the implementation before 2026-09-26), for A/B ─────────────
def _install_legacy():
    def legacy_prompt(question, prev, extra_context="", kind=None, cleared=None):
        prompt = (
            "Rewrite the FOLLOW-UP as a complete, standalone question by reusing "
            "context from the PREVIOUS question. Keep the user's intent; change only "
            "what the follow-up changes (e.g. a different district, year, or metric). "
            "Do not introduce any district, year, tranche, scheme, category or other "
            "filter that is not present in the PREVIOUS question, the PREVIOUS answer, "
            "the Known context below, or the FOLLOW-UP itself — when in doubt, leave it "
            "out rather than guess one. "
            "Return ONLY the rewritten question, nothing else.\n\n"
            + (f"{extra_context}\n\n" if extra_context else "")
            + f'PREVIOUS question: "{prev.question}"\n'
            f'PREVIOUS answer (for context): "{(prev.answer or "")[:300]}"\n'
            f'FOLLOW-UP: "{question}"\n\n'
            "Standalone question:")
        return [("legacy", prompt)], ["legacy_answer_slice"], {
            "question": question, "allowed_text": "", "allowed_schemes": [], "denied_text": ""}

    # Everything the 2026-09-26 context work added to the rewrite path is switched
    # off: the evidence tiers and removed-filter line (legacy prompt above), the
    # provenance checks, the merge plan's pruning of inherited filters, and the
    # scope-less follow-up detection. Shared state (session_sync) stays on, so
    # the A/B isolates the context logic from the worker-routing fix.
    pipeline._build_rewrite_prompt = legacy_prompt
    pipeline._rewrite_provenance_violation = lambda *a, **k: None
    pipeline.is_scopeless_followup = lambda *a, **k: False
    context_policy.apply_merge_plan = lambda prior, plan: dict(prior or {})


# ── Capture of prompt_context records and timed stages ─────────────────────
class _PromptCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records: list[dict] = []

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("prompt_context "):
            try:
                self.records.append(json.loads(msg[len("prompt_context "):]))
            except ValueError:
                pass


_stage_times: list[dict] = []


def _time_stage(obj, name, label):
    orig = getattr(obj, name)

    async def wrapped(*a, **k):
        t = time.perf_counter()
        try:
            return await orig(*a, **k)
        finally:
            _stage_times.append({"stage": label, "ms": round((time.perf_counter() - t) * 1000, 1)})
    setattr(obj, name, wrapped)


# ── One conversation ────────────────────────────────────────────────────────
class Conversation:
    def __init__(self, sid: str, workers: int, db_sync: bool, tenant_id: int):
        self.sid, self.db_sync, self.tenant_id = sid, db_sync, tenant_id
        self.stores = [SessionStore(ttl=1800, max_sessions=10, max_turns=8)
                       for _ in range(workers if db_sync else 1)]
        self.turn_no = 0
        self.sync_log: list[str] = []
        self.sync_ms: list[tuple[str, float]] = []

    async def ask(self, question: str, capture: _PromptCapture, *, auto_click=True,
                  seed_answer: str | None = None) -> dict:
        store = self.stores[self.turn_no % len(self.stores)]
        self.turn_no += 1
        session = store.ensure(self.sid, "live-harness")
        if self.db_sync:
            t_sync = time.perf_counter()
            self.sync_log.append(await session_sync.sync_in(session, self.sid))
            self.sync_ms.append(("in", round((time.perf_counter() - t_sync) * 1000, 1)))
        if seed_answer is not None and session.last_turn is not None:
            session.last_turn.answer = seed_answer       # Scenario G: contaminate the antecedent
        rec = {"worker": self.stores.index(store), "question": question, "chain": []}
        q = question
        for _hop in range(3):
            calls: list = []
            token = llm.llm_calls_var.set(calls)
            n_prompts = len(capture.records)
            stage_start = len(_stage_times)
            t0 = time.perf_counter()
            step = {"asked": q, "cacheable": router.is_cacheable(q, session)}
            try:
                result = await pipeline.answer_question(q, session=session, scope=None)
                session.turns.append(router.build_turn(q, result))
                step.update(kind="answer", result=result)
            except ClarificationNeeded as e:
                router.remember_pause(session, q, e)
                step.update(kind="clarification", rule=e.rule,
                            options=[o.get("label") for o in (e.options or [])], result=None,
                            clar_options=e.options)
            finally:
                llm.llm_calls_var.reset(token)
            step["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            step["llm_calls"] = calls
            step["prompts"] = capture.records[n_prompts:]
            step["stages"] = _stage_times[stage_start:]
            step["plan"] = dict(session.turn_context.get("plan") or {})
            step["substituted"] = session.turn_context.get("substituted_question")
            step["state"] = {k: getattr(session.state, k) for k in
                             ("scheme", "district", "block", "village", "year", "metric", "tranche")}
            step["provenance"] = dict(session.state.provenance)
            rec["chain"].append(step)
            if step["kind"] == "clarification" and auto_click and step.get("clar_options"):
                opts = step["clar_options"]
                chip = next((o for o in opts if _AUTO_CHIP.search(o.get("label", ""))), opts[0])
                step["auto_clicked"] = chip.get("label")
                q = chip.get("question") or chip.get("label")
                continue
            break
        if self.db_sync:
            t_sync = time.perf_counter()
            ok = await session_sync.sync_out(session, self.sid, tenant_id=self.tenant_id,
                                             user_id=None, title=question)
            self.sync_ms.append(("out", round((time.perf_counter() - t_sync) * 1000, 1)))
            self.sync_log.append("saved" if ok else "save-failed")
        final = rec["chain"][-1]
        res = final.get("result") or {}
        rec.update(
            final_kind=final["kind"], rule=final.get("rule"),
            rewritten=res.get("rewritten_question"), route=res.get("route"),
            schemes=res.get("schemes"), resolved=res.get("resolved_entities"),
            sql=res.get("sql"), row_count=res.get("row_count"),
            rows_head=(res.get("rows") or res.get("data") or [])[:3],
            answer=(res.get("answer") or final.get("rule") or "")[:400],
            state=final["state"], provenance=final["provenance"], plan=final["plan"],
            rewrite_tiers=[p_.get("tiers") for p_ in final["prompts"] if p_.get("kind") == "rewrite"],
            rewrite_guard=[p_.get("guard") for p_ in final["prompts"] if p_.get("kind") == "rewrite"],
            cacheable_first=rec["chain"][0]["cacheable"])
        return rec


# ── Expectations ───────────────────────────────────────────────────────────
def _sql(t):
    return (t.get("sql") or "").upper()


def _has(t, *needles):
    s = _sql(t)
    return all(n.upper() in s for n in needles)


def _no(t, *needles):
    s = _sql(t) + " " + (t.get("rewritten") or "").upper()
    return [n for n in needles if n.upper() in s]


def _year_in_sql(t, y):
    return str(y) in _sql(t)


def check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": str(detail)[:300]}


def expectations(key, turns):
    c = []
    if key in ("A", "B", "F", "G"):
        t1 = turns[0]
        c.append(check("T1 Focus Plus, West Garo Hills, FY 2025-26",
                       t1["schemes"] == ["Focus Plus"] and _has(t1, "WEST GARO HILLS")
                       and _year_in_sql(t1, 2025), t1["sql"]))
    if key == "A":
        t2, t3 = turns[1], turns[2]
        c += [check("T2 geography changes to Dalu block", _has(t2, "DALU"), t2["sql"]),
              check("T2 scheme and year kept", t2["schemes"] == ["Focus Plus"]
                    and _year_in_sql(t2, 2025), t2["sql"]),
              check("T2 metric kept (money)", re.search(r"SUM\(", _sql(t2)), t2["sql"]),
              check("T2 kind GEOGRAPHY_CHANGE", t2["plan"].get("kind") == "GEOGRAPHY_CHANGE",
                    t2["plan"]),
              check("T3 metric changes to a count", re.search(r"COUNT\(", _sql(t3)), t3["sql"]),
              check("T3 scheme, Dalu and year kept", t3["schemes"] == ["Focus Plus"]
                    and _has(t3, "DALU") and _year_in_sql(t3, 2025), t3["sql"]),
              check("T3 kind METRIC_CHANGE, filters only", t3["plan"].get("kind") == "METRIC_CHANGE"
                    and all("result" not in (x or []) for x in t3["rewrite_tiers"]),
                    [t3["plan"], t3["rewrite_tiers"]])]
    if key == "B":
        t2 = turns[1]
        first = t2["chain"][0]
        c += [check("T2 'last year' resolved to FY 2024-25", "2024-25" in (first.get("substituted") or ""),
                    first.get("substituted")),
              check("T2 honestly paused: FY 2024-25 is not held for Focus Plus",
                    first.get("rule") == "year-out-of-range" or (_year_in_sql(t2, 2024)
                                                                 and not _year_in_sql(t2, 2025)),
                    [first.get("rule"), t2["sql"]])]
    if key == "B2":
        t1, t2 = turns[0], turns[1]
        c += [check("T1 MGNREGA, West Garo Hills, FY 2024-25", t1["schemes"] == ["MGNREGA"]
                    and _has(t1, "WEST GARO HILLS") and _year_in_sql(t1, 2024), t1["sql"]),
              check("T2 year changes to FY 2023-24", _year_in_sql(t2, 2023)
                    and not _year_in_sql(t2, 2024), t2["sql"]),
              check("T2 scheme/district/metric kept", t2["schemes"] == ["MGNREGA"]
                    and _has(t2, "WEST GARO HILLS") and "PERSON_DAYS" in _sql(t2), t2["sql"]),
              check("T2 year provenance is resolved_reference or current_user",
                    (t2["provenance"].get("year") or {}).get("source")
                    in ("resolved_reference", "current_user"), t2["provenance"].get("year"))]
    if key == "C":
        t2 = turns[1]
        c += [check("T2 grouped by district", "GROUP BY" in _sql(t2) and "DISTRICT" in _sql(t2),
                    t2["sql"]),
              check("T2 district filter dropped", "= 'WEST GARO HILLS'" not in _sql(t2), t2["sql"]),
              check("T2 scheme kept", t2["schemes"] == ["Focus Plus"], t2["schemes"])]
    if key == "D":
        t2, t3 = turns[1], turns[2]
        top = next((str(v) for v in (t2["rows_head"][0].values() if t2["rows_head"] else [])
                    if isinstance(v, str)), None)
        c += [check("T2 ranks (ORDER BY ... DESC / MAX)",
                    re.search(r"ORDER BY .* DESC|MAX\(", _sql(t2)), t2["sql"]),
              check("T2 not given the result rows", all("result" not in (x or [])
                                                        for x in t2["rewrite_tiers"]),
                    t2["rewrite_tiers"]),
              check("T3 given the result rows (RESULT_REFERENCE)",
                    t3["plan"].get("kind") == "RESULT_REFERENCE"
                    and any("result" in (x or []) for x in t3["rewrite_tiers"]),
                    [t3["plan"], t3["rewrite_tiers"]]),
              check("T3 filters on T2's top district", top is not None
                    and top.upper() in _sql(t3), [top, t3["sql"]])]
    if key == "E":
        t1, t2 = turns[0], turns[1]
        c += [check("T1 asks which scheme", t1["chain"][0].get("rule") == "scheme-not-specified",
                    t1["chain"][0].get("rule")),
              check("T2 reply not cacheable", t2["cacheable_first"] is False, t2["cacheable_first"]),
              check("T2 resumes the paused question",
                    "BENEFICIAR" in ((t2["chain"][0].get("result") or {}).get("rewritten_question")
                                     or t2["rewritten"] or "").upper()
                    or t2["chain"][0].get("kind") == "clarification",
                    [t2["rewritten"], t2["chain"][0].get("rule")]),
              check("T2 answers for Focus Plus", t2["schemes"] == ["Focus Plus"], t2["schemes"])]
    if key == "F":
        t2, t3, t4, t5 = turns[1:5]
        c += [check("T2 switches to MGNREGA", t2["schemes"] == ["MGNREGA"]
                    and "FOCUS_PLUS" not in _sql(t2), t2["sql"]),
              check("T3 continues MGNREGA, not Focus Plus", t3["schemes"] == ["MGNREGA"]
                    and "FOCUS_PLUS" not in _sql(t3) and _has(t3, "DALU"), t3["sql"]),
              check("T4 switches to Focus Legacy only", t4["schemes"] == ["Focus Legacy"]
                    and "V_EMPLOYMENT" not in _sql(t4), t4["sql"]),
              check("T5 CM Elevate applications, not the Legacy dataset",
                    t5["schemes"] == ["CM Elevate"] and "DISBURSEMENT" not in _sql(t5), t5["sql"])]
    if key == "G":
        for i, t in enumerate(turns[1:], start=2):
            leaked = _no(t, *CONTAMINANTS)
            c.append(check(f"T{i} no entity from the contaminated answer", not leaked, leaked))
        c.append(check("T2 Dalu applied", _has(turns[1], "DALU"), turns[1]["sql"]))
    return c


# ── Metrics ────────────────────────────────────────────────────────────────
def _pct(values, q):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return values[k]


def summarize(all_turns):
    calls = [c for t in all_turns for s in t["chain"] for c in s["llm_calls"]]
    by_role: dict = {}
    for c in calls:
        r = by_role.setdefault(c["role"], {"calls": 0, "prompt_tokens": [], "completion_tokens": [],
                                            "latency_ms": []})
        r["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "latency_ms"):
            if c.get(k) is not None:
                r[k].append(c[k])
    roles = {role: {"calls": r["calls"],
                    "input_tokens_total": sum(r["prompt_tokens"]) if r["prompt_tokens"] else None,
                    "input_tokens_mean": round(statistics.mean(r["prompt_tokens"]), 1)
                    if r["prompt_tokens"] else None,
                    "output_tokens_total": sum(r["completion_tokens"]) if r["completion_tokens"] else None,
                    "latency_p50_ms": _pct(r["latency_ms"], 0.5),
                    "latency_p95_ms": _pct(r["latency_ms"], 0.95),
                    "ttft": "n/a: not streamed; equals latency"}
             for role, r in by_role.items()}
    stages = [s for t in all_turns for st in t["chain"] for s in st["stages"]]
    stage_lat = {}
    for label in ("rewrite", "sql_generation"):
        v = [s["ms"] for s in stages if s["stage"] == label]
        stage_lat[label] = {"n": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95)}
    turn_lat = [s["latency_ms"] for t in all_turns for s in t["chain"]]
    data_turns = [t for t in all_turns if t["final_kind"] == "answer" and t["route"] == "data"]
    sql_ok = [t for t in data_turns if t["sql"] and "couldn't build a working query" not in t["answer"]]
    return {"llm_by_role": roles, "stages": stage_lat,
            "rewrite_prompt_tokens_approx": _rewrite_tokens(all_turns),
            "turn_latency_ms": {"p50": _pct(turn_lat, 0.5), "p95": _pct(turn_lat, 0.95)},
            "sql_success": f"{len(sql_ok)}/{len(data_turns)}"}


# ── Main ───────────────────────────────────────────────────────────────────
async def main(args) -> int:
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "legacy" if args.legacy else "semantic",
              "models": {"classifier/rewrite": settings.CLASSIFIER_MODEL,
                         "sql": settings.SQL_GENERATION_MODEL, "verifier": settings.SQL_VERIFY_MODEL,
                         "composer": settings.RESPONSE_MODEL},
              "db_sync": not args.no_db_sync}
    gw_ok = _reachable(settings.SQL_GENERATION_BASE_URL)
    _db = urlparse(settings.DATABASE_URL) if settings.DATABASE_URL else None
    db_ok = bool(_db and _db.hostname) and _reachable(_db.hostname, _db.port or 5432)
    if not (gw_ok and db_ok):
        report["status"] = "not_run"
        report["reason"] = f"unreachable: gateway={gw_ok} db={db_ok} (VPN off?)"
        print(f"SKIPPED - {report['reason']}")
        _write(args.out, report)
        return 3

    if args.legacy:
        _install_legacy()
    _time_stage(pipeline, "rewrite_followup", "rewrite")
    _time_stage(pipeline, "generate_sql", "sql_generation")
    capture = _PromptCapture()
    logging.getLogger("app.context_budget").addHandler(capture)
    logging.getLogger("app.context_budget").setLevel(logging.INFO)

    from app import db, main as app_main
    sids: list[str] = []
    results, all_checks = {}, []
    keys = [k.strip().upper() for k in args.only.split(",")] if args.only else list(SCENARIOS)
    async with app_main.lifespan(app_main.app):
        try:
            for key in keys:
                title, qs = SCENARIOS[key]
                sid = f"live-ctx-{key}-{uuid.uuid4().hex[:8]}"
                sids.append(sid)
                conv = Conversation(sid, 3, not args.no_db_sync, args.tenant_id)
                turns = []
                for i, q in enumerate(qs):
                    turns.append(await conv.ask(
                        q, capture, auto_click=not ((key == "E" and i == 0) or (key == "B" and i == 1)),
                        seed_answer=ANSWER_G if (key == "G" and i == 1) else None))
                checks = expectations(key, turns)
                if not args.no_db_sync:
                    checks.append(check("state crossed workers (every sync_in after turn 1 applied)",
                                        all(s == "applied" for s in conv.sync_log[2::2]),
                                        conv.sync_log))
                results[key] = {"title": title, "turns": turns, "checks": checks,
                                "sync": conv.sync_log, "sync_ms": conv.sync_ms}
                all_checks += [(key, c) for c in checks]
                print(f"\n== {key}. {title}")
                for t in turns:
                    print(f"  [{t['worker']}] {t['question']}\n      -> {t['rewritten'] or '(unchanged)'}"
                          f" | kind={t['plan'].get('kind')} | tiers={t['rewrite_tiers']}"
                          f" | guard={t['rewrite_guard']}\n      SQL: {(t['sql'] or t['rule'] or '')[:220]}")
                for c in checks:
                    print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}"
                          + ("" if c["ok"] else f"  :: {c['detail']}"))
        finally:
            if sids and not args.no_db_sync:
                try:
                    await db.execute("DELETE FROM app.conversations WHERE session_id = ANY($1::text[])",
                                     [sids])
                except Exception as e:  # noqa: BLE001
                    print(f"cleanup of {sids} failed: {e}")
    all_turns = [t for r in results.values() for t in r["turns"]]
    report.update(status="run", scenarios=results,
                  metrics={**summarize(all_turns), **_sync_latency(results)},
                  checks_passed=sum(c["ok"] for _, c in all_checks), checks_total=len(all_checks),
                  followup_resolution_accuracy=_followup_accuracy(results))
    print("\nMETRICS", json.dumps(report["metrics"], indent=1))
    print(f"CHECKS {report['checks_passed']}/{report['checks_total']}  "
          f"follow-up resolution {report['followup_resolution_accuracy']}")
    _write(args.out, report)
    return 0 if report["checks_passed"] == report["checks_total"] else 1


def _rewrite_tokens(all_turns):
    v = [pr["total_tokens"] for t in all_turns for st in t["chain"] for pr in st["prompts"]
         if pr.get("kind") == "rewrite"]
    return {"n": len(v), "mean": round(statistics.mean(v), 1) if v else None, "max": max(v) if v else None}


def _sync_latency(results):
    out = {}
    for d in ("in", "out"):
        v = [ms for r in results.values() for (k, ms) in r.get("sync_ms", []) if k == d]
        out[f"sync_{d}"] = {"n": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95)}
    return out


def _followup_accuracy(results):
    fu = [c for r in results.values() for c in r["checks"]
          if re.match(r"T[2-9]", c["check"])]
    return f"{sum(c['ok'] for c in fu)}/{len(fu)}"


def _write(path, report):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"report written to {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--legacy", action="store_true", help="pre-2026-09-26 rewrite (answer[:300]) for A/B")
    ap.add_argument("--only", default="", help="comma-separated scenario keys, e.g. A,E")
    ap.add_argument("--no-db-sync", action="store_true", help="single worker, no app.conversations writes")
    ap.add_argument("--tenant-id", type=int, default=1, help="tenant_id for the temporary rows")
    ap.add_argument("--out", default="logs/live_context_validation.json")
    sys.exit(asyncio.run(main(ap.parse_args())))
