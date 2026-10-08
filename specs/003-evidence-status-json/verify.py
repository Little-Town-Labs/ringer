#!/usr/bin/env python3
"""Acceptance check for `ringer evidence status --json` (spec.md R1-R7). Run from the repo root.

Drives the real CLI as a subprocess against temporary evidence files and prints every
mismatch, so a failing worker can see exactly what differs.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path.cwd()
fails: list[str] = []


def row(i: int, when: str, sink: str, reason: str | None = None) -> dict:
    return dict(run_id=f"r{i}", task_key=f"t{i}", verdict="PASS", logged_at=when, worker_engine="e", log_sink=sink, fallback_reason=reason)


def write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def digest(*paths: Path) -> str:
    return hashlib.sha256(b"".join(p.read_bytes() for p in paths if p.exists())).hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    env = dict(os.environ, HOME=str(tmp / "home"), RINGER_NO_SELF_UPDATE="1")
    (tmp / "home").mkdir()
    cfg = tmp / "cfg.toml"
    cfg.write_text(f'[eval]\nbackend = "jsonl"\njsonl_path = "{tmp}/none.jsonl"\n')

    good, second, empty, bad, invalid, missing = (tmp / n for n in ("good.jsonl", "second.jsonl", "empty.jsonl", "bad.jsonl", "invalid.jsonl", "missing.jsonl"))
    write(good, [row(1, "2026-10-01T10:00:00+00:00", "jsonl"), row(2, "2026-10-03T12:30:00+00:00", "postgres"),
                 row(3, "2026-10-02T09:15:00+00:00", "jsonl", "postgres connect failed: refused")])
    write(second, [row(4, "2026-09-20T08:00:00+00:00", "postgres"), row(5, "2026-10-05T18:45:00+02:00", "jsonl", "postgres insert failed: later")])
    empty.write_text("")
    bad.write_text(json.dumps(row(6, "2026-10-01T00:00:00+00:00", "jsonl")) + "\n\n{not json\n")
    invalid.write_text(json.dumps(dict(run_id="x", task_key="y", verdict="PASS")) + "\n")
    before = digest(good, second, empty, bad, invalid)

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "ringer.py", "--config", str(cfg), "evidence", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=120)

    def status_json(*files: Path) -> tuple[subprocess.CompletedProcess[str], dict | None]:
        proc = cli("status", "--json", *[a for f in files for a in ("--file", str(f))])
        try:
            return proc, json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            fails.append(f"stdout is not a single JSON document for {[f.name for f in files]}: {exc}; stdout was {proc.stdout[:300]!r}; stderr {proc.stderr[:300]!r}")
            return proc, None

    # R1, R2, R3: one good file
    proc, doc = status_json(good)
    if doc is not None:
        if proc.returncode != 0:
            fails.append(f"good file: exit {proc.returncode}, want 0")
        if list(doc) != ["ok", "files", "totals"]:
            fails.append(f"top-level keys {list(doc)}, want ['ok', 'files', 'totals']")
        entry = (doc.get("files") or [{}])[0]
        want_keys = ["path", "rows", "first_logged_at", "last_logged_at", "confirmed_central", "local_only", "latest_fallback_reason", "error"]
        if list(entry) != want_keys:
            fails.append(f"file entry keys {list(entry)}, want {want_keys}")
        want = {"path": str(good), "rows": 3, "first_logged_at": "2026-10-01T10:00:00+00:00", "last_logged_at": "2026-10-03T12:30:00+00:00",
                "confirmed_central": 1, "local_only": 2, "latest_fallback_reason": "postgres connect failed: refused", "error": None}
        if entry != want:
            fails.append(f"good file entry {entry} != {want}")
        if doc.get("ok") is not True:
            fails.append(f"ok should be true, got {doc.get('ok')!r}")
        tkeys = ["rows", "first_logged_at", "last_logged_at", "confirmed_central", "local_only", "latest_fallback_reason"]
        if list(doc.get("totals", {})) != tkeys:
            fails.append(f"totals keys {list(doc.get('totals', {}))}, want {tkeys}")

    # totals across two files; latest fallback chosen by instant, not by file order or string order
    proc, doc = status_json(good, second)
    if doc is not None:
        t = doc["totals"]
        want_t = {"rows": 5, "first_logged_at": "2026-09-20T08:00:00+00:00", "last_logged_at": "2026-10-05T18:45:00+02:00",
                  "confirmed_central": 2, "local_only": 3, "latest_fallback_reason": "postgres insert failed: later"}
        if t != want_t:
            fails.append(f"totals {t} != {want_t}")
        if [f["path"] for f in doc["files"]] != [str(good), str(second)]:
            fails.append("files must follow the --file argument order")

    # R5 empty file
    proc, doc = status_json(empty)
    if doc is not None:
        e = doc["files"][0]
        if proc.returncode != 0 or e["rows"] != 0 or e["confirmed_central"] != 0 or e["local_only"] != 0 or e["first_logged_at"] is not None \
                or e["last_logged_at"] is not None or e["latest_fallback_reason"] is not None or e["error"] is not None or doc["ok"] is not True:
            fails.append(f"empty file should be a valid zero entry with exit 0: exit {proc.returncode} {e}")

    # R4 error kinds, always alongside a good file so one bad file cannot hide the others
    def error_case(label: str, f: Path, needle: str) -> None:
        proc, doc = status_json(good, f)
        if doc is None:
            return
        if proc.returncode != 2:
            fails.append(f"{label}: exit {proc.returncode}, want 2")
        if doc["ok"] is not False:
            fails.append(f"{label}: ok should be false")
        by = {x["path"]: x for x in doc["files"]}
        bad_entry = by.get(str(f))
        if bad_entry is None:
            fails.append(f"{label}: no entry for {f.name}")
            return
        if not bad_entry["error"] or needle not in bad_entry["error"]:
            fails.append(f"{label}: error {bad_entry['error']!r} should contain {needle!r}")
        for k in ("rows", "first_logged_at", "last_logged_at", "confirmed_central", "local_only", "latest_fallback_reason"):
            if bad_entry[k] is not None:
                fails.append(f"{label}: {k} should be null in an error entry, got {bad_entry[k]!r}")
        if by[str(good)]["rows"] != 3 or by[str(good)]["error"] is not None:
            fails.append(f"{label}: the good file's entry must be unaffected")
        if doc["totals"]["rows"] != 3:
            fails.append(f"{label}: totals must cover readable files only, got {doc['totals']['rows']}")
        if not proc.stderr.strip():
            fails.append(f"{label}: a diagnostic should still be printed to stderr")

    error_case("malformed line", bad, f"{bad}:3")
    error_case("invalid row", invalid, "invalid row")
    error_case("missing file", missing, "not found")

    # R6: text mode byte-for-byte unchanged (golden captured from the implementation before this feature)
    proc = cli("status", "--file", str(good))
    golden = """evidence status: {p}
rows: 3
date range: 2026-10-01T10:00:00+00:00 to 2026-10-03T12:30:00+00:00
confirmed central (log_sink=postgres): 1
local-only (log_sink=jsonl): 2
rows: 3
date range: 2026-10-01T10:00:00+00:00 to 2026-10-03T12:30:00+00:00
confirmed central (log_sink=postgres): 1
local-only (log_sink=jsonl): 2
latest fallback_reason: postgres connect failed: refused
run: ringer evidence push
""".format(p=good)
    if proc.stdout != golden or proc.returncode != 0:
        fails.append(f"text mode changed (exit {proc.returncode}); got:\n{proc.stdout}want:\n{golden}")
    proc = cli("status", "--file", str(missing))
    if proc.returncode != 2 or "file not found" not in proc.stderr or proc.stdout.strip():
        fails.append(f"text mode with a missing file changed: exit {proc.returncode}, stdout {proc.stdout!r}, stderr {proc.stderr!r}")

    # R6: --json is for status only
    proc = cli("push", "--json", "--dry-run", "--file", str(good))
    if proc.returncode != 2 or "Traceback" in proc.stderr:
        fails.append(f"push --json must be rejected by the parser with exit 2 (got {proc.returncode}): {proc.stderr[-200:]!r}")
    helptext = cli("status", "--help").stdout
    if "--json" not in helptext:
        fails.append("`evidence status --help` should list --json")

    # R7 and hygiene: no tracebacks anywhere, files untouched
    for args in (("status", "--json", "--file", str(bad)), ("status", "--json", "--file", str(invalid))):
        if "Traceback" in cli(*args).stderr:
            fails.append(f"traceback printed for {args}")
    if digest(good, second, empty, bad, invalid) != before:
        fails.append("an evidence file was modified")

    if fails:
        print(f"FAIL: {len(fails)} problem(s):")
        for message in fails:
            print(" -", message)
        return 1
    print("PASS: evidence status --json meets spec R1-R7 and text mode is unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
