from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ringer_core.config import ENV_VAR_PREFIX, STATE_DIR_NAME


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
            fh.flush()
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def ringer_home() -> Path:
    value = os.environ.get(f"{ENV_VAR_PREFIX}_HOME")
    if value and value.strip():
        return Path(value).expanduser().resolve()
    return (Path.home() / STATE_DIR_NAME).resolve()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def active_runs_path() -> Path:
    return ringer_home() / "active-runs.json"


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_active_runs_raw(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    runs: dict[str, dict[str, Any]] = {}
    for run_id, value in data.items():
        if isinstance(run_id, str) and isinstance(value, dict):
            runs[run_id] = value
    return runs


def _prune_active_runs(runs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pruned: dict[str, dict[str, Any]] = {}
    for run_id, entry in runs.items():
        pid = entry.get("pid")
        if isinstance(pid, bool):
            continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        if not pid_is_alive(pid_int):
            continue
        pruned[run_id] = {
            "pid": pid_int,
            "identity": str(entry.get("identity", "")),
            "run_name": str(entry.get("run_name", "")),
            "workdir": str(entry.get("workdir", "")),
            "started_at": str(entry.get("started_at", "")),
        }
    return pruned


def _write_active_runs(runs: dict[str, dict[str, Any]]) -> None:
    path = active_runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_prune_active_runs(runs), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_active_runs() -> dict[str, dict[str, Any]]:
    path = active_runs_path()
    runs = _read_active_runs_raw(path)
    pruned = _prune_active_runs(runs)
    if pruned != runs:
        _write_active_runs(pruned)
    return pruned


def register_active_run(
    run_id: str,
    identity: str,
    run_name: str,
    workdir: Path,
    *,
    pid: int | None = None,
    started_at: datetime | None = None,
) -> None:
    runs = read_active_runs()
    runs[run_id] = {
        "pid": int(pid if pid is not None else os.getpid()),
        "identity": identity,
        "run_name": run_name,
        "workdir": str(workdir),
        "started_at": (started_at or datetime.now(timezone.utc)).isoformat(),
    }
    _write_active_runs(runs)


def unregister_active_run(run_id: str) -> None:
    runs = read_active_runs()
    runs.pop(run_id, None)
    _write_active_runs(runs)


def scan_run_states(state_dir: Path) -> list[dict[str, Any]]:
    """Best-effort scan of every run state file, for the multi-run index artifact."""
    runs_dir = state_dir / "runs"
    entries: list[dict[str, Any]] = []
    try:
        paths = list(runs_dir.glob("*.json"))
    except OSError:
        return entries
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append(
            {
                "run_id": data.get("run_id", path.stem),
                "run_name": data.get("run_name", "ringer"),
                "identity": data.get("identity", "unknown"),
                "state": data.get("state", "finished" if data.get("finished") else "live"),
                "pass": data.get("pass", 0),
                "fail": data.get("fail", 0),
                "elapsed_s": data.get("elapsed_s", 0),
                "started_at": data.get("started_at", ""),
                "artifact_path": data.get("artifact_path"),
                "report_path": data.get("report_path"),
                "report_ready": data.get("report_ready", False),
                "mtime": mtime,
            }
        )
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    return entries


def read_json_object(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default
    return data if isinstance(data, dict) else default


def scan_hud_run_states(state_dir: Path, *, limit: int = 12) -> list[dict[str, Any]]:
    runs_dir = state_dir / "runs"
    try:
        paths = [path for path in runs_dir.glob("*.json") if path.is_file()]
    except OSError:
        return []

    def path_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    paths.sort(key=path_mtime, reverse=True)
    runs: list[dict[str, Any]] = []
    for path in paths[:limit]:
        data = read_json_object(path, {})
        if data:
            runs.append(data)
    return runs


def read_active_runs_file() -> dict[str, Any]:
    return read_json_object(active_runs_path(), {})
