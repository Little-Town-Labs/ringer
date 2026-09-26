import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ringer_core.evidence import append_jsonl


def _process_write(path, count, prefix):
    for index in range(count):
        append_jsonl(Path(path), {"writer": prefix, "index": index, "blob": "x" * 12000})


class EvidenceTests(unittest.TestCase):
    def test_appends_without_mutating_and_keeps_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            row = {"run_id": "r1", "retry": False}
            original = dict(row)
            append_jsonl(path, row)
            saved = json.loads(path.read_text().splitlines()[0])
            self.assertEqual({k: saved[k] for k in row}, row)
            self.assertEqual(row, original)
            self.assertEqual(saved, row)

    def test_serialization_matches_previous_json_dumps_bytes(self):
        cases = [
            {"spec": "café 東京", "retry": False},
            {"spec": "filename-" + chr(0xdcff), "retry": False},
        ]
        for row in cases:
            with self.subTest(spec=repr(row["spec"])), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "evidence.jsonl"
                original = dict(row)
                expected = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
                append_jsonl(path, row)
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(row, original)

    def test_rejects_corrupt_tail_without_changing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            prior = b'{"old":1}\nnot-json'
            path.write_bytes(prior)
            with self.assertRaisesRegex(ValueError, "tail"):
                append_jsonl(path, {"new": 2})
            self.assertEqual(path.read_bytes(), prior)

    def test_rejects_malformed_complete_tail_without_changing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            prior = b'{"old":1}\nnot-json\n'
            path.write_bytes(prior)
            with self.assertRaisesRegex(ValueError, "malformed"):
                append_jsonl(path, {"new": 2})
            self.assertEqual(path.read_bytes(), prior)

    def test_serialization_failure_does_not_create_or_change_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            with self.assertRaises(TypeError):
                append_jsonl(path, {"bad": object()})
            self.assertFalse(path.exists())

    def test_concurrent_threads_and_processes_append_whole_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            threads = [threading.Thread(target=_process_write, args=(str(path), 8, f"t{i}")) for i in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            children = [multiprocessing.Process(target=_process_write, args=(str(path), 8, f"p{i}")) for i in range(3)]
            for child in children: child.start()
            for child in children:
                child.join(20)
                self.assertEqual(child.exitcode, 0)
            lines = path.read_bytes().splitlines()
            self.assertEqual(len(lines), 56)
            rows = [json.loads(line) for line in lines]
            self.assertEqual(len({(row["writer"], row["index"]) for row in rows}), 56)

    def test_short_and_interrupted_writes_are_completed(self):
        import ringer_core.evidence as evidence
        real_write = os.write
        calls = 0
        def partial(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError()
            return real_write(fd, data[:max(1, len(data)//3)])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(evidence.os, "write", side_effect=partial):
            path = Path(directory) / "rows"
            append_jsonl(path, {"ok": True})
            self.assertTrue(json.loads(path.read_text())["ok"])
            self.assertGreater(calls, 2)

    def test_append_failure_preserves_prior_rows(self):
        import ringer_core.evidence as evidence
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows"
            path.write_bytes(b'{"prior":true}\n')
            with mock.patch.object(evidence.os, "write", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "append"):
                    append_jsonl(path, {"next": True})
            self.assertEqual(path.read_bytes(), b'{"prior":true}\n')

    def test_partial_append_failure_rolls_back_only_current_row(self):
        import ringer_core.evidence as evidence
        real_write = os.write
        calls = 0
        def partial_then_fail(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:5])
            raise OSError("disk full")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows"
            prior = b'{"prior":true}\n'
            path.write_bytes(prior)
            with mock.patch.object(evidence.os, "write", side_effect=partial_then_fail):
                with self.assertRaisesRegex(OSError, "append"):
                    append_jsonl(path, {"next": True})
            self.assertEqual(path.read_bytes(), prior)

    def test_open_failure_is_contextual(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("ringer_core.evidence.os.open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(OSError, "evidence"):
                append_jsonl(Path(directory) / "rows", {"a": 1})


if __name__ == "__main__":
    unittest.main()
