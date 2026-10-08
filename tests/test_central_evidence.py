from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ringer_core.central_evidence import (
    ATTEMPT_COLUMNS, INSERT_SQL, Credentials, PushResult, apply_spec_policy,
    attempt_uid, connect, push_rows, read_jsonl_rows, resolve_credentials, stamp,
    to_params,
)


def _row() -> dict:
    return {"logged_at": "2026-10-07T13:47:30.683590+00:00", "run_id": "run-1",
            "task_key": "task-1", "verdict": "pass"}


class FakeCursor:
    def __init__(self, counts: list[int], error: Exception | None = None):
        self.counts = iter(counts)
        self.error = error
        self.executed: list[tuple[str, dict]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql: str, params: dict) -> None:
        self.executed.append((sql, params))
        if self.error is not None and len(self.executed) == 2:
            raise self.error
        self.rowcount = next(self.counts)


class FakeConnection:
    def __init__(self, counts: list[int], error: Exception | None = None):
        self.cur = FakeCursor(counts, error)
        self.cursor_calls = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        self.cursor_calls += 1
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class CentralEvidenceTests(unittest.TestCase):
    def test_connect_can_enable_autocommit(self):
        driver = mock.Mock()
        credentials = Credentials("host", 5440, "writer", "test-password", "ringer")
        with mock.patch.dict(sys.modules, {"psycopg": driver}):
            self.assertIs(connect(credentials, autocommit=True), driver.connect.return_value)
        driver.connect.assert_called_once_with(**credentials.connect_kwargs(), autocommit=True)

    def test_resolve_source_host_configured_default_and_empty(self):
        from ringer_core.central_evidence import resolve_source_host

        with mock.patch("ringer_core.central_evidence.socket.gethostname", return_value="host.example"):
            self.assertEqual(resolve_source_host("  configured  "), "configured")
            for configured in (None, "", "  "):
                with self.subTest(configured=configured):
                    self.assertEqual(resolve_source_host(configured), "host")
        for hostname in ("", ".example"):
            with self.subTest(hostname=hostname), mock.patch(
                "ringer_core.central_evidence.socket.gethostname", return_value=hostname
            ):
                self.assertEqual(resolve_source_host(), "unknown-host")

    def test_stamp_replaces_none_and_empty_timestamp(self):
        now = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
        for logged_at in (None, ""):
            with self.subTest(logged_at=logged_at):
                row = {**_row(), "logged_at": logged_at}
                result = stamp(row, "host", now=lambda: now)
                self.assertEqual(result["logged_at"], now.isoformat())
                self.assertEqual(result["attempt_uid"], attempt_uid("host", result))
                self.assertEqual(row["logged_at"], logged_at)

    def test_attempt_uid_golden_vector(self):
        row = {
            "logged_at": "2026-10-07T13:47:30.683590+00:00",
            "run_id": "cloak-lite-006-attributes-restoration-quality-20261007T134615Z-p1195023",
            "task_key": "attrfix-review",
            "worker_engine": "quality-reviewer",
        }
        self.assertEqual(
            attempt_uid("powerbox2", row),
            "d44445b75273c1b76e4548a6684a7c5ec724fa36f922f2b9603d372aa5e78479",
        )

    def test_attempt_uid_changes_for_each_identity_field(self):
        row = {**_row(), "worker_engine": "worker"}
        original = attempt_uid("powerbox2", row)
        self.assertNotEqual(original, attempt_uid("powerbox3", row))
        for field in ("logged_at", "run_id", "task_key", "worker_engine"):
            with self.subTest(field=field):
                changed = {**row, field: row[field] + "changed"}
                self.assertNotEqual(original, attempt_uid("powerbox2", changed))

    def test_attempt_uid_missing_worker_engine_is_empty(self):
        row = _row()
        for value in ("", None):
            with self.subTest(value=value):
                self.assertEqual(attempt_uid("host", row),
                                 attempt_uid("host", {**row, "worker_engine": value}))

    def test_stamp_adds_timestamp_identity_and_preserves_input(self):
        row = {"run_id": "run-1", "task_key": "task-1"}
        original = dict(row)
        now = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
        result = stamp(row, "host", now=lambda: now)
        self.assertIsNot(result, row)
        self.assertEqual(row, original)
        self.assertEqual(result["logged_at"], now.isoformat())
        self.assertEqual(result["source_host"], "host")
        self.assertEqual(result["attempt_uid"], attempt_uid("host", result))
        self.assertEqual(stamp(result, "host")["attempt_uid"], result["attempt_uid"])

    def test_stamp_preserves_existing_timestamp_exactly(self):
        row = {**_row(), "logged_at": "2026-10-07T15:47:30.683590+02:00"}
        original = dict(row)
        now = mock.Mock(side_effect=AssertionError("clock must not be called"))
        result = stamp(row, "host", now=now)
        self.assertEqual(result["logged_at"], row["logged_at"])
        self.assertEqual(row, original)
        now.assert_not_called()

    def test_stamp_injected_timestamp_is_utc(self):
        now = datetime(2026, 10, 8, 14, 30, tzinfo=timezone(timedelta(hours=2)))
        row = {"run_id": "run-1", "task_key": "task-1"}
        result = stamp(row, "host", now=lambda: now)
        self.assertEqual(result["logged_at"], "2026-10-08T12:30:00+00:00")

    def test_spec_policy_hash_and_excerpt(self):
        row = {"spec": "café 東京", "notes": "keep"}
        original = dict(row)
        digest = hashlib.sha256(row["spec"].encode("utf-8")).hexdigest()
        for mode, text in (("hash", None), ("excerpt", row["spec"])):
            with self.subTest(mode=mode):
                result = apply_spec_policy(row, mode)
                self.assertIsNot(result, row)
                self.assertEqual(result, {**row, "spec": text, "spec_sha256": digest})
                self.assertEqual(row, original)

    def test_spec_policy_empty_spec(self):
        for row in ({}, {"spec": None}, {"spec": ""}):
            for mode in ("hash", "excerpt"):
                with self.subTest(row=row, mode=mode):
                    result = apply_spec_policy(row, mode)
                    self.assertIsNone(result["spec_sha256"])
                    self.assertEqual(result["spec"], row.get("spec") if mode == "excerpt" else None)

    def test_spec_policy_redaction_in_both_modes(self):
        row = {"spec": "[redacted request packet]", "spec_sha256": "old"}
        for mode in ("hash", "excerpt"):
            with self.subTest(mode=mode):
                result = apply_spec_policy(row, mode)
                self.assertIsNone(result["spec"])
                self.assertIsNone(result["spec_sha256"])
        self.assertEqual(row["spec_sha256"], "old")

    def test_spec_policy_invalid_mode_lists_valid_modes(self):
        for row in ({}, {"spec": "[redacted request packet]"}):
            with self.subTest(row=row), self.assertRaises(ValueError) as raised:
                apply_spec_policy(row, "full")
            self.assertIn("hash", str(raised.exception))
            self.assertIn("excerpt", str(raised.exception))

    def test_to_params_exact_columns_defaults_and_extra_keys(self):
        row = {**_row(), "extra": "ignore", "attempt_uid": "stale", "source_host": "stale"}
        original = dict(row)
        result = to_params(row, "host")
        self.assertEqual(set(result), set(ATTEMPT_COLUMNS))
        self.assertEqual(result["log_sink"], "jsonl")
        self.assertEqual(result["source_host"], "host")
        self.assertEqual(result["attempt_uid"], attempt_uid("host", row))
        for field in set(ATTEMPT_COLUMNS) - set(_row()) - {"source_host", "attempt_uid", "log_sink"}:
            with self.subTest(field=field):
                self.assertIsNone(result[field])
        self.assertEqual(row, original)

    def test_to_params_preserves_optional_fields_and_spec_policy(self):
        row = {**_row(), "retry": False, "duration_ms": 0, "worker_tokens": 12,
               "notes": "note", "log_sink": "postgres", "fallback_reason": "prior failure",
               "spec": "excerpt"}
        result = to_params(row, "host", "excerpt")
        for field, value in row.items():
            self.assertEqual(result[field], value)
        self.assertEqual(result["spec_sha256"], hashlib.sha256(b"excerpt").hexdigest())
        self.assertIsNone(to_params(row, "host")["spec"])
        self.assertEqual(to_params({**row, "log_sink": ""}, "host")["log_sink"], "jsonl")

    def test_to_params_required_fields_are_nonempty_strings(self):
        for field in ("logged_at", "run_id", "task_key", "verdict"):
            missing = _row()
            del missing[field]
            with self.subTest(field=field, value="missing"), self.assertRaisesRegex(ValueError, field):
                to_params(missing, "host")
            for value in (None, "", 1, True):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    to_params({**_row(), field: value}, "host")

    def test_to_params_invalid_timestamp_names_field(self):
        with self.assertRaisesRegex(ValueError, "logged_at"):
            to_params({**_row(), "logged_at": "not-a-timestamp"}, "host")

    def test_to_params_integer_validation_rejects_bools(self):
        for field in ("duration_ms", "worker_tokens"):
            for value in (True, False, 1.5, "12"):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    to_params({**_row(), field: value}, "host")
            for value in (None, 0, 12):
                with self.subTest(field=field, value=value):
                    self.assertEqual(to_params({**_row(), field: value}, "host")[field], value)

    def test_to_params_retry_validation(self):
        for value in (0, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "retry"):
                to_params({**_row(), "retry": value}, "host")
        for value in (None, False, True):
            self.assertIs(to_params({**_row(), "retry": value}, "host")["retry"], value)

    def test_insert_sql_has_columns_and_no_conflict_target(self):
        expected = (f"INSERT INTO ringer.attempts ({', '.join(ATTEMPT_COLUMNS)}) VALUES ("
                    + ", ".join(f"%({column})s" for column in ATTEMPT_COLUMNS)
                    + ") ON CONFLICT DO NOTHING")
        self.assertEqual(INSERT_SQL, expected)
        self.assertIn("ON CONFLICT DO NOTHING", INSERT_SQL)
        self.assertNotIn("ON CONFLICT (", INSERT_SQL)

    def test_resolve_credentials_ringer_names(self):
        env = {"RINGER_DB_HOST": "host", "RINGER_DB_PORT": "5440", "RINGER_DB_USER": "writer",
               "RINGER_DB_PASSWORD": "test-password", "RINGER_DB_NAME": "ringer"}
        credentials = resolve_credentials(env)
        self.assertEqual(credentials, Credentials("host", 5440, "writer", "test-password", "ringer"))
        self.assertEqual(credentials.connect_kwargs(),
                         {"host": "host", "port": 5440, "user": "writer", "password": "test-password",
                          "dbname": "ringer", "connect_timeout": 5})

    def test_resolve_credentials_legacy_names(self):
        env = {"SUPABASE_DB_HOST": "host", "SUPABASE_DB_PORT": "5432", "SUPABASE_DB_USER": "writer",
               "SUPABASE_DB_PASSWORD": "test-password", "SUPABASE_DB_NAME": "ringer"}
        self.assertEqual(resolve_credentials(env), Credentials("host", 5432, "writer", "test-password", "ringer"))

    def test_resolve_credentials_mixed_per_key_and_precedence(self):
        env = {"RINGER_DB_HOST": "new-host", "SUPABASE_DB_HOST": "old-host",
               "RINGER_DB_PORT": "5440", "SUPABASE_DB_PORT": "5432",
               "SUPABASE_DB_USER": "writer", "RINGER_DB_PASSWORD": "",
               "SUPABASE_DB_PASSWORD": "test-password", "RINGER_DB_NAME": "ringer"}
        self.assertEqual(resolve_credentials(env),
                         Credentials("new-host", 5440, "writer", "test-password", "ringer"))

    def test_resolve_credentials_missing_settings_are_named(self):
        with self.assertRaises(ValueError) as raised:
            resolve_credentials({"RINGER_DB_HOST": "", "SUPABASE_DB_HOST": ""})
        self.assertTrue(str(raised.exception).startswith("missing database settings: "))
        for suffix in ("HOST", "PORT", "USER", "PASSWORD", "NAME"):
            self.assertIn("RINGER_DB_" + suffix, str(raised.exception))

    def test_resolve_credentials_invalid_port(self):
        env = {"RINGER_DB_HOST": "host", "RINGER_DB_PORT": "not-an-int", "RINGER_DB_USER": "writer",
               "RINGER_DB_PASSWORD": "test-password", "RINGER_DB_NAME": "ringer"}
        with self.assertRaises(ValueError):
            resolve_credentials(env)

    def test_connect_missing_driver_has_install_hint(self):
        with mock.patch.dict(sys.modules, {"psycopg": None}), self.assertRaises(RuntimeError) as raised:
            connect(Credentials("host", 5440, "writer", "test-password", "ringer"))
        self.assertIn("psycopg is required for the postgres backend", str(raised.exception))
        self.assertIn('pip install "psycopg[binary]"', str(raised.exception))

    def test_connect_uses_credentials_without_autocommit(self):
        driver = mock.Mock()
        credentials = Credentials("host", 5440, "writer", "test-password", "ringer")
        with mock.patch.dict(sys.modules, {"psycopg": driver}):
            self.assertIs(connect(credentials), driver.connect.return_value)
        driver.connect.assert_called_once_with(**credentials.connect_kwargs())

    def test_module_import_does_not_require_psycopg(self):
        script = '''
import sys
class BlockDriver:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "psycopg" or fullname.startswith("psycopg."):
            raise ImportError("driver blocked")
sys.meta_path.insert(0, BlockDriver())
import ringer_core.central_evidence
assert "psycopg" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_jsonl_good_rows_blank_lines_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            rows = [_row(), {"spec": "café 東京"}]
            data = ("\n" + json.dumps(rows[0]) + "\n  \t\n" + json.dumps(rows[1])).encode("utf-8")
            path.write_bytes(data)
            self.assertEqual(read_jsonl_rows(path), rows)
            self.assertEqual(path.read_bytes(), data)

    def test_read_jsonl_invalid_json_reports_path_and_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            data = b'{}\n\nnot-json\n'
            path.write_bytes(data)
            with self.assertRaises(ValueError) as raised:
                read_jsonl_rows(path)
            self.assertTrue(str(raised.exception).startswith(f"{path}:3: "))
            self.assertEqual(path.read_bytes(), data)

    def test_read_jsonl_nonobjects_report_path_and_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            for value in ([], None, 12, "text", True):
                with self.subTest(value=value):
                    data = ("{}\n" + json.dumps(value) + "\n").encode("utf-8")
                    path.write_bytes(data)
                    with self.assertRaises(ValueError) as raised:
                        read_jsonl_rows(path)
                    self.assertTrue(str(raised.exception).startswith(f"{path}:2: "))
                    self.assertIn("JSON object", str(raised.exception))
                    self.assertEqual(path.read_bytes(), data)

    def test_push_rows_counts_and_commits_once(self):
        conn = FakeConnection([1, 0, 2])
        rows = [_row(), {**_row(), "task_key": "task-2"}, {**_row(), "task_key": "task-3"}]
        self.assertEqual(push_rows(conn, rows, "host", "excerpt"), PushResult(3, 2, 1))
        self.assertEqual(conn.cursor_calls, 1)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(conn.cur.executed, [(INSERT_SQL, to_params(row, "host", "excerpt")) for row in rows])

    def test_push_rows_invalid_later_row_makes_no_database_calls(self):
        conn = FakeConnection([1])
        with self.assertRaisesRegex(ValueError, "verdict"):
            push_rows(conn, [_row(), {**_row(), "verdict": ""}], "host")
        self.assertEqual(conn.cursor_calls, 0)
        self.assertEqual(conn.cur.executed, [])
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)

    def test_push_rows_execute_failure_rolls_back_and_reraises(self):
        error = RuntimeError("insert failed")
        conn = FakeConnection([1, 1], error=error)
        with self.assertRaises(RuntimeError) as raised:
            push_rows(conn, [_row(), _row()], "host")
        self.assertIs(raised.exception, error)
        self.assertEqual(len(conn.cur.executed), 2)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)

    def test_push_rows_commit_failure_rolls_back(self):
        conn = FakeConnection([1])
        error = RuntimeError("commit failed")
        with mock.patch.object(conn, "commit", side_effect=error), self.assertRaises(RuntimeError) as raised:
            push_rows(conn, [_row()], "host")
        self.assertIs(raised.exception, error)
        self.assertEqual(conn.rollbacks, 1)

    def test_push_rows_all_present(self):
        conn = FakeConnection([0, 0])
        self.assertEqual(push_rows(conn, [_row(), _row()], "host"), PushResult(2, 0, 2))
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)

    def test_module_does_not_import_ringer(self):
        script = '''
import sys
class BlockRinger:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ringer" or fullname.startswith("ringer."):
            raise ImportError("ringer blocked")
sys.meta_path.insert(0, BlockRinger())
import ringer_core.central_evidence
assert "ringer" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
