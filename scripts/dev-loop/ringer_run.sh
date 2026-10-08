#!/usr/bin/env bash
# Generate a loop manifest and run it through Ringer.
#   ringer_run.sh KIND SPEC_DIR [RINGER_CONFIG]    KIND = build | review | fix | closeout
# The Ringer exit status is recorded in the run state: the workflow continues past a failed run so
# review can see it, and `decide` escalates a wave whose build run did not succeed.
set -uo pipefail
cd "$(dirname "$0")/../.."
KIND="$1"; SPEC_DIR="$2"; CFG="${3:-}"
RB=(./ringer.py)
[ -n "$CFG" ] && RB=(./ringer.py --config "$CFG")
export RINGER_NO_SELF_UPDATE=1
rc=0
M=$(python3 scripts/dev-loop/devloop.py manifest "$SPEC_DIR" "$KIND") || rc=1
if [ $rc -eq 0 ]; then
  "${RB[@]}" lint "$M" || rc=1
fi
if [ $rc -eq 0 ]; then
  "${RB[@]}" run "$M" --identity dev-loop || rc=$?
fi
python3 scripts/dev-loop/devloop.py record-run "$SPEC_DIR" "$KIND" "$rc" >/dev/null 2>&1
exit $rc
