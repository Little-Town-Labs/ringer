#!/usr/bin/env bash
# Generate a loop manifest and run it through Ringer.
#   ringer_run.sh KIND SPEC_DIR [RINGER_CONFIG]    KIND = build | review | fix
set -uo pipefail
cd "$(dirname "$0")/../.."
KIND="$1"; SPEC_DIR="$2"; CFG="${3:-}"
M=$(python3 scripts/dev-loop/devloop.py manifest "$SPEC_DIR" "$KIND") || exit 1
RB=(./ringer.py)
[ -n "$CFG" ] && RB=(./ringer.py --config "$CFG")
export RINGER_NO_SELF_UPDATE=1
"${RB[@]}" lint "$M" || exit 1
"${RB[@]}" run "$M" --identity dev-loop
