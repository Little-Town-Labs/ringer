# Dev loop

A spec-kit workflow that delivers a whole specified feature through Ringer: it works through `tasks.md`
wave by wave in an isolated worktree, gates every wave with verification and review, and finishes with a
whole-feature closeout review. It never merges and never pushes; a human merges.

It combines three things your process already has:

| From | What it contributes here |
|---|---|
| Spec Kit | `tasks.md` (task ids, `[P]` parallel markers, phases, dependencies), the requirements checklist gate, the `analyze` result |
| Ringer | every model-calling step as a manifest with an **executed check**, retry once, the evidence log |
| `code-review-and-quality` skill | the five-axis quality gate: severity labels, verify the verification, `APPROVE` or `REQUEST CHANGES` |

```
preflight -> init (or resume)
  waves (at most 20):  next -> build -> verify -> review -> triage -> [fix -> verify -> review -> triage] x2 -> decide -> finish-wave
final -> closeout (whole-feature review) -> settle -> human gate if needed -> report
```

## Run it

```bash
mkdir -p .specify            # the workflow engine keeps run state here (git-ignored)
specify workflow run scripts/dev-loop/workflow.yml -i spec_dir=specs/NNN-feature --json
specify workflow status <run_id> --json
specify workflow resume <run_id> -i human_verdict=approve      # or reject, after a pause
specify workflow resume <run_id> -i preflight_verdict=approve  # after a preflight pause
```

Commit the spec directory first, so the branch carries it (an uncommitted spec is copied into the worktree,
which is fine while drafting). A run takes minutes to hours; run it in the background. Ringer attempts are
logged under the identity `dev-loop`.

## The outer loop

State lives in **`tasks.md` and the branch**, not in the workflow run, so a run that stops can simply be
started again: `init` resumes the existing worktree and `next` picks up at the first unchecked ready task.

- **Task ids and state.** `- [ ] T003 ...` / `- [x] T003 ...`. A feature without a task list is one implicit task.
- **Ready.** A task is ready when every task in an earlier `## Phase N` is done, its explicit deps
  (`(deps: T001, T002)`, `depends on T001`, `Prerequisite T001`) are done, and, if it is not marked `[P]`, the earlier
  sequential tasks in its phase are done.
- **Wave.** If the first ready task is sequential, the wave is that one task. Otherwise it is all ready `[P]` tasks
  whose owned paths are disjoint, up to `max_wave_size` (default 3), built in parallel in one Ringer manifest.
- **Marking done.** After an `APPROVE`, the loop (not a worker) checks the tasks off in `tasks.md` and appends a
  section to `delivery.md`, both committed on the branch. Workers never touch task state.
- **Ending.** No unchecked task left: closeout. Nothing ready but tasks remain, a scope change, an escalation, or the
  20-wave cap: the run stops at the human gate. Approving an escalated wave records a human override and marks its
  tasks done; rejecting stops the run and leaves the tasks unchecked.

The gate is always outside every loop, because resuming a paused nested step re-runs its whole parent.

## Gates

| Gate | Where | Fails when |
|---|---|---|
| Checklists (Spec Kit `implement` rule) | preflight | `checklists/` is missing (mode `required`) or has unchecked items |
| Analysis | preflight | `analysis.md` reports `Critical Issues Count` above 0 (or is missing in mode `required`) |
| Risk route | preflight | `risk` is `sensitive` or `production`: a hard stop, no mutable worker ever starts |
| Route approval | preflight | `route_gate: true` asks a human before any worker starts |
| Executed check | each build, fix, review | Ringer's check exits non-zero (one retry with the failure output) |
| Verify | each wave | the `verify` commands fail (suite, real-environment checks, scope checks) |
| Five-axis quality gate | each review round and closeout | a Critical or Required finding, or a verdict other than `APPROVE` |
| Scope change | build, fix | a worker wrote a valid `scope-change.md`: the wave escalates and the lead returns to clarify and plan |
| Decide | each wave | confirmed findings remain, a lens did not report, nothing changed, files outside the owned paths, a protected path was touched, or verification fails |

A preflight pause is a "proceed anyway?" question, as in Spec Kit: approve continues, reject stops.

## A spec directory

`spec.md`, `plan.md`, `tasks.md`, `loop.json`, a build brief, optional `checklists/` and `analysis.md`.

`loop.json`:

| Key | Meaning |
|---|---|
| `name`, `engine` | run name (a slug) and the Ringer engine (`codex` by default) |
| `risk` | `routine` (default), `sensitive` or `production` |
| `checklists` | `required` (default), `if-present` or `off` |
| `analysis` | `if-present` (default), `required` or `off` |
| `route_gate` | ask a human to approve the route before any worker starts |
| `max_wave_size` | most parallel tasks per wave (default 3) |
| `verify` | commands run in the worktree. `{repo}` is this checkout, `{base}` the wave's start commit, `{feature_base}` the feature's |
| `build` | defaults for every task: `model`, `effort`, `timeout_s`, `brief`, `owned`, `required_text` |
| `tasks.T00n` | per-task overrides; **required for every task in a feature with more than one**, and tasks that run in a parallel wave need their own `verify` |
| `review.lenses` | custom read-only lenses: `key`, `model`, `effort`, `surface` |
| `review.quality_gate` | enables the five-axis gate beside the lenses |
| `fix`, `closeout` | models for the fix worker and the whole-feature review (closeout defaults to the quality gate) |
| `known_failures`, `escalate_paths` | tests allowed to fail; path prefixes that always escalate |

`diff_scope.py {base} FILE FUNC...` confines edits to named functions; `suite.py --known TEST` runs the whole suite
and tolerates known failures.

## Policy

- **Review.** Custom lenses must cite real `file:line` locations and quotations (`check_review.py`). The quality gate
  must use its exact headings, cover all five axes, label findings, cite real evidence for every Critical or Required
  finding, and give a verdict consistent with them (`check_gate_report.py`). Priority is by realistic impact: input that
  needs to be pathological or adversarial is P3.
- **Confirmed findings.** P0-P2 with high or medium confidence from a lens; Critical or Required from the gate. The fix
  worker must reproduce each one before changing code and add a regression test, or say it could not reproduce it.
- **Bounded.** At most two fix rounds per wave, then the wave escalates.
- **Clean-context, same-family review.** Every worker starts fresh with no conversation history, so reviews are not
  anchored on how the code was justified. That is not independence from blind spots shared by one model family; the
  executed checks, real-environment verification and escalation rules cover that. Put a lens on another family for
  security-sensitive changes.
- **Never merges.** The report prints the merge command for a human.

## Testing the loop

`scripts/dev-loop/selftest.sh` runs the real workflow engine on `examples/toy` with a scripted engine (no model calls,
no network): two waves with a fixed finding and closeout; a stuck wave that escalates, is accepted and resumed in a second
run; a scope change; and an unchecked checklist stopping the run at the preflight gate. `tests/test_devloop.py`,
`tests/test_devloop_waves.py` and `tests/test_diff_scope.py` cover the pure logic.

## Known limits

- Triage is mechanical (severity and confidence thresholds); the fix worker is told to reproduce first, but nothing
  executes a reproduction before the fix.
- The constitution check and `analyze` itself are agent commands in Spec Kit; the loop only reads their results
  (`analysis.md`, `checklists/`). The spec, plan and tasks are still written by hand or by those commands.
- Nested loops and gates are verified on the toy feature, not yet on a real multi-wave run.
- A wave that is rejected at the gate leaves its commits on the branch; the next run builds on top of them.
