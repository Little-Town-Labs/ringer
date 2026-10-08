#!/usr/bin/env python3
"""T005 check. Run from the repo root.

1. Full suite passes except the known pre-existing contributor-credit failure.
2. ringer.py edits stay inside the import block, build_parser and main.
3. The real CLI (ringer.main) is driven in-process with a fake psycopg across
   push, dry-run, validation failure, filters, idempotent re-push, config and
   connection failures, and status; asserting outputs, exit codes and that the
   JSONL files are never modified.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO = Path.cwd()
KNOWN = "test_every_merged_contributor_is_credited_in_readme"
fails: list[str] = []


def suite() -> None:
    env = dict(os.environ, RINGER_NO_SELF_UPDATE="1")
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
    out = proc.stdout + proc.stderr
    bad = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
    unexpected = [n for n in bad if n != KNOWN]
    ran = re.search(r"^Ran (\d+) tests", out, re.M)
    print(f"suite: {ran.group(0) if ran else 'no summary'}; failing: {bad}")
    if unexpected or not ran:
        fails.append(f"unexpected suite failures: {unexpected}")
        print(out[-4000:])


def span(lines: list[str], name: str) -> tuple[int, int]:
    start = next(i for i, l in enumerate(lines, 1) if re.match(rf"(async )?def {name}\b", l))
    end = next((i for i, l in enumerate(lines[start:], start + 1) if re.match(r"(class |def |async def )", l)), len(lines) + 1) - 1
    return start, end


def diff_scope() -> None:
    head = subprocess.run(["git", "show", "HEAD:ringer.py"], cwd=REPO, capture_output=True, text=True).stdout.splitlines()
    allowed = [span(head, "build_parser"), span(head, "main")]
    diff = subprocess.run(["git", "diff", "-U0", "ringer.py"], cwd=REPO, capture_output=True, text=True).stdout
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", diff, re.M):
        a, n = int(m.group(1)), int(m.group(2) or 1)
        lo, hi = a, a + max(n, 1) - 1
        if not (hi <= 60 or any(s <= lo and hi <= e for s, e in allowed)):
            fails.append(f"ringer.py edit at old lines {lo}-{hi} is outside the import block, build_parser {allowed[0]} and main {allowed[1]}")
    print(f"ringer.py diff scope checked against build_parser {allowed[0]} and main {allowed[1]}")


class FakeCursor:
    def __init__(self, conn): self.conn = conn; self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        uid = params["attempt_uid"]
        self.conn.executed.append(params)
        if uid in self.conn.store:
            self.rowcount = 0
        else:
            self.conn.store.add(uid); self.rowcount = 1


class FakeConn:
    store: set = set()
    executed: list = []
    commits = 0
    def __init__(self): pass
    def cursor(self): return FakeCursor(FakeConn)
    def commit(self): FakeConn.commits += 1
    def rollback(self): pass
    def close(self): pass


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def drive() -> None:
    os.environ["RINGER_NO_SELF_UPDATE"] = "1"
    tmp = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(tmp / "home")
    os.environ.pop("XDG_CONFIG_HOME", None); os.environ.pop("XDG_STATE_HOME", None)
    sys.path.insert(0, str(REPO))
    fake = types.ModuleType("psycopg")
    calls = {"connect": 0}

    def fake_connect(**kw):
        calls["connect"] += 1
        if os.environ.get("FAKE_DB_DOWN"):
            raise OSError("connection refused")
        return FakeConn()
    fake.connect = fake_connect
    sys.modules["psycopg"] = fake
    import ringer
    from ringer_core import central_evidence as ce

    envf = tmp / "db.env"
    envf.write_text("RINGER_DB_HOST=db.example\nRINGER_DB_PORT=5440\nRINGER_DB_USER=writer\nRINGER_DB_PASSWORD=topsecret\nRINGER_DB_NAME=ringer\n")
    jl, jl2 = tmp / "runs.jsonl", tmp / "old.jsonl"

    def row(i, when, sink="jsonl", reason=None, verdict="PASS"):
        return dict(run_id=f"r{i}", pattern="p", task_key=f"t{i}", spec="the prompt", worker_engine="codex",
                    shepherd_model="s", verify_method="v", verdict=verdict, duration_ms=10, worker_tokens=5,
                    notes="n", orchestrator="o", model="m", task_type="code-feature", reasoning_effort="high",
                    retry=False, logged_at=when, log_sink=sink, fallback_reason=reason)
    r1 = row(1, "2026-09-01T10:00:00+00:00")
    r2 = row(2, "2026-10-01T10:00:00+00:00", "postgres")
    r3 = row(3, "2026-10-05T10:00:00+00:00", "jsonl", "postgres connect failed: boom", "FAIL")
    jl.write_text("\n".join(json.dumps(r) for r in (r1, r2, r3)) + "\n")
    r4 = row(4, "2026-08-09T10:00:00+00:00")
    jl2.write_text(json.dumps(r4) + "\n")
    cfg = tmp / "config.toml"
    cfg.write_text(f'[eval]\nbackend = "jsonl"\njsonl_path = "{jl}"\n[eval.postgres]\nenv_file = "{envf}"\nsource_host = "hostA"\n')
    cfg_nopg = tmp / "config-nopg.toml"
    cfg_nopg.write_text(f'[eval]\nbackend = "jsonl"\njsonl_path = "{jl}"\n')
    before = (sha(jl), sha(jl2))

    def cli(*argv, config=cfg):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ringer.main(["--config", str(config), *argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    # help wiring
    code, out, err = cli("evidence", "--help")
    if code != 0 or "push" not in out or "status" not in out:
        fails.append(f"`evidence --help` should list push and status (code {code})")

    # dry run: no connection, counts, files untouched
    calls["connect"] = 0
    code, out, err = cli("evidence", "push", "--dry-run", "--file", str(jl), "--file", str(jl2))
    if code != 0 or calls["connect"] != 0:
        fails.append(f"dry-run must exit 0 without connecting (code {code}, connects {calls['connect']}): {err[-300:]}")
    if not re.search(r"\b4\b", out) or "DRY RUN" not in out:
        fails.append(f"dry-run output should report 4 rows and say DRY RUN: {out[-400:]!r}")
    if "topsecret" in out + err:
        fails.append("password leaked in dry-run output")

    # validation failure: whole command aborts, names path:line, nothing sent
    bad = tmp / "bad.jsonl"
    bad.write_text(json.dumps(r1) + "\n{not json\n")
    calls["connect"] = 0
    code, out, err = cli("evidence", "push", "--file", str(jl), "--file", str(bad))
    if code != 2 or calls["connect"] != 0 or f"{bad}:2" not in (out + err):
        fails.append(f"malformed file must exit 2, name '{bad}:2', and never connect (code {code}, connects {calls['connect']}): {(out+err)[-300:]!r}")

    # real push through fake db, then idempotent re-push
    FakeConn.store.clear(); FakeConn.executed.clear(); FakeConn.commits = 0
    code, out, err = cli("evidence", "push", "--file", str(jl), "--file", str(jl2))
    final = [l for l in out.splitlines() if "inserted" in l.lower()]
    if code != 0 or not final or not re.search(r"inserted\s+4\b", final[-1], re.I) or not re.search(r"already present\s+0\b", final[-1], re.I):
        fails.append(f"first push should insert 4 and find 0 present (code {code}): {out[-400:]!r} {err[-200:]!r}")
    if "topsecret" in out + err:
        fails.append("password leaked in push output")
    if FakeConn.commits < 2:
        fails.append(f"expected one commit per file (2), saw {FakeConn.commits}")
    for p in FakeConn.executed:
        if p["source_host"] != "hostA" or p["spec"] is not None or not p["spec_sha256"]:
            fails.append(f"push params wrong (source_host/spec policy): {p['source_host']!r} {p['spec']!r}"); break
    uids = {p["attempt_uid"] for p in FakeConn.executed}
    if uids != {ce.attempt_uid("hostA", r) for r in (r1, r2, r3, r4)}:
        fails.append("pushed attempt_uids differ from attempt_uid(source_host, jsonl_row); a later logger/backfill would duplicate")
    pushed = {p["run_id"]: p for p in FakeConn.executed}
    if pushed.get("r3", {}).get("log_sink") != "jsonl" or "boom" not in (pushed.get("r3", {}).get("fallback_reason") or ""):
        fails.append("push must preserve each row's original log_sink and fallback_reason")
    if pushed.get("r2", {}).get("log_sink") != "postgres":
        fails.append("push must preserve log_sink 'postgres' on rows already marked central")
    code, out, err = cli("evidence", "push", "--file", str(jl), "--file", str(jl2))
    final = [l for l in out.splitlines() if "inserted" in l.lower()]
    if code != 0 or not final or not re.search(r"inserted\s+0\b", final[-1], re.I) or not re.search(r"already present\s+4\b", final[-1], re.I):
        fails.append(f"re-push should insert 0 and find 4 present (code {code}): {out[-300:]!r}")

    # filters and overrides
    FakeConn.store.clear(); FakeConn.executed.clear()
    code, out, err = cli("evidence", "push", "--file", str(jl), "--since", "2026-10-01", "--source-host", "hostB", "--spec-storage", "excerpt")
    got = {p["run_id"] for p in FakeConn.executed}
    if code != 0 or got != {"r2", "r3"}:
        fails.append(f"--since 2026-10-01 should select r2 and r3, got {sorted(got)} (code {code}) {err[-200:]!r}")
    if any(p["source_host"] != "hostB" or p["spec"] != "the prompt" for p in FakeConn.executed):
        fails.append("--source-host and --spec-storage overrides not applied")
    code, out, err = cli("evidence", "push", "--file", str(jl), "--since", "not-a-date")
    if code != 2:
        fails.append(f"invalid --since must exit 2, got {code}")

    # configuration and connection failures
    code, out, err = cli("evidence", "push", "--file", str(jl), config=cfg_nopg)
    if code != 2 or "eval.postgres" not in (out + err):
        fails.append(f"push without [eval.postgres] must exit 2 and mention eval.postgres (code {code}): {(out+err)[-200:]!r}")
    sys.modules["psycopg"] = None
    code, out, err = cli("evidence", "push", "--file", str(jl))
    if code != 2 or "psycopg" not in (out + err):
        fails.append(f"missing psycopg must exit 2 with an install hint (code {code}): {(out+err)[-200:]!r}")
    sys.modules["psycopg"] = fake
    os.environ["FAKE_DB_DOWN"] = "1"
    code, out, err = cli("evidence", "push", "--file", str(jl))
    del os.environ["FAKE_DB_DOWN"]
    if code != 3:
        fails.append(f"unreachable database must exit 3, got {code}: {(out+err)[-200:]!r}")
    bad_env = tmp / "bad.env"; bad_env.write_text("RINGER_DB_HOST=x\n")
    cfg_badenv = tmp / "config-badenv.toml"
    cfg_badenv.write_text(f'[eval]\nbackend = "jsonl"\njsonl_path = "{jl}"\n[eval.postgres]\nenv_file = "{bad_env}"\n')
    code, out, err = cli("evidence", "push", "--file", str(jl), config=cfg_badenv)
    if code != 2 or "RINGER_DB_" not in (out + err):
        fails.append(f"incomplete env file must exit 2 naming RINGER_DB_ settings (code {code}): {(out+err)[-200:]!r}")

    # status: local only
    calls["connect"] = 0
    code, out, err = cli("evidence", "status", "--file", str(jl))
    low = out.lower()
    if code != 0 or calls["connect"] != 0:
        fails.append(f"status must exit 0 and never connect (code {code}, connects {calls['connect']})")
    for needle in ("rows", "log_sink", "boom"):
        if needle not in low:
            fails.append(f"status output should mention {needle!r}: {out[-500:]!r}")
    if not re.search(r"local-only[^\n]*\b2\b", low) and not re.search(r"\b2\b[^\n]*local-only", low):
        fails.append("status should report 2 local-only rows (log_sink jsonl)")
    if not re.search(r"central[^\n]*\b1\b", low):
        fails.append("status should report 1 confirmed central row (log_sink postgres)")

    if (sha(jl), sha(jl2)) != before:
        fails.append("a command modified a JSONL evidence file")
    print("evidence CLI drive complete")


def main() -> int:
    for step in (diff_scope, drive, suite):
        try:
            step()
        except Exception as exc:
            fails.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
    if fails:
        print("FAIL:")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS: evidence push/status behave end to end, ringer.py edits confined, suite clean except the known failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
