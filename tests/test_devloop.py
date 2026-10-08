from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("devloop", ROOT / "scripts" / "dev-loop" / "devloop.py")
devloop = importlib.util.module_from_spec(spec)
sys.modules["devloop"] = devloop
spec.loader.exec_module(devloop)

REPORT = """# Review Report

## Findings
### Finding: First problem
Evidence: a.py:3 `x = 1`
spans a second line
Impact: breaks things
Fix: change it
Priority: P2
Confidence: High

### Finding: Minor nit
Evidence: a.py:4
Impact: none
Fix: none
Priority: P3
Confidence: medium

### Finding: Unsure
Evidence: a.py:5
Impact: maybe
Fix: maybe
Priority: P1
Confidence: low

## Clean
- ok
"""

CFG = {
    "name": "t", "verify": ["true"], "known_failures": ["known_one"],
    "build": {"model": "m", "effort": "low", "timeout_s": 60, "brief": "brief.md", "owned": ["src", "tests/test_x.py"], "required_text": ["foo"]},
    "review": {"lenses": [{"key": "one", "model": "m", "effort": "low", "surface": "surface one"},
                          {"key": "two", "model": "m", "effort": "low", "surface": "surface two"}]},
    "fix": {"model": "m", "effort": "high"},
    "escalate_paths": ["protected/"],
}


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class Fixture:
    """A throwaway repo standing in for the loop worktree, plus a run directory."""

    def __init__(self, tmp: Path) -> None:
        self.wt = tmp / "wt"
        self.wt.mkdir()
        run("init", "-q", "-b", "main", cwd=self.wt)
        run("config", "user.email", "t@example.com", cwd=self.wt)
        run("config", "user.name", "t", cwd=self.wt)
        (self.wt / "specs").mkdir()
        (self.wt / "specs" / "brief.md").write_text("do the thing\n")
        (self.wt / "README.md").write_text("hi\n")
        run("add", "-A", cwd=self.wt)
        run("commit", "-q", "-m", "base", cwd=self.wt)
        self.base = run("rev-parse", "HEAD", cwd=self.wt).strip()
        self.run_dir = tmp / "run"
        self.loop = devloop.Loop("specs", copy.deepcopy(CFG), "t", self.run_dir, self.wt, "loop/t")
        devloop.write_status(self.loop, {"name": "t", "base_sha": self.base, "round": 0})

    def write(self, rel: str, text: str = "x\n") -> None:
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, *paths: str) -> None:
        run("add", "--", *paths, cwd=self.wt)
        run("commit", "-q", "-m", "change", cwd=self.wt)

    def passing_verify_and_clean_triage(self) -> None:
        (self.run_dir / "verify-0.json").write_text(json.dumps({"passed": True}))
        (self.run_dir / "triage-1.json").write_text(json.dumps({"round": 1, "confirmed": [], "noted": [], "incomplete_lenses": []}))
        status = devloop.read_status(self.loop)
        status.update(last_verify="verify-0", last_triage=1, round=1, run_exit={"build": 0})
        devloop.write_status(self.loop, status)


class ParseReportTests(unittest.TestCase):
    def test_parses_fields_and_multiline_evidence(self) -> None:
        first = devloop.parse_report(REPORT, "lens")[0]
        self.assertEqual("First problem", first["title"])
        self.assertEqual("P2", first["priority"])
        self.assertEqual("high", first["confidence"])
        self.assertIn("spans a second line", first["evidence"])

    def test_no_findings_report_yields_nothing(self) -> None:
        self.assertEqual([], devloop.parse_report("# Review Report\n\n## Findings\nNo findings for this surface.\n", "lens"))


class TriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker_ok = True
        original = devloop.report_passes
        devloop.report_passes = lambda *a, **k: self.checker_ok
        self.addCleanup(setattr, devloop, "report_passes", original)

    def triage(self, reports: dict[str, str]) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            devloop.write_status(fx.loop, {"base_sha": fx.base, "round": 1})
            for key, text in reports.items():
                path = fx.run_dir / "review-1" / key
                path.mkdir(parents=True)
                (path / "report.md").write_text(text)
            args = type("A", (), {"spec_dir": "specs", "closeout": False})()
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                import contextlib, io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    devloop.cmd_triage(args)
            finally:
                devloop.load = original
            return json.loads(buf.getvalue()), json.loads((fx.run_dir / "triage-1.json").read_text())

    def test_p2_high_is_confirmed_p3_and_low_confidence_are_only_noted(self) -> None:
        summary, detail = self.triage({"one": REPORT, "two": "# Review Report\n\n## Findings\nNo findings for this surface.\n"})
        self.assertTrue(summary["need_fix"])
        self.assertEqual(["First problem"], [f["title"] for f in detail["confirmed"]])
        self.assertEqual({"Minor nit", "Unsure"}, {f["title"] for f in detail["noted"]})

    def test_a_missing_lens_report_is_flagged_not_ignored(self) -> None:
        summary, detail = self.triage({"one": "# Review Report\n\n## Findings\nNo findings for this surface.\n"})
        self.assertFalse(summary["need_fix"])
        self.assertEqual(["two"], detail["incomplete_lenses"])

    def test_a_report_the_checker_rejects_is_incomplete_even_though_the_file_exists(self) -> None:
        self.checker_ok = False
        summary, detail = self.triage({"one": REPORT, "two": REPORT})
        self.assertEqual(["one", "two"], detail["incomplete_lenses"])
        self.assertEqual([], detail["confirmed"] + detail["noted"])

    def test_finding_ids_are_unique_across_lenses(self) -> None:
        _, detail = self.triage({"one": REPORT, "two": REPORT})
        ids = [f["id"] for f in detail["confirmed"] + detail["noted"]]
        self.assertEqual(len(ids), len(set(ids)))


class ManifestTests(unittest.TestCase):
    def test_build_manifest_confines_the_worker_and_embeds_the_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            task = devloop.manifest_build(fx.loop, devloop.read_status(fx.loop))["tasks"][0]
        self.assertIn("do the thing", task["spec"])
        self.assertIn("you own only these repo paths: src, tests/test_x.py", task["spec"])
        self.assertIn("--owned='src,tests/test_x.py'", task["check"])
        self.assertIn("--required-text='foo'", task["check"])
        self.assertIn(str(fx.wt), task["engine_args"][-1])

    def test_a_required_text_starting_with_dashes_survives_the_check_wrapper(self) -> None:
        # Regression: `--required-text '--json'` made argparse treat the value as an option and the check failed.
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.loop.cfg["build"]["required_text"] = ["--json"]
            task = devloop.manifest_build(fx.loop, devloop.read_status(fx.loop))["tasks"][0]
            (fx.wt / "src").mkdir()
            (fx.wt / "src" / "a.py").write_text("flag = '--json'\n")
            (fx.wt / "tests").mkdir()
            (fx.wt / "tests" / "test_x.py").write_text("")
            (Path(tmp) / "notes.md").write_text("notes\n")
            run("add", "-A", cwd=fx.wt)
            proc = subprocess.run(task["check"], shell=True, cwd=tmp, capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertNotIn("unrecognized arguments", proc.stderr)

    def test_review_manifest_advances_the_round_and_points_at_the_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            manifest = devloop.manifest_review(fx.loop, devloop.read_status(fx.loop))
            self.assertEqual(1, devloop.read_status(fx.loop)["round"])
        self.assertEqual(["one", "two"], [t["key"] for t in manifest["tasks"]])
        self.assertTrue(all(f"diff {fx.base}..HEAD" in t["spec"] for t in manifest["tasks"]))
        self.assertTrue(all("known_one" in t["spec"] for t in manifest["tasks"]))
        self.assertTrue(all("engine_args" in t and "writable_roots" not in " ".join(t["engine_args"]) for t in manifest["tasks"]))

    def test_fix_manifest_lists_only_confirmed_findings_and_demands_reproduction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            devloop.write_status(fx.loop, {"base_sha": fx.base, "round": 1})
            confirmed = {"id": "R1-1", "priority": "P2", "confidence": "high", "lens": "one", "title": "Confirmed bug",
                         "evidence": "a.py:3", "impact": "bad", "fix": "do x"}
            (fx.run_dir / "triage-1.json").write_text(json.dumps({"confirmed": [confirmed], "noted": [dict(confirmed, title="Nit")]}))
            task = devloop.manifest_fix(fx.loop, devloop.read_status(fx.loop))["tasks"][0]
        self.assertIn("Confirmed bug", task["spec"])
        self.assertNotIn("Nit", task["spec"].split("FINDINGS:")[1])
        self.assertIn("first reproduce it", task["spec"])
        self.assertEqual("fix-1", task["key"])


class CommitTests(unittest.TestCase):
    def test_commit_stages_only_owned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.write("src/a.py")
            fx.write("stray.txt")
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                import contextlib, io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    devloop.cmd_commit(type("A", (), {"spec_dir": "specs", "message": "loop: build"})())
            finally:
                devloop.load = original
            result = json.loads(buf.getvalue())
            untracked = run("status", "--porcelain", cwd=fx.wt)
        self.assertEqual(["src/a.py"], result["files"])
        self.assertIn("stray.txt", untracked)

    def test_commit_works_when_some_owned_paths_do_not_exist_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.write("src/a.py")  # tests/test_x.py is owned but was never created
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                import contextlib, io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    devloop.cmd_commit(type("A", (), {"spec_dir": "specs", "message": "m"})())
            finally:
                devloop.load = original
        self.assertEqual(["src/a.py"], json.loads(buf.getvalue())["files"])

    def test_commit_with_nothing_owned_changed_makes_no_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                import contextlib, io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    devloop.cmd_commit(type("A", (), {"spec_dir": "specs", "message": "m"})())
            finally:
                devloop.load = original
        self.assertFalse(json.loads(buf.getvalue())["committed"])


class DecideTests(unittest.TestCase):
    def decision(self, setup) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            setup(fx)
            return devloop.decide(fx.loop)

    def clean_change(self, fx: Fixture) -> None:
        fx.write("src/a.py")
        fx.commit("src/a.py")
        fx.passing_verify_and_clean_triage()

    def test_approves_a_verified_clean_in_scope_change(self) -> None:
        d = self.decision(self.clean_change)
        self.assertEqual("APPROVE", d["decision"], d["reasons"])

    def test_escalates_when_nothing_changed(self) -> None:
        def setup(fx: Fixture) -> None:
            fx.passing_verify_and_clean_triage()
        self.assertIn("the loop produced no change", self.decision(setup)["reasons"])

    def test_escalates_a_change_outside_the_owned_paths(self) -> None:
        def setup(fx: Fixture) -> None:
            self.clean_change(fx)
            fx.write("other/b.py")
            fx.commit("other/b.py")
        d = self.decision(setup)
        self.assertEqual("ESCALATE", d["decision"])
        self.assertTrue(any("outside the owned paths" in r for r in d["reasons"]))

    def test_escalates_when_a_protected_path_is_touched(self) -> None:
        def setup(fx: Fixture) -> None:
            fx.loop.cfg["build"]["owned"].append("protected")
            self.clean_change(fx)
            fx.write("protected/x.sql")
            fx.commit("protected/x.sql")
        self.assertTrue(any("protected paths" in r for r in self.decision(setup)["reasons"]))

    def test_escalates_when_verification_is_failing(self) -> None:
        def setup(fx: Fixture) -> None:
            self.clean_change(fx)
            (fx.run_dir / "verify-0.json").write_text(json.dumps({"passed": False}))
        self.assertTrue(any("verification is failing" in r for r in self.decision(setup)["reasons"]))

    def test_escalates_when_confirmed_findings_remain(self) -> None:
        def setup(fx: Fixture) -> None:
            self.clean_change(fx)
            bad = {"priority": "P2", "title": "Still broken"}
            (fx.run_dir / "triage-1.json").write_text(json.dumps({"round": 1, "confirmed": [bad], "noted": [], "incomplete_lenses": []}))
        self.assertTrue(any("confirmed finding(s) remain" in r for r in self.decision(setup)["reasons"]))

    def test_escalates_when_a_review_lens_did_not_report(self) -> None:
        def setup(fx: Fixture) -> None:
            self.clean_change(fx)
            (fx.run_dir / "triage-1.json").write_text(json.dumps({"round": 1, "confirmed": [], "noted": [], "incomplete_lenses": ["two"]}))
        self.assertTrue(any("did not report" in r for r in self.decision(setup)["reasons"]))

    def test_escalates_when_the_build_run_failed_or_was_not_recorded(self) -> None:
        def failed(fx: Fixture) -> None:
            self.clean_change(fx)
            status = devloop.read_status(fx.loop)
            status["run_exit"] = {"build": 1}
            devloop.write_status(fx.loop, status)
        self.assertTrue(any("build run did not succeed (Ringer exit 1)" in r for r in self.decision(failed)["reasons"]))

        def unrecorded(fx: Fixture) -> None:
            self.clean_change(fx)
            status = devloop.read_status(fx.loop)
            status.pop("run_exit")
            devloop.write_status(fx.loop, status)
        self.assertTrue(any("not recorded" in r for r in self.decision(unrecorded)["reasons"]))

    def test_escalates_when_a_stray_change_is_left_uncommitted_in_the_worktree(self) -> None:
        def setup(fx: Fixture) -> None:
            self.clean_change(fx)
            fx.write("other/stray.py")
            (fx.wt / "README.md").write_text("edited\n")
        d = self.decision(setup)
        reason = next(r for r in d["reasons"] if "uncommitted changes" in r)
        self.assertIn("other/stray.py", reason)
        self.assertIn("README.md", reason)

    def test_record_run_stores_the_exit_status_and_a_new_wave_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                devloop.cmd_record_run(type("A", (), {"spec_dir": "specs", "kind": "build", "exit_code": 3})())
            finally:
                devloop.load = original
            self.assertEqual({"build": 3}, devloop.read_status(fx.loop)["run_exit"])

    def test_escalates_without_any_verification_record(self) -> None:
        def setup(fx: Fixture) -> None:
            fx.write("src/a.py")
            fx.commit("src/a.py")
        self.assertIn("no verification result recorded", self.decision(setup)["reasons"])

    def test_decision_files_start_with_the_machine_readable_word(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            self.clean_change(fx)
            devloop.decide(fx.loop)
            first = (fx.run_dir / "decision.md").read_text().splitlines()[0]
        self.assertEqual("APPROVE", first)


class ConfigTests(unittest.TestCase):
    def test_a_loop_name_must_be_a_safe_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_dir = Path(tmp)
            (spec_dir / "loop.json").write_text(json.dumps(dict(CFG, name="Bad Name")))
            original = devloop.REPO
            devloop.REPO = spec_dir.parent
            try:
                with self.assertRaises(SystemExit):
                    devloop.load(spec_dir.name)
            finally:
                devloop.REPO = original


if __name__ == "__main__":
    unittest.main()
