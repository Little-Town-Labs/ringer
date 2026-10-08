from __future__ import annotations

import os
import unittest
from decimal import Decimal
import uuid
from pathlib import Path

try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
except ImportError:
    psycopg = None
    conninfo_to_dict = None


DSN = os.environ.get("RINGER_TEST_PG_DSN")
SCHEMA_PATH = Path(__file__).parents[1] / "scripts" / "central-evidence" / "schema.sql"


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(DSN and psycopg is not None, "requires RINGER_TEST_PG_DSN and psycopg")
class CentralSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner_params = conninfo_to_dict(DSN)
        host = cls.owner_params.get("host", "")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("RINGER_TEST_PG_DSN must use a loopback host for a throwaway database")
        cls.writer_password = "writer-" + uuid.uuid4().hex
        cls.reader_password = "reader-" + uuid.uuid4().hex
        cls.apply_schema()

    @classmethod
    def apply_schema(cls):
        sql = SCHEMA_PATH.read_text()
        sql = sql.replace(":'writer_pw'", quote_literal(cls.writer_password))
        sql = sql.replace(":'reader_pw'", quote_literal(cls.reader_password))
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(sql)

    def setUp(self):
        with psycopg.connect(DSN) as connection:
            connection.execute("TRUNCATE ringer.attempts RESTART IDENTITY")

    def connect_as(self, role: str, password: str):
        params = dict(self.owner_params)
        params["user"] = role
        params["password"] = password
        return psycopg.connect(**params)

    def insert_attempt(self, attempt_uid: str, run_id: str, verdict: str = "PASS"):
        statement = (
            "INSERT INTO ringer.attempts "
            "(attempt_uid, source_host, run_id, task_key, model, task_type, verdict) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )
        values = (attempt_uid, "test-host", run_id, "task", "test-model", "test-type", verdict)
        return statement, values

    def test_schema_applies_twice(self):
        self.apply_schema()
        self.apply_schema()

    def test_legacy_insert_gets_legacy_identity_and_host(self):
        with self.connect_as("ringer_writer", self.writer_password) as connection:
            connection.execute(
                "INSERT INTO swarm_runs "
                "(run_id, pattern, task_key, spec, worker_engine, shepherd_model, verify_method, "
                "verdict, duration_ms, worker_tokens, notes, orchestrator) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("legacy-run", "pattern", "task", None, "engine", "shepherd", "verify", "PASS", 100, 2, None, "test"),
            )
        with psycopg.connect(DSN) as connection:
            row = connection.execute(
                "SELECT attempt_uid, source_host FROM ringer.attempts WHERE run_id = 'legacy-run'"
            ).fetchone()
        self.assertTrue(row[0].startswith("legacy:"))
        self.assertEqual(row[1], "legacy")

    def test_insert_only_writer_can_repeat_idempotent_insert(self):
        statement, values = self.insert_attempt("repeat-uid", "repeat-run")
        with self.connect_as("ringer_writer", self.writer_password) as connection:
            first = connection.execute(statement, values).rowcount
            second = connection.execute(statement, values).rowcount
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_writer_cannot_select_attempts(self):
        with self.connect_as("ringer_writer", self.writer_password) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM ringer.attempts").fetchall()

    def test_reader_can_select_views_and_cannot_mutate(self):
        with self.connect_as("ringer_reader", self.reader_password) as connection:
            for relation in ("ringer.attempts", "ringer.model_scoreboard", "ringer.daily_activity"):
                connection.execute(f"SELECT * FROM {relation} LIMIT 0")
            for statement in (
                "INSERT INTO ringer.attempts (attempt_uid, run_id, task_key, verdict) VALUES ('x', 'x', 'x', 'PASS')",
                "UPDATE ringer.attempts SET verdict = 'FAIL'",
                "DELETE FROM ringer.attempts",
            ):
                with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
                connection.rollback()

    def test_model_scoreboard_aggregates_inserted_rows(self):
        with self.connect_as("ringer_writer", self.writer_password) as connection:
            for uid, run_id, verdict in (
                ("score-1", "score-run-1", "PASS"),
                ("score-2", "score-run-2", "PASS"),
                ("score-3", "score-run-3", "FAIL"),
            ):
                statement, values = self.insert_attempt(uid, run_id, verdict)
                connection.execute(statement, values)
        with self.connect_as("ringer_reader", self.reader_password) as connection:
            row = connection.execute(
                "SELECT attempts, passes, pass_pct FROM ringer.model_scoreboard "
                "WHERE model = 'test-model' AND task_type = 'test-type'"
            ).fetchone()
        self.assertEqual(row, (3, 2, Decimal("66.7")))

    def test_named_conflict_target_is_denied_for_writer(self):
        with self.connect_as("ringer_writer", self.writer_password) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO ringer.attempts (attempt_uid, run_id, task_key, verdict) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (attempt_uid) DO NOTHING",
                    ("named-conflict", "named-run", "task", "PASS"),
                )


if __name__ == "__main__":
    unittest.main()
