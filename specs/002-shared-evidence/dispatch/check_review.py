#!/usr/bin/env python3
"""Review check: kit report contract + every cited file:line is real + long quotes exist.

Usage: check_review.py --repo REPO --report report.md --surface KEY --kit-check PATH
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CITE = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|sql|md|yaml|yml|toml|sh|json|html)):(\d+)(?:-(\d+))?")
QUOTE = re.compile(r"`([^`\n]{20,})`")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
    problems: list[str] = []
    for index, block in enumerate(findings, 1):
        title = block.splitlines()[0].strip()[:70] if block.strip() else "?"
        evidence = re.search(r"Evidence:([\s\S]*?)(?=^\s*Impact:|\Z)", block, re.M)
        ev = evidence.group(1) if evidence else block
        cited: list[Path] = []
        good = 0
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
            good += 1
            cited.append(path)
        if good == 0:
            problems.append(f"finding {index} ({title!r}): no valid file:line citation in Evidence")
            continue
        haystacks = [norm(p.read_text(encoding="utf-8", errors="replace")) for p in set(cited)]
        for q in QUOTE.findall(ev):
            if CITE.search(q):
                continue
            if not any(norm(q) in h for h in haystacks):
                problems.append(f"finding {index} ({title!r}): quoted text not found in the cited file(s): `{q[:80]}`")
    if problems:
        print("FAIL: citations or quotes do not match the repository:")
        for p in problems:
            print(" -", p)
        return 1
    print(f"PASS: report contract met; {len(findings)} finding(s), every citation and quote verified against {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
