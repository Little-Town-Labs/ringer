from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
from ringer import AppConfig, ArtifactConfig, EngineConfig, EvalConfig, Manifest, RingerRunner  # noqa: E402


class RecordingLogger:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.closed = False

    def log_attempt(self, row: dict[str, object]) -> None:
        self.rows.append(row)

    def close(self) -> None:
        self.closed = True


def config(root: Path) -> AppConfig:
    state = root / "state"
    return AppConfig(
        path=None,
        identity_default=None,
        state_dir=state,
        dashboard_port_base=8787,
        hud_port=8700,
        hud_app_path=None,
        allow_full_access=False,
        eval=EvalConfig("jsonl", root / "attempts.jsonl"),
        engines={
            "test": EngineConfig(
                name="test", bin=sys.executable, args_template=("-c", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            )
        },
        artifact=ArtifactConfig(
            enabled=False, out_template=str(root / "{run_id}.html"),
            report_template=str(root / "{run_id}-report.html"), index_out=root / "index.html",
        ),
    )


def manifest(root: Path) -> Manifest:
    return Manifest.from_obj({
        "run_name": "logger-test", "workdir": str(root / "work"), "max_parallel": 1,
        "tasks": [{"key": "one", "engine": "test", "spec": "from pathlib import Path; Path('out').write_text('ok')", "check": "test -s out"}],
    })


class RunnerLoggerTests(unittest.TestCase):
    def test_logger_is_required_keyword_only_and_receives_rows_then_closes(self) -> None:
        parameter = inspect.signature(RingerRunner).parameters["logger"]
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logger = RecordingLogger()
            runner = RingerRunner(manifest(root), config(root), "test", dashboard_enabled=False, logger=logger)
            self.assertEqual(0, asyncio.run(runner.run()))
            self.assertTrue(logger.rows)
            self.assertEqual("PASS", logger.rows[0]["verdict"])
            self.assertTrue(logger.closed)

    def test_cli_closes_logger_when_runner_construction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logger = RecordingLogger()
            with mock.patch.object(ringer, "EvalLogger", return_value=logger), mock.patch.object(
                ringer, "RingerRunner", side_effect=RuntimeError("construction failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "construction failed"):
                    asyncio.run(ringer.run_manifest(manifest(root), config(root), "test", False, False))
            self.assertTrue(logger.closed)


if __name__ == "__main__":
    unittest.main()
