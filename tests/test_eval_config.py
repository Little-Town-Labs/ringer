from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from ringer_core.config import EvalConfig, load_eval_config


class EvalConfigTests(unittest.TestCase):
    def load(self, raw: dict[str, object]) -> EvalConfig:
        return load_eval_config(raw, Path("/tmp/ringer-test-state"))

    def test_postgres_defaults_with_only_env_file(self) -> None:
        config = self.load({"postgres": {"env_file": "db.env"}})

        self.assertEqual(config.postgres.spec_storage, "hash")
        self.assertIsNone(config.postgres.source_host)

    def test_spec_storage_values_are_accepted(self) -> None:
        for value in ("hash", "excerpt"):
            with self.subTest(value=value):
                config = self.load({"postgres": {"env_file": "db.env", "spec_storage": value}})
                self.assertEqual(config.postgres.spec_storage, value)

    def test_invalid_spec_storage_values_are_rejected(self) -> None:
        for value in ("HASH", "", 1, ["hash"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "eval.postgres.spec_storage must be 'hash' or 'excerpt'",
                ):
                    self.load({"postgres": {"env_file": "db.env", "spec_storage": value}})

    def test_source_host_is_trimmed(self) -> None:
        config = self.load({"postgres": {"env_file": "db.env", "source_host": "  laptop  "}})

        self.assertEqual(config.postgres.source_host, "laptop")

    def test_invalid_source_hosts_are_rejected(self) -> None:
        for value in ("", "   ", 12, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "eval.postgres.source_host must be a non-empty string",
                ):
                    self.load({"postgres": {"env_file": "db.env", "source_host": value}})

    def test_existing_eval_errors_are_unchanged(self) -> None:
        cases = (
            ({"backend": "postgres"}, "eval.backend='postgres' requires [eval.postgres].env_file"),
            ({"postgres": {}}, "eval.postgres.env_file is required"),
            ({"backend": "sqlite"}, "eval.backend must be 'jsonl' or 'postgres'"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:
                    self.load(raw)
                self.assertEqual(str(error.exception), message)

    def test_shipped_sample_keeps_jsonl_default(self) -> None:
        sample_path = Path(__file__).resolve().parents[1] / "config.sample.toml"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_eval_config(
                tomllib.loads(sample_path.read_text(encoding="utf-8"))["eval"],
                Path(temp_dir),
            )

        self.assertEqual(config.backend, "jsonl")
        self.assertIsNone(config.postgres)


if __name__ == "__main__":
    unittest.main()
