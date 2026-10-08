# Plan

## Evidence
Targeted source reads of `ringer.py` (EvalLogger, `parse_env_file`, CLI parser/dispatch), `ringer_core/config.py` (`EvalConfig`, `PostgresEvalConfig`, `load_eval_config`), `ringer_core/evidence.py`, `ringer_core/runner.py` (`_log_attempt`), and the tests that touch them. The codebase graph was not used; reads are authoritative, not a claimed graph. Production facts (<db-host>, schema, roles, Grafana datasource, 323-row backfill) were established and verified in this session and are recorded in spec.md.

Findings that bound the work:
- `EvalLogger` lives in `ringer.py` (ADR-001 keeps it there). `log_attempt` writes Postgres **or** JSONL and stamps `logged_at`, `log_sink`, `fallback_reason` only inside `_write_jsonl`, so an identity stamped before both sinks (R3) and dual-write (R10) change its control flow, not just its SQL.
- The insert column list excludes `model`, `task_type`, `retry`. `tests/test_model_log.py::test_postgres_params_exclude_local_model_log_keys` asserts that exclusion and the exact parameter key set. R2 reverses it, so this test is rewritten on purpose, not worked around.
- `spec` is cut to 500 characters before it reaches the logger (`runner.py` `_log_attempt`), so the spec policy (R5) operates on the excerpt.
- `RingerRunner` takes an injected `logger` with `log_attempt(row)` and `close()` (ADR-001). That interface must not change; tests fake it.
- `PostgresEvalConfig` holds only `env_file`. `spec_storage` and `source_host` are new validated fields. `load_eval_config` already rejects `backend = "postgres"` without `[eval.postgres]`.
- The CLI has no `evidence` command. Subcommands are registered in `ringer.py` near `models`/`catalog`/`db` and dispatched in `main`.
- `tests/test_module_boundaries.py` enforces import discipline: new logic that does not need the CLI goes under `ringer_core/` and must not import `ringer`. The database driver is imported lazily so importing the module needs no `psycopg`.
- Evidence is split across two files on powerbox2 (config path `~/.ringer/runs.jsonl`, newer rows in `~/.local/share/ringer/performance/runs.jsonl`), so `evidence push` takes repeated `--file`.
- Public-repo hygiene: shipped assets must not carry this tailnet's address or hostnames. The compose file takes the bind address from the environment.

## Design
New module `ringer_core/central_evidence.py` (no import of `ringer`, lazy `psycopg`):
- `stamp(row, source_host)`: adds `logged_at` (if absent) and `attempt_uid`; one stamp is shared by both sinks.
- `attempt_uid(source_host, row)`, `apply_spec_policy(row, mode)`, `to_params(row)` (all columns), `resolve_credentials(env)` (`RINGER_DB_*` then `SUPABASE_DB_*`).
- `read_jsonl_rows(path)` (strict, line-numbered errors), `push_rows(conn, rows)` (single transaction, `ON CONFLICT DO NOTHING`, returns inserted count).

`EvalLogger.log_attempt` order: stamp once; if a connection exists, try the Postgres insert and set `log_sink` and `fallback_reason` from the outcome; then always `append_jsonl`. A JSONL failure propagates exactly as today. A Postgres failure after success elsewhere never raises. One stderr line per run on first fallback.

`ringer evidence push|status` in `ringer.py` is composition only and delegates to the module. Ops assets move into the repo under `scripts/central-evidence/` (schema, compose template, backup script, runbook) and the spec-folder copies become references.

## Sequence
1. Baseline and worktree on a feature branch (T001).
2. Pure module and its tests (T002) in parallel with config fields (T003) and ops assets with a gated Postgres integration test (T006): disjoint owned files.
3. `EvalLogger` dual-write (T004), then CLI `evidence` (T005). Both edit `ringer.py`, so they run one after the other, not in parallel.
4. Production proof and retirement of the one-off backfill (T007), separately approved.
5. ADR-002 and documentation (T008), then final verification and independent review (T009).

## Verification
`RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests` (expected: one known pre-existing contributor-credit failure, recorded in T001). Unit tests use fake connections. A schema integration test runs only when `RINGER_TEST_PG_DSN` points at a throwaway Postgres (CI skips it); it must never be pointed at <db-host>. Final CLI smoke uses a mock worker, a temp HOME/state, and a fake or throwaway database. Same-family review is recorded as same-family; at least one review lens should use a different model family.

## Rollout and rollback
Nothing changes for installs that do not set `backend = "postgres"` (JSONL stays default). For powerbox2: merge, `pip install psycopg`, set `backend = "postgres"` with `env_file = ~/.ringer/ringer-db.env`, run one task, confirm it appears centrally and locally. Rollback: set `backend = "jsonl"`; the database and local files are unaffected. Central rows already written stay (idempotent, unique key), and the nightly dump covers loss. The <db-host> database, Grafana datasource, and cron are already live and are not part of any code rollback.

## Risks
- Dual-write changes observable behavior for current Postgres users (an extra local JSONL row). Mitigation: it is the point of R10; documented in ADR-002.
- `ringer.py` is a ~3,000-line entry point; keep edits minimal and test through the existing logger seam.
- Hostname drift changes `source_host` and so the uid, risking duplicates; `[eval.postgres] source_host` pins it.
