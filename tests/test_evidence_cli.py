from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ringer_core import central_evidence
from ringer_core.evidence_cli import run_evidence_command


def row(day: str, **extra: object) -> dict:
    return {"logged_at": f"2026-10-{day}T10:00:00Z", "run_id": "run", "task_key": day,
            "verdict": "PASS", "log_sink": "jsonl", **extra}


class FakeConnection:
    def __init__(self, fail: bool = False) -> None:
        self.ids: set[str] = set()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail = fail

    def cursor(self):
        connection = self
        class Cursor:
            rowcount = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql, params):
                if connection.fail:
                    raise RuntimeError("database unavailable")
                uid = params["attempt_uid"]
                self.rowcount = int(uid not in connection.ids)
                connection.ids.add(uid)
        return Cursor()

    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


class EvidenceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_file = self.root / "db.env"
        self.env_file.write_text("unused")
        pg = SimpleNamespace(env_file=self.env_file, spec_storage="hash", source_host="host")
        self.config = SimpleNamespace(eval=SimpleNamespace(jsonl_path=self.root / "default.jsonl", postgres=pg))
        self.connect = FakeConnection()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def source(self, name: str, rows: list[dict]) -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(item) + "\n" for item in rows))
        return path

    def args(self, command="push", **kwargs):
        values = dict(evidence_command=command, file=None, since=None, source_host=None,
                      spec_storage=None, dry_run=False)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def invoke(self, args, env=None, connect=None):
        out, err = io.StringIO(), io.StringIO()
        rc = run_evidence_command(self.config, args, read_env=lambda _: env or {
            "RINGER_DB_HOST": "db", "RINGER_DB_PORT": "5440", "RINGER_DB_USER": "writer",
            "RINGER_DB_PASSWORD": "topsecret", "RINGER_DB_NAME": "ringer"},
            connect=connect or (lambda credentials: self.connect), stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def test_default_source_push_is_idempotent_and_never_rewrites_jsonl(self):
        path = self.source("default.jsonl", [row("01"), row("02")])
        before = path.read_bytes()
        rc, out, err = self.invoke(self.args())
        self.assertEqual((0, ""), (rc, err))
        self.assertIn("inserted 2, already present 0 (of 2 rows)", out)
        self.assertEqual(1, self.connect.commits)
        rc, out, _ = self.invoke(self.args())
        self.assertEqual(0, rc)
        self.assertIn("inserted 0, already present 2", out)
        self.assertEqual(2, self.connect.commits)
        self.assertEqual(before, path.read_bytes())

    def test_repeated_files_since_forms_overrides_and_validation_before_connect(self):
        first = self.source("one.jsonl", [row("01"), row("02")])
        second = self.source("two.jsonl", [row("03")])
        rc, out, _ = self.invoke(self.args(file=[first, second], since="2026-10-02"))
        self.assertEqual(0, rc)
        self.assertIn("inserted 2, already present 0 (of 2 rows)", out)
        self.assertEqual(2, self.connect.commits)
        before_calls = self.connect.commits
        rc, out, _ = self.invoke(self.args(file=[first], since="2026-10-02T00:00:00Z", dry_run=True,
                                           source_host="override", spec_storage="excerpt"))
        self.assertEqual(0, rc)
        self.assertIn("source host: override", out)
        self.assertIn("spec storage: excerpt", out)
        self.assertEqual(before_calls, self.connect.commits)
        rc, _, err = self.invoke(self.args(file=[first], since="not-a-date", dry_run=True))
        self.assertEqual(2, rc)
        self.assertIn("--since", err)
        bad = self.root / "bad.jsonl"
        bad.write_text(json.dumps({"logged_at": "2026-10-01T00:00:00Z", "run_id": "x", "task_key": "x"}) + "\n")
        real_to_params = central_evidence.to_params
        def bad_to_params(item, host, storage):
            if item.get("run_id") == "x":
                raise ValueError("bad row")
            return real_to_params(item, host, storage)
        with mock.patch.object(central_evidence, "to_params", side_effect=bad_to_params):
            rc, _, err = self.invoke(self.args(file=[first, bad]))
        self.assertEqual(2, rc)
        self.assertIn(f"{bad}:1", err)
        self.assertEqual(before_calls, self.connect.commits)

    def test_missing_file_dry_run_and_configuration_errors(self):
        rc, _, err = self.invoke(self.args(file=[self.root / "missing"]))
        self.assertEqual(2, rc)
        self.assertIn("missing", err)
        path = self.source("valid.jsonl", [row("01")])
        read_env = mock.Mock(side_effect=AssertionError("env read"))
        out, err = io.StringIO(), io.StringIO()
        rc = run_evidence_command(self.config, self.args(file=[path], dry_run=True),
                                  read_env=read_env, connect=mock.Mock(), stdout=out, stderr=err)
        self.assertEqual(0, rc)
        read_env.assert_not_called()
        self.assertIn("DRY RUN: nothing sent", out.getvalue())
        self.config.eval = SimpleNamespace(jsonl_path=path, postgres=None)
        rc, _, err = self.invoke(self.args(file=[path]))
        self.assertEqual(2, rc)
        self.assertIn("[eval.postgres]", err)
        self.config.eval = SimpleNamespace(jsonl_path=path, postgres=SimpleNamespace(env_file=self.env_file,
                                                                                      spec_storage="hash", source_host=None))
        rc, out, err = self.invoke(self.args(file=[path]), env={"RINGER_DB_USER": "writer"})
        self.assertEqual(2, rc)
        self.assertIn("RINGER_DB_", err)
        self.assertNotIn("topsecret", out + err)

    def test_database_failures_and_missing_driver(self):
        first = self.source("first.jsonl", [row("01")])
        second = self.source("second.jsonl", [row("02")])
        conn = FakeConnection()
        calls = 0
        def connect(_credentials):
            nonlocal calls
            calls += 1
            if calls == 2: conn.fail = True
            return conn
        # One connection is used for every file; a write error names the file.
        rc, _, err = self.invoke(self.args(file=[first, second]), connect=lambda _: FakeConnection(fail=True))
        self.assertEqual(3, rc)
        self.assertIn(str(first), err)
        rc, _, err = self.invoke(self.args(file=[first]), connect=mock.Mock(side_effect=RuntimeError("install psycopg")))
        self.assertEqual(2, rc)
        self.assertIn("psycopg", err)

    def test_status_is_local_and_reports_counts_reasons_and_bad_files(self):
        path = self.source("status.jsonl", [row("01", fallback_reason="old"),
                                              row("02", log_sink="postgres", fallback_reason="new"),
                                              row("03", log_sink="other")])
        rc, out, _ = self.invoke(self.args("status", file=[path]), connect=mock.Mock(side_effect=AssertionError))
        self.assertEqual(0, rc)
        self.assertIn("confirmed central (log_sink=postgres): 1", out)
        self.assertIn("local-only (log_sink=jsonl): 2", out)
        self.assertIn("latest fallback_reason: new", out)
        self.assertIn("run: ringer evidence push", out)
        bad = self.root / "malformed.jsonl"
        bad.write_text("not json\n")
        rc, out, _ = self.invoke(self.args("status", file=[path, bad]))
        self.assertEqual(2, rc)
        self.assertIn("evidence status:", out)

    def test_main_parser_wiring(self):
        import ringer
        path = self.source("cli.jsonl", [row("01")])
        config_path = self.root / "config.toml"
        config_path.write_text(f'[eval]\njsonl_path = "{path}"\n')
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "RINGER_NO_SELF_UPDATE": "1"}):
            rc = ringer.main(["--config", str(config_path), "evidence", "push", "--dry-run"])
            self.assertEqual(0, rc)
            rc = ringer.main(["--config", str(config_path), "evidence", "status"])
            self.assertEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
