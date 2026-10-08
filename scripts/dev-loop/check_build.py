#!/usr/bin/env python3
"""Build check wrapper that accepts a valid scope-change report.

Usage: check_build.py --repo REPO --allow-status PATHS -- KIT_CHECK_COMMAND...

If ./scope-change.md exists it must carry the five required headings, and the repository must
have no changes outside the allowed paths; then the check passes (so Ringer does not retry a worker
that correctly stopped) and the loop escalates the scope change. Otherwise the normal check runs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HEADINGS = ("# Scope Change Required", "## Blocking Task", "## Evidence", "## Required Decision", "## Suggested Spec Kit Update")


def changed_outside(repo: Path, allowed: list[str]) -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout.splitlines()
    paths = [line[3:].strip() for line in out if line.strip()]
    return [p for p in paths if not any(p == a or p.startswith(a.rstrip("/") + "/") or a.startswith(p.rstrip("/") + "/") for a in allowed if a)]


def main() -> int:
    argv = sys.argv[1:]
    if "--" not in argv:
        print("FAIL: check_build.py needs '-- <check command>'")
        return 2
    cut = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--allow-status", default="")
    args = ap.parse_args(argv[:cut])
    command = argv[cut + 1:]
    report = Path("scope-change.md")
    if report.is_file() and report.stat().st_size > 0:
        text = report.read_text(encoding="utf-8", errors="replace")
        missing = [h for h in HEADINGS if h not in text]
        if missing:
            print("FAIL: scope-change.md is missing: " + ", ".join(missing))
            return 1
        stray = changed_outside(Path(args.repo), [a for a in args.allow_status.split(",") if a])
        if stray:
            print("FAIL: scope-change.md was written but the repository also changed; leave every tracked file unchanged: " + ", ".join(stray[:6]))
            return 1
        print("PASS: a valid scope-change report was written and the repository is unchanged; the loop will escalate it")
        return 0
    return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
