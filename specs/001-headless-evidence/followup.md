# T002a: owner-approved bounded follow-up

## Authority
After the first wave stopped with F1/F5 and baseline B1, the user explicitly said 'approved' to fixing the two remaining cases and leaving the unrelated contributor-credit failure documented. This is a new explicit bounded task, not an automatic reset of the previous wave. No commit, publication, production, or configuration mutation is authorized.

## Scope
- F1 / R2: HTML paths in durable metadata must correspond to successful generated output, including rendering and partial write failures. Always retain JSON evidence/version records; failed presentation must not lose evidence.
- F5 / R1: optional Ringside must not synthesize or load missing HTML for metadata-only runs/versions. Retain metadata and deliverables; choose a simple explicit unavailable state or exclude nonexistent pages from page selectors, without a UI redesign.
- R6: regression tests for rendering/write failures, partial success, metadata-only and mixed-history presentation. Existing explicit-presentation behavior must continue to work.

Owned paths: ringer.py, dashboard/ringside.html, tests/ only. No module extraction, schemas/dependencies, backend changes, or unrelated cleanup.

## Evidence and checks
Source locations in review.md bound this follow-up. Existing base graph was ready and verified against source before T002; current diff and targeted source reads are authoritative for this follow-up, graph is not claimed refreshed.
Checks: RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests; focused regression tests; git diff --check. Use mock workers and temporary files; no model calls or external services. Baseline B1 is an owner-approved known exception, not a passing test.

## Roles and limits
Implementation: resumed headless-build, openai-codex/gpt-6-luna medium, existing isolated worktree, one writer, max 20 minutes; no network/credentials/commits/agents/spec status edits. Final review: resumed headless-review-2, openai-codex/gpt-6-sol medium, read-only scoped review after parent verification. Both models already executed successfully. Usage unavailable must be recorded as unavailable.
One implementation delivery plus quality review; at most one remediation and one delta review if needed. Lead owns integration and acceptance. Previous wave remains recorded as exhausted. Scope uncertainty returns to lead.
