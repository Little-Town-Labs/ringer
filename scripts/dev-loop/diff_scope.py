#!/usr/bin/env python3
"""Fail if a file changed outside the named top-level functions.

  diff_scope.py BASE FILE FUNC [FUNC ...] [--imports-up-to N]

Compares the working tree of FILE with BASE. Every changed hunk (by line number in the BASE
version) must fall inside one of the named top-level functions or classes, or inside the first
N lines of the file when --imports-up-to is given. Prints each offending hunk so a worker can
see what to move. Run it from the repository root.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys


def span(lines: list[str], name: str) -> tuple[int, int]:
    start = next((i for i, l in enumerate(lines, 1) if re.match(rf"(async )?(def|class) {re.escape(name)}\b", l)), None)
    if start is None:
        raise SystemExit(f"diff_scope: {name} not found in the base version")
    end = next((i for i, l in enumerate(lines[start:], start + 1) if re.match(r"(class |def |async def )", l)), len(lines) + 1) - 1
    return start, end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("file")
    ap.add_argument("funcs", nargs="+")
    ap.add_argument("--imports-up-to", type=int, default=0)
    args = ap.parse_args()
    base = subprocess.run(["git", "show", f"{args.base}:{args.file}"], capture_output=True, text=True, check=True).stdout.splitlines()
    allowed = [span(base, name) for name in args.funcs]
    diff = subprocess.run(["git", "diff", "-U0", args.base, "--", args.file], capture_output=True, text=True, check=True).stdout
    bad = []
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", diff, re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        lo, hi = start, start + max(count, 1) - 1
        if count == 0:  # pure insertion after line `start`
            lo = hi = start
        if not (hi <= args.imports_up_to or any(a <= lo and hi <= b for a, b in allowed)):
            bad.append((lo, hi))
    if bad:
        print(f"FAIL: {args.file} changed outside {', '.join(args.funcs)}"
              + (f" and its first {args.imports_up_to} lines" if args.imports_up_to else "") + ":")
        for lo, hi in bad:
            print(f"  - base lines {lo}-{hi}; allowed spans: {allowed}")
        return 1
    print(f"PASS: {args.file} changes stay inside {', '.join(args.funcs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
