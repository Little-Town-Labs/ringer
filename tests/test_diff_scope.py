from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev-loop" / "diff_scope.py"
SOURCE = """import os
import sys


def alpha():
    return 1


def beta():
    return 2


class Gamma:
    pass
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class DiffScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "m.py").write_text(SOURCE)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def check(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), self.base, "m.py", *args], cwd=self.repo, capture_output=True, text=True)

    def edit(self, old: str, new: str) -> None:
        path = self.repo / "m.py"
        path.write_text(path.read_text().replace(old, new))

    def test_a_change_inside_the_named_function_passes(self) -> None:
        self.edit("return 1", "return 10")
        self.assertEqual(0, self.check("alpha").returncode)

    def test_a_change_in_another_function_fails_and_names_the_lines(self) -> None:
        self.edit("return 2", "return 20")
        result = self.check("alpha")
        self.assertEqual(1, result.returncode)
        self.assertIn("changed outside alpha", result.stdout)

    def test_a_change_to_a_class_can_be_allowed_by_name(self) -> None:
        self.edit("    pass", "    value = 3")
        self.assertEqual(0, self.check("Gamma").returncode)

    def test_the_import_block_is_allowed_only_when_requested(self) -> None:
        self.edit("import sys\n", "import sys\nimport json\n")
        self.assertEqual(1, self.check("alpha").returncode)
        self.assertEqual(0, self.check("alpha", "--imports-up-to", "3").returncode)

    def test_committed_changes_since_the_base_are_included(self) -> None:
        self.edit("return 2", "return 20")
        git(self.repo, "commit", "-q", "-am", "later")
        self.assertEqual(1, self.check("alpha").returncode)

    def test_no_change_passes(self) -> None:
        self.assertEqual(0, self.check("alpha").returncode)

    def test_a_function_missing_from_the_base_is_reported(self) -> None:
        result = self.check("nope")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not found", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
