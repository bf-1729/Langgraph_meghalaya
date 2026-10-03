-- ============================================================================
-- megh_app — the least-privilege role the NLP service should connect as.
--
-- WHY THIS EXISTS
--   The service currently connects as `postgres` (superuser). Every statement
--   the LLM generates therefore runs with full write and DDL privileges. The
--   SQL guard in app/db.py rejects non-SELECT statements, but that is one
--   regex layer — not a substitute for the database refusing the write.
--
--   The existing `megh_readonly` role cannot be used as-is: it has SELECT on
--   curated.* and semantic.* but NO access to the `app` schema, where this
--   service keeps its own users, sessions, chat history and audit trail — all
--   of which it must WRITE on every login and every query.
--
--   So: read-only on the scheme data (where generated SQL runs), read-write on
--   the application's own bookkeeping. One role, one pool, no code change.
--
-- REVIEW BEFORE RUNNING. Requires a superuser (postgres / dbadmin).
-- Idempotent: safe to re-run.
-- ============================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. The role
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'megh_app') THEN
        -- REPLACE the password before running. Generate one with:
        --   python -c "import secrets; print(secrets.token_urlsafe(32))"
        CREATE ROLE megh_app LOGIN PASSWORD 'REPLACE_ME_WITH_A_STRONG_PASSWORD';
        RAISE NOTICE 'created role megh_app';
    ELSE
        RAISE NOTICE 'role megh_app already exists — leaving password untouched';
    END IF;
END
$$;

-- Belt and braces: this role must never acquire these, even by inheritance.
ALTER ROLE megh_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Keep query cost bounded at the role level, independent of the app's own
-- SQL_EXECUTION_TIMEOUT_MS. A runaway generated query cannot pin a backend.
ALTER ROLE megh_app SET statement_timeout = '30s';
ALTER ROLE megh_app SET idle_in_transaction_session_timeout = '60s';

-- ---------------------------------------------------------------------------
-- 2. Scheme data — READ ONLY.
--    This is the surface LLM-generated SQL runs against.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE megh_db TO megh_app;

GRANT USAGE ON SCHEMA curated  TO megh_app;
GRANT USAGE ON SCHEMA semantic TO megh_app;

GRANT SELECT ON ALL TABLES    IN SCHEMA curated  TO megh_app;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA curated  TO megh_app;
GRANT SELECT ON ALL TABLES    IN SCHEMA semantic TO megh_app;

-- Tables added to those schemas later are read-only for this role too.
-- NOTE: default privileges apply per granting role. If the ETL writes as a
-- different owner, repeat these with FOR ROLE <that_owner>.
ALTER DEFAULT PRIVILEGES IN SCHEMA curated  GRANT SELECT ON TABLES TO megh_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA semantic GRANT SELECT ON TABLES TO megh_app;

-- Explicitly NOT granted on curated/semantic: INSERT, UPDATE, DELETE, TRUNCATE,
-- REFERENCES, TRIGGER, CREATE. A generated write is refused by the server.

-- ---------------------------------------------------------------------------
-- 3. Application schema — READ/WRITE.
--    app.users, app.tenants, app.login_events, app.conversations,
--    app.conversation_turns, app.query_audit
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS app;

GRANT USAGE ON SCHEMA app TO megh_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO megh_app;
-- IDENTITY columns need their sequences (6 tables use GENERATED ... AS IDENTITY).
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO megh_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO megh_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT USAGE, SELECT ON SEQUENCES TO megh_app;

-- The service runs CREATE TABLE/INDEX IF NOT EXISTS and ALTER TABLE ... ADD
-- COLUMN IF NOT EXISTS in app.appdb.ensure_schema() at every startup, so it
-- needs CREATE on this schema. Scoped to `app` only — it cannot create objects
-- in curated or semantic.
GRANT CREATE ON SCHEMA app TO megh_app;

-- ---------------------------------------------------------------------------
-- 4. Lock down everything else
-- ---------------------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM megh_app;
REVOKE ALL ON DATABASE megh_db FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- 5. Verify (run after the grants above)
-- ---------------------------------------------------------------------------
\echo ''
\echo '=== megh_app effective privileges ==='

SELECT 'schema USAGE' AS check,
       n.nspname      AS object,
       has_schema_privilege('megh_app', n.nspname, 'USAGE')  AS usage,
       has_schema_privilege('megh_app', n.nspname, 'CREATE') AS create
FROM   pg_namespace n
WHERE  n.nspname IN ('app', 'curated', 'semantic', 'public')
ORDER  BY n.nspname;

SELECT 'scheme data must be read-only' AS check,
       c.relname                       AS object,
       has_table_privilege('megh_app', c.oid, 'SELECT') AS can_select,
       has_table_privilege('megh_app', c.oid, 'INSERT') AS can_insert,
       has_table_privilege('megh_app', c.oid, 'UPDATE') AS can_update,
       has_table_privilege('megh_app', c.oid, 'DELETE') AS can_delete
FROM   pg_class c
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  n.nspname = 'curated' AND c.relkind IN ('r','v','m')
ORDER  BY c.relname
LIMIT  10;

SELECT 'app schema must be writable' AS check,
       c.relname                     AS object,
       has_table_privilege('megh_app', c.oid, 'SELECT') AS can_select,
       has_table_privilege('megh_app', c.oid, 'INSERT') AS can_insert,
       has_table_privilege('megh_app', c.oid, 'UPDATE') AS can_update,
       has_table_privilege('megh_app', c.oid, 'DELETE') AS can_delete
FROM   pg_class c
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  n.nspname = 'app' AND c.relkind = 'r'
ORDER  BY c.relname;

\echo ''
\echo 'Expected: curated.* -> select=t, insert/update/delete=f'
\echo '          app.*     -> all four = t'
\echo ''
\echo 'Then set in .env:'
\echo '  DATABASE_URL=postgresql+asyncpg://megh_app:<password>@10.48.242.4:5432/megh_db'
\echo 'and restart the service.'
