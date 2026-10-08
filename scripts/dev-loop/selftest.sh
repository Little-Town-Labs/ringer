#!/usr/bin/env bash
# End-to-end plumbing test of the dev loop with a scripted engine. No model calls, no network.
#   scripts/dev-loop/selftest.sh
# Runs the real `specify workflow` engine on a three-task toy feature (examples/toy):
#   A. two waves (T001+T002 in parallel, then T003), a Required finding fixed in wave 1, then closeout   -> complete
#   B. wave 2 never gets clean: it escalates at the gate; approve accepts it; a second run resumes        -> complete
#   C. T003 reports a scope change: the loop stops and a reject leaves T003 unchecked
#   D. an unchecked checklist stops the run at the preflight gate before any worktree exists
set -uo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd); SPEC=scripts/dev-loop/examples/toy; UNCHK=scripts/dev-loop/examples/toy-unchecked
command -v specify >/dev/null || { echo "selftest needs the specify CLI"; exit 2; }
T=$(mktemp -d -t devloop-selftest.XXXXXX)
export RINGER_LOOP_DIR=$T/runs RINGER_LOOP_WORKTREES=$T/trees RINGER_NO_SELF_UPDATE=1
cleanup() { for s in $SPEC $UNCHK; do python3 scripts/dev-loop/devloop.py cleanup $s >/dev/null 2>&1; done; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p .specify
cat > $T/config.toml <<CFG
state_dir = "$T/state"
[eval]
backend = "jsonl"
jsonl_path = "$T/runs.jsonl"
[artifact]
enabled = false
[engines.scripted]
bin = "python3"
args_template = ["$REPO/scripts/dev-loop/fake_engine.py", "{taskdir}", "{spec}"]
sandbox_args = []
full_access_args = []
CFG
fail=0
check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }
jget() { python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]));print(eval(sys.argv[2]))" "$1" "$2" 2>/dev/null; }
wfget() { python3 -I -c "import json,sys;d=json.load(sys.stdin);print(d.get(sys.argv[1]))" "$1"; }
run_wf() { local s=$1; shift; specify workflow run scripts/dev-loop/workflow.yml -i spec_dir=$s -i ringer_config=$T/config.toml "$@" --json < /dev/null 2>$T/wf.err; }
resume_wf() { local rid=$1; shift; specify workflow resume $rid "$@" --json < /dev/null 2>$T/wf.err; }
D=$RINGER_LOOP_DIR/toy; W=$RINGER_LOOP_WORKTREES/loop-toy; TASKS=$W/$SPEC/tasks.md
steps_ran() { python3 -I -c "import json;print('$2' in json.load(open('.specify/workflows/runs/$1/state.json'))['step_results'])"; }
reset() { python3 scripts/dev-loop/devloop.py cleanup $1 >/dev/null 2>&1; }

echo "== A: two waves, a finding fixed in wave 1, closeout"
unset DEVLOOP_FAKE_SCENARIO
OUT=$(run_wf $SPEC); RID=$(echo "$OUT" | wfget run_id)
check "workflow completed" "$(echo "$OUT" | wfget status)" completed
check "feature settled complete" "$(jget $D/settled.json "d['state']")" complete
check "two waves ran" "$(jget $D/status.json "d['wave']")" 2
check "wave 1 round 1 confirmed the Required finding" "$(jget $D/triage-1.json "len(d['confirmed'])")" 1
check "wave 1 round 2 is clean" "$(jget $D/triage-2.json "len(d['confirmed'])")" 0
check "wave 1 reviewed with the lens and the quality gate" "$(ls $D/review-1 | tr '\n' ' ')" "correctness quality-gate "
check "wave 2 artifacts are prefixed and needed one round" "$(jget $D/w2-triage-1.json "len(d['confirmed'])")" 0
check "all three tasks are checked on the branch" "$(grep -c '\[x\] T00' $TASKS)" 3
check "delivery record has both waves" "$(grep -c '^## Wave .: APPROVE' $W/$SPEC/delivery.md)" 2
check "closeout approved" "$(jget $D/closeout-decision.json "d['decision']")" APPROVE
check "fix landed on the branch" "$(cat $W/toy_out/alpha/result.txt)" "alpha final"
check "main checkout untouched" "$(git status --porcelain -- toy_out | wc -l | tr -d ' ')" 0
check "no human gate ran" "$(steps_ran $RID human-gate)" False
reset $SPEC

echo "== B: wave 2 stuck -> escalate -> accept -> resume -> complete"
export DEVLOOP_FAKE_SCENARIO=fake-scenario-stuck.json
OUT=$(run_wf $SPEC); RID=$(echo "$OUT" | wfget run_id)
check "paused at the human gate" "$(echo "$OUT" | wfget current_step_id)" human-gate
check "settled escalated" "$(jget $D/settled.json "d['state']")" escalated
check "bounded at three review rounds in wave 2" "$(ls $D | grep -c '^w2-triage-')" 3
check "wave 1 tasks checked, T003 not" "$(grep -c '\[x\] T00[12]' $TASKS)/$(grep -c '\[ \] T003' $TASKS)" "2/1"
OUT=$(resume_wf $RID -i human_verdict=approve)
check "approve completes the run" "$(echo "$OUT" | wfget status)" completed
check "the accepted wave is marked done" "$(grep -c '\[x\] T003' $TASKS)" 1
check "the override is recorded" "$(grep -c 'ACCEPTED BY A HUMAN' $W/$SPEC/delivery.md)" 1
OUT=$(run_wf $SPEC); RID2=$(echo "$OUT" | wfget run_id)
check "a second run resumes and completes" "$(echo "$OUT" | wfget status)" completed
check "the second run did no new wave" "$(jget $D/status.json "d['wave']")" 2
check "closeout approved on the second run" "$(jget $D/settled.json "d['state']")" complete
reset $SPEC

echo "== C: scope change"
: > $T/runs.jsonl
export DEVLOOP_FAKE_SCENARIO=fake-scenario-scope.json
OUT=$(run_wf $SPEC); RID=$(echo "$OUT" | wfget run_id)
check "paused at the human gate" "$(echo "$OUT" | wfget current_step_id)" human-gate
check "settled as a scope change" "$(jget $D/settled.json "d['state']")" scope-change
check "the decision needed is reported" "$(jget $D/settled.json "any('Allow combining across a new directory?' in r for r in d['reasons'])")" True
check "Ringer accepted the worker that stopped" "$(python3 -I -c "
import json
rows=[r for r in map(json.loads, open('$T/runs.jsonl')) if r['task_key']=='build-T003']
print(rows[-1]['verdict'], len(rows))")" "PASS 1"
OUT=$(resume_wf $RID -i human_verdict=reject)
check "reject aborts" "$(echo "$OUT" | wfget status)" aborted
check "T003 stays unchecked" "$(grep -c '\[ \] T003' $TASKS)" 1
reset $SPEC

echo "== D: an unchecked checklist stops the run before any worker starts"
unset DEVLOOP_FAKE_SCENARIO
OUT=$(run_wf $UNCHK); RID=$(echo "$OUT" | wfget run_id)
check "paused at the preflight gate" "$(echo "$OUT" | wfget current_step_id)" preflight-approval
check "no worktree was created" "$(ls $RINGER_LOOP_WORKTREES 2>/dev/null | grep -c unchecked)" 0
OUT=$(resume_wf $RID -i preflight_verdict=reject)
check "reject aborts" "$(echo "$OUT" | wfget status)" aborted
check "still no worktree" "$(ls $RINGER_LOOP_WORKTREES 2>/dev/null | grep -c unchecked)" 0
[ $fail = 0 ] && echo "PASS: the dev loop (outer loop, gates, scope change, resume) behaves as designed" || echo "FAIL: see above"
exit $fail
