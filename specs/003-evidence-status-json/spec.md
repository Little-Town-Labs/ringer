# `ringer evidence status --json`

Machine-readable output for `ringer evidence status`, so scripts (including the dev loop's own
verification steps) can read local evidence counts without parsing text.

## Requirements
- R1: `ringer evidence status --json` prints exactly one JSON document to stdout and nothing else on stdout. Diagnostics stay on stderr as today.
- R2: Shape (keys in this order):
  ```json
  {
    "ok": true,
    "files": [
      {"path": "<path as given>", "rows": 3,
       "first_logged_at": "2026-10-01T10:00:00+00:00", "last_logged_at": "2026-10-03T12:30:00+00:00",
       "confirmed_central": 1, "local_only": 2,
       "latest_fallback_reason": "postgres connect failed: refused", "error": null}
    ],
    "totals": {"rows": 3, "first_logged_at": "...", "last_logged_at": "...",
               "confirmed_central": 1, "local_only": 2, "latest_fallback_reason": "..."}
  }
  ```
  `files` follows the order of `--file` arguments (or the single configured file). Timestamps use the same normalization as the existing human output (`datetime.isoformat()` after treating a naive time as UTC).
- R3: Counting matches the human output: `confirmed_central` is rows with `log_sink == "postgres"`, `local_only` is every other row, and `latest_fallback_reason` is the `fallback_reason` of the most recent row (by `logged_at`) that has one, or `null`.
- R4: A file that is missing, unreadable, malformed, or contains an invalid row produces an entry with `"error": "<message>"` and `null` for `rows`, both timestamps, both counts and `latest_fallback_reason`. Then `ok` is `false`, the exit code is 2 (as today), and `totals` covers only the readable files. The error message names the file and, for a malformed line, its physical line number (`path:LINE`).
- R5: An empty file is valid: `rows` 0, both counts 0, timestamps and reason `null`.
- R6: Without `--json`, the output and exit codes of `evidence status` are byte-for-byte unchanged. `--json` is accepted by `status` only; `evidence push --json` is rejected by the argument parser (exit 2).
- R7: `status --json` never reads credentials and never connects to a database (unchanged for `status`).
- R8: `docs/EVIDENCE.md` documents the option and the shape, and a new test file covers every requirement, one behavior per test method.

## Non-goals
Changing `push`; adding fields beyond R2; pretty-printing options; reading the central database.
