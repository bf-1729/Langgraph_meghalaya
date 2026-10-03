"""
Loads the SME-curated annotation layer from data/ at startup — few-shot
question-to-SQL examples and the foreign-key/join-graph edges, for both
schemes. These are the real, reviewed artefacts (see data/mgnrega/README.md
and the PMAY equivalent); this module only reads and reshapes them, it does
not invent content.

Deliberately NOT loaded here yet — each needs a design decision before it can
be wired in safely, not just a parser:
  - *_classification_rules.yaml / *_default_rules.yaml: each rule's `condition`
    is a named label (e.g. "per_capita_requested"), not executable code. Needs
    a matcher (regex/keyword, per scheme) mapping a question to a condition
    name before these rules can fire.
  - *_entity_resolver.yaml: large (1500-1800 lines/scheme) alias tables for
    village/district/block/year matching against curated.dim_geography_alias.
    Real, needed for questions that name a village by spelling, but a bigger
    lift than a straight loader.
  - *_response_template.yaml: template selection logic isn't specified by the
    YAML alone (which template applies to which answer shape).
mgnrega_schema_partitions.yaml / pmay_schema_partitions.yaml describe the
pre-migration Excel tables (table_name: mgnrega_expenditure, not
curated.fact_mgnrega_expenditure) — column meanings/synonyms are still valid,
but names need reconciling against SCHEMA_FOR_DEVELOPERS.md before use;
schema_context.py's hand-written version is used for now instead.
"""
import logging
import math
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# app/annotations.py -> repo root -> data/
_DATA_PART = Path(__file__).resolve().parents[1] / "data"

_SCHEME_DIRS = {
    "MGNREGA": _DATA_PART / "mgnrega",
    "PMAY-G": _DATA_PART / "pmay",
    "Focus Plus": _DATA_PART / "focus_plus",
    "CM Elevate": _DATA_PART / "cm_elevate",
    "Focus Legacy": _DATA_PART / "focus_legacy",
    "CM Elevate Legacy": _DATA_PART / "cm_elevate_legacy",
}
_FEW_SHOT_FILE = {
    "MGNREGA": "few_shot.yaml",
    "PMAY-G": "pmay_few_shot.yaml",
    "Focus Plus": "focusplus_few_shot.yaml",
    "CM Elevate": "cmelevate_few_shot.yaml",
    "Focus Legacy": "focuslegacy_few_shot.yaml",
    # The v2 prompt-layer bank (140 shots, re-verified), not the v1
    # cmelevatelegacy_few_shot.yaml beside it — v1 marks refusals
    # `status: REFUSED`, which this loader does not treat as a negative example.
    "CM Elevate Legacy": "cmelevatelegacy_prompt_few_shots.yaml",
}
_FK_FILE = {
    "MGNREGA": "foreign_key_augmentation.yaml",
    "PMAY-G": "pmay_foreign_key_augmentation.yaml",
    "Focus Plus": "focusplus_foreign_key_augmentation.yaml",
    "CM Elevate": "cmelevate_foreign_key_augmentation.yaml",
    "Focus Legacy": "focuslegacy_foreign_key_augmentation.yaml",
    "CM Elevate Legacy": "cmelevatelegacy_foreign_key_augmentation.yaml",
}

_few_shot_cache: dict[str, list[dict]] = {}
# scheme -> contrastive WRONG/RIGHT pairs and composer answer shots, from the
# same few-shot file. Only a file that carries `common_mistakes` / `answer_shots`
# (CM Elevate Legacy's v2 bank) populates these; every other scheme stays empty.
_common_mistakes: dict[str, list[dict]] = {}
_answer_shots: dict[str, list[dict]] = {}
# scheme -> (score factor for refusal examples, max refusal examples per prompt).
# Set only by a file that declares `refusal_score_factor` / `max_refusal_shots`
# (CM Elevate Legacy: "at most one refusal shot when SQL is likely"); every other
# scheme gets (1.0, None) — exactly the ranking used before.
_refusal_policy: dict[str, tuple[float, "int | None"]] = {}
_fk_cache: dict[str, dict] = {}
# scheme -> {token: idf}. Built once per scheme in load_all(). Plain token
# overlap treats the scheme's own name as hard evidence, but few_shot_examples()
# is already called per-scheme, so every candidate shares it and it separates
# nothing: "focus" is in 32% of the Focus Legacy corpus and "legacy" 28%, while
# "district" — the token that actually says what SHAPE the answer needs — is in
# 12%. Weighting by inverse document frequency makes the rare, topical token
# outrank the ubiquitous one.
_fewshot_idf: dict[str, dict[str, float]] = {}
# scheme -> tokens too common within that scheme to discriminate (see _build_scheme_stop)
_fewshot_scheme_stop: dict[str, set[str]] = {}


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_all() -> None:
    """Read every scheme's few-shot and FK files once, at process startup."""
    for scheme, folder in _SCHEME_DIRS.items():
        few_shot_path = folder / _FEW_SHOT_FILE[scheme]
        try:
            data = _load_yaml(few_shot_path)
            examples = data.get("sql_generation_examples", [])
            # Drop RETIRED/negative examples (PMAY v2.0 keeps some on purpose,
            # as things the generator must NOT reproduce).
            examples = [e for e in examples if e.get("status") != "RETIRED_v2.0"]
            _few_shot_cache[scheme] = examples
            _fewshot_idf[scheme] = _build_idf(examples)
            # A file may also name its own scheme-name tokens outright
            # (`ranking_stop_words`). Needed when the name is RARE inside the
            # pool: CM Elevate Legacy's shots rarely repeat "legacy", so the IDF
            # rated it a strong signal and every question naming the scheme
            # pulled the same two shots to the top. The pool is already one
            # scheme's, so its name separates nothing.
            _fewshot_scheme_stop[scheme] = _build_scheme_stop(examples) | {
                _FEWSHOT_SYNONYMS.get(str(t).lower(), str(t).lower())
                for t in (data.get("ranking_stop_words") or [])}
            _refusal_policy[scheme] = (
                float(data.get("refusal_score_factor", 1.0)),
                data.get("max_refusal_shots"),
            )
            _common_mistakes[scheme] = data.get("common_mistakes") or []
            _answer_shots[scheme] = data.get("answer_shots") or []
            logger.info("annotations: loaded %d few-shot examples for %s", len(examples), scheme)
        except FileNotFoundError:
            logger.warning("annotations: no few-shot file for %s at %s", scheme, few_shot_path)
            _few_shot_cache[scheme] = []
            _fewshot_idf[scheme] = {}
            _fewshot_scheme_stop[scheme] = set()
            _common_mistakes[scheme] = []
            _answer_shots[scheme] = []

        fk_path = folder / _FK_FILE[scheme]
        try:
            data = _load_yaml(fk_path)
            _fk_cache[scheme] = data.get("foreign_key_augmentation", {})
            logger.info(
                "annotations: loaded %d join edges for %s",
                len(_fk_cache[scheme].get("edges", [])), scheme,
            )
        except FileNotFoundError:
            logger.warning("annotations: no FK file for %s at %s", scheme, fk_path)
            _fk_cache[scheme] = {}


# Metric/geo synonyms folded to one canonical token before overlap scoring, so
# "how much was spent" matches the "expenditure" example and "man-days" matches
# "person-days". Scheme-agnostic — just the words that actually differ between
# how a user phrases something and how the few-shot file names it. Broadened
# from the original small set (money-verb variants, plurals, superlatives) so
# more phrasings of an already-covered question land on the right example
# instead of only the exact wording the few-shot file happens to use.
_FEWSHOT_SYNONYMS = {
    # money paid out — collapse every verb a user might use for "how much
    # money moved" to one bucket. Deliberately broad: for ranking purposes we
    # only need the right MONEY example in-scheme, not to distinguish
    # sanctioned-vs-released (schema_context.py rules still enforce that
    # distinction in the generated SQL itself).
    "spend": "expenditure", "spending": "expenditure", "spent": "expenditure",
    "cost": "expenditure", "expense": "expenditure", "expenses": "expenditure",
    "disbursed": "expenditure", "disburse": "expenditure",
    "disbursement": "expenditure", "disbursements": "expenditure",
    "disbursal": "expenditure", "disbursals": "expenditure",
    "payout": "expenditure", "payouts": "expenditure",
    "released": "expenditure", "outlay": "expenditure", "paidout": "expenditure",
    "amount": "expenditure", "money": "expenditure", "fund": "expenditure",
    "funds": "expenditure",
    "wagebill": "wages", "wage": "wages",
    "manday": "persondays", "mandays": "persondays", "persondays": "persondays",
    "workday": "persondays", "workdays": "persondays",
    "jobcard": "jobcards",
    "hh": "households", "household": "households",
    "house": "houses", "dwelling": "houses", "dwellings": "houses", "unit": "houses",
    "tranch": "tranche", "tranches": "tranche", "installments": "installment",
    "cohort": "batch", "batches": "batch",
    "districts": "district", "blocks": "block", "villages": "village",
    "panchayat": "village", "gp": "village",
    "panchayats": "village",
    # generic plurals -> singular (only the ones actually used across the four
    # schemes' few-shot files; not a general stemmer, so no risk of a wrong
    # guess on a word we haven't checked).
    "payments": "payment", "applications": "application", "schemes": "scheme",
    "beneficiaries": "beneficiary", "records": "record", "years": "year",
    "farmers": "farmer", "completions": "completion", "sanctions": "sanction",
    "members": "member", "requests": "request", "statuses": "status",
    "sites": "site", "stages": "stage", "women": "woman",
    "women's": "woman",
    "sanctioned": "sanction",
    # superlatives/rank direction — canonicalised so "least"/"fewest"/"bottom"
    # all retrieve a BOTTOM-N example and "most"/"top"/"maximum" all retrieve
    # a TOP-N example, regardless of which specific word the question or the
    # few-shot file happens to use.
    "least": "lowest", "lowest": "lowest", "smallest": "lowest",
    "minimum": "lowest", "fewest": "lowest", "bottom": "lowest",
    "fewer": "lowest", "min": "lowest", "lower": "lowest", "worst": "lowest",
    "most": "highest", "highest": "highest", "largest": "highest",
    "maximum": "highest", "greatest": "highest", "top": "highest",
    "greater": "highest", "max": "highest", "best": "highest",
    "exceeding": "highest", "more": "highest",
    # US/UK spelling variants seen across the reference docs.
    "utilization": "utilisation", "program": "programme",
}
# Question-shape and filler words carry no signal for which example fits.
_FEWSHOT_STOP = {
    "the", "a", "an", "of", "in", "for", "is", "are", "was", "were", "how", "many",
    "much", "what", "which", "and", "to", "by", "on", "across", "all", "show", "me",
    "list", "total", "number", "count", "give", "get", "there", "have", "has", "been",
    "do", "does", "did", "per", "each", "every", "with", "that", "this", "over",
}


def _fewshot_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) < 2 or t in _FEWSHOT_STOP:
            continue
        out.add(_FEWSHOT_SYNONYMS.get(t, t))
    return out


# A token this common within ONE scheme's examples says nothing about which of
# them fits — the pool is already scheme-scoped, so the scheme's own vocabulary
# is shared by every candidate. Measured on Focus Legacy's 81 examples: "focus"
# 32%, "payment" 32%, "legacy" 28%, against "district" at 12%. Down-weighting
# these by IDF was not enough (three weak tokens still out-totalled two strong
# ones); they are skipped outright.
_FEWSHOT_UBIQUITOUS_DF = 0.25
# What a ubiquitous token is worth relative to a discriminating one. Small
# enough that ONE topical token outweighs several house-vocabulary matches,
# non-zero so it still orders candidates that are otherwise tied.
_FEWSHOT_UBIQUITOUS_WEIGHT = 0.05


def _build_idf(examples: list[dict]) -> dict[str, float]:
    """token -> inverse document frequency over one scheme's example questions.

    log(N / df) with a +1 floor, so a common token scores near the floor and a
    rare one scores high. Computed once at load."""
    n = len(examples)
    if not n:
        return {}
    df: dict[str, int] = {}
    for ex in examples:
        for t in _example_tokens(ex):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / d) + 1.0 for t, d in df.items()}


def _example_tokens(ex: dict) -> set[str]:
    """Tokens of an example's question plus any `variants`. With no variants
    this is exactly the question's own token set."""
    toks = _fewshot_tokens(ex.get("question", ""))
    for v in ex.get("variants") or []:
        toks |= _fewshot_tokens(v)
    return toks


def _build_scheme_stop(examples: list[dict]) -> set[str]:
    """Tokens in >= _FEWSHOT_UBIQUITOUS_DF of one scheme's examples — its own
    name and house vocabulary. Derived from the corpus, so it stays correct as
    examples are added and needs no hand-maintained per-scheme list."""
    n = len(examples)
    if not n:
        return set()
    df: dict[str, int] = {}
    for ex in examples:
        for t in _example_tokens(ex):
            df[t] = df.get(t, 0) + 1
    return {t for t, d in df.items() if d / n >= _FEWSHOT_UBIQUITOUS_DF}


def _fewshot_score(q_tokens: set[str], example_question: str,
                   idf: dict[str, float] | None = None,
                   stop: set[str] | None = None) -> float:
    e = _fewshot_tokens(example_question)
    if not q_tokens or not e:
        return 0.0
    shared = q_tokens & e
    if not shared:
        return 0.0
    # The scheme's own ubiquitous vocabulary matches every candidate in this
    # pool, so it is the WEAKEST evidence available, not the strongest. Score it
    # at a small residual rather than dropping it (a residual still breaks ties
    # among otherwise-equal candidates) and rather than keeping it at full
    # weight (which let "What is the total amount disbursed under Focus Legacy?"
    # — overlapping only on focus/legacy/expenditure — outrank "Top 5 districts
    # by amount disbursed" for a by-district question).
    _stop = stop or set()
    # IDF-weighted overlap. Without the weighting every token counts the same,
    # so an example sharing only the scheme name ("What is the total amount
    # disbursed under Focus Legacy?") outranked one sharing the topic word
    # ("Top 5 districts by amount disbursed") and the generator copied a bare
    # SUM with no GROUP BY for a "by district" question (reported 2026-09-23).
    # An unseen token defaults to 1.0 — the old, unweighted behaviour.
    weight = sum(
        (idf or {}).get(t, 1.0) * (_FEWSHOT_UBIQUITOUS_WEIGHT if t in _stop else 1.0)
        for t in shared
    )
    # Same length normalisation as before, so a short on-topic example is not
    # buried by a long one that merely shares more words.
    return weight / (len(q_tokens | e) ** 0.5)


def few_shot_examples(schemes: list[str], question: str = "", top_k: int = 5) -> list[dict]:
    """Up to top_k examples per scheme, question+sql only (tables not needed in-prompt).

    Ranked by token overlap with `question` so the examples the generator sees are
    the ones on-topic for this query — not just the first top_k in file order,
    which for MGNREGA are all expenditure queries and left job-card / person-day /
    household questions with no worked example at all. `sorted` is stable, so when
    nothing overlaps (or `question` is empty) the original file order is kept.

    A `status: UNANSWERABLE` example (CM Elevate's money/date questions the data
    genuinely can't answer) has `sql: null` on purpose — it's a negative example
    meant to be retrieved for exactly this kind of question and teach the refusal,
    not a worked query. It is returned with sql=None and its `reason` instead of
    being skipped, so the generator sees the on-topic guard rather than nothing."""
    out: list[dict] = []
    q_tokens = _fewshot_tokens(question)
    for scheme in schemes:
        pool = _few_shot_cache.get(scheme, [])
        if q_tokens:
            idf = _fewshot_idf.get(scheme) or {}
            stop = _fewshot_scheme_stop.get(scheme) or set()
            # An example may list other phrasings under `variants` (CM Elevate
            # Legacy's v2 bank: "kitne records hai piggery me" for the Piggery
            # count). It is scored on whichever phrasing fits the question best.
            # No variants -> exactly the single-question score used before.
            factor, _ = _refusal_policy.get(scheme, (1.0, None))
            pool = sorted(pool,
                          key=lambda ex: max(
                              _fewshot_score(q_tokens, text, idf, stop)
                              for text in [ex["question"], *(ex.get("variants") or [])])
                          * (factor if ex.get("status") == "UNANSWERABLE" else 1.0),
                          reverse=True)
        _, max_refusals = _refusal_policy.get(scheme, (1.0, None))
        if max_refusals is not None:
            kept, n_ref = [], 0
            for ex in pool:
                if ex.get("status") == "UNANSWERABLE":
                    if n_ref >= max_refusals:
                        continue
                    n_ref += 1
                kept.append(ex)
            pool = kept
        for ex in pool[:top_k]:
            if ex.get("status") == "UNANSWERABLE":
                reason = " ".join(ex.get("reason", "").split())
                out.append({"question": ex["question"], "sql": None, "reason": reason})
            else:
                item = {"question": ex["question"], "sql": ex["sql"].strip()}
                # The reasoning steps behind the query, when the example carries
                # them — rendered as a "Plan:" line so the generator sees WHY the
                # SQL has its shape (COALESCE bucket, Unresolved exclusion, ...),
                # not only the shape itself.
                if ex.get("plan"):
                    item["plan"] = [str(p) for p in ex["plan"]]
                out.append(item)
    return out


def common_mistakes_text(schemes: list[str]) -> str:
    """The contrastive WRONG -> RIGHT block for the scheme(s) in play, or "".
    Each pair names the validator rule it breaks, so the generator sees the
    exact failure shape rather than only the correct form."""
    lines: list[str] = []
    for scheme in schemes:
        for ap in _common_mistakes.get(scheme, []):
            lines.append(f"  {ap['id']} {ap['title']} (breaks {ap.get('rule', '?')}): {ap.get('why', '')}")
            lines.append(f"    WRONG: {ap['wrong']}")
            lines.append(f"    RIGHT: {ap['right']}")
    return "\n".join(lines)


def refusal_reason(scheme: str, code: str) -> str:
    """The reviewed refusal wording for one refusal_code (e.g. SANCTION_RATE),
    taken from the first negative example that carries it — so a deterministic
    refusal says exactly what the few-shot bank teaches the generator to say."""
    for ex in _few_shot_cache.get(scheme, []):
        if ex.get("refusal_code") == code and ex.get("reason"):
            return " ".join(str(ex["reason"]).split())
    return ""


def answer_shots(scheme: str, question: str = "", top_k: int = 2) -> list[dict]:
    """Up to top_k worked answers (result rows + finished wording) for the
    composer, ranked by the same token overlap as few_shot_examples."""
    pool = _answer_shots.get(scheme, [])
    q_tokens = _fewshot_tokens(question)
    if q_tokens:
        pool = sorted(pool, key=lambda a: _fewshot_score(q_tokens, a.get("question", "")),
                      reverse=True)
    return pool[:top_k]


def prohibited_joins_text(schemes: list[str]) -> str:
    """Human-readable list of joins the generator must refuse, with the alternative."""
    lines = []
    for scheme in schemes:
        for edge in _fk_cache.get(scheme, {}).get("edges", []):
            if edge.get("is_prohibited"):
                use_instead = edge.get("use_instead", "aggregate each side independently first")
                lines.append(
                    f"  - NEVER join {edge['from_table']} -> {edge['to_table']} "
                    f"directly. Use {use_instead} instead."
                )
    return "\n".join(lines)
