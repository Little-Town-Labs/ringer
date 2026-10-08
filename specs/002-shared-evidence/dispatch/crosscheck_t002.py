#!/usr/bin/env python3
"""T002 check: ringer_core.central_evidence must reproduce backfill.py exactly.

Run from the repo root. Reads the real local evidence files read-only and compares
every row's parameters with those the production backfill loaded. Prints the first
mismatches so a failing worker can see exactly what differs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path.cwd()
sys.path.insert(0, str(REPO))
GOLDEN_UID = "d44445b75273c1b76e4548a6684a7c5ec724fa36f922f2b9603d372aa5e78479"
GOLDEN = dict(
    logged_at="2026-10-07T13:47:30.683590+00:00",
    run_id="cloak-lite-006-attributes-restoration-quality-20261007T134615Z-p1195023",
    task_key="attrfix-review",
    worker_engine="quality-reviewer",
)
FILES = ("~/.ringer/runs.jsonl", "~/.local/share/ringer/performance/runs.jsonl")


def load_backfill():
    path = REPO / "specs" / "002-shared-evidence" / "backfill.py"
    spec = importlib.util.spec_from_file_location("backfill_ref", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    import ringer_core.central_evidence as ce

    ref = load_backfill()
    failures: list[str] = []
    if tuple(ce.ATTEMPT_COLUMNS) != tuple(ref.COLUMNS):
        failures.append(f"ATTEMPT_COLUMNS differ from backfill COLUMNS:\n  got  {ce.ATTEMPT_COLUMNS}\n  want {tuple(ref.COLUMNS)}")
    got_uid = ce.attempt_uid("powerbox2", GOLDEN)
    if got_uid != GOLDEN_UID:
        failures.append(f"golden attempt_uid mismatch: got {got_uid} want {GOLDEN_UID}")

    checked = 0
    for name in FILES:
        path = Path(os.path.expanduser(name))
        if not path.exists():
            print(f"note: {path} not present, skipped")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            want = ref.convert(row, "powerbox2", "hash")
            got = ce.to_params(row, "powerbox2", "hash")
            checked += 1
            if set(got) != set(want):
                failures.append(f"{path}:{number}: key set differs: extra={sorted(set(got)-set(want))} missing={sorted(set(want)-set(got))}")
                continue
            diff = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
            if diff:
                failures.append(f"{path}:{number}: {diff}")
    if checked < 300:
        failures.append(f"expected to compare about 323 real rows, compared {checked}")
    if failures:
        print(f"FAIL: {len(failures)} problem(s) over {checked} rows (showing up to 8)")
        for message in failures[:8]:
            print(" -", message[:600])
        return 1
    print(f"PASS: golden uid matches and {checked} real rows reproduce backfill.py parameters exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
