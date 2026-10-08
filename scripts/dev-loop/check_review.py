#!/usr/bin/env python3
"""Review check: kit report contract + every cited file:line is real + long quotes exist.

Usage: check_review.py --repo REPO --report report.md --surface KEY --kit-check PATH

`citation_problems` is shared with check_gate_report.py.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CITE = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}):(\d+)(?:-(\d+))?")
QUOTE = re.compile(r"`([^`\n]{20,})`")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def evidence_of(block: str) -> str:
    m = re.search(r"Evidence:([\s\S]*?)(?=^\s*Impact:|\Z)", block, re.M)
    return m.group(1) if m else block


def citation_problems(repo: Path, blocks: list[tuple[str, str]]) -> list[str]:
    """For (title, evidence_text) pairs: every finding needs a real file:line, and long quotes must appear in the cited files."""
    problems: list[str] = []
    for index, (title, ev) in enumerate(blocks, 1):
        title = title[:70]
        cited: list[Path] = []
        for m in CITE.finditer(ev):
            rel = m.group(1).removeprefix("./")
            if rel.startswith(str(repo)):
                rel = rel[len(str(repo)):].lstrip("/")
            path = repo / rel
            if not path.is_file():
                problems.append(f"finding {index} ({title!r}): cited file does not exist: {rel}")
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            last = int(m.group(3) or m.group(2))
            if int(m.group(2)) < 1 or last > len(lines):
                problems.append(f"finding {index} ({title!r}): {rel}:{m.group(2)} is outside the file ({len(lines)} lines)")
                continue
            cited.append(path)
        if not cited:
            problems.append(f"finding {index} ({title!r}): no valid file:line citation in Evidence")
            continue
        haystacks = [norm(p.read_text(encoding="utf-8", errors="replace")) for p in set(cited)]
        for q in QUOTE.findall(ev):
            if CITE.search(q):
                continue
            if not any(norm(q) in h for h in haystacks):
                problems.append(f"finding {index} ({title!r}): quoted text not found in the cited file(s): `{q[:80]}`")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--surface", required=True)
    ap.add_argument("--kit-check", required=True)
    args = ap.parse_args()
    repo = Path(args.repo)

    kit = subprocess.run([sys.executable, args.kit_check, "--report", args.report, "--surface", args.surface],
                         capture_output=True, text=True)
    if kit.returncode != 0:
        print(kit.stdout + kit.stderr)
        return 1

    text = Path(args.report).read_text(encoding="utf-8", errors="replace")
    findings = re.split(r"^###\s+Finding:", text, flags=re.M)[1:]
    blocks = [((b.splitlines()[0] if b.strip() else "?"), evidence_of(b)) for b in findings]
    problems = citation_problems(repo, blocks)
    if problems:
        print("FAIL: citations or quotes do not match the repository:")
        for p in problems:
            print(" -", p)
        return 1
    print(f"PASS: report contract met; {len(findings)} finding(s), every citation and quote verified against {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
