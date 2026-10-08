from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_devloop import CFG, Fixture, devloop, run

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "dev-loop"

TASKS = """# Tasks

## Phase 1: Setup
- [ ] T001 [P] Create alpha (deps: none)
- [ ] T002 [P] Create beta
- [x] T000 Already done

## Phase 2: Combine
- [ ] T003 Combine alpha and beta. Prerequisite T001, T002.
- [ ] T004 [US1] Polish (depends on T003)
"""

GATE_REPORT = """# Code Review Quality Gate

## Scope
Wave 1.

## Findings
### [Required] Duplicate branch instead of a shared helper
Evidence: src/a.py:1 `value = compute()`
Impact: two code paths can drift
Fix: collapse them into one helper

### [Nit] Naming
Evidence: src/a.py:1
Impact: none
Fix: rename

## Five-axis summary
- Correctness: ok
- Readability: ok
- Architecture: duplicate branch
- Security: ok
- Performance: ok

## Verification
Ran the tests.

## Verdict
REQUEST CHANGES
"""


def loop_with(fx: Fixture, tasks_md: str, **extra) -> None:
    fx.write("specs/tasks.md", tasks_md)
    fx.loop.cfg["tasks"] = {t: {"brief": "brief.md", "owned": [f"out/{t}"], "verify": ["true"]} for t in
                            ("T001", "T002", "T003", "T004")}
    fx.loop.cfg.update(extra)


class TaskParsingTests(unittest.TestCase):
    def test_parses_ids_state_markers_phases_and_titles(self) -> None:
        tasks = {t.id: t for t in devloop.parse_tasks(TASKS)}
        self.assertEqual(["T000", "T001", "T002", "T003", "T004"], sorted(tasks))
        self.assertTrue(tasks["T000"].done)
        self.assertFalse(tasks["T001"].done)
        self.assertTrue(tasks["T001"].parallel and tasks["T002"].parallel)
        self.assertFalse(tasks["T003"].parallel)
        self.assertEqual((1, 1, 2, 2), (tasks["T001"].phase, tasks["T002"].phase, tasks["T003"].phase, tasks["T004"].phase))
        self.assertEqual("Polish", tasks["T004"].title)

    def test_dependency_phrasings(self) -> None:
        tasks = {t.id: t for t in devloop.parse_tasks(TASKS)}
        self.assertEqual(("T001", "T002"), tasks["T003"].deps)
        self.assertEqual(("T003",), tasks["T004"].deps)

    def test_uppercase_x_counts_as_done_and_other_lines_are_ignored(self) -> None:
        tasks = devloop.parse_tasks("Notes\n- [X] T010 Done thing\n* [ ] T011 Open\n- not a task\n")
        self.assertEqual([("T010", True), ("T011", False)], [(t.id, t.done) for t in tasks])

    def test_mark_done_only_touches_the_named_tasks(self) -> None:
        text = devloop.mark_done(TASKS, ["T001"])
        self.assertIn("- [x] T001", text)
        self.assertIn("- [ ] T002", text)
        self.assertIn("- [ ] T003", text)


class ReadyWaveTests(unittest.TestCase):
    def wave(self, tasks_md: str, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, tasks_md, **extra)
            chosen, state, reasons = devloop.ready_wave(fx.loop, devloop.parse_tasks(tasks_md))
            return [t.id for t in chosen], state, reasons

    def test_parallel_tasks_with_disjoint_paths_form_one_wave(self) -> None:
        self.assertEqual((["T001", "T002"], "run"), self.wave(TASKS)[:2])

    def test_later_phase_waits_for_earlier_phase(self) -> None:
        ids, state, _ = self.wave(TASKS.replace("- [ ] T001", "- [x] T001").replace("- [ ] T002", "- [x] T002"))
        self.assertEqual((["T003"], "run"), (ids, state))

    def test_a_later_phase_waits_even_with_no_explicit_dependency(self) -> None:
        md = "## Phase 1\n- [ ] T001 [P] First\n## Phase 2\n- [ ] T002 [P] Second\n"
        self.assertEqual((["T001"], "run"), self.wave(md)[:2])
        self.assertEqual((["T002"], "run"), self.wave(md.replace("- [ ] T001", "- [x] T001"))[:2])

    def test_dependency_not_done_blocks_a_task(self) -> None:
        md = "- [ ] T001 First\n- [ ] T002 Second (deps: T001)\n"
        self.assertEqual((["T001"], "run"), self.wave(md)[:2])

    def test_a_cycle_is_reported_as_blocked(self) -> None:
        md = "- [ ] T001 A (deps: T002)\n- [ ] T002 B (deps: T001)\n"
        ids, state, reasons = self.wave(md)
        self.assertEqual(([], "blocked"), (ids, state))
        self.assertIn("waiting on", reasons[0])

    def test_everything_checked_is_done(self) -> None:
        self.assertEqual(([], "done"), self.wave("- [x] T001 A\n- [x] T002 B\n")[:2])

    def test_overlapping_owned_paths_do_not_share_a_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            fx.loop.cfg["tasks"]["T002"]["owned"] = ["out/T001/sub"]
            chosen, _, _ = devloop.ready_wave(fx.loop, devloop.parse_tasks(TASKS))
        self.assertEqual(["T001"], [t.id for t in chosen])

    def test_wave_size_is_capped(self) -> None:
        md = "- [ ] T001 [P] A\n- [ ] T002 [P] B\n- [ ] T003 [P] C\n"
        self.assertEqual(["T001", "T002"], self.wave(md, max_wave_size=2)[0])

    def test_a_task_without_a_loop_config_entry_blocks_a_multi_task_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            del fx.loop.cfg["tasks"]["T001"]
            ids, state, reasons = devloop.ready_wave(fx.loop, devloop.parse_tasks(TASKS))
        self.assertEqual("blocked", state)
        self.assertIn("T001", reasons[0])

    def test_a_sequential_task_waits_for_the_earlier_sequential_task_in_its_phase(self) -> None:
        md = "## Phase 1\n- [ ] T001 One\n- [ ] T002 Two\n"
        self.assertEqual(["T001"], self.wave(md)[0])


class StartWaveTests(unittest.TestCase):
    def test_starting_a_wave_records_its_base_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            out = devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            status = devloop.read_status(fx.loop)
        self.assertEqual((True, 1, ["T001", "T002"]), (out["run"], out["wave"], out["tasks"]))
        self.assertEqual(fx.base, status["wave_base_sha"])
        self.assertEqual(0, status["round"])

    def test_a_second_wave_numbers_up_and_prefixes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS.replace("- [ ] T001", "- [x] T001").replace("- [ ] T002", "- [x] T002"))
            status = devloop.read_status(fx.loop)
            status["wave"] = 1
            devloop.write_status(fx.loop, status)
            out = devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            prefix = devloop.pfx(devloop.read_status(fx.loop))
        self.assertEqual((2, ["T003"], "w2-"), (out["wave"], out["tasks"], prefix))

    def test_a_feature_without_a_task_list_is_one_implicit_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            out = devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            self.assertEqual(["T001"], out["tasks"])
            status = devloop.read_status(fx.loop)
            status["implicit_done"] = True
            devloop.write_status(fx.loop, status)
            again = devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
        self.assertTrue(again["done"])

    def test_nothing_ready_stops_with_blocked_state_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, "- [ ] T001 A (deps: T002)\n- [ ] T002 B (deps: T001)\n")
            out = devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            self.assertTrue(out["blocked"])
            self.assertEqual("blocked", devloop.read_status(fx.loop)["wave_state"])


class PreflightTests(unittest.TestCase):
    def check(self, files: dict[str, str], **cfg) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.loop.cfg.update(cfg)
            for rel, text in files.items():
                fx.write(f"specs/{rel}", text)
            return devloop.preflight(fx.loop)

    def test_required_checklists_must_exist(self) -> None:
        out = self.check({})
        self.assertFalse(out["ok"])
        self.assertTrue(out["needs_gate"])
        self.assertIn("no checklists", out["reasons"][0])

    def test_unchecked_items_fail_the_gate_and_are_counted(self) -> None:
        out = self.check({"checklists/requirements.md": "- [x] a\n- [ ] b\n- [ ] c\n"})
        self.assertFalse(out["ok"])
        self.assertIn("2 of 3 items unchecked", out["reasons"][0])
        self.assertEqual({"file": "requirements.md", "total": 3, "checked": 1, "unchecked": 2}, out["checklists"][0])

    def test_fully_checked_checklists_pass(self) -> None:
        self.assertTrue(self.check({"checklists/requirements.md": "- [x] a\n- [X] b\n"})["ok"])

    def test_if_present_mode_allows_a_missing_folder_but_not_unchecked_items(self) -> None:
        self.assertTrue(self.check({}, checklists="if-present")["ok"])
        self.assertFalse(self.check({"checklists/r.md": "- [ ] a\n"}, checklists="if-present")["ok"])

    def test_off_mode_skips_the_checklist_gate(self) -> None:
        self.assertTrue(self.check({"checklists/r.md": "- [ ] a\n"}, checklists="off")["ok"])

    def test_an_empty_checklist_does_not_count_as_complete(self) -> None:
        self.assertFalse(self.check({"checklists/r.md": "nothing here\n"})["ok"])

    def test_analysis_with_critical_issues_fails(self) -> None:
        files = {"checklists/r.md": "- [x] a\n", "analysis.md": "Critical Issues Count: 2\n"}
        out = self.check(files)
        self.assertFalse(out["ok"])
        self.assertIn("2 critical", out["reasons"][0])

    def test_analysis_with_zero_critical_issues_passes_and_required_mode_needs_the_file(self) -> None:
        files = {"checklists/r.md": "- [x] a\n", "analysis.md": "Critical Issues Count: 0\n"}
        self.assertTrue(self.check(files)["ok"])
        self.assertFalse(self.check({"checklists/r.md": "- [x] a\n"}, analysis="required")["ok"])

    def test_sensitive_or_production_risk_is_a_hard_stop(self) -> None:
        for risk in ("sensitive", "production"):
            out = self.check({"checklists/r.md": "- [x] a\n"}, risk=risk)
            self.assertTrue(out["hard_stop"], risk)
            self.assertIn("lead-controlled", out["reasons"][0])

    def test_a_route_gate_asks_for_approval_even_when_everything_else_passes(self) -> None:
        out = self.check({"checklists/r.md": "- [x] a\n"}, route_gate=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["needs_gate"])
        self.assertFalse(out["hard_stop"])


class GateReportTests(unittest.TestCase):
    def test_parses_severities_titles_fields_and_verdict(self) -> None:
        findings, verdict = devloop.parse_gate_report(GATE_REPORT, "quality-gate")
        self.assertEqual("REQUEST CHANGES", verdict)
        self.assertEqual(["Required", "Nit"], [f["severity"] for f in findings])
        self.assertEqual("Duplicate branch instead of a shared helper", findings[0]["title"])
        self.assertIn("collapse them", findings[0]["fix"])

    def test_an_invalid_verdict_line_is_none(self) -> None:
        _, verdict = devloop.parse_gate_report(GATE_REPORT.replace("REQUEST CHANGES", "maybe"), "quality-gate")
        self.assertIsNone(verdict)

    def triage(self, report: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "review-1" / "quality-gate"
            root.mkdir(parents=True)
            (root / "report.md").write_text(report)
            return devloop.triage_reports(Path(tmp) / "review-1", ["quality-gate"], ("quality-gate",), 1)

    def test_required_findings_are_confirmed_and_nits_are_only_noted(self) -> None:
        out = self.triage(GATE_REPORT)
        self.assertEqual(["Required"], [f["severity"] for f in out["confirmed"]])
        self.assertEqual(["Nit"], [f["severity"] for f in out["noted"]])
        self.assertEqual({"quality-gate": "REQUEST CHANGES"}, out["gate_verdicts"])

    def test_request_changes_without_a_blocking_finding_still_escalates(self) -> None:
        out = self.triage(GATE_REPORT.replace("### [Required]", "### [Optional]"))
        self.assertEqual([], out["confirmed"])
        self.assertTrue(any("without a Critical or Required" in r for r in devloop.gate_reasons(out, "round 1")))

    def test_a_missing_or_invalid_verdict_is_a_reason_to_escalate(self) -> None:
        out = self.triage(GATE_REPORT.replace("REQUEST CHANGES", "maybe"))
        self.assertTrue(any("valid verdict" in r for r in devloop.gate_reasons(out, "round 1")))

    def test_a_clean_approving_gate_has_no_reasons(self) -> None:
        clean = GATE_REPORT.split("## Findings")[0] + "## Findings\nNo findings.\n\n" + GATE_REPORT.split("## Five-axis summary")[1].join(
            ["## Five-axis summary", ""]).replace("REQUEST CHANGES", "APPROVE")
        out = self.triage(clean)
        self.assertEqual([], devloop.gate_reasons(out, "round 1"))


class ScopeChangeTests(unittest.TestCase):
    REPORT = ("# Scope Change Required\n\n## Blocking Task\nT003\n\n## Evidence\nThe spec forbids it.\n\n## Required Decision\n"
              "Allow a new config key?\n\n## Suggested Spec Kit Update\nAdd R9.\n")

    def test_a_valid_report_in_a_work_dir_escalates_the_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.passing_verify_and_clean_triage()
            task = fx.run_dir / "work-build" / "build-T001"
            task.mkdir(parents=True)
            (task / "scope-change.md").write_text(self.REPORT)
            d = devloop.decide(fx.loop)
        self.assertEqual("ESCALATE", d["decision"])
        self.assertEqual(["build-T001"], d["scope_change"])
        self.assertTrue(any("Allow a new config key?" in r for r in d["reasons"]))

    def test_a_report_missing_a_heading_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            task = fx.run_dir / "work-build" / "build-T001"
            task.mkdir(parents=True)
            (task / "scope-change.md").write_text("# Scope Change Required\nonly this\n")
            self.assertEqual([], devloop.scope_changes(fx.loop, devloop.read_status(fx.loop)))


class FinishWaveTests(unittest.TestCase):
    def wave_ready(self, fx: Fixture, tasks_md: str = TASKS) -> None:
        loop_with(fx, tasks_md)
        fx.commit("specs/tasks.md")
        status = devloop.read_status(fx.loop)
        out = devloop.start_wave(fx.loop, status)
        self.assertTrue(out["run"])
        fx.write("out/T001/a.py")
        fx.write("out/T002/b.py")
        devloop.commit_paths(fx.loop, devloop.owned_union(fx.loop, devloop.read_status(fx.loop)), "loop: build")
        fx.passing_verify_and_clean_triage()

    def test_an_approved_wave_marks_its_tasks_done_and_records_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            self.wave_ready(fx)
            devloop.decide(fx.loop)
            out = devloop.finish_wave(fx.loop)
            tasks = (fx.wt / "specs" / "tasks.md").read_text()
            delivery = (fx.wt / "specs" / "delivery.md").read_text()
            log = run("log", "--format=%s", cwd=fx.wt)
            tracked = run("status", "--porcelain", cwd=fx.wt)
        self.assertEqual({"state": "approved", "continue": True, "wave": 1}, out)
        self.assertIn("- [x] T001", tasks)
        self.assertIn("- [x] T002", tasks)
        self.assertIn("- [ ] T003", tasks)
        self.assertIn("## Wave 1: APPROVE", delivery)
        self.assertIn("T001, T002", delivery)
        self.assertIn("loop: wave 1 approved", log)
        self.assertEqual("", tracked.strip())

    def test_an_escalated_wave_leaves_the_tasks_unchecked_but_records_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            self.wave_ready(fx)
            (fx.run_dir / "triage-1.json").write_text(json.dumps(
                {"round": 1, "confirmed": [{"title": "Broken", "severity": "Required"}], "noted": [], "incomplete_lenses": []}))
            d = devloop.decide(fx.loop)
            out = devloop.finish_wave(fx.loop)
            tasks = (fx.wt / "specs" / "tasks.md").read_text()
            delivery = (fx.wt / "specs" / "delivery.md").read_text()
        self.assertEqual("ESCALATE", d["decision"])
        self.assertEqual({"state": "escalated", "continue": False, "wave": 1}, out)
        self.assertIn("- [ ] T001", tasks)
        self.assertIn("## Wave 1: ESCALATE", delivery)
        self.assertIn("confirmed finding(s) remain", delivery)

    def test_accepting_an_escalated_wave_marks_the_tasks_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            self.wave_ready(fx)
            (fx.run_dir / "triage-1.json").write_text(json.dumps(
                {"round": 1, "confirmed": [{"title": "Broken", "severity": "Required"}], "noted": [], "incomplete_lenses": []}))
            devloop.decide(fx.loop)
            devloop.finish_wave(fx.loop)
            original = devloop.load
            devloop.load = lambda spec_dir: fx.loop
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    devloop.cmd_accept_wave(type("A", (), {"spec_dir": "specs"})())
            finally:
                devloop.load = original
            tasks = (fx.wt / "specs" / "tasks.md").read_text()
            delivery = (fx.wt / "specs" / "delivery.md").read_text()
            state = devloop.read_status(fx.loop)["wave_state"]
        self.assertIn("- [x] T001", tasks)
        self.assertIn("ACCEPTED BY A HUMAN", delivery)
        self.assertEqual("approved", state)


class FinalAndSettleTests(unittest.TestCase):
    def state(self, wave_state: str, tasks_md: str, closeout: str | None = None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, tasks_md)
            status = devloop.read_status(fx.loop)
            status.update(wave_state=wave_state, wave=1)
            devloop.write_status(fx.loop, status)
            (fx.run_dir / "decision.json").write_text(json.dumps({"reasons": ["why"]}))
            if closeout:
                (fx.run_dir / "closeout-decision.json").write_text(json.dumps({"decision": closeout, "reasons": ["closeout why"]}))
            return devloop.settle(fx.loop)

    def test_done_with_an_approving_closeout_needs_no_human(self) -> None:
        out = self.state("done", "- [x] T001 A\n", "APPROVE")
        self.assertEqual(("complete", False), (out["state"], out["needs_human"]))

    def test_done_without_a_closeout_decision_is_not_silently_complete(self) -> None:
        out = self.state("done", "- [x] T001 A\n")
        self.assertEqual(("closeout-missing", True), (out["state"], out["needs_human"]))

    def test_an_escalating_closeout_needs_a_human(self) -> None:
        out = self.state("done", "- [x] T001 A\n", "ESCALATE")
        self.assertEqual(("closeout-escalated", True, ["closeout why"]), (out["state"], out["needs_human"], out["reasons"]))

    def test_an_escalated_wave_carries_its_reasons(self) -> None:
        out = self.state("escalated", "- [ ] T001 A\n")
        self.assertEqual(("escalated", True, ["why"]), (out["state"], out["needs_human"], out["reasons"]))

    def test_an_approved_wave_with_tasks_left_means_the_wave_cap_was_hit(self) -> None:
        self.assertEqual("cap", self.state("approved", "- [ ] T001 A\n")["state"])

    def test_blocked_and_unknown_states(self) -> None:
        self.assertEqual("blocked", self.state("blocked", "- [ ] T001 A\n")["state"])
        self.assertEqual("stopped", self.state("running", "- [ ] T001 A\n")["state"])


class ManifestWaveTests(unittest.TestCase):
    def build(self, fx: Fixture, tasks_md: str = TASKS) -> dict:
        loop_with(fx, tasks_md)
        fx.write("specs/brief.md", "do the thing\n")
        status = devloop.read_status(fx.loop)
        devloop.start_wave(fx.loop, status)
        return devloop.manifest_build(fx.loop, devloop.read_status(fx.loop))

    def test_one_ringer_task_per_wave_task_each_told_not_to_touch_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            m = self.build(fx)
        self.assertEqual(["build-T001", "build-T002"], [t["key"] for t in m["tasks"]])
        self.assertEqual(2, m["max_parallel"])
        first = m["tasks"][0]["spec"]
        self.assertIn("you own only these repo paths: out/T001", first)
        self.assertIn("must not touch them: out/T002", first)
        self.assertIn("scope-change.md", first)

    def test_checks_go_through_the_scope_aware_wrapper_and_allow_the_other_tasks_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            m = self.build(fx)
        check = m["tasks"][0]["check"]
        self.assertIn(str(devloop.BUILD_CHECK), check)
        self.assertIn("--allowed-status='specs,out/T002'", check)

    def test_a_parallel_wave_task_without_its_own_verify_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            fx.write("specs/brief.md", "x\n")
            del fx.loop.cfg["tasks"]["T002"]["verify"]
            devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            with self.assertRaises(SystemExit):
                devloop.manifest_build(fx.loop, devloop.read_status(fx.loop))

    def test_the_review_manifest_adds_the_quality_gate_lens_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            fx.loop.cfg["review"]["quality_gate"] = {"model": "m", "effort": "high"}
            devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            m = devloop.manifest_review(fx.loop, devloop.read_status(fx.loop))
        self.assertEqual(["one", "two", "quality-gate"], [t["key"] for t in m["tasks"]])
        gate = m["tasks"][2]
        self.assertIn("Code Review Quality Gate", gate["spec"])
        for axis in ("Correctness", "Readability and simplicity", "Architecture", "Security", "Performance"):
            self.assertIn(axis, gate["spec"])
        self.assertIn("bolted onto an unrelated flow", gate["spec"])
        self.assertIn("pathological or adversarial input", gate["spec"])
        self.assertIn("do not read the loop's run artifacts", gate["spec"])
        self.assertIn("must also appear under Findings", gate["spec"])
        self.assertIn(str(devloop.GATE_CHECK), gate["check"])

    def test_the_review_manifest_has_no_gate_lens_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS)
            devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            m = devloop.manifest_review(fx.loop, devloop.read_status(fx.loop))
        self.assertEqual(["one", "two"], [t["key"] for t in m["tasks"]])

    def test_wave_two_artifacts_are_prefixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            loop_with(fx, TASKS.replace("- [ ] T001", "- [x] T001").replace("- [ ] T002", "- [x] T002"))
            status = devloop.read_status(fx.loop)
            status["wave"] = 1
            devloop.write_status(fx.loop, status)
            fx.write("specs/brief.md", "x\n")
            devloop.start_wave(fx.loop, devloop.read_status(fx.loop))
            m = devloop.manifest_review(fx.loop, devloop.read_status(fx.loop))
        self.assertTrue(m["workdir"].endswith("w2-review-1"), m["workdir"])

    def test_closeout_reviews_the_whole_feature_from_the_original_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.loop.cfg["review"]["quality_gate"] = {"model": "m", "effort": "high"}
            m = devloop.manifest_closeout(fx.loop, devloop.read_status(fx.loop))
        task = m["tasks"][0]
        self.assertEqual("closeout-gate", task["key"])
        self.assertIn(f"diff {fx.base}..HEAD", task["spec"])
        self.assertIn("WHOLE feature", task["spec"])


class CheckScriptTests(unittest.TestCase):
    def run_script(self, name: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=cwd, capture_output=True, text=True)

    def repo(self, tmp: str) -> Path:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        run("init", "-q", "-b", "main", cwd=repo)
        run("config", "user.email", "t@example.com", cwd=repo)
        run("config", "user.name", "t", cwd=repo)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("value = compute()\n")
        run("add", "-A", cwd=repo)
        run("commit", "-q", "-m", "base", cwd=repo)
        return repo

    def test_build_check_passes_a_valid_scope_change_with_an_unchanged_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, task = self.repo(tmp), Path(tmp) / "task"
            task.mkdir()
            (task / "scope-change.md").write_text(ScopeChangeTests.REPORT)
            res = self.run_script("check_build.py", "--repo", str(repo), "--allow-status", "specs", "--", "false", cwd=task)
        self.assertEqual(0, res.returncode, res.stdout)
        self.assertIn("scope-change report", res.stdout)

    def test_build_check_rejects_a_scope_change_when_the_repo_also_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, task = self.repo(tmp), Path(tmp) / "task"
            task.mkdir()
            (task / "scope-change.md").write_text(ScopeChangeTests.REPORT)
            (repo / "src" / "a.py").write_text("changed\n")
            res = self.run_script("check_build.py", "--repo", str(repo), "--allow-status", "specs", "--", "true", cwd=task)
        self.assertEqual(1, res.returncode)
        self.assertIn("src/a.py", res.stdout)

    def test_build_check_rejects_a_scope_change_missing_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, task = self.repo(tmp), Path(tmp) / "task"
            task.mkdir()
            (task / "scope-change.md").write_text("# Scope Change Required\nno other headings\n")
            res = self.run_script("check_build.py", "--repo", str(repo), "--allow-status", "", "--", "true", cwd=task)
        self.assertEqual(1, res.returncode)
        self.assertIn("## Evidence", res.stdout)

    def test_build_check_runs_the_normal_check_without_a_scope_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, task = self.repo(tmp), Path(tmp) / "task"
            task.mkdir()
            ok = self.run_script("check_build.py", "--repo", str(repo), "--allow-status", "", "--", "true", cwd=task)
            bad = self.run_script("check_build.py", "--repo", str(repo), "--allow-status", "", "--", "false", cwd=task)
        self.assertEqual((0, 1), (ok.returncode, bad.returncode))

    def gate(self, report: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.repo(tmp)
            (repo / "report.md").write_text(report)
            return self.run_script("check_gate_report.py", "--repo", str(repo), "--report", str(repo / "report.md"), cwd=repo)

    def test_gate_check_accepts_a_complete_report_with_real_citations(self) -> None:
        res = self.gate(GATE_REPORT)
        self.assertEqual(0, res.returncode, res.stdout)

    def test_gate_check_demands_every_axis_to_be_covered(self) -> None:
        res = self.gate(GATE_REPORT.replace("- Performance: ok\n", ""))
        self.assertEqual(1, res.returncode)
        self.assertIn("performance", res.stdout)

    def test_gate_check_rejects_a_verdict_that_contradicts_the_findings(self) -> None:
        res = self.gate(GATE_REPORT.replace("REQUEST CHANGES", "APPROVE"))
        self.assertEqual(1, res.returncode)
        self.assertIn("APPROVE but a Critical or Required", res.stdout)

    def test_gate_check_rejects_a_fabricated_citation(self) -> None:
        res = self.gate(GATE_REPORT.replace("src/a.py:1 `value = compute()`", "src/a.py:99 `nothing like this exists`"))
        self.assertEqual(1, res.returncode)
        self.assertIn("outside the file", res.stdout)

    def test_gate_check_rejects_an_invalid_verdict_line_and_missing_headings(self) -> None:
        res = self.gate(GATE_REPORT.replace("REQUEST CHANGES", "perhaps").replace("## Verification", "## Checks"))
        self.assertEqual(1, res.returncode)
        self.assertIn("exactly APPROVE or REQUEST CHANGES", res.stdout)
        self.assertIn("missing heading '## Verification'", res.stdout)

    def test_gate_check_requires_evidence_impact_and_fix_on_each_finding(self) -> None:
        res = self.gate(GATE_REPORT.replace("Impact: two code paths can drift\n", ""))
        self.assertEqual(1, res.returncode)
        self.assertIn("missing 'Impact:'", res.stdout)


if __name__ == "__main__":
    unittest.main()
