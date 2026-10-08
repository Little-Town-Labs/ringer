from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import ringer
from ringer_core import central_evidence
from ringer_core.config import EvalConfig, PostgresEvalConfig


class FakeConn:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[tuple[str, dict]] = []
        self.closed = False

    def execute(self, sql: str, params: dict) -> None:
        self.executed.append((sql, dict(params)))
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class EvalLoggerCentralTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "runs.jsonl"
        self.env_file = self.root / "db.env"
        self.env_file.write_text(
            "RINGER_DB_HOST=host\nRINGER_DB_PORT=5440\nRINGER_DB_USER=writer\n"
            "RINGER_DB_PASSWORD=test-password\nRINGER_DB_NAME=ringer\n",
            encoding="utf-8",
        )
        self.conn = FakeConn()
        self.driver = types.ModuleType("psycopg")
        self.driver.connect = mock.Mock(return_value=self.conn)
        patcher = mock.patch.dict(sys.modules, {"psycopg": self.driver})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.row = {
            "run_id": "run-1", "task_key": "task-1", "worker_engine": "codex",
            "verdict": "PASS", "model": "model-1", "task_type": "code-feature",
            "retry": False, "reasoning_effort": "high", "spec": "café 東京",
            "duration_ms": 5, "worker_tokens": 7,
        }

    def config(self, **kwargs) -> EvalConfig:
        return EvalConfig("postgres", self.path, PostgresEvalConfig(self.env_file, **kwargs))

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def assert_warning(self, stderr: io.StringIO, reason: str) -> None:
        self.assertEqual(stderr.getvalue().splitlines(), [
            f"ringer: central evidence write failed ({reason}); "
            f"attempt rows are still saved to {self.path}"
        ])

    def test_success_dual_writes_with_configured_source_host(self) -> None:
        original = dict(self.row)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(self.config(source_host=" configured-host "))
            logger.log_attempt(self.row)
            logger.close()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.row, original)
        self.assertTrue(self.conn.closed)
        self.driver.connect.assert_called_once_with(
            host="host", port=5440, user="writer", password="test-password",
            dbname="ringer", connect_timeout=5, autocommit=True,
        )
        sql, params = self.conn.executed[0]
        local = self.rows()[0]
        self.assertEqual(sql, central_evidence.INSERT_SQL)
        self.assertEqual(set(params), set(central_evidence.ATTEMPT_COLUMNS))
        self.assertEqual(params["logged_at"], local["logged_at"])
        self.assertEqual(params["source_host"], "configured-host")
        self.assertEqual(params["log_sink"], "postgres")
        self.assertIsNone(params["fallback_reason"])
        self.assertEqual(params["attempt_uid"], central_evidence.attempt_uid("configured-host", local))
        self.assertIsNone(params["spec"])
        self.assertEqual(params["spec_sha256"], hashlib.sha256(self.row["spec"].encode()).hexdigest())
        self.assertEqual(local, dict(self.row, logged_at=params["logged_at"],
                                    log_sink="postgres", fallback_reason=None))

    def test_default_hostname_and_existing_timestamp_are_preserved(self) -> None:
        row = dict(self.row, logged_at="2026-10-08T14:30:00+02:00")
        with mock.patch("ringer_core.central_evidence.socket.gethostname", return_value="host.example"):
            logger = ringer.EvalLogger(self.config())
            logger.log_attempt(row)
            logger.close()
        params = self.conn.executed[0][1]
        local = self.rows()[0]
        self.assertEqual(params["source_host"], "host")
        self.assertEqual(params["logged_at"], row["logged_at"])
        self.assertEqual(local["logged_at"], row["logged_at"])
        self.assertEqual(params["attempt_uid"], central_evidence.attempt_uid("host", local))

    def test_excerpt_keeps_spec_text_centrally(self) -> None:
        logger = ringer.EvalLogger(self.config(spec_storage="excerpt"))
        logger.log_attempt(self.row)
        logger.close()
        self.assertEqual(self.conn.executed[0][1]["spec"], self.row["spec"])
        self.assertEqual(self.rows()[0]["spec"], self.row["spec"])

    def test_insert_failure_closes_connection_and_warns_once(self) -> None:
        self.conn.error = RuntimeError("insert unavailable")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(self.config())
            for number in range(3):
                logger.log_attempt(dict(self.row, task_key=f"task-{number}"))
            logger.close()
        self.assertTrue(self.conn.closed)
        self.assertIsNone(logger._conn)
        self.assertEqual(len(self.conn.executed), 1)
        reason = "postgres insert failed: insert unavailable"
        self.assertEqual(len(self.rows()), 3)
        for local in self.rows():
            self.assertEqual(local["log_sink"], "jsonl")
            self.assertEqual(local["fallback_reason"], reason)
        self.assert_warning(stderr, reason)

    def test_injected_connection_failure_warns_even_for_jsonl_backend(self) -> None:
        logger = ringer.EvalLogger(EvalConfig("jsonl", self.path))
        self.conn.error = RuntimeError("injected failure")
        logger._conn = self.conn
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger.log_attempt(self.row)
            logger.log_attempt(self.row)
        self.assertTrue(self.conn.closed)
        self.assert_warning(stderr, "postgres insert failed: injected failure")

    def test_connect_failure_falls_back_and_warns_once(self) -> None:
        self.driver.connect.side_effect = OSError("connect unavailable")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(self.config())
            logger.log_attempt(self.row)
            logger.log_attempt(self.row)
            logger.close()
        self.assertIsNone(logger._conn)
        reason = "postgres connect failed: connect unavailable"
        for local in self.rows():
            self.assertEqual(local["log_sink"], "jsonl")
            self.assertEqual(local["fallback_reason"], reason)
        self.assert_warning(stderr, reason)

    def test_missing_psycopg_falls_back_and_warns_once(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(sys.modules, {"psycopg": None}), contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(self.config())
            logger.log_attempt(self.row)
            logger.log_attempt(self.row)
            logger.close()
        local = self.rows()[0]
        self.assertEqual(local["log_sink"], "jsonl")
        self.assertTrue(local["fallback_reason"].startswith("postgres "))
        self.assertIn("psycopg", local["fallback_reason"])
        self.assertIsNone(logger._conn)
        self.assert_warning(stderr, local["fallback_reason"])

    def test_missing_database_settings_fall_back(self) -> None:
        self.env_file.write_text("", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(self.config())
            logger.log_attempt(self.row)
            logger.close()
        local = self.rows()[0]
        self.assertEqual(local["log_sink"], "jsonl")
        self.assertTrue(local["fallback_reason"].startswith("postgres "))
        self.assertIn("missing database settings", local["fallback_reason"])
        self.driver.connect.assert_not_called()
        self.assert_warning(stderr, local["fallback_reason"])

    def test_missing_postgres_config_falls_back(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(EvalConfig("postgres", self.path))
            logger.log_attempt(self.row)
            logger.close()
        self.assertEqual(self.rows()[0]["fallback_reason"], "postgres config missing")
        self.assert_warning(stderr, "postgres config missing")

    def test_plain_jsonl_keeps_shape_and_prints_nothing(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            logger = ringer.EvalLogger(EvalConfig("jsonl", self.path))
            logger.log_attempt(self.row)
            logger.close()
        local = self.rows()[0]
        self.assertEqual(set(local), set(self.row) | {"logged_at", "log_sink", "fallback_reason"})
        self.assertEqual(local["log_sink"], "jsonl")
        self.assertIsNone(local["fallback_reason"])
        self.assertEqual(stderr.getvalue(), "")
        self.driver.connect.assert_not_called()

    def test_jsonl_failure_propagates_after_central_insert(self) -> None:
        logger = ringer.EvalLogger(self.config())
        error = OSError("local write failed")
        with mock.patch.object(ringer, "append_jsonl", side_effect=error), self.assertRaises(OSError) as raised:
            logger.log_attempt(self.row)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(self.conn.executed), 1)
        self.assertFalse(self.path.exists())
        logger.close()


if __name__ == "__main__":
    unittest.main()
