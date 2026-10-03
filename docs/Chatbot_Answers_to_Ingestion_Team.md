# Chatbot team: answers to the ingestion team's three questions

From: the Megh One AI chatbot team.
To: the ingestion/database team (`megh-ingestion`).
Date: 2026-09-25

The short answers:

1. **Is the chatbot limited to `semantic.v_embedding_documents`?** No. The chatbot never reads that view. Its SQL generator is shown every column in `curated` and `semantic`.
2. **Has the view been loaded into the vector index?** No. Nothing re-syncs it on a schedule.
3. **Does the chatbot count CM Elevate applicants correctly?** Partly. "Applicants" questions use `COUNT(DISTINCT request_id)`. Our rules assume a different reason for the repeated rows than the one you describe, though, and we need you to confirm which is right.

---

## Question 1: Is retrieval limited to `semantic.v_embedding_documents`?

**No.** No code in the chatbot reads `semantic.v_embedding_documents`. The chatbot has two paths, and neither uses the view.

### SQL path (all data questions)

- At startup the chatbot reads every column in the `curated` and `semantic` schemas from `information_schema.columns`. It also reads the foreign keys from `pg_constraint`. (Code: `app/schema_introspect.py`, `_load_live_schema`.)
- That whole column list goes into the prompt of the model that writes the SQL. The list is filtered only by scheme, not by any visibility flag. (Code: `app/prompt_builder.py`, the "LIVE SCHEMA" block.)
- The generated SQL then runs after one check: it must be a single, read-only `SELECT`/`WITH` statement. Nothing limits which tables or columns it can touch. (Code: `app/db.py`, `_assert_safe` / `run_readonly`.)
- `is_chatbot_visible` is respected, but only for the descriptive notes taken from `semantic.table_catalog` and `semantic.column_catalog`. The column list above ignores it.

### RAG path ("what is / who is eligible" questions)

- This path uses markdown documents only. It never queries the database.

### What this means for you

Leaving a field out of the view does **not** hide it. Anything `megh_readonly` can `SELECT` in `curated` or `semantic` is visible to the SQL generator, and it can end up in an answer.

The only dependable safeguard is on the database side:

- revoke column privileges on those fields from `megh_readonly`, or
- move those fields to a schema `megh_readonly` cannot read.

We can also add a table/column allowlist on our side. But a privilege revoke is the only way to guarantee a field stays hidden.

---

## Question 2: Has the view been loaded into the vector index?

**No.** None of the 222 rows in `semantic.v_embedding_documents` has been embedded.

- The vector index is a Qdrant collection on `115.124.102.167:6335` (moved from the data box on 2026-10-03).
- It is built only from SME-written markdown files:
  - `data/reference/*.md`: one reference document and one FAQ per scheme (MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy);
  - `data/web/*.md`: collected public background material.
  
  (Code: `app/kb_ingest.py`, `_SOURCES` and `_web_sources`.)
- **Nothing re-syncs it on a schedule.** The index is rebuilt only:
  - at app startup, if a scheme is missing from the collection or it holds too few chunks; or
  - when an admin calls `POST /api/rag/reingest`.

Your semantic tables do reach the chatbot, but through the SQL prompt, not the vector index. `table_catalog`, `column_catalog`, `glossary`, `metric_definitions` and `join_graph` are read at every app restart. So a new `data_quality_note` takes effect after a restart, with no code change on our side.

If you want `semantic.v_embedding_documents` embedded, please tell us what each row is meant to answer. We'll scope the work from that.

---

## Question 3: CM Elevate applicant counting

The chatbot queries `curated.v_cm_elevate`, not `curated.fact_cm_elevate_application` directly. Its counting rules are in `app/schema_context.py`, CM Elevate rule 8 and the phrase map:

| Question wording | SQL the chatbot uses | Shallang, Piggery |
|---|---|---|
| "applicants", "beneficiaries", "unique applications" | `COUNT(DISTINCT request_id)` | 32 (correct) |
| "applications", "requests" | `COUNT(*)` | 37 |

### Where our rules may be wrong

Our rules give this reason for the repeated rows: *"one row = one application; about 36 `request_id`s repeat, all inside Piggery."* That treats the repeats as a few stray duplicates.

You describe something different: the source adds a new row every time an application's status changes. If that is true everywhere, two things are wrong:

1. **The "applications" count is also inflated.** Each application appears once per status change, not once.
2. **Status breakdowns are wrong.** Any breakdown by `current_file_status`, `current_level` or `data_verified` counts old statuses as if they were current. For example, an application that has been paid is also counted under "submitted".

### What we need from you

Please confirm which is true:

- **(a) Status history.** Every application with more than one row is a status history, across all 15 schemes, not just Piggery. If so:
  - can you provide a view that keeps only the latest row per `request_id`? and
  - which column tells us which row is the latest?
- **(b) Stray duplicates.** The repeats are limited to the ~36 Piggery IDs in our rules.

Once you confirm, we will point the counting rules at the right view.

### Checking this across the whole scheme

We tried to run this ourselves across the whole scheme, but the database was not reachable from our machine at the time. You have `megh_readonly` access, so these two queries will settle it:

```sql
-- 1. How many request_ids have more than one row,
--    and did the status or the payload change between them?
WITH d AS (
  SELECT request_id,
         count(*)                               AS n,
         count(DISTINCT current_file_status)    AS status_values,
         count(DISTINCT scheme_specific::text)  AS payload_values
  FROM curated.v_cm_elevate
  GROUP BY 1
  HAVING count(*) > 1
)
SELECT count(*)                                  AS dup_ids,
       sum(n - 1)                                AS extra_rows,
       count(*) FILTER (WHERE status_values > 1)  AS status_differs,
       count(*) FILTER (WHERE payload_values > 1) AS payload_differs
FROM d;

-- 2. Extra rows per scheme
SELECT scheme_name,
       count(*)                            AS row_count,
       count(DISTINCT request_id)          AS applicants,
       count(*) - count(DISTINCT request_id) AS extra_rows
FROM curated.v_cm_elevate
GROUP BY 1
ORDER BY 4 DESC;
```

How to read the results:

- **Status history (a):** extra rows appear in many schemes, and `status_differs` is close to `dup_ids`.
- **Stray duplicates (b):** about 36 extra rows, all in Piggery, with the same status and payload on each copy.
