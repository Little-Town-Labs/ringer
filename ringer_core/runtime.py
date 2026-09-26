from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ringer_core.config import DEFAULT_TOKEN_REGEX, EngineConfig
from ringer_core.manifests import TaskSpec
from ringer_core.verification import VerifyResult


@dataclass
class TaskRuntime:
    task: TaskSpec
    taskdir: Path
    log_path: Path
    report_paths: dict[str, Path] = field(default_factory=dict)
    deliverables: list[dict[str, Any]] = field(default_factory=list)
    deliverable_notes: list[str] = field(default_factory=list)
    status: str = "queued"
    spec_short: str = ""
    attempts: int = 0
    started_at_monotonic: float | None = None
    ended_at_monotonic: float | None = None
    worker_pid: int | None = None
    tokens: int | None = None
    final_verdict: str | None = None
    last_check_returncode: int | None = None
    last_check_timed_out: bool = False
    last_check_output: str = ""
    # Why task setup failed before any worker could spawn (e.g. a stale
    # worktree from a previous failed run). Without this an ERROR verdict at
    # 0.0s carries no diagnostics anywhere the operator looks.
    setup_error: str | None = None
    last_worker_command: list[str] = field(default_factory=list)
    steering: dict[str, Any] | None = None

    def elapsed_s(self, now: float) -> float:
        if self.started_at_monotonic is None:
            return 0.0
        end = self.ended_at_monotonic if self.ended_at_monotonic is not None else now
        return max(0.0, end - self.started_at_monotonic)


@dataclass(frozen=True)
class WorkerResult:
    returncode: int | None
    timed_out: bool
    tokens: int | None
    error: str | None = None
    reported_model: str | None = None


class ProcessTree:
    @staticmethod
    def read() -> tuple[dict[int, list[int]], dict[int, str]]:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,args="],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            return {}, {}
        children: dict[int, list[int]] = {}
        commands: dict[int, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            command = parts[2] if len(parts) > 2 else ""
            children.setdefault(ppid, []).append(pid)
            commands[pid] = command
        return children, commands

    @staticmethod
    def count_named_descendants(
        root_pid: int | None,
        children: dict[int, list[int]],
        commands: dict[int, str],
        process_name: str,
    ) -> int:
        if root_pid is None:
            return 0
        needle = process_name.lower()
        count = 0
        stack = list(children.get(root_pid, []))
        while stack:
            pid = stack.pop()
            command = commands.get(pid, "")
            if command:
                executable = Path(command.split()[0]).name.lower()
                if needle and needle in executable:
                    count += 1
            stack.extend(children.get(pid, []))
        return count


class RollingBytes:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.data = bytearray()

    def extend(self, chunk: bytes) -> None:
        self.data.extend(chunk)
        overflow = len(self.data) - self.max_bytes
        if overflow > 0:
            del self.data[:overflow]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class AsyncFileCloser:
    def __init__(self, fh: Any) -> None:
        self.fh = fh

    async def __aenter__(self) -> Any:
        return self.fh

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.fh.close()


def verdict_for(worker: WorkerResult, verify: VerifyResult) -> str:
    if worker.error:
        return "ERROR"
    if worker.timed_out or verify.check_timed_out:
        return "TIMEOUT"
    if verify.ok:
        return "PASS"
    return "FAIL"


def build_run_id(run_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_name.strip()).strip("-")
    # pid suffix: same-second launches of the same run_name must not collide
    # (concurrent ringer runs would otherwise share a state file and eval run_id).
    return f"{safe_name or 'ringer'}-{stamp}-p{os.getpid()}"


def parse_token_count(text: str, token_regex: str | None = DEFAULT_TOKEN_REGEX) -> int | None:
    if token_regex:
        matches = list(re.finditer(token_regex, text, flags=re.IGNORECASE))
        for match in reversed(matches):
            groups = [item for item in match.groups() if item]
            value = groups[0] if groups else match.group(0)
            number = re.search(r"([0-9][0-9,]*)", value)
            if number:
                return int(number.group(1).replace(",", ""))
        return None
    matches = re.findall(r"tokens\s+used\s*:?\s*([0-9][0-9,]*)", text, flags=re.IGNORECASE)
    if not matches:
        matches = re.findall(
            r"tokens\s+used\s*\r?\n\s*([0-9][0-9,]*)",
            text,
            flags=re.IGNORECASE,
        )
    if not matches:
        return None
    return int(matches[-1].replace(",", ""))


def parse_reported_model(text: str, model_report_regex: str | None) -> str | None:
    if not model_report_regex:
        return None
    match = re.search(model_report_regex, text, flags=re.IGNORECASE)
    if match is None or match.lastindex is None:
        return None
    value = match.group(1).strip()
    return value or None


def effective_model_from_command(command: list[str]) -> str:
    """Return the model selected by a composed worker argv, if present."""
    for index, item in enumerate(command):
        if item in {"-m", "--model"}:
            if index + 1 >= len(command):
                return ""
            return command[index + 1]
        if item.startswith("--model="):
            return item.removeprefix("--model=")
    return ""


def effective_reasoning_effort_from_command(command: list[str]) -> str | None:
    """Return an explicitly configured model reasoning effort from worker argv."""
    for item in command:
        match = re.search(
            r"(?:^|[=,\s])model_reasoning_effort\s*=\s*[\"']?([^\"',\s]+)",
            item,
        )
        if match:
            effort = match.group(1).strip()
            return effort or None
    return None


def resolved_task_model(
    task: TaskSpec,
    engine: EngineConfig | None,
    command: list[str] | None = None,
) -> str:
    return (
        task.model
        or (engine.model_default if engine else "")
        or effective_model_from_command(command or [])
    )


def build_worker_command(
    engine: EngineConfig,
    *,
    taskdir: Path,
    spec: str,
    full_access: bool,
    engine_args: tuple[str, ...] = (),
    model: str = "",
) -> list[str]:
    access_args = engine.full_access_args if full_access else engine.sandbox_args
    resolved_model = model or engine.model_default
    command = [engine.bin]
    for item in engine.args_template:
        if item == "{access_args}":
            command.extend(access_args)
            continue
        if item == "{model_args}":
            if resolved_model:
                command.extend(("-m", resolved_model))
            continue
        if item == "{engine_args}":
            command.extend(engine_args)
            continue
        if item == "{sandbox_args}":
            command.extend(engine.sandbox_args)
            continue
        if item == "{full_access_args}":
            command.extend(engine.full_access_args)
            continue
        command.append(
            item.replace("{taskdir}", str(taskdir))
            .replace("{spec}", spec)
            .replace("{model}", resolved_model)
        )
    return command


def shell_command_for_display(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)
