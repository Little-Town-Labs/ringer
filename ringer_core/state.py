from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ringer_core.config import ArtifactConfig, EngineConfig
from ringer_core.runtime import ProcessTree, TaskRuntime, effective_model_from_command
from ringer_core.artifact_views import ArtifactRenderer
from ringer_core.state_files import atomic_write_json, atomic_write_text, scan_run_states
from ringer_core.worker_logs import shorten, tail_lines, worker_activity
from ringer_core.artifact_store import (
    append_artifact_library_version,
    artifact_live_path,
    artifact_outcome_from_state,
    artifact_version_path,
    collect_state_deliverables,
    reconcile_artifact_library_dead_runs,
    update_artifact_library_live,
)


class StateWriter:
    def __init__(
        self,
        run_id: str,
        run_name: str,
        identity: str,
        state_dir: Path,
        engines: dict[str, EngineConfig],
        started_at: datetime,
        runtimes: list[TaskRuntime],
        lock: threading.RLock,
        max_parallel: int = 1,
        artifact: ArtifactConfig | None = None,
        path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_name = run_name
        self.identity = identity
        self.engines = engines
        self.started_at = started_at
        self.runtimes = runtimes
        self.lock = lock
        self.max_parallel = max_parallel
        self.state_dir = state_dir
        self.path = path or (state_dir / "runs" / f"{run_id}.json")
        self.pid = os.getpid()
        self.port: int | None = None
        self.finished = False
        self.summary: dict[str, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.artifact = artifact or ArtifactConfig(
            enabled=False,
            out_template=str(state_dir / "artifacts" / "{run_id}.html"),
            report_template=str(state_dir / "artifacts" / "{run_id}-report.html"),
            index_out=state_dir / "artifacts" / "index.html",
        )
        self.artifact_path = self.artifact.artifact_path(self.run_id, self.run_name)
        self.live_path = artifact_live_path(self.state_dir, self.run_name)
        self.version_path = artifact_version_path(self.state_dir, self.run_name, self.run_id)
        self.report_path = self.artifact.report_path(self.run_id, self.run_name)
        self.artifact_renderer = ArtifactRenderer(self.artifact_path)
        self.report_written = False
        self.artifact_page_written = False
        self.live_page_written = False
        self.report_page_written = False
        self.version_page_written = False
        self.version_recorded = False
        self._last_library_state: str | None = None
        self._last_library_write_monotonic = 0.0

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._reconcile_library_safe()
        self.flush()
        self._thread = threading.Thread(target=self._loop, name="ringer-state-writer", daemon=True)
        self._thread.start()

    def set_port(self, port: int | None) -> None:
        self.port = port
        self.flush()

    def finish(self) -> None:
        self.finished = True
        self.summary = self.build_summary()
        state = self.flush()
        self._write_final_report_safe(state)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.flush()

    def flush(self) -> dict[str, Any]:
        state = self.snapshot()
        # atomic_write_json, not a fixed "<name>.json.tmp": the background
        # writer thread and an explicit flush (a signal handler, set_port)
        # can overlap. With one shared temp name, the first os.replace wins
        # and the second raises FileNotFoundError on a temp file that no
        # longer exists — which surfaced as a ~30% flake in the shutdown
        # tests. mkstemp gives each writer its own file; last replace wins.
        atomic_write_json(self.path, state)
        if self.artifact.enabled:
            self._write_status_artifact_safe(state)
            state = self._sync_output_metadata(state)
            atomic_write_json(self.path, state)
            self._write_index_safe()
        self._write_library_live_safe(state)
        return state

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        children, commands = ProcessTree.read()
        with self.lock:
            tasks = []
            for runtime in self.runtimes:
                log_tail = tail_lines(runtime.log_path, line_count=3)
                log_tail_full = tail_lines(runtime.log_path, line_count=40)
                engine = self.engines.get(runtime.task.engine)
                process_name = engine.process_name if engine else runtime.task.engine
                task_state = {
                    "key": runtime.task.key,
                    "status": runtime.status,
                    "verdict": runtime.final_verdict,
                    "engine": runtime.task.engine,
                    "model": (
                        runtime.task.model
                        or (engine.model_default if engine else "")
                        or effective_model_from_command(runtime.last_worker_command)
                    ),
                    "spec": (
                        "[redacted request packet]"
                        if runtime.task.redact_spec
                        else runtime.task.spec
                    ),
                    "spec_short": (
                        "[redacted request packet]"
                        if runtime.task.redact_spec
                        else runtime.spec_short
                    ),
                    "verified": runtime.task.verified,
                    "check": runtime.task.check,
                    "check_returncode": runtime.last_check_returncode,
                    "check_timed_out": runtime.last_check_timed_out,
                    "check_output_tail": shorten(runtime.last_check_output, 4000),
                    "setup_error": runtime.setup_error,
                    "timeout_s": runtime.task.timeout_s,
                    "max_attempts": runtime.task.max_attempts,
                    "taskdir": str(runtime.taskdir),
                    "log_path": str(runtime.log_path),
                    "report_paths": {
                        name: str(path) for name, path in runtime.report_paths.items()
                    },
                    "deliverables": [dict(item) for item in runtime.deliverables],
                    "deliverable_notes": list(runtime.deliverable_notes),
                    "activity": worker_activity(runtime.log_path, log_tail),
                    "elapsed_s": round(runtime.elapsed_s(now), 1),
                    "tokens": runtime.tokens,
                    "attempts": runtime.attempts,
                    "children": ProcessTree.count_named_descendants(
                        runtime.worker_pid, children, commands, process_name
                    ),
                    "log_tail": log_tail,
                    "log_tail_full": log_tail_full,
                }
                if runtime.steering is not None:
                    task_state["steering"] = dict(runtime.steering)
                tasks.append(task_state)
            pass_count = sum(1 for item in tasks if item["status"] == "pass")
            fail_count = sum(1 for item in tasks if item["status"] == "fail")
            running_count = sum(
                1 for item in tasks if item["status"] in {"running", "verifying", "retrying"}
            )
            totals = {
                "running": running_count,
                "done": pass_count + fail_count,
                "pass": pass_count,
                "fail": fail_count,
                "tokens": sum(int(item["tokens"] or 0) for item in tasks),
            }
            return {
                "run_id": self.run_id,
                "run_name": self.run_name,
                "identity": self.identity,
                "state": "finished" if self.finished else "live",
                "pid": self.pid,
                "port": self.port,
                "dashboard_port": self.port,
                "max_parallel": self.max_parallel,
                "finished": self.finished,
                "summary": self.summary if self.finished else None,
                "started_at": self.started_at.isoformat(),
                "elapsed_s": max((float(item["elapsed_s"]) for item in tasks), default=0.0),
                "tasks": tasks,
                "totals": totals,
                "pass": totals["pass"],
                "fail": totals["fail"],
                "tokens": totals["tokens"],
                "artifact_path": str(self.artifact_path) if self.artifact_page_written else None,
                "live_path": str(self.live_path) if self.live_page_written else None,
                "report_path": str(self.report_path) if self.report_page_written else None,
                "report_ready": self.report_written,
            }

    def build_summary(self) -> dict[str, int]:
        with self.lock:
            return {
                "pass": sum(1 for runtime in self.runtimes if runtime.status == "pass"),
                "fail": sum(1 for runtime in self.runtimes if runtime.status == "fail"),
                "tokens": sum(int(runtime.tokens or 0) for runtime in self.runtimes),
            }

    def _write_status_artifact_safe(self, state: dict[str, Any]) -> None:
        finished = bool(state.get("finished")) or str(state.get("state")) == "finished"
        render = (
            self.artifact_renderer.render_final_report_html
            if finished
            else self.artifact_renderer.render_status_html
        )
        try:
            html = render(state, page_path=self.artifact_path)
            atomic_write_text(self.artifact_path, html)
            self.artifact_page_written = True
        except Exception as exc:
            print(f"artifact render error (run page, non-fatal): {exc}", file=sys.stderr)
        try:
            html = render(state, page_path=self.live_path)
            atomic_write_text(self.live_path, html)
            self.live_page_written = True
        except Exception as exc:
            print(f"artifact render error (live page, non-fatal): {exc}", file=sys.stderr)

    def _write_final_report_safe(self, state: dict[str, Any]) -> None:
        if self.artifact.enabled:
            try:
                html = self.artifact_renderer.render_final_report_html(
                    state, page_path=self.report_path
                )
                atomic_write_text(self.report_path, html)
                self.report_page_written = True
            except Exception as exc:
                print(f"artifact render error (final report, non-fatal): {exc}", file=sys.stderr)
            try:
                html = self.artifact_renderer.render_final_report_html(
                    state, page_path=self.version_path
                )
                atomic_write_text(self.version_path, html)
                self.version_page_written = True
            except Exception as exc:
                print(f"artifact render error (version page, non-fatal): {exc}", file=sys.stderr)
            self.report_written = self.report_page_written
        state = self._sync_output_metadata(state)
        atomic_write_json(self.path, state)
        if self.artifact.enabled:
            self._write_index_safe()
        self._append_library_version_safe(state)

    def _sync_output_metadata(self, state: dict[str, Any]) -> dict[str, Any]:
        synced = dict(state)
        synced["artifact_path"] = str(self.artifact_path) if self.artifact_page_written else None
        synced["live_path"] = str(self.live_path) if self.live_page_written else None
        synced["report_path"] = str(self.report_path) if self.report_page_written else None
        synced["report_ready"] = self.report_written
        return synced

    def _write_library_live_safe(self, state: dict[str, Any]) -> None:
        outcome = artifact_outcome_from_state(state)
        now = time.monotonic()
        if self._last_library_state == outcome and now - self._last_library_write_monotonic < 5:
            return
        try:
            update_artifact_library_live(
                self.state_dir,
                run_name=self.run_name,
                run_id=self.run_id,
                identity=self.identity,
                state=outcome,
                artifact_enabled=self.live_page_written,
            )
            self._last_library_state = outcome
            self._last_library_write_monotonic = now
        except Exception as exc:
            print(f"artifact library update error (non-fatal): {exc}", file=sys.stderr)

    def _append_library_version_safe(self, state: dict[str, Any]) -> None:
        if self.version_recorded:
            return
        totals = state.get("totals") if isinstance(state.get("totals"), dict) else {}
        outcome = artifact_outcome_from_state(state)
        try:
            append_artifact_library_version(
                self.state_dir,
                run_name=self.run_name,
                run_id=self.run_id,
                identity=self.identity,
                outcome=outcome,
                version_path=self.version_path if self.version_page_written else None,
                report_path=(
                    self.report_path
                    if self.report_page_written and self.report_path != self.version_path
                    else None
                ),
                artifact_enabled=self.live_page_written,
                tasks_pass=int(totals.get("pass", state.get("pass", 0)) or 0),
                tasks_fail=int(totals.get("fail", state.get("fail", 0)) or 0),
                deliverables=collect_state_deliverables(state),
            )
            self.version_recorded = True
            self._last_library_state = outcome
            self._last_library_write_monotonic = time.monotonic()
        except Exception as exc:
            print(f"artifact library version error (non-fatal): {exc}", file=sys.stderr)

    def _reconcile_library_safe(self) -> None:
        try:
            reconcile_artifact_library_dead_runs(self.state_dir)
        except Exception as exc:
            print(f"artifact library reconcile error (non-fatal): {exc}", file=sys.stderr)

    def _write_index_safe(self) -> None:
        try:
            entries = scan_run_states(self.state_dir)
            html = self.artifact_renderer.render_artifact_index_html(entries)
            atomic_write_text(self.artifact.index_out, html)
        except Exception as exc:
            print(f"artifact render error (index, non-fatal): {exc}", file=sys.stderr)

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.flush()
            except Exception as exc:
                print(f"state writer error: {exc}", file=sys.stderr)
