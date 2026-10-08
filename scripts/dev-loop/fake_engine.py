#!/usr/bin/env python3
"""Scripted stand-in for a model worker, used only to test the dev-loop plumbing offline.

Ringer calls it as: fake_engine.py TASKDIR SPEC. It reads a scenario file from the loop's spec
directory (fake-scenario.json, or the file named by DEVLOOP_FAKE_SCENARIO) and acts according to the
task key, the wave and the round, so a whole multi-wave feature can be exercised deterministically.

Scenario keys ("<wave>-<round>" for reviews, gates and fixes):
  tasks:   {"T001": {"files": {path: text}, "scope_change": "decision text"}}
  reviews: {"1-1": {"correctness": [{"title", "file", "line", "priority", "confidence"}]}}
  gate:    {"1-1": {"verdict": "REQUEST CHANGES", "findings": [{"severity", "title", "file", "line"}]}}
  fix:     {"1-1": {"files": {path: text}}}
  closeout: {"verdict": "APPROVE", "findings": []}
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

AXES = ("Correctness", "Readability", "Architecture", "Security", "Performance")


def quote_of(wt: Path, rel: str, line: int) -> str:
    return (wt / rel).read_text().splitlines()[line - 1].strip()


def write_files(wt: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def lens_report(wt: Path, key: str, rnd: str, findings: list[dict]) -> str:
    lines = ["# Review Report", "", "## Summary", f"- scripted review {rnd} for {key}", "", "## Findings"]
    if not findings:
        lines.append("No findings for this surface.")
    for f in findings:
        lines += [f"### Finding: {f['title']}", f"Evidence: {f['file']}:{f['line']} `{quote_of(wt, f['file'], f['line'])}`",
                  "Impact: scripted impact", "Fix: scripted fix", f"Priority: {f['priority']}", f"Confidence: {f['confidence']}", ""]
    return "\n".join(lines + ["", "## Clean", "- scripted", "", "## Assumptions", "- scripted engine"]) + "\n"


def gate_report(wt: Path, verdict: str, findings: list[dict]) -> str:
    lines = ["# Code Review Quality Gate", "", "## Scope", "Scripted scope.", "", "## Findings"]
    if not findings:
        lines.append("No findings.")
    for f in findings:
        lines += [f"### [{f['severity']}] {f['title']}", f"Evidence: {f['file']}:{f['line']} `{quote_of(wt, f['file'], f['line'])}`",
                  "Impact: scripted impact", "Fix: scripted fix", ""]
    lines += ["", "## Five-axis summary"] + [f"- {a}: scripted judgement" for a in AXES]
    lines += ["", "## Verification", "Scripted run.", "", "## Verdict", verdict, ""]
    return "\n".join(lines)


def main() -> int:
    taskdir, spec = Path(sys.argv[1]), sys.argv[2]
    key = taskdir.name
    wt = Path((re.search(r"checkout you may edit is (\S+)", spec) or re.search(r"repository at '([^']+)'", spec)).group(1))
    spec_dir = re.search(r"((?:[\w.-]+/)+)spec\.md", spec).group(1).rstrip("/")
    scenario = json.loads((wt / spec_dir / os.environ.get("DEVLOOP_FAKE_SCENARIO", "fake-scenario.json")).read_text())
    parent = taskdir.parent.name
    wave = (re.match(r"w(\d+)-", parent) or [None, "1"])[1]
    rnd = (re.search(r"(?:review|fix)-(\d+)$", parent) or re.search(r"^fix-(\d+)$", key) or [None, "1"])[1]
    slot = f"{wave}-{rnd}"

    if key.startswith("build-"):
        task = scenario["tasks"][key.removeprefix("build-")]
        if task.get("scope_change"):
            (taskdir / "scope-change.md").write_text(
                "# Scope Change Required\n\n## Blocking Task\n" + key + "\n\n## Evidence\nScripted.\n\n## Required Decision\n"
                + task["scope_change"] + "\n\n## Suggested Spec Kit Update\nScripted.\n")
        else:
            write_files(wt, task.get("files", {}))
        (taskdir / "notes.md").write_text(f"# notes\nscripted {key}\n")
    elif key.startswith("fix-"):
        write_files(wt, scenario.get("fix", {}).get(slot, {}).get("files", {}))
        (taskdir / "notes.md").write_text(f"# notes\nscripted {key} for {slot}\n")
    elif key == "quality-gate":
        g = scenario.get("gate", {}).get(slot, {"verdict": "APPROVE", "findings": []})
        (taskdir / "report.md").write_text(gate_report(wt, g["verdict"], g["findings"]))
    elif key == "closeout-gate":
        g = scenario.get("closeout", {"verdict": "APPROVE", "findings": []})
        (taskdir / "report.md").write_text(gate_report(wt, g["verdict"], g["findings"]))
    else:
        (taskdir / "report.md").write_text(lens_report(wt, key, slot, scenario.get("reviews", {}).get(slot, {}).get(key, [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
