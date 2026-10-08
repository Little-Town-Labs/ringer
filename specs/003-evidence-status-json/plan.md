# Plan

`ringer_core/evidence_cli.py` already collects, per file, the rows, the log_sink counts and the fallback reasons for the human output of `status`. Build the JSON document from the same data so the two outputs cannot disagree: factor the per-file summary into one helper that both the text printer and the JSON printer use, and keep the text output untouched.

`ringer.py`: add `--json` (store_true) to the `evidence status` sub-parser only, inside `build_parser`. No other change to `ringer.py`.

Errors become per-file `error` entries in JSON mode instead of an early return, so one bad file never hides the others. Exit code stays 0 or 2.

Tests: `tests/test_evidence_status_json.py`, one behavior per method (shape, counts, totals across files, latest fallback, empty file, each error kind, stdout is only JSON, text mode unchanged, `push --json` rejected, no credentials read).
Docs: a short section in `docs/EVIDENCE.md`.
