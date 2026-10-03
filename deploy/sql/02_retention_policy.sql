-- ============================================================================
-- Retention for the conversation and audit tables.
--
-- WHY
--   app.conversation_turns stores every question, the full answer text, and the
--   generated SQL. app.query_audit stores the caller's IP. Both grow without
--   bound today — there is no purge anywhere in the application.
--
--   For a government deployment that is a standing data-protection exposure:
--   an officer's queries and the answers they saw are retained indefinitely,
--   alongside the IP they came from.
--
-- RETENTION PERIODS BELOW ARE PLACEHOLDERS. Set them from the department's
-- actual policy before running — they are the one thing here that is a
-- business decision, not a technical one.
--
-- Requires a superuser (postgres / dbadmin). Idempotent.
-- ============================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Tunables — adjust to the department's policy
-- ---------------------------------------------------------------------------
--   chat history : how long an officer's own conversations stay readable
--   audit trail  : how long the compliance record is kept (usually longer)
--   login events : how long sign-in/failure records are kept
--
-- Deliberately different: deleting a chat should not delete its audit row, and
-- the audit trail is what an investigation would rely on.

CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.purge_expired(
    chat_retention    interval DEFAULT '180 days',
    audit_retention   interval DEFAULT '365 days',
    login_retention   interval DEFAULT '365 days'
)
RETURNS TABLE(what text, rows_deleted bigint)
LANGUAGE plpgsql
AS $$
DECLARE
    n bigint;
BEGIN
    -- Turns first: they FK to app.conversations.
    DELETE FROM app.conversation_turns
     WHERE created_at < now() - chat_retention;
    GET DIAGNOSTICS n = ROW_COUNT;
    what := 'conversation_turns'; rows_deleted := n; RETURN NEXT;

    -- Then any conversation left with no turns and past the window.
    DELETE FROM app.conversations c
     WHERE c.last_at < now() - chat_retention
       AND NOT EXISTS (SELECT 1 FROM app.conversation_turns t
                        WHERE t.conv_id = c.conv_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    what := 'conversations'; rows_deleted := n; RETURN NEXT;

    DELETE FROM app.query_audit
     WHERE created_at < now() - audit_retention;
    GET DIAGNOSTICS n = ROW_COUNT;
    what := 'query_audit'; rows_deleted := n; RETURN NEXT;

    DELETE FROM app.login_events
     WHERE created_at < now() - login_retention;
    GET DIAGNOSTICS n = ROW_COUNT;
    what := 'login_events'; rows_deleted := n; RETURN NEXT;
END
$$;

COMMENT ON FUNCTION app.purge_expired IS
  'Deletes conversation/audit rows past their retention window. Call from cron; '
  'see deploy/sql/02_retention_policy.sql for the rationale and defaults.';

-- The service role may execute it, but the defaults are set by whoever
-- schedules it — the app never purges on its own.
GRANT EXECUTE ON FUNCTION app.purge_expired(interval, interval, interval) TO megh_app;

-- ---------------------------------------------------------------------------
-- Dry run — what WOULD be deleted at the current defaults
-- ---------------------------------------------------------------------------
\echo ''
\echo '=== rows currently past the default windows (nothing deleted yet) ==='

SELECT 'conversation_turns' AS table_name,
       count(*) FILTER (WHERE created_at < now() - interval '180 days') AS would_delete,
       count(*) AS total,
       min(created_at)::date AS oldest
FROM   app.conversation_turns
UNION ALL
SELECT 'query_audit',
       count(*) FILTER (WHERE created_at < now() - interval '365 days'),
       count(*), min(created_at)::date
FROM   app.query_audit
UNION ALL
SELECT 'login_events',
       count(*) FILTER (WHERE created_at < now() - interval '365 days'),
       count(*), min(created_at)::date
FROM   app.login_events;

\echo ''
\echo 'To purge with the defaults:      SELECT * FROM app.purge_expired();'
\echo 'To purge with your own windows:  SELECT * FROM app.purge_expired(''90 days'', ''730 days'', ''365 days'');'
\echo ''
\echo 'Schedule it daily, e.g. in crontab for the postgres user:'
\echo '  15 3 * * *  psql -d megh_db -c "SELECT * FROM app.purge_expired();" >> /var/log/megh-purge.log 2>&1'
