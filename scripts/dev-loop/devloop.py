#!/usr/bin/env python3
"""Mechanics for the spec-driven delivery loop (see README.md).

Every model-calling step runs through Ringer under an executed check. This tool does the
deterministic parts around them: preflight gates, task and wave selection from tasks.md, an
isolated worktree, Ringer manifests, verification, review triage, commits, decisions and the
durable delivery record. It never merges and never pushes.

  devloop.py preflight SPEC_DIR              checklist, analysis and risk gates (read-only)
  devloop.py refuse    SPEC_DIR              fail with the hard-stop reasons (risk route)
  devloop.py init      SPEC_DIR [--force]    create the worktree, or resume an existing one
  devloop.py next      SPEC_DIR              select the next dependency-ready wave of tasks
  devloop.py manifest  SPEC_DIR KIND         KIND = build | review | fix | closeout
  devloop.py verify    SPEC_DIR TAG
  devloop.py commit    SPEC_DIR MESSAGE
  devloop.py triage    SPEC_DIR [--closeout]
  devloop.py decide    SPEC_DIR [--closeout]
  devloop.py finish-wave SPEC_DIR            mark tasks done after approval, record the wave
  devloop.py wave-status SPEC_DIR
  devloop.py accept-wave SPEC_DIR            human override: accept an escalated wave
  devloop.py final     SPEC_DIR
  devloop.py settle    SPEC_DIR
  devloop.py report    SPEC_DIR
  devloop.py cleanup   SPEC_DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REPO_FEATURE_CHECK = REPO / "templates" / "repo-feature" / "checks" / "check_repo_feature.py"
REVIEW_KIT_CHECK = REPO / "templates" / "review-swarm" / "checks" / "review-swarm.py"
REVIEW_CHECK = HERE / "check_review.py"
GATE_CHECK = HERE / "check_gate_report.py"
BUILD_CHECK = HERE / "check_build.py"
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
CONFIRM_PRIORITIES = ("P0", "P1", "P2")
CONFIRM_CONFIDENCE = ("high", "medium")
GATE_BLOCKING = ("Critical", "Required")
IMPLICIT_TASK = "T001"
SCOPE_HEADINGS = ("# Scope Change Required", "## Blocking Task", "## Evidence", "## Required Decision", "## Suggested Spec Kit Update")


@dataclass(frozen=True)
class Loop:
    spec_dir: str
    cfg: dict[str, Any]
    name: str
    run_dir: Path
    worktree: Path
    branch: str

    @property
    def status_path(self) -> Path:
        return self.run_dir / "status.json"


def load(spec_dir: str) -> Loop:
    spec_dir = spec_dir.strip("/")
    cfg_path = REPO / spec_dir / "loop.json"
    if not cfg_path.is_file():
        raise SystemExit(f"devloop: {cfg_path} not found")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    name = cfg["name"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise SystemExit("devloop: loop name must be lowercase letters, digits and dashes")
    runs = Path(os.environ.get("RINGER_LOOP_DIR", "~/.ringer/loop")).expanduser()
    trees = Path(os.environ.get("RINGER_LOOP_WORKTREES", str(REPO.parent / "ringer-worktrees"))).expanduser()
    return Loop(spec_dir, cfg, name, runs / name, trees / f"loop-{name}", f"loop/{name}")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=cwd or REPO, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"devloop: git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def read_status(loop: Loop) -> dict[str, Any]:
    if not loop.status_path.is_file():
        raise SystemExit(f"devloop: no run for {loop.name}; run `init` first")
    return json.loads(loop.status_path.read_text(encoding="utf-8"))


def write_status(loop: Loop, status: dict[str, Any]) -> None:
    loop.run_dir.mkdir(parents=True, exist_ok=True)
    loop.status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj))


def pfx(status: dict[str, Any]) -> str:
    """Artifact prefix: wave 1 keeps the plain names, later waves are prefixed w2-, w3-, ..."""
    wave = int(status.get("wave", 1))
    return "" if wave <= 1 else f"w{wave}-"


def source_root(loop: Loop) -> Path:
    return loop.worktree if loop.worktree.exists() else REPO


# ---------------------------------------------------------------- tasks and waves

@dataclass(frozen=True)
class Task:
    id: str
    title: str
    done: bool
    parallel: bool
    phase: int
    deps: tuple[str, ...]
    order: int


TASK_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<id>T\d+)\b(?P<rest>.*)$")
PHASE_RE = re.compile(r"^#{2,4}\s+Phase\s+(\d+)", re.I)
DEPS_RE = re.compile(r"(?:\bdeps?\b|\bdepends on\b|\bprerequisites?\b)\s*:?\s*((?:T\d+(?:\s*(?:,|and|&)\s*)?)+)", re.I)


def parse_tasks(text: str) -> list[Task]:
    """Parse a Spec Kit tasks.md: checkboxes with T-ids, [P] parallel markers, phases and explicit deps."""
    tasks, phase = [], 0
    for line in text.splitlines():
        m = PHASE_RE.match(line)
        if m:
            phase = int(m.group(1))
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        deps = tuple(dict.fromkeys(re.findall(r"T\d+", " ".join(d.group(1) for d in DEPS_RE.finditer(rest)))))
        title = re.sub(r"\[(?:P|US\d+)\]", "", rest)
        title = re.sub(r"\((?:deps?|depends on|prerequisites?)[^)]*\)", "", title, flags=re.I).strip(" :.-\t")
        tasks.append(Task(m.group("id"), title, m.group("mark").lower() == "x", "[P]" in rest, phase, deps, len(tasks)))
    return tasks


def paths_overlap(a: list[str], b: list[str]) -> bool:
    for x in a:
        for y in b:
            x1, y1 = x.rstrip("/"), y.rstrip("/")
            if x1 == y1 or x1.startswith(y1 + "/") or y1.startswith(x1 + "/"):
                return True
    return False


def task_cfg(loop: Loop, tid: str) -> dict[str, Any]:
    base = loop.cfg["build"]
    merged = {**base, **loop.cfg.get("tasks", {}).get(tid, {})}
    for key in ("brief", "owned"):
        if key not in merged:
            raise SystemExit(f"devloop: task {tid} has no {key}; add loop.json tasks.{tid}.{key} or build.{key}")
    return merged


def ready_wave(loop: Loop, tasks: list[Task]) -> tuple[list[Task], str, list[str]]:
    """The next wave: (tasks, state, reasons). State is run | done | blocked."""
    unchecked = [t for t in tasks if not t.done]
    if not unchecked:
        return [], "done", []
    done_ids = {t.id for t in tasks if t.done}
    ready = []
    for t in unchecked:
        if any(not o.done for o in tasks if o.phase < t.phase):
            continue
        if any(d not in done_ids for d in t.deps):
            continue
        if not t.parallel and any(not o.done for o in tasks if o.phase == t.phase and o.order < t.order and not o.parallel):
            continue
        ready.append(t)
    if not ready:
        return [], "blocked", [f"no unchecked task is ready; waiting on: {', '.join(t.id for t in unchecked[:6])}"]
    if len(tasks) > 1:
        missing = [t.id for t in ready if t.id not in loop.cfg.get("tasks", {})]
        if missing:
            return [], "blocked", [f"task(s) {', '.join(missing)} have no entry in loop.json tasks"]
    if not ready[0].parallel:
        return [ready[0]], "run", []
    chosen, taken = [], []
    limit = int(loop.cfg.get("max_wave_size", 3))
    for t in ready:
        if not t.parallel or len(chosen) >= limit:
            continue
        owned = task_cfg(loop, t.id)["owned"]
        if paths_overlap(owned, taken):
            continue
        chosen.append(t)
        taken += owned
    return chosen, "run", []


def tasks_for(loop: Loop) -> list[Task]:
    path = source_root(loop) / loop.spec_dir / "tasks.md"
    return parse_tasks(path.read_text(encoding="utf-8")) if path.is_file() else []


def mark_done(text: str, ids: list[str]) -> str:
    for tid in ids:
        text = re.sub(rf"^(\s*[-*]\s+)\[ \](\s+{re.escape(tid)}\b)", r"\1[x]\2", text, flags=re.M)
    return text


def owned_union(loop: Loop, status: dict[str, Any]) -> list[str]:
    ids = status.get("wave_tasks") or [IMPLICIT_TASK]
    out: list[str] = []
    for tid in ids:
        out += [p for p in task_cfg(loop, tid)["owned"] if p not in out]
    return out


# ---------------------------------------------------------------- preflight

def scan_checklists(root: Path) -> list[dict[str, Any]]:
    out = []
    d = root / "checklists"
    for f in sorted(d.glob("*.md")) if d.is_dir() else []:
        text = f.read_text(encoding="utf-8", errors="replace")
        checked = len(re.findall(r"^\s*[-*]\s+\[[xX]\]", text, re.M))
        unchecked = len(re.findall(r"^\s*[-*]\s+\[ \]", text, re.M))
        out.append({"file": f.name, "total": checked + unchecked, "checked": checked, "unchecked": unchecked})
    return out


def preflight(loop: Loop) -> dict[str, Any]:
    cfg, reasons, hard = loop.cfg, [], []
    root = source_root(loop) / loop.spec_dir
    risk = cfg.get("risk", "routine")
    if risk != "routine":
        hard.append(f"risk is {risk}: this work is lead-controlled and workers may only run read-only; the loop will not start mutable workers")
    mode = cfg.get("checklists", "required")
    lists = scan_checklists(root)
    if mode != "off":
        if not lists:
            if mode == "required":
                reasons.append("no checklists found in checklists/ (requirements.md is expected from specify/clarify)")
        else:
            reasons += [f"checklist {c['file']}: {c['unchecked']} of {c['total']} items unchecked" for c in lists if c["unchecked"]]
            if mode == "required" and all(c["total"] == 0 for c in lists):
                reasons.append("checklists contain no items")
    amode = cfg.get("analysis", "if-present")
    analysis = root / "analysis.md"
    if amode != "off":
        if analysis.is_file():
            m = re.search(r"Critical Issues Count\D*(\d+)", analysis.read_text(encoding="utf-8", errors="replace"), re.I)
            if m and int(m.group(1)) > 0:
                reasons.append(f"analysis.md reports {m.group(1)} critical issue(s)")
            elif not m and amode == "required":
                reasons.append("analysis.md has no readable 'Critical Issues Count' (run analyze again)")
        elif amode == "required":
            reasons.append("analysis.md is missing (run analyze first)")
    needs_route = bool(cfg.get("route_gate", False))
    return {"ok": not reasons and not hard and not needs_route, "hard_stop": bool(hard), "needs_gate": bool(reasons) or needs_route,
            "route_gate": needs_route, "reasons": hard + reasons, "checklists": lists, "risk": risk}


def cmd_preflight(args: argparse.Namespace) -> int:
    emit(preflight(load(args.spec_dir)))
    return 0


def cmd_refuse(args: argparse.Namespace) -> int:
    """Used by the workflow for a hard stop: say why on stderr and fail the run."""
    out = preflight(load(args.spec_dir))
    print("dev-loop refuses to start: " + "; ".join(out["reasons"]), file=sys.stderr)
    return 1


# ---------------------------------------------------------------- init / cleanup

def cmd_init(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    if loop.status_path.exists() and loop.worktree.exists() and not args.force:
        status = read_status(loop)
        dirty = git("status", "--porcelain", "--untracked-files=no", cwd=loop.worktree).stdout.strip()
        emit({"name": loop.name, "resumed": True, "worktree": str(loop.worktree), "branch": loop.branch,
              "base_sha": status["base_sha"], "run_dir": str(loop.run_dir), "dirty": bool(dirty)})
        return 0
    if loop.status_path.exists() or loop.worktree.exists():
        if not args.force:
            raise SystemExit(f"devloop: {loop.name} is in an inconsistent state (worktree {loop.worktree}); use --force or cleanup")
        cleanup(loop)
    base_sha = git("rev-parse", loop.cfg.get("base", "HEAD")).stdout.strip()
    loop.worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", loop.branch, str(loop.worktree), base_sha)
    copied = False
    if not (loop.worktree / loop.spec_dir / "loop.json").is_file():
        # The spec is not committed at the base. Copy it in uncommitted (handy while drafting and for the self-test);
        # a real run should commit its spec first so the branch carries it.
        shutil.copytree(REPO / loop.spec_dir, loop.worktree / loop.spec_dir, dirs_exist_ok=True)
        copied = True
    write_status(loop, {"name": loop.name, "base_sha": base_sha, "wave": 0, "round": 0, "worktree": str(loop.worktree),
                        "branch": loop.branch})
    emit({"name": loop.name, "resumed": False, "worktree": str(loop.worktree), "branch": loop.branch, "base_sha": base_sha,
          "run_dir": str(loop.run_dir), "spec_dir_copied_uncommitted": copied})
    return 0


def cleanup(loop: Loop) -> None:
    if loop.worktree.exists():
        git("worktree", "remove", "--force", str(loop.worktree), check=False)
    git("worktree", "prune", check=False)
    git("branch", "-D", loop.branch, check=False)
    if loop.run_dir.exists():
        shutil.rmtree(loop.run_dir)


def cmd_cleanup(args: argparse.Namespace) -> int:
    cleanup(load(args.spec_dir))
    print("cleaned")
    return 0


# ---------------------------------------------------------------- next wave

def start_wave(loop: Loop, status: dict[str, Any]) -> dict[str, Any]:
    tasks = tasks_for(loop)
    if not tasks:
        if status.get("implicit_done"):
            chosen, state, reasons = [], "done", []
        else:
            chosen, state, reasons = [Task(IMPLICIT_TASK, loop.name, False, False, 0, (), 0)], "run", []
    else:
        chosen, state, reasons = ready_wave(loop, tasks)
    if state != "run":
        status["wave_state"] = state
        write_status(loop, status)
        return {"run": False, "done": state == "done", "blocked": state == "blocked", "wave": status.get("wave", 0),
                "tasks": [], "reasons": reasons}
    head = git("rev-parse", "HEAD", cwd=loop.worktree).stdout.strip()
    status.update(wave=int(status.get("wave", 0)) + 1, wave_base_sha=head, round=0, wave_tasks=[t.id for t in chosen],
                  wave_titles={t.id: t.title for t in chosen}, wave_state="running")
    for key in ("last_verify", "last_triage", "run_exit"):
        status.pop(key, None)
    write_status(loop, status)
    return {"run": True, "done": False, "blocked": False, "wave": status["wave"], "tasks": status["wave_tasks"], "reasons": []}


def cmd_next(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    emit(start_wave(loop, read_status(loop)))
    return 0


# ---------------------------------------------------------------- manifests

def verify_cmds(loop: Loop, status: dict[str, Any], closeout: bool = False) -> list[str]:
    """Verify commands with {repo} (this checkout) and {base} (the wave's, or for closeout the feature's, start commit)."""
    return expand_cmds(loop.cfg["verify"], status, closeout)


def expand_cmds(cmds: list[str], status: dict[str, Any], closeout: bool = False) -> list[str]:
    base = status["base_sha"] if closeout else status.get("wave_base_sha", status["base_sha"])
    return [c.replace("{repo}", str(REPO)).replace("{base}", base).replace("{feature_base}", status["base_sha"]) for c in cmds]


def verify_chain(loop: Loop, status: dict[str, Any]) -> str:
    return " && ".join(verify_cmds(loop, status))


def ringer_task(loop: Loop, key: str, model: str, effort: str, timeout_s: int, spec: str, check: str,
                expect: list[str], verified: str, task_type: str, writable: bool) -> dict[str, Any]:
    engine = loop.cfg.get("engine", "codex")
    task: dict[str, Any] = {"key": key, "engine": engine, "task_type": task_type, "timeout_s": timeout_s,
                            "max_attempts": 2, "expect_files": expect, "spec": spec, "check": check, "verified": verified}
    if engine == "codex":
        task["model"] = model
        args = ["-c", f"model_reasoning_effort={effort}"]
        if writable:
            args += ["-c", f'sandbox_workspace_write.writable_roots=["{loop.worktree}"]']
        task["engine_args"] = args
    return task


SCOPE_INSTRUCTION = (
    "If the task cannot be completed inside the frozen spec, plan and your owned paths, do NOT force a solution: leave every tracked "
    "file unchanged and write ./scope-change.md with exactly these headings: '# Scope Change Required', '## Blocking Task', "
    "'## Evidence', '## Required Decision', '## Suggested Spec Kit Update'. An automated check accepts that report only when the "
    "repository is otherwise unchanged, and the loop then stops and returns the decision to the lead."
)


def preamble(loop: Loop, owned: list[str], others: list[str] | None = None) -> str:
    extra = f" Other workers are editing these paths at the same time and you must not touch them: {', '.join(others)}." if others else ""
    return (
        f"You are a Python implementation worker on the Ringer repository. The checkout you may edit is {loop.worktree} "
        f"(a git worktree on branch {loop.branch}). Your current working directory is a scratch task directory; use it only for "
        f"./notes.md. Run repository commands with `cd {loop.worktree} && ...`.\n\n"
        f"OWNERSHIP BOUNDARY: you own only these repo paths: {', '.join(owned)}. Do not modify, create, delete, format or stage "
        f"anything else.{extra} Do not run git add, git commit or git push. Do not install packages or add dependencies. "
        f"Match the surrounding code style; make the simplest change that meets the brief; no extras.\n\n"
    )


def contract_tail(loop: Loop, cmds: list[str]) -> str:
    return (
        "\n\nHOW TO VERIFY (from the repo root; fix failures inside your owned files only):\n"
        + "".join(f"  cd {loop.worktree} && {cmd}\n" for cmd in cmds)
        + "These commands are the acceptance check; read their failure lines carefully, they say exactly what differs.\n\n"
        "OUTPUT CONTRACT: the owned files changed as described, plus ./notes.md in the scratch directory listing what you read, "
        "what you changed, the commands that passed, and any assumption. HARD RULES: never edit outside your owned files; "
        "if a requirement is ambiguous choose the reading that makes the acceptance check pass and record it in notes.md."
    )


def build_check(loop: Loop, owned: list[str], allowed: list[str], required_text: list[str], chain: str) -> str:
    # Attached --flag=value form throughout: argparse reads a separate value that starts with "--" (such as a
    # required text of "--json") as another option and the check itself fails.
    kit = (f"python3 '{REPO_FEATURE_CHECK}' --repo='{loop.worktree}' --owned='{','.join(owned)}' "
           f"--allowed-status='{','.join(allowed)}' --required-paths='' --required-text='{','.join(required_text)}' "
           f"--build-command=\"cd {loop.worktree} && {chain}\" --notes=notes.md")
    return f"python3 '{BUILD_CHECK}' --repo='{loop.worktree}' --allow-status='{','.join(allowed)}' -- {kit}"


def manifest_build(loop: Loop, status: dict[str, Any]) -> dict[str, Any]:
    wave = status.get("wave_tasks") or [IMPLICIT_TASK]
    base = status.get("wave_base_sha", status["base_sha"])
    tasks = []
    for tid in wave:
        cfg = task_cfg(loop, tid)
        others = [p for o in wave if o != tid for p in task_cfg(loop, o)["owned"]]
        brief = (loop.worktree / loop.spec_dir / cfg["brief"]).read_text(encoding="utf-8")
        if cfg.get("verify"):
            cmds = expand_cmds(cfg["verify"], status)
        elif len(wave) == 1:
            cmds = verify_cmds(loop, status)
        else:
            raise SystemExit(f"devloop: task {tid} runs in a parallel wave and needs its own verify list in loop.json tasks.{tid}.verify")
        title = status.get("wave_titles", {}).get(tid, "")
        spec = (preamble(loop, cfg["owned"], others)
                + f"READ FIRST (read-only): {loop.spec_dir}/spec.md, plan.md and tasks.md. Do not change tasks.md.\n\n"
                f"SELECTED TASK: {tid} {title}\n\nBRIEF:\n{brief}\n\n{SCOPE_INSTRUCTION}" + contract_tail(loop, cmds))
        check = build_check(loop, cfg["owned"], [loop.spec_dir] + others, cfg.get("required_text", []), " && ".join(cmds))
        tasks.append(ringer_task(loop, f"build-{tid}", cfg["model"], cfg["effort"], cfg.get("timeout_s", 3000), spec, check,
                                 ["notes.md"], "the task's verification passed and git status shows only owned paths "
                                 "(or a valid scope-change report with no repository change)", "code-feature", True))
    return {"run_name": f"loop-{loop.name}-{pfx(status)}build", "workdir": str(loop.run_dir / f"{pfx(status)}work-build"),
            "max_parallel": len(tasks), "tasks": tasks}


REVIEW_CONTRACT = (
    "\n\nOUTPUT CONTRACT for ./report.md: start with '# Review Report'. Include '## Summary' with no more than 3 bullets, '## Findings', "
    "'## Clean', and '## Assumptions'. Each finding must use exactly this shape: '### Finding: <neutral summary>', then lines starting "
    "'Evidence:', 'Impact:', 'Fix:', 'Priority: P0|P1|P2|P3' and 'Confidence: high|medium|low'. Evidence must cite repo-relative "
    "`file:line` and quote at most a short phrase in backticks that appears verbatim in the cited file; an automated check verifies "
    "that every cited file and line exists and that every backticked quotation of 20+ characters appears in the cited file, and fails "
    "the report otherwise. Rate priority by realistic impact: a defect that needs pathological or adversarial input is P3, not P2. "
    "Prefer a few confirmed real findings over many speculative ones; for each suspected defect construct a concrete input and run it "
    "with python3 -c when it is cheap and read-only. If you find nothing, write 'No findings for this surface.' under Findings and "
    "still fill Clean. Hard rules: no invented facts, mark assumptions, under 1200 words, and never claim a test fails unless you ran it."
)

GATE_RUBRIC = (
    "Apply the five-axis review from the code-review-and-quality process. Review the tests first (do they test behavior, cover edge "
    "cases, and would they catch a regression?), then the implementation, with these axes in mind. (1) Correctness: matches the spec and "
    "task, edge and error paths, tests that actually test the right thing. (2) Readability and simplicity: could it be done in fewer lines; "
    "is a new conditional bolted onto an unrelated flow (a design smell, not a nit); do repeated conditionals or duplicate branches signal "
    "a missing helper; dead code. (3) Architecture: fits existing patterns, clean module boundaries, no feature-specific logic leaking "
    "into a shared module, reuse of the canonical helper instead of a near-duplicate, does the change follow the plan's structure. "
    "(4) Security: input validation, secrets out of code and logs, injection, data from external sources treated as untrusted. "
    "(5) Performance: unbounded loops or fetching, hot-path allocations, N+1 patterns. Also judge dependency discipline, change size, "
    "and verify the verification: which tests ran, did the build pass, is there evidence of a real run. Label every finding Critical "
    "(blocks merge: vulnerability, data loss, broken functionality), Required (must fix before merge), Optional, Nit or FYI. For every "
    "Critical or Required finding cite concrete evidence, explain impact, and propose a bounded structural remedy (for example collapse "
    "duplicate branches into one flow, extract a helper, reuse the canonical helper). Do not turn unrelated pre-existing issues or "
    "personal preferences into blockers. Rate severity by realistic impact: Required means a defect that a real user or realistic "
    "input can hit, or a departure from a stated requirement that matters in normal use. A failure that needs pathological or "
    "adversarial input (absurdly deep nesting, enormous numbers) is Optional even when it technically departs from the letter of "
    "the spec; say so in the finding. Judge the change from the repository alone: do not read the loop's run artifacts (anything "
    "under ~/.ringer, builder notes, earlier review reports or verification records); if you need evidence that tests pass, run "
    "them yourself. APPROVE when the change improves overall code health and no Critical or Required finding remains."
)

GATE_CONTRACT = (
    "\n\nOUTPUT CONTRACT for ./report.md, with exactly these headings in order: '# Code Review Quality Gate', '## Scope', '## Findings', "
    "'## Five-axis summary', '## Verification', '## Verdict'. Under Findings write each finding as '### [Required] <neutral summary>' "
    "(or [Critical], [Optional], [Nit], [FYI]) followed by lines starting 'Evidence:', 'Impact:' and 'Fix:'. Evidence must cite "
    "repo-relative `file:line` and quote at most a short phrase in backticks that appears verbatim in the cited file; an automated "
    "check verifies every citation and quotation and fails the report otherwise. If there are no findings write 'No findings.' under "
    "Findings. Five-axis summary: one line for each of correctness, readability, architecture, security and performance stating what "
    "you checked and your judgement. Every concern you name in the summary (for example a plan structure that was not followed, "
    "duplicated branches, a missing helper) must also appear under Findings as a labelled finding, using Optional or Nit when it "
    "does not block; a concern that exists only in the summary is lost. Verdict: the first non-empty line under '## Verdict' must be exactly APPROVE or REQUEST CHANGES. "
    "Hard rules: no invented facts, mark assumptions, never claim a test fails unless you ran it, never modify the repository."
)


def reviewer_spec(loop: Loop, surface: str, diff_base: str, scope: str) -> str:
    return (
        f"You are a read-only reviewer. Boundary: the repository at '{loop.worktree}' is source material only. Never modify, create, "
        f"delete, format, install or commit anything there. You own exactly one output file in your task directory: ./report.md. You may "
        f"read files and run read-only commands (rg, sed, cat, git diff/log/show, python3 -c for pure computation, a specific test "
        f"module). Do not start network connections or databases.\n\n"
        f"CONTEXT: the feature specification is {loop.spec_dir}/spec.md, plan.md and tasks.md in the repo. {scope} The change under "
        f"review is `git -C {loop.worktree} diff {diff_base}..HEAD` (new and changed files only; the rest is unchanged context). "
        f"Known and accepted failures: {', '.join(loop.cfg.get('known_failures', [])) or 'none'}; do not report them.\n\n"
        f"SURFACE AND DIMENSIONS: {surface}\n\nHOW TO RUN: from your task directory use absolute paths under {loop.worktree}."
    )


def gate_task(loop: Loop, key: str, diff_base: str, scope: str, gate_cfg: dict[str, Any]) -> dict[str, Any]:
    spec = reviewer_spec(loop, GATE_RUBRIC, diff_base, scope) + GATE_CONTRACT
    check = report_check(loop, key, True)
    return ringer_task(loop, key, gate_cfg["model"], gate_cfg["effort"], gate_cfg.get("timeout_s", 1800), spec, check, ["report.md"],
                       "the report follows the quality-gate contract, covers all five axes, and every citation and quotation exists",
                       "code-review", False)


def manifest_review(loop: Loop, status: dict[str, Any]) -> dict[str, Any]:
    status["round"] = int(status.get("round", 0)) + 1
    write_status(loop, status)
    rnd, base = status["round"], status.get("wave_base_sha", status["base_sha"])
    scope = f"This wave implements task(s) {', '.join(status.get('wave_tasks', []))}."
    tasks = []
    for lens in loop.cfg["review"]["lenses"]:
        spec = reviewer_spec(loop, lens["surface"], base, scope) + REVIEW_CONTRACT
        check = report_check(loop, lens["key"], False)
        tasks.append(ringer_task(loop, lens["key"], lens["model"], lens["effort"], lens.get("timeout_s", 1800), spec, check,
                                 ["report.md"], "report.md follows the review contract and every citation and long quotation exists",
                                 "code-review", False))
    if loop.cfg["review"].get("quality_gate"):
        tasks.append(gate_task(loop, "quality-gate", base, scope, loop.cfg["review"]["quality_gate"]))
    return {"run_name": f"loop-{loop.name}-{pfx(status)}review-{rnd}", "workdir": str(loop.run_dir / f"{pfx(status)}review-{rnd}"),
            "max_parallel": len(tasks), "tasks": tasks}


def manifest_fix(loop: Loop, status: dict[str, Any]) -> dict[str, Any]:
    f = loop.cfg["fix"]
    owned = owned_union(loop, status)
    triage = json.loads((loop.run_dir / f"{pfx(status)}triage-{status['round']}.json").read_text(encoding="utf-8"))
    findings = "\n\n".join(
        f"[{x['id']}] ({x.get('severity') or x['priority'] + ', ' + x['confidence'] + ' confidence'}, lens {x['lens']}) {x['title']}\n"
        f"Evidence: {x['evidence']}\nImpact: {x['impact']}\nSuggested fix: {x['fix']}" for x in triage["confirmed"])
    base = status.get("wave_base_sha", status["base_sha"])
    spec = (preamble(loop, owned)
            + f"READ FIRST (read-only): {loop.spec_dir}/spec.md, plan.md and the current diff `git -C {loop.worktree} diff {base}..HEAD`.\n\n"
            f"TASK: independent reviewers reported the findings below against the current change. For EACH finding: first reproduce it "
            f"(a failing test or a small read-only command); if you cannot reproduce it, do NOT change code for it, and say so in notes.md "
            f"with what you tried. When it reproduces, fix it with the smallest change and add a regression test that would have failed "
            f"before the fix. Do not make unrelated changes.\n\n{SCOPE_INSTRUCTION}\n\nFINDINGS:\n{findings}"
            + contract_tail(loop, verify_cmds(loop, status)))
    check = build_check(loop, owned, [loop.spec_dir], [], verify_chain(loop, status))
    task = ringer_task(loop, f"fix-{status['round']}", f["model"], f["effort"], f.get("timeout_s", 3000), spec, check, ["notes.md"],
                       "the verification passed and git status shows only owned paths", "code-fix", True)
    return {"run_name": f"loop-{loop.name}-{pfx(status)}fix-{status['round']}",
            "workdir": str(loop.run_dir / f"{pfx(status)}work-fix-{status['round']}"), "max_parallel": 1, "tasks": [task]}


def manifest_closeout(loop: Loop, status: dict[str, Any]) -> dict[str, Any]:
    gate = loop.cfg.get("closeout") or loop.cfg["review"].get("quality_gate")
    if not gate:
        raise SystemExit("devloop: closeout needs review.quality_gate (or closeout) in loop.json")
    task = gate_task(loop, "closeout-gate", status["base_sha"],
                     "This is the final review of the WHOLE feature, all waves together; also check that the implementation as a whole "
                     "matches spec.md and that docs and tests agree with the code.", gate)
    return {"run_name": f"loop-{loop.name}-closeout", "workdir": str(loop.run_dir / "closeout"), "max_parallel": 1, "tasks": [task]}


def cmd_manifest(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    status = read_status(loop)
    builder = {"build": manifest_build, "review": manifest_review, "fix": manifest_fix, "closeout": manifest_closeout}[args.kind]
    manifest = builder(loop, status)
    name = f"{pfx(status)}manifest-{args.kind}-{status.get('round', 0)}.json" if args.kind != "closeout" else "manifest-closeout.json"
    path = loop.run_dir / name
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(path)
    return 0


# ---------------------------------------------------------------- verify / commit

def cmd_record_run(args: argparse.Namespace) -> int:
    """Called by ringer_run.sh with the Ringer exit status, which the workflow's continue_on_error would otherwise lose."""
    loop = load(args.spec_dir)
    status = read_status(loop)
    status.setdefault("run_exit", {})[args.kind] = args.exit_code
    write_status(loop, status)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    status = read_status(loop)
    closeout = args.tag == "closeout"
    results, passed = [], True
    for cmd in verify_cmds(loop, status, closeout=closeout):
        proc = subprocess.run(cmd, shell=True, cwd=loop.worktree, capture_output=True, text=True,
                              timeout=loop.cfg.get("verify_timeout_s", 1800))
        results.append({"cmd": cmd, "exit": proc.returncode, "tail": (proc.stdout + proc.stderr)[-1500:]})
        if proc.returncode != 0:
            passed = False
            break
    name = "closeout-verify" if closeout else f"{pfx(status)}{args.tag}"
    (loop.run_dir / f"{name}.json").write_text(json.dumps({"tag": args.tag, "passed": passed, "results": results}, indent=2), encoding="utf-8")
    if not closeout:
        status["last_verify"] = args.tag
        write_status(loop, status)
    emit({"tag": args.tag, "passed": passed})
    return 0 if passed else 1


def commit_paths(loop: Loop, paths: list[str], message: str) -> dict[str, Any]:
    for path in paths:
        # One path at a time: `git add` refuses the whole command if any single path does not exist yet.
        git("add", "-A", "--", path, cwd=loop.worktree, check=False)
    staged = git("diff", "--cached", "--name-only", cwd=loop.worktree).stdout.split()
    if not staged:
        return {"committed": False, "files": []}
    git("commit", "-q", "-m", message, "-m", TRAILER, cwd=loop.worktree)
    return {"committed": True, "sha": git("rev-parse", "--short", "HEAD", cwd=loop.worktree).stdout.strip(), "files": staged}


def cmd_commit(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    emit(commit_paths(loop, owned_union(loop, read_status(loop)), args.message))
    return 0


# ---------------------------------------------------------------- triage

FIELD = re.compile(r"^\s*(Evidence|Impact|Fix|Priority|Confidence):\s*(.*)$", re.I)
GATE_HEADING = re.compile(r"^###\s+\[?(Critical|Required|Optional|Consider|Nit|FYI)\]?:?\s*(.*)$", re.I | re.M)


def parse_report(text: str, lens: str) -> list[dict[str, Any]]:
    findings = []
    for block in re.split(r"^###\s+Finding:", text, flags=re.M)[1:]:
        lines = block.splitlines()
        item: dict[str, Any] = {"lens": lens, "title": lines[0].strip(), "evidence": "", "impact": "", "fix": "",
                                "priority": "", "confidence": "", "severity": ""}
        current = None
        for line in lines[1:]:
            m = FIELD.match(line)
            if m:
                current = m.group(1).lower()
                item[current] = m.group(2).strip()
            elif current in ("evidence", "impact", "fix") and line.strip():
                item[current] += " " + line.strip()
        item["priority"] = (re.search(r"P[0-3]", item["priority"]) or [""])[0]
        item["confidence"] = item["confidence"].split()[0].lower() if item["confidence"] else ""
        findings.append(item)
    return findings


def parse_gate_report(text: str, lens: str) -> tuple[list[dict[str, Any]], str | None]:
    """Findings and the verdict from a quality-gate report."""
    findings = []
    parts = GATE_HEADING.split(text)
    for i in range(1, len(parts) - 2, 3):
        severity, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]
        severity = "FYI" if severity.lower() == "fyi" else severity.capitalize()
        body = re.split(r"^##\s", body, flags=re.M)[0]
        item: dict[str, Any] = {"lens": lens, "title": title, "evidence": "", "impact": "", "fix": "", "priority": "",
                                "confidence": "", "severity": severity}
        current = None
        for line in body.splitlines():
            m = FIELD.match(line)
            if m and m.group(1).lower() in ("evidence", "impact", "fix"):
                current = m.group(1).lower()
                item[current] = m.group(2).strip()
            elif current and line.strip():
                item[current] += " " + line.strip()
        findings.append(item)
    verdict = None
    m = re.search(r"^##\s+Verdict\s*$([\s\S]*?)(?=^##\s|\Z)", text, re.M)
    if m:
        first = next((l.strip() for l in m.group(1).splitlines() if l.strip()), "")
        verdict = first if first in ("APPROVE", "REQUEST CHANGES") else None
    return findings, verdict


def report_check(loop: Loop, key: str, gate: bool) -> str:
    if gate:
        return f"python3 '{GATE_CHECK}' --repo '{loop.worktree}' --report report.md"
    return f"python3 '{REVIEW_CHECK}' --repo '{loop.worktree}' --report report.md --surface '{key}' --kit-check '{REVIEW_KIT_CHECK}'"


def report_passes(loop: Loop, key: str, gate: bool, cwd: Path) -> bool:
    return subprocess.run(report_check(loop, key, gate), shell=True, cwd=cwd, capture_output=True).returncode == 0


def triage_reports(loop: Loop, report_root: Path, keys: list[str], gate_keys: tuple[str, ...], rnd: int) -> dict[str, Any]:
    confirmed, noted, missing, verdicts = [], [], [], {}
    for key in keys:
        report = report_root / key / "report.md"
        # A report that exists is not a report that passed: Ringer's check can still be failing after the retry.
        if not report.is_file() or not report_passes(loop, key, key in gate_keys, report.parent):
            missing.append(key)
            continue
        text = report.read_text(encoding="utf-8", errors="replace")
        if key in gate_keys:
            found, verdict = parse_gate_report(text, key)
            verdicts[key] = verdict
        else:
            found = parse_report(text, key)
        for finding in found:
            finding["id"] = f"R{rnd}-{len(confirmed) + len(noted) + 1}"
            if key in gate_keys:
                ok = finding["severity"] in GATE_BLOCKING
            else:
                ok = finding["priority"] in CONFIRM_PRIORITIES and finding["confidence"] in CONFIRM_CONFIDENCE
            (confirmed if ok else noted).append(finding)
    return {"round": rnd, "confirmed": confirmed, "noted": noted, "incomplete_lenses": missing, "gate_verdicts": verdicts}


def cmd_triage(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    status = read_status(loop)
    if args.closeout:
        out = triage_reports(loop, loop.run_dir / "closeout", ["closeout-gate"], ("closeout-gate",), 0)
        (loop.run_dir / "closeout-triage.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    else:
        rnd = int(status.get("round", 0))
        keys = [lens["key"] for lens in loop.cfg["review"]["lenses"]]
        gate = ("quality-gate",) if loop.cfg["review"].get("quality_gate") else ()
        out = triage_reports(loop, loop.run_dir / f"{pfx(status)}review-{rnd}", keys + list(gate), gate, rnd)
        (loop.run_dir / f"{pfx(status)}triage-{rnd}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        status["last_triage"] = rnd
        write_status(loop, status)
    emit({"round": out["round"], "need_fix": bool(out["confirmed"]), "confirmed": len(out["confirmed"]), "noted": len(out["noted"]),
          "incomplete": out["incomplete_lenses"], "gate_verdicts": out["gate_verdicts"]})
    return 0


# ---------------------------------------------------------------- decide

def changed_files(loop: Loop, base_sha: str) -> list[str]:
    return [p for p in git("diff", "--name-only", f"{base_sha}..HEAD", cwd=loop.worktree).stdout.split() if p]


def dirty_paths(loop: Loop) -> list[str]:
    """Anything uncommitted in the worktree: commit_paths stages owned paths only, so a stray change would be verified but never merged."""
    out = git("status", "--porcelain", "--untracked-files=all", cwd=loop.worktree).stdout.splitlines()
    return [line[3:].split(" -> ")[-1].strip('"') for line in out if line.strip()]


def scope_changes(loop: Loop, status: dict[str, Any]) -> list[dict[str, str]]:
    found = []
    for d in sorted(loop.run_dir.glob(f"{pfx(status)}work-*")):
        for f in sorted(d.glob("*/scope-change.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            if all(h in text for h in SCOPE_HEADINGS):
                block = re.search(r"## Required Decision\s*\n+([^\n]+)", text)
                found.append({"task": f.parent.name, "decision": (block.group(1).strip() if block else "")[:160], "path": str(f)})
    return found


def gate_reasons(triage: dict[str, Any], label: str) -> list[str]:
    reasons = []
    if triage["incomplete_lenses"]:
        reasons.append(f"{label}: lenses did not report: " + ", ".join(triage["incomplete_lenses"]))
    if triage["confirmed"]:
        reasons.append(f"{len(triage['confirmed'])} confirmed finding(s) remain after {label}: "
                       + "; ".join(f"{x.get('severity') or x.get('priority', '')} {x['title'][:60]}" for x in triage["confirmed"][:4]))
    for key, verdict in triage.get("gate_verdicts", {}).items():
        if verdict is None and key not in triage["incomplete_lenses"]:
            reasons.append(f"{key} did not declare a valid verdict")
        elif verdict == "REQUEST CHANGES" and not triage["confirmed"]:
            reasons.append(f"{key} requested changes without a Critical or Required finding")
    return reasons


def decide(loop: Loop, closeout: bool = False) -> dict[str, Any]:
    status = read_status(loop)
    reasons: list[str] = []
    changes: list[dict[str, str]] = []
    if closeout:
        vpath, tpath = loop.run_dir / "closeout-verify.json", loop.run_dir / "closeout-triage.json"
        files, label = changed_files(loop, status["base_sha"]), "the closeout review"
    else:
        files = changed_files(loop, status.get("wave_base_sha", status["base_sha"]))
        tag = status.get("last_verify")
        vpath = loop.run_dir / f"{pfx(status)}{tag}.json" if tag else None
        tpath = loop.run_dir / f"{pfx(status)}triage-{status.get('last_triage', 0)}.json"
        label = f"round {status.get('last_triage')}"
        changes = scope_changes(loop, status)
        for c in changes:
            reasons.append(f"scope change requested by {c['task']}: {c['decision'] or 'see the report'}")
        if not files and not changes:
            reasons.append("the loop produced no change")
        if status.get("run_exit", {}).get("build") != 0:
            reasons.append(f"the build run did not succeed (Ringer exit {status.get('run_exit', {}).get('build', 'not recorded')}): "
                           "a task's executed check failed")
        owned = owned_union(loop, status)
        outside = [p for p in files if not any(p == o or p.startswith(o.rstrip("/") + "/") for o in owned)]
        if outside:
            reasons.append("changes outside the owned paths: " + ", ".join(outside[:5]))
        protected = [p for p in files if any(p.startswith(x) for x in loop.cfg.get("escalate_paths", []))]
        if protected:
            reasons.append("touches protected paths: " + ", ".join(protected[:5]))
    dirty = dirty_paths(loop)
    if dirty:
        reasons.append("uncommitted changes in the worktree: " + ", ".join(dirty[:5]))
    if not vpath or not vpath.is_file():
        reasons.append("no verification result recorded")
    elif not json.loads(vpath.read_text(encoding="utf-8"))["passed"]:
        reasons.append("verification is failing" + ("" if closeout else f" ({status.get('last_verify')})"))
    if not tpath.is_file():
        reasons.append("no review result recorded")
    else:
        reasons += gate_reasons(json.loads(tpath.read_text(encoding="utf-8")), label)
    decision = "ESCALATE" if reasons else "APPROVE"
    out = {"decision": decision, "reasons": reasons, "branch": loop.branch, "worktree": str(loop.worktree),
           "merge_command": f"git -C {REPO} merge --no-ff {loop.branch}", "files": files, "rounds": status.get("round", 0),
           "wave": status.get("wave", 1), "tasks": status.get("wave_tasks", []), "scope_change": [c["task"] for c in changes]}
    stem = "closeout-decision" if closeout else f"{pfx(status)}decision"
    (loop.run_dir / f"{stem}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    (loop.run_dir / f"{stem}.md").write_text(decision + "\n\n" + "\n".join(f"- {r}" for r in reasons) + "\n", encoding="utf-8")
    return out


def cmd_decide(args: argparse.Namespace) -> int:
    emit(decide(load(args.spec_dir), args.closeout))
    return 0


# ---------------------------------------------------------------- waves: finish, accept, status, final, settle

def delivery_record(loop: Loop, status: dict[str, Any], decision: dict[str, Any], note: str = "") -> None:
    path = loop.worktree / loop.spec_dir / "delivery.md"
    head = "" if path.is_file() else f"# Delivery record\n\nBranch `{loop.branch}`, base `{status['base_sha'][:10]}`. One section per wave.\n"
    triage_path = loop.run_dir / f"{pfx(status)}triage-{status.get('last_triage', 0)}.json"
    counts = ""
    if triage_path.is_file():
        t = json.loads(triage_path.read_text(encoding="utf-8"))
        counts = f"Last review: {len(t['confirmed'])} confirmed, {len(t['noted'])} noted finding(s)."
    lines = [f"\n## Wave {status.get('wave', 1)}: {decision['decision']}{note}\n",
             f"Tasks: {', '.join(decision['tasks']) or '-'}. Review rounds: {decision['rounds']}. Files changed: {len(decision['files'])}.",
             counts]
    lines += [f"- {r}" for r in decision["reasons"]]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(head + "\n".join(x for x in lines if x) + "\n")


def mark_wave_done(loop: Loop, status: dict[str, Any]) -> None:
    path = loop.worktree / loop.spec_dir / "tasks.md"
    if path.is_file() and tasks_for(loop):
        path.write_text(mark_done(path.read_text(encoding="utf-8"), status.get("wave_tasks", [])), encoding="utf-8")
    else:
        status["implicit_done"] = True


def finish_wave(loop: Loop) -> dict[str, Any]:
    status = read_status(loop)
    decision = json.loads((loop.run_dir / f"{pfx(status)}decision.json").read_text(encoding="utf-8"))
    paths = [f"{loop.spec_dir}/delivery.md"]
    if decision["decision"] == "APPROVE":
        mark_wave_done(loop, status)
        paths.append(f"{loop.spec_dir}/tasks.md")
        state = "approved"
    else:
        state = "scope-change" if decision["scope_change"] else "escalated"
    delivery_record(loop, status, decision)
    commit_paths(loop, paths, f"loop: wave {status.get('wave', 1)} {state}")
    status["wave_state"] = state
    write_status(loop, status)
    return {"state": state, "continue": state == "approved", "wave": status.get("wave", 1)}


def cmd_finish_wave(args: argparse.Namespace) -> int:
    emit(finish_wave(load(args.spec_dir)))
    return 0


def cmd_accept_wave(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    status = read_status(loop)
    decision = json.loads((loop.run_dir / f"{pfx(status)}decision.json").read_text(encoding="utf-8"))
    mark_wave_done(loop, status)
    delivery_record(loop, status, dict(decision, decision="ACCEPTED BY A HUMAN"), " (override of an escalation)")
    commit_paths(loop, [f"{loop.spec_dir}/tasks.md", f"{loop.spec_dir}/delivery.md"], f"loop: wave {status.get('wave', 1)} accepted by a human")
    status["wave_state"] = "approved"
    write_status(loop, status)
    emit({"accepted": True, "tasks": status.get("wave_tasks", [])})
    return 0


def cmd_wave_status(args: argparse.Namespace) -> int:
    status = read_status(load(args.spec_dir))
    state = status.get("wave_state", "running")
    emit({"state": state, "continue": state == "approved", "wave": status.get("wave", 0)})
    return 0


def final_state(loop: Loop) -> dict[str, Any]:
    status = read_status(loop)
    state = status.get("wave_state", "running")
    remaining = [t.id for t in tasks_for(loop) if not t.done]
    reasons: list[str] = []
    if state == "done":
        state = "complete"
    elif state == "approved":
        state = "complete" if not remaining else "cap"
        if state == "cap":
            reasons.append(f"stopped at the wave limit with tasks remaining: {', '.join(remaining[:6])}")
    elif state in ("escalated", "scope-change"):
        d = loop.run_dir / f"{pfx(status)}decision.json"
        reasons = json.loads(d.read_text(encoding="utf-8"))["reasons"] if d.is_file() else []
    elif state == "blocked":
        reasons = ["no unchecked task is ready (dependencies, phases, or missing loop.json task entries)"]
    else:
        reasons = [f"the loop stopped unexpectedly in state {state}"]
        state = "stopped"
    return {"state": state, "reasons": reasons, "remaining": remaining, "waves": status.get("wave", 0)}


def cmd_final(args: argparse.Namespace) -> int:
    emit(final_state(load(args.spec_dir)))
    return 0


def settle(loop: Loop) -> dict[str, Any]:
    fin = final_state(loop)
    out = {"state": fin["state"], "needs_human": fin["state"] != "complete", "reasons": fin["reasons"],
           "merge_command": f"git -C {REPO} merge --no-ff {loop.branch}"}
    if fin["state"] == "complete":
        p = loop.run_dir / "closeout-decision.json"
        if not p.is_file():
            out.update(state="closeout-missing", needs_human=True, reasons=["feature complete but no closeout decision was recorded"])
        else:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d["decision"] == "ESCALATE":
                out.update(state="closeout-escalated", needs_human=True, reasons=d["reasons"])
    (loop.run_dir / "settled.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def cmd_settle(args: argparse.Namespace) -> int:
    emit(settle(load(args.spec_dir)))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    loop = load(args.spec_dir)
    status = read_status(loop)
    settled = loop.run_dir / "settled.json"
    if not settled.is_file():
        # A run started by the previous single-task version of the loop: report from its decision record.
        legacy = json.loads((loop.run_dir / "decision.json").read_text(encoding="utf-8"))
        s = {"state": legacy["decision"].lower(), "reasons": legacy["reasons"], "merge_command": legacy["merge_command"]}
    else:
        s = json.loads(settled.read_text(encoding="utf-8"))
    print(f"dev-loop {loop.name}: {s['state']} after {status.get('wave', 1)} wave(s)")
    for reason in s["reasons"]:
        print(f"  - {reason}")
    print(f"branch {loop.branch} in {loop.worktree}")
    print(f"run artifacts: {loop.run_dir}; delivery record: {loop.spec_dir}/delivery.md on the branch")
    if s["state"] in ("complete", "approve"):
        print(f"to merge (not done automatically): {s['merge_command']}")
    elif s["state"] in ("escalated", "scope-change", "blocked", "cap", "closeout-escalated"):
        print("re-run the workflow after resolving this; it resumes at the next unchecked task")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "refuse", "init", "next", "manifest", "verify", "commit", "triage", "decide", "finish-wave", "wave-status",
                 "accept-wave", "final", "settle", "report", "cleanup", "record-run"):
        s = sub.add_parser(name)
        s.add_argument("spec_dir")
        if name == "init":
            s.add_argument("--force", action="store_true")
        if name == "manifest":
            s.add_argument("kind", choices=("build", "review", "fix", "closeout"))
        if name == "verify":
            s.add_argument("tag")
        if name == "record-run":
            s.add_argument("kind")
            s.add_argument("exit_code", type=int)
        if name == "commit":
            s.add_argument("message")
        if name in ("triage", "decide"):
            s.add_argument("--closeout", action="store_true")
    args = p.parse_args(argv)
    handlers = {"preflight": cmd_preflight, "refuse": cmd_refuse, "init": cmd_init, "next": cmd_next, "manifest": cmd_manifest, "verify": cmd_verify,
                "commit": cmd_commit, "triage": cmd_triage, "decide": cmd_decide, "finish-wave": cmd_finish_wave,
                "wave-status": cmd_wave_status, "accept-wave": cmd_accept_wave, "final": cmd_final, "settle": cmd_settle,
                "report": cmd_report, "cleanup": cmd_cleanup, "record-run": cmd_record_run}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
