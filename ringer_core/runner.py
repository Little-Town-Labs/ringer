from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ringer_core.artifact_store import artifact_deliverables_dir, artifact_live_path
from ringer_core.artifact_views import (
    IMAGE_DELIVERABLE_SUFFIXES,
    TASK_REPORT_FILENAMES,
    TEXT_DELIVERABLE_SUFFIXES,
)
from ringer_core.config import AppConfig, TOOL_NAME
from ringer_core.manifests import Manifest, TaskSpec
from ringer_core.models import model_log_text
from ringer_core.presentation import Dashboard
from ringer_core.runtime import (
    AsyncFileCloser,
    RollingBytes,
    TaskRuntime,
    WorkerResult,
    build_run_id,
    build_worker_command,
    effective_reasoning_effort_from_command,
    parse_reported_model,
    parse_token_count,
    resolved_task_model,
    shell_command_for_display,
    verdict_for,
)
from ringer_core.state import StateWriter
from ringer_core.steering import inject_steering_spec, resolve_steering_profile
from ringer_core.verification import (
    VerifyResult,
    Verifier,
    kill_process_group,
    terminate_process_group,
)
from ringer_core.worker_logs import append_text, build_failure_context, shorten


class _AttemptLogger(Protocol):
    def log_attempt(self, row: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


DELIVERABLE_MAX_BYTES = 20 * 1024 * 1024
FALLBACK_HARVEST_SUFFIXES = (
    (TEXT_DELIVERABLE_SUFFIXES - {".log"})
    | IMAGE_DELIVERABLE_SUFFIXES
    | {".html", ".htm", ".json", ".csv", ".pdf", ".mp4", ".webm", ".mov", ".gif"}
)
FALLBACK_HARVEST_MAX_FILES = 8
SHEPHERD_MODEL = f"none ({TOOL_NAME}.py)"
VERIFY_METHOD = "executed-check"

class RingerRunner:
    def __init__(
        self,
        manifest: Manifest,
        config: AppConfig,
        identity: str,
        dashboard_enabled: bool = True,
        force_browser: bool = False,
        *,
        logger: _AttemptLogger,
    ) -> None:
        self.manifest = manifest
        self.config = config
        self.identity = identity
        self.dashboard_enabled = dashboard_enabled
        self.run_id = build_run_id(manifest.run_name)
        self.started_at = datetime.now(timezone.utc)
        self.lock = threading.RLock()
        self.runtimes = [self._task_runtime(task) for task in manifest.tasks]
        self.state_writer = StateWriter(
            self.run_id,
            manifest.run_name,
            identity,
            config.state_dir,
            config.engines,
            self.started_at,
            self.runtimes,
            self.lock,
            max_parallel=manifest.max_parallel,
            artifact=config.artifact,
        )
        self.dashboard = (
            Dashboard(
                state_path=self.state_writer.path,
                preferred_port=config.dashboard_port_base,
                hud_app_path=config.hud_app_path,
                force_browser=force_browser,
            )
            if dashboard_enabled
            else None
        )
        self.logger = logger
        self.verifier = Verifier()
        self.semaphore = asyncio.Semaphore(manifest.max_parallel)
        self.active_processes: dict[int, asyncio.subprocess.Process] = {}

    async def run(self) -> int:
        self.manifest.workdir.mkdir(parents=True, exist_ok=True)
        final_state = False
        try:
            self.state_writer.start()
            if self.dashboard is not None:
                self.state_writer.set_port(self.dashboard.start())
            await asyncio.gather(*(self._run_task(runtime) for runtime in self.runtimes))
            final_state = True
            return 0 if all(runtime.status == "pass" for runtime in self.runtimes) else 1
        except asyncio.CancelledError:
            await self.kill_all_workers()
            with self.lock:
                now = time.monotonic()
                for runtime in self.runtimes:
                    if runtime.status not in {"pass", "fail"}:
                        runtime.status = "fail"
                        runtime.final_verdict = "ERROR"
                        runtime.ended_at_monotonic = runtime.ended_at_monotonic or now
            self.state_writer.flush()
            final_state = True
            raise
        finally:
            if final_state:
                self.state_writer.finish()
            self.state_writer.stop()
            if self.dashboard is not None:
                self.dashboard.stop()
            self.logger.close()
            print_summary(self.run_id, self.runtimes)
            print("Model log updated; run './ringer.py models' for the per-model scoreboard.")
            # The post-run journey: tell a human exactly where the results live.
            with contextlib.suppress(Exception):
                if self.state_writer.artifact is not None and self.state_writer.artifact.enabled:
                    results_page = artifact_live_path(self.state_writer.state_dir, self.manifest.run_name)
                    print(f"\nYour results: {results_page}")
                    print("Open it in a browser, or run './ringer.py hud' for the full Ringside view (http://127.0.0.1:8700).")

    async def kill_all_workers(self) -> None:
        procs = list(self.active_processes.values())
        for proc in procs:
            if proc.returncode is None:
                terminate_process_group(proc)
        if procs:
            await asyncio.sleep(1)
        for proc in procs:
            if proc.returncode is None:
                kill_process_group(proc)

    async def _run_task(self, runtime: TaskRuntime) -> None:
        async with self.semaphore:
            with self.lock:
                runtime.started_at_monotonic = time.monotonic()
            prepared, prepare_error = await self._prepare_taskdir(runtime)
            if not prepared:
                await self._record_prepare_error(runtime, prepare_error or "taskdir preparation failed")
                return
            current_spec = runtime.task.spec
            max_attempts = runtime.task.max_attempts
            for attempt in range(1, max_attempts + 1):
                retrying = attempt > 1
                with self.lock:
                    runtime.attempts = attempt
                    runtime.status = "retrying" if retrying else "running"
                attempt_started = time.monotonic()
                worker = await self._run_worker(runtime, current_spec, attempt)
                with self.lock:
                    runtime.worker_pid = None
                    runtime.status = "verifying"
                    if worker.tokens is not None:
                        runtime.tokens = (runtime.tokens or 0) + worker.tokens
                verify = await self.verifier.verify(runtime.task, runtime.taskdir)
                verdict = verdict_for(worker, verify)
                with self.lock:
                    runtime.last_check_returncode = verify.check_returncode
                    runtime.last_check_timed_out = verify.check_timed_out
                    runtime.last_check_output = verify.raw_output_excerpt
                duration_ms = int((time.monotonic() - attempt_started) * 1000)
                self._log_attempt(runtime, current_spec, retrying, worker, verify, verdict, duration_ms)
                if verdict == "PASS":
                    self._harvest_deliverables_on_pass(runtime)
                    with self.lock:
                        runtime.status = "pass"
                        runtime.final_verdict = verdict
                        runtime.ended_at_monotonic = time.monotonic()
                    await self._cleanup_worktree_on_pass(runtime)
                    return
                if attempt < max_attempts and verdict in {"FAIL", "TIMEOUT"}:
                    failure_context = build_failure_context(runtime.log_path, verify.raw_output_excerpt)
                    current_spec = (
                        f"{runtime.task.spec}\n\n"
                        f"Previous attempt failed: {failure_context}. Fix it."
                    )
                    continue
                with self.lock:
                    runtime.status = "fail"
                    runtime.final_verdict = verdict
                    runtime.ended_at_monotonic = time.monotonic()
                return

    def _harvest_deliverables_on_pass(self, runtime: TaskRuntime) -> None:
        harvested: list[dict[str, Any]] = []
        notes: list[str] = []
        target_dir = artifact_deliverables_dir(
            self.config.state_dir,
            self.run_id,
            runtime.task.key,
        )
        expect_files: tuple[str, ...] = runtime.task.expect_files
        worktree_task = self.manifest.worktrees and self.manifest.repo is not None
        if not expect_files and not worktree_task:
            # (In worktrees mode the taskdir root is a whole repo checkout —
            # guessing there would harvest README.md and friends, not work.)
            # No declared deliverables: rescue what the worker left at the
            # top of its task directory, or the run's real output (a review,
            # a report) never reaches the results page. Declaring
            # expect_files remains the way to control exactly what is shown.
            candidates = sorted(
                path.name
                for path in runtime.taskdir.glob("*")
                if path.is_file()
                and not path.name.startswith(".")
                and path.suffix.lower() in FALLBACK_HARVEST_SUFFIXES
            )
            if len(candidates) > FALLBACK_HARVEST_MAX_FILES:
                notes.append(
                    f"Only the first {FALLBACK_HARVEST_MAX_FILES} of {len(candidates)} files were "
                    "collected automatically; declare expect_files to choose exactly what is kept."
                )
                candidates = candidates[:FALLBACK_HARVEST_MAX_FILES]
            expect_files = tuple(candidates)
        for expect_path in expect_files:
            source = Verifier._expect_file_path(runtime.taskdir, expect_path)
            try:
                stat = source.stat()
            except OSError:
                continue
            if not source.is_file():
                continue
            if stat.st_size > DELIVERABLE_MAX_BYTES:
                notes.append(
                    f"{source.name} was not copied because it is larger than 20 MB "
                    f"({stat.st_size:,} bytes)."
                )
                continue
            target = target_dir / source.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied_size = target.stat().st_size
            except OSError as exc:
                append_text(
                    runtime.log_path,
                    f"[ringer.py] deliverable copy failed for {source.name}: {exc}\n",
                )
                continue
            harvested.append({"name": source.name, "path": str(target), "bytes": copied_size})
        if harvested or notes:
            with self.lock:
                runtime.deliverables = harvested
                runtime.deliverable_notes.extend(notes)

    async def _prepare_taskdir(self, runtime: TaskRuntime) -> tuple[bool, str | None]:
        taskdir = runtime.taskdir
        if self.manifest.worktrees and self.manifest.repo is not None:
            taskdir.parent.mkdir(parents=True, exist_ok=True)
            if taskdir.exists():
                # Failed tasks keep their worktrees for post-mortems, so a
                # re-run with the same run_name lands here. Name the exact
                # command that unblocks it — the bare "already exists" cost a
                # full diagnosis cycle in the field. A linked worktree has a
                # .git *file*; only then is `git worktree remove` the right
                # command, and it must be repo-qualified and quoted to be
                # paste-safe from anywhere.
                if (taskdir / ".git").is_file():
                    remove_cmd = (
                        f"git -C {shlex.quote(str(self.manifest.repo))} "
                        f"worktree remove --force {shlex.quote(str(taskdir))}"
                    )
                    return False, (
                        f"worktree taskdir already exists (left by a previous "
                        f"failed run?): {taskdir} — remove it with "
                        f"`{remove_cmd}` and re-run"
                    )
                return False, (
                    f"taskdir already exists but is not a registered git "
                    f"worktree: {taskdir} — move or delete it, then re-run"
                )
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.manifest.repo),
                "worktree",
                "add",
                str(taskdir),
                "HEAD",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                message = stdout.decode("utf-8", errors="replace")
                append_text(runtime.log_path, f"[ringer.py] git worktree add failed:\n{message}\n")
                return False, message.strip() or "git worktree add failed"
            return True, None
        taskdir.mkdir(parents=True, exist_ok=True)
        return True, None

    async def _cleanup_worktree_on_pass(self, runtime: TaskRuntime) -> None:
        if not (self.manifest.worktrees and self.manifest.repo is not None):
            return
        self._snapshot_worktree_reports(runtime)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.manifest.repo),
            "worktree",
            "remove",
            "--force",
            str(runtime.taskdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            message = stdout.decode("utf-8", errors="replace")
            append_text(runtime.log_path, f"[ringer.py] git worktree remove failed:\n{message}\n")

    def _snapshot_worktree_reports(self, runtime: TaskRuntime) -> None:
        copied: dict[str, Path] = {}
        report_dir = (runtime.log_path.parent / f"{runtime.log_path.stem}.reports").resolve()
        for report_name in TASK_REPORT_FILENAMES:
            source = runtime.taskdir / report_name
            if not source.exists():
                continue
            target = report_dir / report_name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError as exc:
                append_text(
                    runtime.log_path,
                    f"[ringer.py] report snapshot failed for {report_name}: {exc}\n",
                )
                continue
            copied[report_name] = target
        if copied:
            with self.lock:
                runtime.report_paths.update(copied)

    async def _record_prepare_error(self, runtime: TaskRuntime, error: str) -> None:
        with self.lock:
            runtime.attempts = 1
            runtime.status = "fail"
            runtime.final_verdict = "ERROR"
            runtime.setup_error = error
            runtime.ended_at_monotonic = time.monotonic()
        # The worker log is where every other surface (HUD activity,
        # log_tail, post-mortems) looks first — leave the reason there too.
        with contextlib.suppress(Exception):
            append_text(
                runtime.log_path,
                f"[ringer.py] task setup failed before any worker could "
                f"spawn: {error}\n",
            )
        verify = VerifyResult(
            ok=False,
            check_returncode=None,
            check_timed_out=False,
            raw_output_excerpt="",
        )
        worker = WorkerResult(returncode=None, timed_out=False, tokens=None, error=error)
        self._log_attempt(runtime, runtime.task.spec, False, worker, verify, "ERROR", 0)

    async def _run_worker(self, runtime: TaskRuntime, spec: str, attempt: int) -> WorkerResult:
        log_path = runtime.log_path
        engine = self.config.engines.get(runtime.task.engine)
        if engine is None:
            return WorkerResult(
                returncode=None,
                timed_out=False,
                tokens=None,
                error=f"unknown worker engine: {runtime.task.engine}",
            )
        if runtime.task.full_access and not self.config.allow_full_access:
            return WorkerResult(
                returncode=None,
                timed_out=False,
                tokens=None,
                error=(
                    f"task requested full_access with engine {runtime.task.engine}, "
                    "but config allow_full_access is false"
                ),
            )
        cmd = build_worker_command(
            engine,
            taskdir=runtime.taskdir,
            spec=spec,
            full_access=runtime.task.full_access,
            engine_args=runtime.task.engine_args,
            model=runtime.task.model,
        )
        command_spec = spec
        if self.config.steering.dir is not None:
            original_cmd = cmd
            steering_state: dict[str, Any] = {
                "profile": None,
                "version": None,
                "rule_ids": [],
            }
            steering_line = "[ringer.py] steering: no profile matched\n"
            try:
                resolved_model = resolved_task_model(runtime.task, engine, cmd)
                profile = resolve_steering_profile(self.config.steering.dir, resolved_model)
                injected_spec, rule_ids = inject_steering_spec(
                    spec,
                    profile,
                    inject_candidates=self.config.steering.inject_candidates,
                )
                if profile is not None:
                    steering_state = {
                        "profile": profile.slug,
                        "version": profile.profile_version,
                        "rule_ids": list(rule_ids),
                    }
                    shown_rules = ", ".join(rule_ids) if rule_ids else "(none)"
                    steering_line = (
                        f"[ringer.py] steering: profile={profile.slug} "
                        f"version={profile.profile_version} rule_ids={shown_rules}\n"
                    )
                if injected_spec != spec:
                    cmd = build_worker_command(
                        engine,
                        taskdir=runtime.taskdir,
                        spec=injected_spec,
                        full_access=runtime.task.full_access,
                        engine_args=runtime.task.engine_args,
                        model=runtime.task.model,
                    )
                    command_spec = injected_spec
            except Exception:
                cmd = original_cmd
                command_spec = spec
                steering_state = {"profile": None, "version": None, "rule_ids": []}
                steering_line = "[ringer.py] steering: no profile matched\n"
            with self.lock:
                runtime.steering = steering_state
            with contextlib.suppress(Exception):
                append_text(log_path, steering_line)
        with self.lock:
            runtime.last_worker_command = list(cmd)
        display_cmd = [
            (
                part.replace(command_spec, "[request packet omitted]")
                if runtime.task.redact_spec and command_spec in part
                else part
            )
            for part in cmd
        ]
        append_text(
            log_path,
            "\n"
            f"[ringer.py] attempt {attempt} started {datetime.now(timezone.utc).isoformat()}\n"
            f"[ringer.py] engine: {runtime.task.engine}\n"
            f"[ringer.py] command: {shell_command_for_display(display_cmd)} < /dev/null\n",
        )
        capture = RollingBytes(max_bytes=1_000_000)
        try:
            log_fh = log_path.open("ab")
        except OSError as exc:
            return WorkerResult(returncode=None, timed_out=False, tokens=None, error=str(exc))
        async with AsyncFileCloser(log_fh):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(runtime.taskdir),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                message = f"[ringer.py] worker spawn failed: {exc}\n"
                log_fh.write(message.encode("utf-8", errors="replace"))
                log_fh.flush()
                return WorkerResult(returncode=None, timed_out=False, tokens=None, error=str(exc))
            with self.lock:
                runtime.worker_pid = proc.pid
            self.active_processes[proc.pid] = proc
            reader = asyncio.create_task(self._tee_stream(proc, log_fh, capture))
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=runtime.task.timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                terminate_process_group(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    kill_process_group(proc)
                    await proc.wait()
            try:
                await asyncio.wait_for(reader, timeout=5)
            except asyncio.TimeoutError:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            self.active_processes.pop(proc.pid, None)
        output_tail = capture.text()
        tokens = parse_token_count(output_tail, engine.token_regex)
        reported_model = parse_reported_model(output_tail, engine.model_report_regex)
        if timed_out:
            append_text(log_path, f"\n[ringer.py] worker timed out after {runtime.task.timeout_s}s\n")
        append_text(log_path, f"[ringer.py] attempt {attempt} exited rc={proc.returncode}\n")
        return WorkerResult(
            returncode=proc.returncode,
            timed_out=timed_out,
            tokens=tokens,
            reported_model=reported_model,
        )

    async def _tee_stream(
        self,
        proc: asyncio.subprocess.Process,
        log_fh: Any,
        capture: "RollingBytes",
    ) -> None:
        if proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            log_fh.write(chunk)
            log_fh.flush()
            capture.extend(chunk)
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def _log_attempt(
        self,
        runtime: TaskRuntime,
        spec: str,
        retrying: bool,
        worker: WorkerResult,
        verify: VerifyResult,
        verdict: str,
        duration_ms: int,
    ) -> None:
        engine = self.config.engines.get(runtime.task.engine)
        resolved_model = resolved_task_model(
            runtime.task,
            engine,
            runtime.last_worker_command,
        )
        reported_model = model_log_text(worker.reported_model) or None
        mismatch = bool(reported_model and resolved_model and reported_model != resolved_model)
        stamped_model = reported_model or resolved_model
        expected_model = resolved_model if mismatch else None
        if mismatch:
            with contextlib.suppress(Exception):
                append_text(
                    runtime.log_path,
                    f"[ringer.py] identity: harness reported {reported_model} "
                    f"but manifest/config expected {resolved_model}\n",
                )
        reasoning_effort = effective_reasoning_effort_from_command(
            runtime.last_worker_command
        )
        notes_parts = [
            f"retry={'true' if retrying else 'false'}",
            f"worker_returncode={worker.returncode}",
            f"model={stamped_model}",
            f"task_type={runtime.task.task_type}",
        ]
        if worker.error:
            notes_parts.append(f"worker_error={worker.error}")
        if verify.missing_files:
            notes_parts.append(f"missing_expect_files={json.dumps(list(verify.missing_files))}")
        notes_parts.append("raw_check_output_first_2000_chars:")
        notes_parts.append(verify.raw_output_excerpt)
        with contextlib.suppress(Exception):
            self._write_steering_observation(
                runtime,
                resolved_model=stamped_model,
                retrying=retrying,
                worker=worker,
                verify=verify,
                verdict=verdict,
                duration_ms=duration_ms,
            )
        self.logger.log_attempt(
            {
                "run_id": self.run_id,
                "pattern": "ringer-py",
                "task_key": runtime.task.key,
                "spec": (
                    "[redacted request packet]"
                    if runtime.task.redact_spec
                    else spec[:500]
                ),
                "worker_engine": runtime.task.engine,
                "shepherd_model": SHEPHERD_MODEL,
                "verify_method": VERIFY_METHOD,
                "verdict": verdict,
                "duration_ms": duration_ms,
                "worker_tokens": worker.tokens,
                "notes": "\n".join(notes_parts),
                "orchestrator": self.identity,
                "model": stamped_model,
                "reported_model": reported_model,
                "expected_model": expected_model,
                "reasoning_effort": reasoning_effort,
                "task_type": runtime.task.task_type,
                "retry": retrying,
            }
        )

    def _write_steering_observation(
        self,
        runtime: TaskRuntime,
        *,
        resolved_model: str,
        retrying: bool,
        worker: WorkerResult,
        verify: VerifyResult,
        verdict: str,
        duration_ms: int,
    ) -> None:
        steering_dir = self.config.steering.dir
        if steering_dir is None:
            return
        try:
            steering_state = runtime.steering
            if steering_state is None:
                profile = resolve_steering_profile(steering_dir, resolved_model)
                steering_state = {
                    "profile": profile.slug if profile else None,
                    "version": profile.profile_version if profile else None,
                    "rule_ids": [],
                }
                with self.lock:
                    runtime.steering = steering_state
            now = datetime.now(timezone.utc)
            row = {
                "ts": now.isoformat(),
                "source": "ringer.py",
                "run_id": self.run_id,
                "run_name": self.manifest.run_name,
                "task_key": runtime.task.key,
                "task_type": runtime.task.task_type,
                "engine": runtime.task.engine,
                "model": resolved_model,
                "profile": steering_state.get("profile"),
                "profile_version": steering_state.get("version"),
                "rules_injected": list(steering_state.get("rule_ids", [])),
                "attempt": runtime.attempts,
                "retry": retrying,
                "verdict": verdict,
                "duration_ms": duration_ms,
                "worker_tokens": worker.tokens,
                "check_excerpt": verify.raw_output_excerpt[:500],
            }
            path = (
                steering_dir
                / "observations"
                / "ringer"
                / f"{now.strftime('%Y-%m-%d')}.jsonl"
            )
            append_text(path, json.dumps(row, sort_keys=True) + "\n")
        except Exception as exc:
            with contextlib.suppress(Exception):
                append_text(
                    runtime.log_path,
                    f"[ringer.py] steering: observation write failed {exc}\n",
                )

    def _task_runtime(self, task: TaskSpec) -> TaskRuntime:
        taskdir = self._taskdir(task)
        log_path = self._log_path(task, taskdir)
        with contextlib.suppress(FileNotFoundError):
            log_path.unlink()
        return TaskRuntime(
            task=task,
            taskdir=taskdir,
            log_path=log_path,
            spec_short=shorten(task.spec, 120),
        )

    def _taskdir(self, task: TaskSpec) -> Path:
        taskdir = (self.manifest.workdir / task.key).resolve()
        workdir = self.manifest.workdir.resolve()
        if taskdir != workdir and workdir not in taskdir.parents:
            raise ValueError(f"task key escapes workdir: {task.key}")
        return taskdir

    def _log_path(self, task: TaskSpec, taskdir: Path) -> Path:
        if not self.manifest.worktrees:
            return taskdir / "worker.log"
        logs_dir = (self.manifest.workdir / "logs").resolve()
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = (logs_dir / f"{task.key}.worker.log").resolve()
        if log_path != logs_dir and logs_dir not in log_path.parents:
            raise ValueError(f"task key escapes logs dir: {task.key}")
        return log_path

def print_summary(run_id: str, runtimes: list[TaskRuntime]) -> None:
    print("\nSummary")
    print(f"run_id: {run_id}")
    header = f"{'task':<24} {'status':<8} {'verdict':<8} {'attempts':>8} {'tokens':>10} {'elapsed_s':>10}"
    print(header)
    print("-" * len(header))
    now = time.monotonic()
    for runtime in runtimes:
        tokens = "" if runtime.tokens is None else str(runtime.tokens)
        print(
            f"{runtime.task.key:<24} {runtime.status:<8} "
            f"{(runtime.final_verdict or ''):<8} {runtime.attempts:>8} "
            f"{tokens:>10} {runtime.elapsed_s(now):>10.1f}"
        )
    setup_failures = [r for r in runtimes if r.setup_error]
    if setup_failures:
        print("\nsetup failures (no worker was spawned):")
        for runtime in setup_failures:
            print(f"  {runtime.task.key}: {runtime.setup_error}")
