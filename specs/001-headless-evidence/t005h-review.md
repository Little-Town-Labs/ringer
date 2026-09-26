# T005h scoped review

Reviewer modules-review, openai-codex/gpt-6-sol medium, read-only context-isolated same-family session. Lead accepts two findings:

- H1 medium / fix-before-merge: root-level read_model.py and top-level import/tests violate approved ringer_core package architecture (R5). Move file into ringer_core/read_model.py and correct CLI/test imports without changing SQL or bodies.
- H2 medium / fix-before-merge: read-model import appears before Python 3.12 guard; transitively imports models/tomllib, so unsupported Python can fail before established version message. R5 behavior preservation. Place all extracted-core imports after version guard and test ordering/early exit without importing core on unsupported version.

Parent evidence: 26 moved definitions and optional SQLite block match pre-wave AST, full suite 285/284 passing plus approved B1 exception. These tests do not yet prove H1/H2. T005h not accepted.

One bounded remediation authorized: file move/import correction and focused package/guard regression tests only. Owned root read_model.py removal by safe move to package, ringer_core/read_model.py, ringer.py imports, tests/test_module_boundaries.py. No unrelated code/config/schema changes. Then one read-only delta review, escalate if unresolved. Baseline B1 remains approved unchanged.

## Remediation evidence
Worker moved module into ringer_core and placed CLI import after Python guard. Fresh process regression simulates Python 3.11 and asserts exact original SystemExit plus no ringer_core loaded. Parent inspected package/fallback/guard tests and verified root file absent. Parent suite 286 tests/285 pass approved B1; /tmp/ringer-t005h-final.log. compileall/diff pass. Remediation 1/1 used, delta review 1/1 dispatched to modules-review; reviewer confirmed H1/H2 resolved and passed all six boundary tests. Final H1/H2 disposition not-actionable (resolved). Lead accepts T005h locally with approved B1 exception; both counters exhausted and no blocker remains. No commit/installation/production change.
