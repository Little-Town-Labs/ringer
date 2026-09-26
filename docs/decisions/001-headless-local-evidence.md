# ADR-001: Keep local evidence independent of presentation

## Status

Accepted

## Date

2026-09-25

## Context

Ringer previously coupled normal runs to dashboard presentation and generated HTML. That made browser and listener behavior part of routine command execution. Attempt evidence is useful without those presentation surfaces, and must remain available when HTML output is disabled.

## Decision

Keep `run`, `demo`, and `ask` headless by default. Presentation is an explicit per-run choice through `--dashboard` or `--browser`; `--no-dashboard` takes precedence. Generated HTML remains controlled by `[artifact].enabled` and `--no-artifact`. Run state, artifact-library metadata, deliverables, and attempt evidence remain distinct from generated HTML.

Keep local JSONL as the default attempt evidence backend. Preserve the explicitly configured PostgreSQL backend and its existing `EvalLogger` behavior. Keep SQLite for the existing local analytics flow. Do not add DuckDB ingestion as part of this change.

`RingerRunner` requires a keyword-only logger implementing `log_attempt(row)` and `close()`. The runner owns and closes the logger once `run()` begins. The caller closes it if runner construction fails before `run()` begins. The CLI constructs the logger earlier than before; the owner approved that initialization-order change and the required constructor keyword. Keeping `EvalLogger` in `ringer.py` preserves its existing backend behavior while the runner receives an explicit dependency.

## Alternatives considered

### Start dashboard and HTML by default

Rejected because normal execution should not require a listener, browser, or generated pages to store run state and evidence.

### Replace existing storage and analytics backends

Rejected because JSONL, explicit PostgreSQL support, and SQLite analytics already cover their separate roles. Adding DuckDB would expand scope without a requirement.

### Make the runner construct its own logger

Rejected because that hides the storage dependency and couples task execution to a particular backend. The required keyword makes the dependency explicit. The caller retains cleanup responsibility if construction fails before the runner can own it.

## Consequences

- Normal runs do not start presentation or generate HTML by default.
- Evidence remains in local JSONL without presentation.
- HTML requires the artifact configuration gate and is also disabled by `--no-artifact`.
- Existing explicit PostgreSQL and SQLite analytics paths remain in place.
- No live machine configuration or database activation is claimed or changed.
- Future DuckDB ingestion remains deferred.
