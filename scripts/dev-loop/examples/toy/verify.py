#!/usr/bin/env python3
"""Acceptance check for the toy loop: every file under toy_out is non-empty and has no TODO marker."""
import sys
from pathlib import Path

bad = []
for path in sorted(Path("toy_out").rglob("*")):
    if path.is_file():
        text = path.read_text()
        if not text.strip():
            bad.append(f"{path} is empty")
        if "TODO" in text:
            bad.append(f"{path} still contains a TODO marker")
if bad:
    print("FAIL: " + "; ".join(bad))
    sys.exit(1)
print("PASS: toy_out files are finished")
