# Headless Ringer with local evidence

## Approval and scope
The user approved the headless/local-JSONL refactoring plan in this session. This is brownfield / system / routine local development. No production mutations.

## Requirements
- R1: Normal run, demo, and ask invocations are headless by default: no browser, HTTP listener, or HTML generation. Explicit presentation remains available.
- R2: Preserve JSON runtime state, active-run records, raw worker logs, verification evidence, and harvested deliverables, including worktree outputs before cleanup.
- R3: Local JSONL is the default evidence destination. Preserve existing model, reasoning effort, task type, retry, verdict, duration, and token fields. Explicit existing alternate backend configuration remains supported; live user configuration is not changed.
- R4: Test concurrent JSONL writers and logging failures. Preserve existing record shape and document identifiers for future ingestion; do not invent a database schema.
- R5: Extract coherent Python modules incrementally behind the existing ringer.py entry point, preserving commands, saved formats, and behavior except R1.
- R6: Verify run, ask, lint, dry-run, baseline, retries, cancellation, and deliverable preservation. Keep documentation accurate.

## Non-goals
No DuckDB ingestion, network transfers, aegis-prod changes, credentials, deployment, publishing, database migration, new plugin framework, unrelated fixes, deletion of the optional UI, or live configuration edits.

## Clarification
No critical gaps for first milestone. Interpret 'comment out HTML' as disabling presentation by default without deleting it. Separate evidence/artifacts from presentation. Broader module extraction follows the first milestone.
