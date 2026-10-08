#!/usr/bin/env python3
"""T004 check. Run from the repo root.

1. Full suite passes except the known pre-existing contributor-credit failure.
2. The ringer.py diff stays inside the EvalLogger class (and the import block).
3. An independent end-to-end drive of the real EvalLogger with a fake psycopg:
   success -> failure -> fallback, asserting dual-write, identity, sinks, wording,
   and exactly one stderr warning.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
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
    unexpected = [name for name in bad if name != KNOWN]
    ran = re.search(r"^Ran (\d+) tests", out, re.M)
    print(f"suite: {ran.group(0) if ran else 'no summary'}; failing: {bad}")
    if unexpected or not ran:
        fails.append(f"unexpected suite failures: {unexpected}")
        print(out[-4000:])


def diff_scope() -> None:
    head = subprocess.run(["git", "show", "HEAD:ringer.py"], cwd=REPO, capture_output=True, text=True).stdout.splitlines()
    start = next(i for i, l in enumerate(head, 1) if l.startswith("class EvalLogger"))
    end = next((i for i, l in enumerate(head[start:], start + 1) if re.match(r"(class |def |async def )", l)), len(head) + 1) - 1
    diff = subprocess.run(["git", "diff", "-U0", "ringer.py"], cwd=REPO, capture_output=True, text=True).stdout
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", diff, re.M):
        a, n = int(m.group(1)), int(m.group(2) or 1)
        lo, hi = a, a + max(n, 1) - 1
        inside_class = start <= lo and hi <= end
        in_imports = hi <= 60
        if not (inside_class or in_imports):
            fails.append(f"ringer.py edit at old lines {lo}-{hi} is outside EvalLogger ({start}-{end}) and the import block")
    src = (REPO / "ringer.py").read_text().splitlines()
    cs = next(i for i, l in enumerate(src) if l.startswith("class EvalLogger"))
    ce = next((i for i, l in enumerate(src[cs + 1:], cs + 1) if re.match(r"(class |def |async def )", l)), len(src))
    if any("supabase" in l.lower() for l in src[cs:ce]):
        fails.append("the word 'Supabase' is still present inside EvalLogger")
    print(f"ringer.py diff scope checked against EvalLogger lines {start}-{end}")


class FakeConn:
    log: list = []
    boom_on = 2

    def execute(self, sql, params=None):
        FakeConn.log.append((sql, dict(params or {})))
        if len(FakeConn.log) == FakeConn.boom_on:
            raise RuntimeError("simulated insert failure")

    def close(self):
        pass


def end_to_end() -> None:
    import types
    fake = types.ModuleType("psycopg")
    fake.connect = lambda **kw: FakeConn()
    sys.modules["psycopg"] = fake
    sys.path.insert(0, str(REPO))
    import ringer
    from ringer_core import central_evidence as ce
    from ringer_core.config import EvalConfig, PostgresEvalConfig

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        envf = tmp / "db.env"
        envf.write_text("RINGER_DB_HOST=h\nRINGER_DB_PORT=5440\nRINGER_DB_USER=u\nRINGER_DB_PASSWORD=p\nRINGER_DB_NAME=ringer\n")
        jl = tmp / "runs.jsonl"
        cfg = EvalConfig("postgres", jl, PostgresEvalConfig(env_file=envf, source_host="hostA"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            logger = ringer.EvalLogger(cfg)
            base = dict(run_id="r1", pattern="p", task_key="t", spec="secret prompt", worker_engine="e",
                        shepherd_model="s", verify_method="v", verdict="PASS", duration_ms=5,
                        worker_tokens=7, notes="n", orchestrator="o", model="m", task_type="code-feature",
                        reasoning_effort="high", retry=False)
            for i in range(3):
                logger.log_attempt(dict(base, task_key=f"t{i}"))
            logger.close()
        rows = [json.loads(l) for l in jl.read_text().splitlines()]
        sinks = [r["log_sink"] for r in rows]
        if len(rows) != 3:
            fails.append(f"expected 3 JSONL rows (always written), got {len(rows)}")
            return
        if sinks != ["postgres", "jsonl", "jsonl"]:
            fails.append(f"log_sink sequence {sinks}, want ['postgres','jsonl','jsonl']")
        if rows[0].get("fallback_reason") is not None:
            fails.append(f"fallback_reason must be null after a successful central write: {rows[0].get('fallback_reason')!r}")
        reason = rows[1].get("fallback_reason") or ""
        if "simulated insert failure" not in reason or "supabase" in reason.lower() or "postgres" not in reason.lower():
            fails.append(f"fallback_reason wording wrong: {reason!r}")
        if rows[2].get("fallback_reason") != rows[1].get("fallback_reason"):
            fails.append("fallback_reason should persist for rows after a failure")
        if len(FakeConn.log) != 2:
            fails.append(f"expected exactly 2 insert attempts (third row has no connection), got {len(FakeConn.log)}")
        sql, params = FakeConn.log[0]
        if sql != ce.INSERT_SQL:
            fails.append("insert must use central_evidence.INSERT_SQL exactly")
        if set(params) != set(ce.ATTEMPT_COLUMNS):
            fails.append(f"insert params keys differ from ATTEMPT_COLUMNS: {sorted(set(params) ^ set(ce.ATTEMPT_COLUMNS))}")
        for key in ("model", "task_type", "retry", "reasoning_effort"):
            if params.get(key) != base[key]:
                fails.append(f"central row dropped/changed {key}: {params.get(key)!r}")
        if params["logged_at"] != rows[0]["logged_at"]:
            fails.append("central row and JSONL row must share one logged_at")
        if params["log_sink"] != "postgres" or params["fallback_reason"] is not None:
            fails.append(f"central row must record log_sink 'postgres' and no fallback_reason: {params['log_sink']!r}, {params['fallback_reason']!r}")
        if params["source_host"] != "hostA":
            fails.append(f"source_host should come from config: {params['source_host']!r}")
        if params["attempt_uid"] != ce.attempt_uid("hostA", rows[0]):
            fails.append("attempt_uid must equal attempt_uid(source_host, jsonl row) so a later push dedupes")
        if params["spec"] is not None or not params["spec_sha256"]:
            fails.append("default spec policy must be hash-only (spec NULL, spec_sha256 set)")
        for r in rows:
            for extra in ("source_host", "attempt_uid", "spec_sha256"):
                if extra in r:
                    fails.append(f"JSONL row must keep today's shape; found extra key {extra!r}")
            if r.get("spec") != "secret prompt":
                fails.append("local JSONL must keep the spec text as before")
        warnings = [l for l in err.getvalue().splitlines() if l.strip()]
        if len(warnings) != 1:
            fails.append(f"expected exactly one stderr warning per run, got {len(warnings)}: {warnings}")
        elif "simulated insert failure" not in warnings[0] or str(jl) not in warnings[0]:
            fails.append(f"warning should name the reason and the JSONL path: {warnings[0]!r}")

    # plain jsonl backend: unchanged behavior and shape
    with tempfile.TemporaryDirectory() as tmp:
        jl = Path(tmp) / "runs.jsonl"
        lg = ringer.EvalLogger(EvalConfig("jsonl", jl))
        lg.log_attempt(dict(base))
        lg.close()
        r = json.loads(jl.read_text())
        want = set(base) | {"logged_at", "log_sink", "fallback_reason"}
        if set(r) != want or r["log_sink"] != "jsonl" or r["fallback_reason"] is not None:
            fails.append(f"plain jsonl backend row shape changed: extra={sorted(set(r)-want)} missing={sorted(want-set(r))}")
    print("end-to-end EvalLogger drive complete")


def main() -> int:
    for step in (diff_scope, end_to_end, suite):
        try:
            step()
        except Exception as exc:  # report, never hide, why the check itself broke
            fails.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
    if fails:
        print("FAIL:")
        for f in fails:
            print(" -", f)
        return 1
    print("PASS: ringer.py edits confined to EvalLogger, dual-write behaves end to end, suite clean except the known failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
