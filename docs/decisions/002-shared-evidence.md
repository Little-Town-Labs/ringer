# ADR-002: Optional shared evidence store with local JSONL as the source of truth

## Status

Accepted

## Date

2026-10-08

## Context

ADR-001 kept local JSONL as the default attempt evidence and preserved the explicit PostgreSQL backend unchanged, with DuckDB ingestion deferred. That left three gaps for anyone running Ringer on more than one machine:

- Evidence and Ringside live on the machine that ran the swarm, so there is no single place to report across machines.
- The PostgreSQL path wrote **either** Postgres **or** JSONL, so a row that reached the database existed nowhere else. Its insert also dropped `model`, `task_type`, `reasoning_effort` and `retry`, the fields the scoreboard slices on.
- `run_id` plus `task_key` does not identify an attempt (see [Evidence storage](../EVIDENCE.md)), so rows had no safe key for retries or re-imports.

The shipped table had no definition in the repository, and the settings and messages were named for one hosted provider.

## Decision

Keep local JSONL as the default and the system of record. When `[eval] backend = "postgres"`, `EvalLogger` writes **both**: the JSONL row always, and the central row in addition.

- **Central table.** `ringer.attempts` holds every field of a JSONL row. The schema, a compose template, a backup script and an operator runbook live in `scripts/central-evidence/`.
- **Identity.** Each attempt has `attempt_uid`, the SHA-256 of `source_host | logged_at | run_id | task_key | worker_engine`, using the exact `logged_at` string written to JSONL. The logger stamps `logged_at` once and uses it for both sinks, so a row has the same identity whichever sink stored it.
- **Idempotent writes.** Inserts use `ON CONFLICT DO NOTHING` with no conflict target. The writer role is insert-only and cannot read the table, and naming a conflict target would require SELECT.
- **`log_sink` meaning.** `postgres` means the central write succeeded. `jsonl` means it did not (or no connection was configured); `fallback_reason` records why. A failed central write still saves the row locally and prints one warning per run.
- **Prompt policy.** The central store holds only a SHA-256 of the stored prompt excerpt by default (`[eval.postgres] spec_storage = "hash"`). `"excerpt"` stores the excerpt. Local JSONL is unchanged.
- **Catch-up.** `ringer evidence push` sends local rows to the central store. It validates every selected row in every file before connecting, uses one transaction per file, is safe to repeat, and never modifies JSONL. `ringer evidence status` reports local counts and never connects.
- **Roles.** `ringer_writer` can only INSERT; `ringer_reader` can only SELECT, on the base table and the reporting views.
- **Naming.** Database settings are read from `RINGER_DB_HOST`, `RINGER_DB_PORT`, `RINGER_DB_USER`, `RINGER_DB_PASSWORD` and `RINGER_DB_NAME`, falling back to the older `SUPABASE_DB_*` names per key. Messages say "postgres".
- **Compatibility.** A `swarm_runs` view over the new table accepts the previous 12-column insert, so an already-configured client keeps working.

This supersedes ADR-001's decision to preserve the existing PostgreSQL `EvalLogger` behavior for the `postgres` backend. ADR-001's other decisions stand: runs stay headless by default, and presentation stays separate from evidence.

## Alternatives considered

### A shared DuckDB file

Rejected. DuckDB allows one writer process and cannot be written safely over a network share. DuckDB remains useful as a **reader**: it can attach the central database through its Postgres extension. ADR-001's deferral of a DuckDB ingestion service is unchanged.

### Keep writing Postgres or JSONL, not both

Rejected. Rows that reached only the database would have no local copy, which removes the guarantee that the central store can be rebuilt from local files.

### An HTTP ingest service in front of the database

Deferred. It would add a service to run and secure for no benefit while the writer role is already insert-only and the database is reachable only on a private network.

### Keep the previous table and column set

Rejected for the reasons in Context: no stored definition, a lossy insert, and no attempt key.

## Consequences

- Existing PostgreSQL users get one extra JSONL row per attempt, by design.
- `psycopg` is required only when the `postgres` backend or `evidence push` is used; it is imported lazily.
- Local evidence files remain complete, so the central database can be rebuilt with `ringer evidence push`.
- `source_host` is part of attempt identity. A changed hostname changes the identity and can duplicate rows on a later push; set `[eval.postgres] source_host` to pin it.
- `evidence push` reads the configured JSONL path by default. Evidence kept in other files is sent by passing each with `--file`.
- Prompt text is not copied centrally by default, and the stored excerpt is already truncated to 500 characters, so the hash covers the excerpt, not the full prompt.
- Live "running now" status is not part of this decision. The central store holds finished attempts only.
- Provisioning and operating the database is outside Ringer. This repository ships the assets and a runbook, not a deployment.
