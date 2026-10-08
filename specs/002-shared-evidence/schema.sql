-- Ringer shared evidence store (spec 002-shared-evidence).
-- Target: a DEDICATED Postgres database on aegis-prod, tailnet-bound only.
-- Draft for review. Not applied anywhere.
--
-- Apply as the database owner, passing role passwords as psql variables so
-- no secret lands in this file or in shell history:
--   psql -v ON_ERROR_STOP=1 -v writer_pw="$RINGER_WRITER_PW" \
--        -v reader_pw="$RINGER_READER_PW" -d ringer -f schema.sql
-- Re-running is safe (idempotent) except that role passwords are reset.

BEGIN;

CREATE SCHEMA IF NOT EXISTS ringer;

-- One row per worker attempt. Mirrors the 22 fields of a runs.jsonl row.
-- run_id + task_key is NOT unique per attempt (docs/EVIDENCE.md), so the
-- primary identity is attempt_uid, computed by the client (see spec R3).
CREATE TABLE IF NOT EXISTS ringer.attempts (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_uid      text        NOT NULL UNIQUE,
    source_host      text        NOT NULL DEFAULT 'legacy',
    logged_at        timestamptz NOT NULL DEFAULT now(),
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    log_sink         text        NOT NULL DEFAULT 'postgres',
    fallback_reason  text,

    run_id           text        NOT NULL,
    task_key         text        NOT NULL,
    pattern          text,
    task_type        text,
    orchestrator     text,

    worker_engine    text,
    model            text,
    expected_model   text,
    reported_model   text,
    reasoning_effort text,
    shepherd_model   text,

    verify_method    text,
    verdict          text        NOT NULL,   -- observed: PASS, FAIL, TIMEOUT; unconstrained on purpose
    retry            boolean,
    duration_ms      bigint,
    worker_tokens    bigint,
    notes            text,

    spec             text,                   -- stored per spec policy R5; may be NULL
    spec_sha256      text                    -- always set when spec is withheld or truncated
);

CREATE INDEX IF NOT EXISTS attempts_logged_at_idx   ON ringer.attempts (logged_at DESC);
CREATE INDEX IF NOT EXISTS attempts_run_idx         ON ringer.attempts (run_id, task_key);
CREATE INDEX IF NOT EXISTS attempts_model_type_idx  ON ringer.attempts (model, task_type);
CREATE INDEX IF NOT EXISTS attempts_host_idx        ON ringer.attempts (source_host, logged_at DESC);

-- Backward compatibility: a client that still sends the old 12-column INSERT
-- (no attempt_uid, no source_host) must keep working. Fill identity server-side.
CREATE OR REPLACE FUNCTION ringer.fill_attempt_defaults() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.attempt_uid IS NULL THEN
        NEW.attempt_uid := 'legacy:' || gen_random_uuid()::text;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS attempts_fill_defaults ON ringer.attempts;
CREATE TRIGGER attempts_fill_defaults
    BEFORE INSERT ON ringer.attempts
    FOR EACH ROW EXECUTE FUNCTION ringer.fill_attempt_defaults();

-- The name the shipped EvalLogger writes to. Auto-updatable (single table,
-- plain columns), so existing INSERT INTO swarm_runs (...) works unchanged.
CREATE OR REPLACE VIEW ringer.swarm_runs AS
    SELECT run_id, pattern, task_key, spec, worker_engine, shepherd_model,
           verify_method, verdict, duration_ms, worker_tokens, notes, orchestrator
    FROM ringer.attempts;

-- Reporting views (Grafana / DuckDB read these, not the base table).
CREATE OR REPLACE VIEW ringer.model_scoreboard AS
SELECT model,
       task_type,
       count(*)                                          AS attempts,
       count(*) FILTER (WHERE verdict = 'PASS')          AS passes,
       round(100.0 * count(*) FILTER (WHERE verdict = 'PASS') / count(*), 1) AS pass_pct,
       count(*) FILTER (WHERE retry)                     AS retries,
       round(avg(duration_ms) / 1000.0, 1)               AS avg_seconds,
       sum(worker_tokens)                                AS total_tokens
FROM ringer.attempts
GROUP BY model, task_type;

CREATE OR REPLACE VIEW ringer.daily_activity AS
SELECT date_trunc('day', logged_at) AS day,
       source_host,
       orchestrator,
       count(*)                                  AS attempts,
       count(*) FILTER (WHERE verdict = 'PASS')  AS passes,
       count(*) FILTER (WHERE verdict <> 'PASS') AS non_passes,
       sum(worker_tokens)                        AS total_tokens
FROM ringer.attempts
GROUP BY 1, 2, 3;

-- Roles: writer can only INSERT; reader can only SELECT. Neither can alter data.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ringer_writer') THEN
        CREATE ROLE ringer_writer LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ringer_reader') THEN
        CREATE ROLE ringer_reader LOGIN;
    END IF;
END $$;

ALTER ROLE ringer_writer PASSWORD :'writer_pw';
ALTER ROLE ringer_reader PASSWORD :'reader_pw';
-- Unqualified "swarm_runs" must resolve to the compatibility view.
ALTER ROLE ringer_writer SET search_path = ringer;
ALTER ROLE ringer_reader SET search_path = ringer;
ALTER ROLE ringer_writer SET statement_timeout = '10s';
ALTER ROLE ringer_reader SET statement_timeout = '60s';

REVOKE ALL ON SCHEMA ringer FROM PUBLIC;
GRANT USAGE ON SCHEMA ringer TO ringer_writer, ringer_reader;
GRANT INSERT ON ringer.attempts TO ringer_writer;
GRANT INSERT ON ringer.swarm_runs TO ringer_writer;
GRANT SELECT ON ringer.attempts, ringer.model_scoreboard, ringer.daily_activity TO ringer_reader;
-- Identity column: inserts need no sequence grant (GENERATED ALWAYS AS IDENTITY).

COMMIT;
