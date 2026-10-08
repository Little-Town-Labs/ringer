#!/usr/bin/env python3
"""T010 check (review fixes). Run from the repo root.

Covers: durable identity stamped in local rows (IDF-1), strict value validation
(IDF-2, CLI-3), validate-everything and physical line numbers (CLI-2), password
scrubbing (CLI-1), plus regression: the T005 CLI drive, the real-row crosscheck
against backfill.py, ringer.py edits confined to EvalLogger, and the full suite.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace as NS

REPO = Path.cwd()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
fails: list[str] = []
PW = "S3cretPW99xyz"
ORIG_HOME = os.environ["HOME"]


class DB:
    def __init__(self):
        self.store: set = set()
        self.executed: list = []
        self.commits = 0


class Conn:
    def __init__(self, db: DB, boom_on: int | None = None, text: str = ""):
        self.db, self.boom_on, self.text, self.rowcount = db, boom_on, text, 0

    def _run(self, params):
        self.db.executed.append(dict(params))
        if self.boom_on and len(self.db.executed) == self.boom_on:
            raise RuntimeError(f"insert failed {self.text}")
        uid = params["attempt_uid"]
        self.rowcount = 0 if uid in self.db.store else 1
        self.db.store.add(uid)

    def execute(self, sql, params=None):
        self._run(params or {})

    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def commit(self): self.db.commits += 1
    def rollback(self): pass
    def close(self): pass


def base(i=0, **kw):
    r = dict(run_id=f"r{i}", pattern="p", task_key=f"t{i}", spec="the prompt", worker_engine="codex",
             shepherd_model="s", verify_method="v", verdict="PASS", duration_ms=10, worker_tokens=5,
             notes="n", orchestrator="o", model="m", task_type="code-feature", reasoning_effort="high",
             retry=False, logged_at=f"2026-10-0{i+1}T10:00:00+00:00", log_sink="jsonl", fallback_reason=None)
    r.update(kw)
    return r


def logger_drive() -> None:
    import ringer
    from ringer_core import central_evidence as ce
    from ringer_core.config import EvalConfig, PostgresEvalConfig

    db = DB()
    mode = {"connect_error": None, "boom_on": None}

    def fake_connect(**kw):
        if mode["connect_error"]:
            raise OSError(mode["connect_error"])
        return Conn(db, mode["boom_on"], f"password={PW}")
    fake = types.ModuleType("psycopg"); fake.connect = fake_connect
    sys.modules["psycopg"] = fake

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        envf = tmp / "db.env"
        envf.write_text(f"RINGER_DB_HOST=h\nRINGER_DB_PORT=5440\nRINGER_DB_USER=u\nRINGER_DB_PASSWORD={PW}\nRINGER_DB_NAME=ringer\n")
        # success then insert failure then fallback
        mode["boom_on"] = 2
        jl = tmp / "runs.jsonl"
        cfg = EvalConfig("postgres", jl, PostgresEvalConfig(env_file=envf, source_host="hostA"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            lg = ringer.EvalLogger(cfg)
            for i in range(3):
                row = base(i); row.pop("logged_at"); row.pop("log_sink"); row.pop("fallback_reason")
                lg.log_attempt(row)
            lg.close()
        rows = [json.loads(l) for l in jl.read_text().splitlines()]
        if len(rows) != 3:
            fails.append(f"expected 3 local rows, got {len(rows)}"); return
        for r in rows:
            if r.get("source_host") != "hostA":
                fails.append(f"every local row must carry source_host from config ('hostA'): {r.get('source_host')!r}")
            if r.get("attempt_uid") != ce.attempt_uid("hostA", r):
                fails.append("local attempt_uid must equal attempt_uid(source_host, row)")
            if "spec_sha256" in r:
                fails.append("local rows must not carry spec_sha256")
        if [r["log_sink"] for r in rows] != ["postgres", "jsonl", "jsonl"]:
            fails.append(f"log_sink sequence wrong: {[r['log_sink'] for r in rows]}")
        first = db.executed[0]
        if first["attempt_uid"] != rows[0]["attempt_uid"] or first["source_host"] != "hostA" or first["logged_at"] != rows[0]["logged_at"]:
            fails.append("central row must share logged_at, source_host and attempt_uid with the local row")
        blob = err.getvalue() + "".join(str(r.get("fallback_reason")) for r in rows)
        if PW in blob:
            fails.append("password leaked into stderr warning or fallback_reason after an insert failure")
        if "insert failed" not in blob:
            fails.append("the failure reason (minus the password) should still be reported")

        # connect failure scrubs the password too
        db2 = DB(); db.executed.clear(); mode["boom_on"] = None; mode["connect_error"] = f"auth failed password={PW}"
        jl2 = tmp / "runs2.jsonl"; err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            lg = ringer.EvalLogger(EvalConfig("postgres", jl2, PostgresEvalConfig(env_file=envf, source_host="hostA")))
            lg.log_attempt(base(0)); lg.close()
        r2 = json.loads(jl2.read_text())
        if PW in err2.getvalue() + str(r2.get("fallback_reason")):
            fails.append("password leaked after a connect failure")
        mode["connect_error"] = None

        # plain jsonl backend still stamps identity (default hostname when no source_host)
        jl3 = tmp / "plain.jsonl"
        lg = ringer.EvalLogger(EvalConfig("jsonl", jl3)); row = base(0); row.pop("logged_at"); row.pop("log_sink"); row.pop("fallback_reason")
        lg.log_attempt(row); lg.close()
        r3 = json.loads(jl3.read_text())
        want = set(row) | {"logged_at", "log_sink", "fallback_reason", "source_host", "attempt_uid"}
        if set(r3) != want:
            fails.append(f"plain jsonl row keys wrong: extra={sorted(set(r3)-want)} missing={sorted(want-set(r3))}")
        elif r3["source_host"] != ce.resolve_source_host(None) or r3["attempt_uid"] != ce.attempt_uid(r3["source_host"], r3):
            fails.append("plain jsonl identity must use the default hostname and match attempt_uid()")
    print("logger drive complete")


def module_drive() -> None:
    from ringer_core import central_evidence as ce
    r = base(0)
    p = ce.to_params(dict(r, source_host="hostA"), "hostB")
    if p["source_host"] != "hostA" or p["attempt_uid"] != ce.attempt_uid("hostA", r):
        fails.append("a stored source_host must win over the default host passed to to_params")
    p = ce.to_params(r, "hostB")
    if p["source_host"] != "hostB":
        fails.append("rows without source_host must use the host passed in")
    good = ce.attempt_uid("hostA", r)
    try:
        ce.to_params(dict(r, source_host="hostA", attempt_uid="0" * 64), "hostB"); fails.append("a mismatched stored attempt_uid must raise ValueError")
    except ValueError as exc:
        if "attempt_uid" not in str(exc): fails.append(f"mismatch error should name attempt_uid: {exc}")
    if ce.to_params(dict(r, source_host="hostA", attempt_uid=good), "hostB")["attempt_uid"] != good:
        fails.append("a matching stored attempt_uid must be accepted")
    for field, value in (("duration_ms", 2**63), ("worker_tokens", -2**63 - 1), ("spec", 123), ("pattern", {"x": 1}),
                         ("notes", ["a"]), ("model", 5), ("log_sink", 3), ("retry", "yes")):
        try:
            ce.to_params(dict(r, **{field: value}), "h"); fails.append(f"{field}={value!r} should be rejected")
        except ValueError as exc:
            if field not in str(exc): fails.append(f"error for {field} should name the field: {exc}")
        except Exception as exc:
            fails.append(f"{field}={value!r} raised {type(exc).__name__}, expected ValueError")
    for ok in (2**63 - 1, -2**63, 0, None):
        try: ce.to_params(dict(r, duration_ms=ok), "h")
        except Exception as exc: fails.append(f"duration_ms={ok!r} should be accepted: {exc}")
    tmp = Path(tempfile.mkdtemp()); f = tmp / "a.jsonl"
    f.write_text("\n" + json.dumps(r) + "\n\n" + json.dumps(base(1)) + "\n")
    try:
        got = ce.read_jsonl_numbered(f)
        if [n for n, _ in got] != [2, 4] or got[0][1]["run_id"] != "r0":
            fails.append(f"read_jsonl_numbered must return physical line numbers [2, 4], got {[n for n, _ in got]}")
    except Exception as exc:
        fails.append(f"read_jsonl_numbered missing or broken: {exc!r}")
    print("module drive complete")


def cli_drive() -> None:
    from ringer_core import central_evidence as ce
    from ringer_core.evidence_cli import run_evidence_command

    tmp = Path(tempfile.mkdtemp())
    envf = tmp / "db.env"
    envf.write_text(f"RINGER_DB_HOST=db.example\nRINGER_DB_PORT=5440\nRINGER_DB_USER=w\nRINGER_DB_PASSWORD={PW}\nRINGER_DB_NAME=ringer\n")
    read_env = lambda p: dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l)
    cfg = NS(eval=NS(jsonl_path=tmp / "none.jsonl", postgres=NS(env_file=envf, spec_storage="hash", source_host="cfgHost")))

    def run(files, *, db=None, connect=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        args = NS(evidence_command=kw.pop("cmd", "push"), file=files, since=None, source_host=None, spec_storage=None, dry_run=False)
        args.__dict__.update(kw)
        conns = {"n": 0}

        def default_connect(creds):
            conns["n"] += 1
            return Conn(db or DB())
        code = run_evidence_command(cfg, args, read_env=read_env, connect=connect or default_connect, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), conns["n"]

    # stored identity wins; unstamped rows use override/default
    a = base(0); a["source_host"] = "origHost"; a["attempt_uid"] = ce.attempt_uid("origHost", a)
    b = base(1)
    f = tmp / "mixed.jsonl"; f.write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    db = DB()
    code, out, err, n = run([f], db=db, source_host="overrideHost")
    got = {p["run_id"]: p for p in db.executed}
    if code != 0 or got["r0"]["source_host"] != "origHost" or got["r0"]["attempt_uid"] != a["attempt_uid"]:
        fails.append(f"stamped row must keep its stored identity even with --source-host (code {code}): {got.get('r0', {}).get('source_host')!r} {err[-200:]!r}")
    if got["r1"]["source_host"] != "overrideHost" or got["r1"]["attempt_uid"] != ce.attempt_uid("overrideHost", b):
        fails.append("unstamped row must use --source-host / the default host")
    code, out, err, n = run([f], dry_run=True, source_host="overrideHost")
    if code != 0 or n != 0 or "stamped rows: 1" not in out or "unstamped rows: 1 (using host overrideHost)" not in out:
        fails.append(f"dry-run must report 'stamped rows: 1' and 'unstamped rows: 1 (using host overrideHost)' (code {code}): {out[-500:]!r}")

    # mismatched stored uid -> exit 2 with physical line, nothing sent
    bad = tmp / "mismatch.jsonl"
    c = dict(a, attempt_uid="f" * 64)
    bad.write_text("\n" + json.dumps(b) + "\n" + json.dumps(c) + "\n")
    code, out, err, n = run([bad])
    if code != 2 or n != 0 or f"{bad}:3" not in err:
        fails.append(f"mismatched attempt_uid must exit 2 naming '{bad}:3' and never connect (code {code}, connects {n}): {err[-300:]!r}")

    # malformed row excluded by --since is still validated; physical line numbers
    old = tmp / "old.jsonl"
    old.write_text("\n" + json.dumps(base(0, verdict=None, logged_at="2026-09-01T10:00:00+00:00")) + "\n" + json.dumps(base(2)) + "\n")
    code, out, err, n = run([old], since="2026-09-15")
    if code != 2 or n != 0 or f"{old}:2" not in err:
        fails.append(f"a malformed row excluded by --since must still abort with '{old}:2' (code {code}, connects {n}): {err[-300:]!r}")

    # strict value validation reaches the CLI as a clean exit 2, no traceback
    for label, row in (("bigint", base(0, duration_ms=2**63)), ("spec int", base(0, spec=123)), ("pattern dict", base(0, pattern={"a": 1}))):
        g = tmp / f"v-{label.replace(' ', '')}.jsonl"; g.write_text(json.dumps(row) + "\n")
        for extra in ({"dry_run": True}, {}):
            code, out, err, n = run([g], **extra)
            if code != 2 or n != 0 or "Traceback" in err or "AttributeError" in err:
                fails.append(f"{label} {'dry-run' if extra else 'push'}: expected clean exit 2 without connecting (code {code}, connects {n}): {err[-200:]!r}")

    # password never echoed
    good = tmp / "good.jsonl"; good.write_text(json.dumps(base(0)) + "\n")
    def refuse(creds): raise OSError(f"could not connect: password={PW} rejected")
    code, out, err, n = run([good], connect=refuse)
    if code != 3 or PW in out + err:
        fails.append(f"connection failure must exit 3 without echoing the password (code {code}, leaked {PW in out + err})")
    wdb = DB()
    code, out, err, n = run([good], connect=lambda creds: Conn(wdb, boom_on=1, text=f"password={PW}"))
    if code != 3 or PW in out + err:
        fails.append(f"write failure must exit 3 without echoing the password (code {code}, leaked {PW in out + err})")
    print("cli drive complete")


def regression() -> None:
    import check_t004, check_t005
    check_t004.fails.clear(); check_t005.fails.clear()
    check_t004.diff_scope()
    check_t005.drive()
    fails.extend(f"[t005 regression] {m}" for m in check_t005.fails)
    fails.extend(f"[diff scope] {m}" for m in check_t004.fails)
    proc = subprocess.run([sys.executable, str(HERE / "crosscheck_t002.py")], cwd=REPO, capture_output=True, text=True,
                          env=dict(os.environ, HOME=ORIG_HOME))
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-300:])
    if proc.returncode != 0:
        fails.append("[crosscheck] real rows no longer reproduce backfill.py: " + (proc.stdout + proc.stderr)[-600:])


def suite() -> None:
    import check_t005
    check_t005.fails.clear(); check_t005.suite()
    fails.extend(f"[suite] {m}" for m in check_t005.fails)


def main() -> int:
    os.environ["RINGER_NO_SELF_UPDATE"] = "1"
    for step in (module_drive, logger_drive, cli_drive, regression, suite):
        try:
            step()
        except Exception as exc:
            fails.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
    if fails:
        print("FAIL:")
        for m in fails:
            print(" -", m)
        return 1
    print("PASS: durable identity, strict validation, validate-everything, password scrubbing, and all earlier behavior hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
