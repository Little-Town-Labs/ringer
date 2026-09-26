from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .config import DEFAULT_ENGINE_NAME

DEFAULT_TIMEOUT_S = 900
def require_bool(value: Any, key: str, field: str) -> bool:
    """Reject truthy stand-ins. `"false"` is a string, and `bool("false")` is True."""
    if not isinstance(value, bool):
        raise ValueError(
            f"task {key}: {field} must be true or false, "
            f"got {type(value).__name__} {value!r}"
        )
    return value
@dataclass(frozen=True)
class TaskSpec:
    key: str
    spec: str
    check: str
    engine: str = DEFAULT_ENGINE_NAME
    expect_files: tuple[str, ...] = ()
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_attempts: int = 2
    redact_spec: bool = False
    full_access: bool = False
    engine_args: tuple[str, ...] = ()
    verified: str = ""
    # Which model a harness engine should run for this task (fills the
    # engine's {model} placeholder); empty means the engine's model_default.
    model: str = ""
    task_type: str = ""

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> "TaskSpec":
        key_raw = obj.get("key", "")
        if not isinstance(key_raw, str):
            raise ValueError("task key must be a string")
        key = key_raw.strip()
        if not key:
            raise ValueError("task key is required")
        spec = obj.get("spec", "")
        if not isinstance(spec, str):
            raise ValueError(f"task {key}: spec must be a string")
        if not spec:
            raise ValueError(f"task {key}: spec is required")
        check = obj.get("check", "")
        if not isinstance(check, str):
            raise ValueError(f"task {key}: check must be a string")
        if not check:
            raise ValueError(f"task {key}: check is required")
        expect_files = obj.get("expect_files", [])
        if not isinstance(expect_files, list):
            raise ValueError(f"task {key}: expect_files must be a list")
        engine = str(obj.get("engine", DEFAULT_ENGINE_NAME)).strip()
        if not engine:
            raise ValueError(f"task {key}: engine must not be empty")
        timeout_s = int(obj.get("timeout_s", DEFAULT_TIMEOUT_S))
        if timeout_s <= 0:
            raise ValueError(f"task {key}: timeout_s must be positive")
        # Strict on the fields this release introduces: `1.5` silently
        # truncating to 1 would remove the retry without saying so, and a
        # string is never what the author meant.
        raw_max_attempts = obj.get("max_attempts", 2)
        if isinstance(raw_max_attempts, bool) or not isinstance(raw_max_attempts, int):
            raise ValueError(
                f"task {key}: max_attempts must be an integer, "
                f"got {type(raw_max_attempts).__name__}"
            )
        max_attempts = raw_max_attempts
        if max_attempts <= 0:
            raise ValueError(f"task {key}: max_attempts must be positive")
        engine_args = obj.get("engine_args", [])
        if not isinstance(engine_args, list) or not all(isinstance(item, str) for item in engine_args):
            raise ValueError(f"task {key}: engine_args must be a list of strings")
        verified = obj.get("verified", "")
        if not isinstance(verified, str):
            raise ValueError(f"task {key}: verified must be a string (plain-English description of what the check proves)")
        model = obj.get("model", "")
        if not isinstance(model, str):
            raise ValueError(f"task {key}: model must be a string (e.g. 'openrouter/z-ai/glm-5.2')")
        task_type = obj.get("task_type", "")
        if not isinstance(task_type, str):
            raise ValueError(f"task {key}: task_type must be a string")
        return cls(
            key=key,
            spec=spec,
            check=check,
            engine=engine,
            expect_files=tuple(str(item) for item in expect_files),
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            redact_spec=require_bool(obj.get("redact_spec", False), key, "redact_spec"),
            full_access=bool(obj.get("full_access", False)),
            engine_args=tuple(engine_args),
            verified=verified.strip(),
            model=model.strip(),
            task_type=task_type.strip(),
        )
@dataclass(frozen=True)
class Manifest:
    run_name: str
    workdir: Path
    max_parallel: int
    worktrees: bool
    repo: Path | None
    tasks: tuple[TaskSpec, ...]
    source_path: Path | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Manifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest root must be a JSON object")
        manifest = cls.from_obj(data)
        return cls(
            run_name=manifest.run_name,
            workdir=manifest.workdir,
            max_parallel=manifest.max_parallel,
            worktrees=manifest.worktrees,
            repo=manifest.repo,
            tasks=manifest.tasks,
            source_path=path,
        )

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> "Manifest":
        run_name = str(obj.get("run_name", "")).strip()
        if not run_name:
            raise ValueError("run_name is required")
        if run_name == MODEL_SCOREBOARD_RUN_NAME:
            raise ValueError("run_name model-scoreboard is reserved for the scoreboard page")
        workdir_raw = obj.get("workdir")
        if not workdir_raw:
            raise ValueError("workdir is required")
        workdir = Path(str(workdir_raw)).expanduser().resolve()
        max_parallel = int(obj.get("max_parallel", 1))
        if max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        repo_raw = obj.get("repo")
        repo = Path(str(repo_raw)).expanduser().resolve() if repo_raw else None
        tasks_raw = obj.get("tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ValueError("tasks must be a non-empty list")
        tasks = tuple(TaskSpec.from_obj(task) for task in tasks_raw)
        keys = [task.key for task in tasks]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate task keys: {', '.join(duplicates)}")
        worktrees = bool(obj.get("worktrees", False))
        if worktrees:
            reserved_logs_dir = (workdir / "logs").resolve()
            collisions = []
            for task in tasks:
                taskdir = (workdir / task.key).resolve()
                if taskdir == reserved_logs_dir or reserved_logs_dir in taskdir.parents:
                    collisions.append(task.key)
            if collisions:
                raise ValueError(
                    "task key(s) collide with reserved worktree logs directory "
                    f"'logs': {', '.join(collisions)}"
                )
        return cls(
            run_name=run_name,
            workdir=workdir,
            max_parallel=max_parallel,
            worktrees=worktrees,
            repo=repo,
            tasks=tasks,
        )

    def with_max_parallel(self, value: int | None) -> "Manifest":
        if value is None:
            return self
        if value <= 0:
            raise ValueError("--max-parallel must be positive")
        return Manifest(
            run_name=self.run_name,
            workdir=self.workdir,
            max_parallel=value,
            worktrees=self.worktrees,
            repo=self.repo,
            tasks=self.tasks,
            source_path=self.source_path,
        )
MODEL_SCOREBOARD_RUN_NAME = "model-scoreboard"
