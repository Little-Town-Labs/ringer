# Architecture

Ringer is a Python command-line tool. `ringer.py` parses commands, loads configuration, selects execution paths, and composes the application. It remains a substantial entry point, about 3,266 lines, rather than a tiny shim. `ringer_core/` contains focused modules used by the command layer.

## Module map

- `config.py` reads TOML and builds immutable configuration records for engines, evidence, artifacts, steering, and updates.
- `manifests.py` parses task and run manifests into immutable records.
- `runner.py` owns task scheduling, worker process lifecycle, retry flow, verification coordination, and attempt logging.
- `runtime.py` builds worker commands and holds process and task runtime helpers. `verification.py` runs checks and manages process termination. `worker_logs.py` formats worker logs and failure context.
- `state.py` writes run snapshots and coordinates artifact rendering and library updates. `state_files.py` handles atomic state files and scans stored run state.
- `artifact_store.py` manages generated HTML artifact paths, deliverables, and the artifact library. `artifact_views.py` renders local HTML pages.
- `presentation.py` serves Ringside and the per-run browser view. It reads run state and serves local presentation routes.
- `evidence.py` appends attempt rows to local JSONL evidence. `central_evidence.py` holds attempt identity, the prompt policy, credential resolution, the strict JSONL reader, and the idempotent central insert; it imports no CLI code and loads `psycopg` lazily. `evidence_cli.py` implements `ringer evidence push` and `status`. `ringer.py` also contains `EvalLogger`, which always writes JSONL and, when the PostgreSQL backend is configured, also writes the central row.
- `models.py`, `model_views.py`, `models_api.py`, and `read_model.py` derive model summaries, render model pages and APIs, and read analytics data. SQLite is the existing local analytics format; it is separate from the JSONL attempt log.
- `catalog.py` handles model catalog data. `steering.py` loads optional steering profiles. `context.py` handles context selection for `ask`.

Modules call focused helpers rather than importing the CLI entry point. `runner.py` depends on configuration, manifest, runtime, state, presentation, verification, steering, and log helpers. `state.py` depends on state-file, artifact, and rendering helpers. Model reads and views use the model and catalog modules. `ringer.py` connects these pieces and retains command-specific flows that have not moved into the package.

## Headless runs and stored data

`run`, `demo`, and `ask` do not start Ringside or open a browser by default. `--dashboard` opts into Ringside. `--browser` opts into the per-run browser view. `--no-dashboard` overrides either presentation request. For `run`, `demo`, and `ask`, generated HTML also requires `[artifact].enabled = true`; the shipped sample sets it to `false`. Without presentation, command setup disables HTML generation for the run. `--no-artifact` also disables generated HTML, including when presentation is requested.

Presentation and HTML artifacts are separate from run state and task outputs. Ringer writes run state as JSON beneath the state directory and retains worker logs and deliverables. The artifact-library JSON metadata is updated without requiring a generated HTML page. The attempt evidence defaults to `~/.ringer/runs.jsonl` and is written independently of Ringside and HTML. See [Evidence storage](EVIDENCE.md) for append, malformed-tail, and recovery behavior.

## Runner logger ownership

`RingerRunner` requires a keyword-only `logger` argument that implements `log_attempt(row)` and `close()`. Its normal `run()` cleanup closes the logger. If runner construction fails, the caller must close the logger. `EvalLogger` remains in `ringer.py`.

```python
from pathlib import Path

from ringer import EvalLogger
from ringer_core.config import EvalConfig
from ringer_core.runner import RingerRunner

logger = EvalLogger(
    EvalConfig(backend="jsonl", jsonl_path=Path("/tmp/runs.jsonl"), postgres=None)
)
try:
    runner = RingerRunner(
        manifest=manifest,
        config=config,
        identity="local",
        dashboard_enabled=False,
        logger=logger,
    )
except BaseException:
    logger.close()
    raise
await runner.run()  # RingerRunner closes logger during run cleanup.
```

Use the existing `EvalLogger` implementation when the caller needs the configured evidence backend. The CLI now creates it before runner initialization, so backend initialization can occur earlier if runner construction fails. PostgreSQL remains an explicit backend selected through `[eval]` and `[eval.postgres]`. Since ADR-002 it is written in addition to JSONL, not instead of it, and database settings are read from `RINGER_DB_*` with the older `SUPABASE_DB_*` names as a per-key fallback. SQLite remains an existing analytics input. DuckDB ingestion is deferred.

## Decision record

See [ADR-001](decisions/001-headless-local-evidence.md) for the rationale behind headless local evidence and the boundary between evidence storage and presentation. See [ADR-002](decisions/002-shared-evidence.md) for the optional shared evidence store, which supersedes ADR-001's decision to preserve the previous PostgreSQL logger behavior.
