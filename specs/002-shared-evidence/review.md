# T009 review (same-family, context-isolated)

Reviewed HEAD ef00c49 against base ca3f3b2 with five read-only lenses through Ringer (run shared-evidence-review). Reviewers: gpt-6.1-sol x3 (identity-and-failure, cli-and-config, security-and-privacy), gpt-6-luna x2 (runbook-cold-read, tests-and-coverage). Implementers were the same model families, so this is NOT independent review. Every finding below was reproduced by the lead before being recorded.

Ringer outcomes: four lenses passed the executed check (report contract plus verified file:line citations and quotes). runbook-cold-read failed the check twice for quoting prose across clauses inside backticks; its single finding is real and was found independently by the security lens. tests-and-coverage reported no findings (shortest run, 34 s; treat as weak evidence).

## Confirmed findings (all reproduced)

| ID | Pri | Finding | Disposition |
|---|---|---|---|
| IDF-1 | P1 (design) | Local JSONL rows carry no source_host or attempt_uid, so replaying from another host or after a hostname change computes a different identity and can duplicate central rows. Contradicts a literal reading of R3. | Fixed in T010 (owner approved; identity now stored in local rows) |
| IDF-2 | P2 | to_params accepts integers outside signed 64-bit range; the database would reject them after earlier files were committed. | Fixed in T010 |
| CLI-1 | P3 (rated P1 by reviewer) | Connection and write exception text, and status fallback_reason, are printed unredacted. libpq errors do not normally contain the password, so exposure is hypothetical, but scrubbing the known password is cheap. | Fixed in T010 |
| CLI-2 | P2 | Rows excluded by --since are never validated (R7 says every row); error line numbers are row positions, not physical lines, when blank lines exist. | Fixed in T010 |
| CLI-3 | P2 | Non-text values in text fields pass validation; spec=123 raises an uncaught AttributeError. | Fixed in T010 |
| CLI-4 | P2 | config.sample.toml shows `source_host = ""`, which the loader rejects when uncommented. | Fixed in T010 |
| SEC-1 | P1 (docs) | Provisioning passes role passwords as psql -v process arguments, visible to other local users. | Fixed (docs, verified against a throwaway Postgres) |
| SEC-2 | P2 | Tracked files under specs/ contain installation-specific values (private address, host and user names, local paths). Matters before any push to a public remote. | Approved: delete duplicate draft assets and sanitize specs/ after T007 (repo is private, so low urgency) |
| SEC-3 | P2 | Owner-password rotation guidance (restart after changing the env var) does not change the stored role password. Found independently by two lenses. | Fixed (docs, verified against a throwaway Postgres) |

## Accepted tradeoffs and unestablished claims
- notes column stores up to 2,000 chars of check output in plaintext centrally; a short prompt's SHA-256 can be brute-forced. Documented privacy tradeoff, not anonymization.
- Thread safety of EvalLogger is not established; the runner calls it synchronously from one event loop.
- evidence push loads all rows in memory (about 3.6 MiB per 1,000 rows with 500-char prompts).
- The Postgres integration tests are gated and did not run in the reviewers' sandbox; the lead ran them during T006.

## Blocking assessment
None of the findings affects the correctness of pushing powerbox2's rows (T007): those rows validate and the data path is sound. IDF-1 decision, SEC-2, CLI-4, SEC-1 and SEC-3 should be settled before merging or publishing the branch.

## Resolution
T010 (gpt-6.1-sol, 1 attempt, 97k tokens) fixed IDF-1, IDF-2, CLI-1, CLI-2, CLI-3; CLI-4, SEC-1, SEC-3 were fixed inline and verified. Re-verification by the lead: acceptance check passes; suite 393 tests with only the known failure; real psycopg against a throwaway Postgres (dual-write, push, re-push 0 inserted); identities of the 323 rows already in the shared database are unchanged. SEC-2 remains, scheduled after T007. The owner confirmed this repository will not be public, so SEC-2 is tidying, not a release blocker.
