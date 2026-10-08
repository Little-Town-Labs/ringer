#!/usr/bin/env python3
"""Quality-gate report check (five-axis review).

Usage: check_gate_report.py --repo REPO --report report.md

Requires the exact headings, a verdict that is exactly APPROVE or REQUEST CHANGES, all five axes
named in the Five-axis summary, labelled findings with Evidence/Impact/Fix, real citations and
quotes for every Critical or Required finding, and consistency between verdict and findings.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from check_review import citation_problems, evidence_of

HEADINGS = ("# Code Review Quality Gate", "## Scope", "## Findings", "## Five-axis summary", "## Verification", "## Verdict")
AXES = ("correctness", "readability", "architecture", "security", "performance")
HEADING = re.compile(r"^###\s+\[?(Critical|Required|Optional|Consider|Nit|FYI)\]?:?\s*(.*)$", re.I | re.M)


def section(text: str, heading: str) -> str:
    m = re.search(rf"^{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s|\Z)", text, re.M)
    return m.group(1) if m else ""


def problems_in(text: str, repo: Path) -> list[str]:
    problems = []
    positions = []
    for h in HEADINGS:
        m = re.search(rf"^{re.escape(h)}\s*$", text, re.M)
        if not m:
            problems.append(f"missing heading '{h}'")
        else:
            positions.append(m.start())
    if len(positions) == len(HEADINGS) and positions != sorted(positions):
        problems.append("headings are out of order; required order: " + ", ".join(HEADINGS))
    summary = section(text, "## Five-axis summary").lower()
    missing_axes = [a for a in AXES if a not in summary]
    if missing_axes:
        problems.append("the Five-axis summary does not cover: " + ", ".join(missing_axes))
    m = re.search(r"^## Verdict\s*$([\s\S]*?)(?=^##\s|\Z)", text, re.M)
    first = next((l.strip() for l in m.group(1).splitlines() if l.strip()), "") if m else ""
    if first not in ("APPROVE", "REQUEST CHANGES"):
        problems.append(f"the first line under '## Verdict' must be exactly APPROVE or REQUEST CHANGES, got {first!r}")
    findings_text = section(text, "## Findings")
    parts = HEADING.split(findings_text)
    blocking, to_verify = 0, []
    for i in range(1, len(parts) - 2, 3):
        severity, title, body = parts[i].capitalize(), parts[i + 1].strip(), parts[i + 2]
        for field in ("Evidence:", "Impact:", "Fix:"):
            if field.lower() not in body.lower():
                problems.append(f"finding {title[:60]!r} is missing '{field}'")
        if severity in ("Critical", "Required"):
            blocking += 1
            to_verify.append((title, evidence_of(body)))
    if first == "APPROVE" and blocking:
        problems.append("the verdict is APPROVE but a Critical or Required finding is listed")
    if first == "REQUEST CHANGES" and not blocking:
        problems.append("the verdict is REQUEST CHANGES but no Critical or Required finding is listed")
    problems += citation_problems(repo, to_verify)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--report", default="report.md")
    args = ap.parse_args()
    path = Path(args.report)
    if not path.is_file() or not path.read_text(encoding="utf-8", errors="replace").strip():
        print(f"FAIL: {path} is missing or empty")
        return 1
    problems = problems_in(path.read_text(encoding="utf-8", errors="replace"), Path(args.repo))
    if problems:
        print("FAIL: the quality-gate report does not meet its contract:")
        for p in problems:
            print(" -", p)
        return 1
    print("PASS: quality-gate report is complete, covers all five axes, and its citations are real")
    return 0


if __name__ == "__main__":
    sys.exit(main())
