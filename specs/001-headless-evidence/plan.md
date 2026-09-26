# Plan

## Evidence
Source analysis of main, run_one_request, RingerRunner, StateWriter, EvalLogger, ArtifactRenderer and configuration establishes the affected seams. Graph tools are unavailable; targeted source reads and Python AST analysis substitute, not a claimed current graph.

StateWriter currently combines JSON snapshots, HTML generation, and library maintenance. Runner harvesting is separate and must remain enabled. EvalLogger already defaults to JSONL but explicit PostgreSQL is an alternative sink, not simultaneous replication. Tests import and patch the monolithic module; migrate tests with extracted ownership rather than adding proxy globals.

## Sequence
1. Preserve baseline and implement headless defaults plus evidence/presentation separation with regression tests.
2. Harden and extract evidence writing behind a small interface, retaining record shape and explicit backend support; test concurrent writers and errors.
3. Extract context, manifest/configuration, and verification modules in dependency order, then worker/state/artifact/runner responsibilities. Keep interfaces small, inject concrete dependencies only where tests need substitution.
4. Separate analytics and optional presentation without UI redesign.
5. Run compatibility and lifecycle checks, review integrated changes, document rollout and rollback.

## Verification
RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests
Use mock workers and temporary state/config/work directories; no paid model calls or shared dashboard ports. CLI test subprocesses disable self-update. New tests must prove defaults create no HTML/listeners and retain JSONL/state/deliverables. Explicit presentation remains tested.

## Rollout and rollback
No installation or merge in this session without a separate decision. Review isolated changes first. Reverting the scoped change restores prior defaults; preserve data and worktree for review.
