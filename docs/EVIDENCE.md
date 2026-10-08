# Evidence storage

Ringer writes local evidence as one JSON object per line. The default file is
`~/.ringer/runs.jsonl`; the configured JSONL path is used when set. Each row
keeps the existing fields and adds `logged_at`, `log_sink`, and
`fallback_reason`. JSONL is written locally without a dashboard. When the
PostgreSQL backend is configured, JSONL is still always written and the row is
also sent to the central store (see "Central store" below).

Ringer serializes each row before opening the evidence file. Cooperating
Ringer processes and threads take an exclusive file lock while checking the
last record and appending. A short write is completed; an interrupted write is
retried. A write error attempts to truncate only bytes from that failed append
while the lock is held. If that rollback also fails, the error reports both
failures. Earlier complete rows are not intentionally changed.

Before append, Ringer checks only the final existing row. If the file ends in
an incomplete line, or its final line is not a JSON object, Ringer stops and
leaves the file unchanged. Preserve the file and repair or restore its tail
manually before retrying. It does not scan the full history or silently discard
data. This is not a power-loss durability guarantee: Ringer does not promise
that recently written data survives a machine or storage failure.

`run_id` identifies a run, and `task_key` identifies its task context. Neither
is a unique attempt identifier. The existing `retry` field is Boolean; it does
not distinguish multiple retry numbers. Do not use these fields alone as a
unique key for individual attempts.

## Central store

An optional shared PostgreSQL store can collect attempts from several machines.
Local JSONL stays the source of truth; see
[ADR-002](decisions/002-shared-evidence.md) for the reasoning and
[`scripts/central-evidence/`](../scripts/central-evidence/README.md) for the
schema, compose template, backup script, and operator runbook.

Set `[eval] backend = "postgres"` and point `[eval.postgres] env_file` at a file
with `RINGER_DB_HOST`, `RINGER_DB_PORT`, `RINGER_DB_USER`, `RINGER_DB_PASSWORD`,
and `RINGER_DB_NAME` (the older `SUPABASE_DB_*` names still work). Install
`psycopg` (`pip install "psycopg[binary]"`); it is imported only when needed.

For each attempt the logger stamps `logged_at` once and uses it for both
sinks. `log_sink` records where the database write landed:

- `postgres`: the central write succeeded.
- `jsonl`: it did not, or no connection was configured. `fallback_reason` says
  why, and Ringer prints one warning line per run. Nothing is lost; the row is
  in JSONL.

### Attempt identity

The central table keys each attempt on `attempt_uid`, the SHA-256 of
`source_host | logged_at | run_id | task_key | worker_engine` using the exact
`logged_at` string in the JSONL row. This is the unique attempt key that
`run_id` and `task_key` are not. `source_host` defaults to the machine
hostname; set `[eval.postgres] source_host` to keep identity stable if the
hostname changes. Inserts use `ON CONFLICT DO NOTHING`, so repeating a write is
harmless.

### Prompt text

By default the central row stores only a SHA-256 of the row's `spec` text
(`spec_storage = "hash"`). With `"excerpt"` it stores that text as well. The
`spec` in a JSONL row is already cut to 500 characters before it is logged, so
the hash covers that excerpt, not the full prompt. Rows from tasks that set
`redact_spec` store neither.

### Catching up with `ringer evidence`

```bash
./ringer.py evidence status                      # local counts; never connects
./ringer.py evidence push --dry-run              # validate and count; never connects
./ringer.py evidence push                        # send local rows; safe to repeat
./ringer.py evidence push --file A.jsonl --file B.jsonl --since 2026-10-01
```

`push` reads the configured `[eval] jsonl_path` unless you pass `--file`; if
evidence lives in more than one file, pass each one. It checks every selected
row in every file before it connects, so a bad line sends nothing. It sends one
transaction per file, never modifies the JSONL files, and reports how many rows
were inserted and how many were already present. Exit codes: 0 success, 2
usage, validation, or configuration problem, 3 database connection or write
failure.
