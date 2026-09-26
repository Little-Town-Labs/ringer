from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ringer_core.state_files import atomic_write_json, read_active_runs


ARTIFACT_LIBRARY_MAX_VERSIONS = 20

def artifacts_dir(state_dir: Path) -> Path:
    return state_dir / "artifacts"

def artifact_library_path(state_dir: Path) -> Path:
    return artifacts_dir(state_dir) / "library.json"

def artifact_live_path(state_dir: Path, run_name: str) -> Path:
    return artifacts_dir(state_dir) / "live" / f"{sanitize_artifact_name(run_name)}.html"

def artifact_version_path(state_dir: Path, run_name: str, run_id: str) -> Path:
    return (
        artifacts_dir(state_dir)
        / "versions"
        / sanitize_artifact_name(run_name)
        / f"{sanitize_artifact_name(run_id)}.html"
    )

def artifact_deliverables_dir(state_dir: Path, run_id: str, task_key: str) -> Path:
    return (
        artifacts_dir(state_dir)
        / "deliverables"
        / sanitize_artifact_name(run_id)
        / sanitize_artifact_name(task_key)
    )

def read_artifact_library(state_dir: Path) -> dict[str, Any]:
    path = artifact_library_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"artifacts": {}}
    if not isinstance(data, dict):
        return {"artifacts": {}}
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        return {"artifacts": {}}
    clean: dict[str, Any] = {"artifacts": {}}
    for run_name, entry in artifacts.items():
        if isinstance(run_name, str) and isinstance(entry, dict):
            clean["artifacts"][run_name] = entry
    return clean

def write_artifact_library(state_dir: Path, library: dict[str, Any]) -> None:
    atomic_write_json(artifact_library_path(state_dir), library)

def artifact_outcome_from_state(state: dict[str, Any]) -> str:
    if str(state.get("state", "")) == "died":
        return "died"
    if not bool(state.get("finished")) and str(state.get("state", "live")) == "live":
        return "live"
    totals = state.get("totals") if isinstance(state.get("totals"), dict) else {}
    fail_n = int(totals.get("fail", state.get("fail", 0)) or 0)
    return "fail" if fail_n else "pass"

def _library_entry(
    *,
    state_dir: Path,
    run_name: str,
    run_id: str,
    identity: str,
    state: str,
    now_iso: str,
    existing: dict[str, Any] | None = None,
    artifact_enabled: bool = True,
) -> dict[str, Any]:
    versions = []
    if existing and isinstance(existing.get("versions"), list):
        versions = [item for item in existing["versions"] if isinstance(item, dict)]
    return {
        "live_path": str(artifact_live_path(state_dir, run_name)) if artifact_enabled else None,
        "state": state,
        "identity": identity,
        "current_run_id": run_id,
        "updated_at": now_iso,
        "versions": versions,
    }

def update_artifact_library_live(
    state_dir: Path,
    *,
    run_name: str,
    run_id: str,
    identity: str,
    state: str,
    artifact_enabled: bool = True,
    now: datetime | None = None,
) -> None:
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    library = read_artifact_library(state_dir)
    artifacts = library.setdefault("artifacts", {})
    existing = artifacts.get(run_name) if isinstance(artifacts.get(run_name), dict) else None
    artifacts[run_name] = _library_entry(
        state_dir=state_dir,
        run_name=run_name,
        run_id=run_id,
        identity=identity,
        state=state,
        now_iso=now_iso,
        existing=existing,
        artifact_enabled=artifact_enabled,
    )
    write_artifact_library(state_dir, library)

def append_artifact_library_version(
    state_dir: Path,
    *,
    run_name: str,
    run_id: str,
    identity: str,
    outcome: str,
    version_path: Path | None,
    report_path: Path | None,
    artifact_enabled: bool = True,
    tasks_pass: int,
    tasks_fail: int,
    deliverables: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    library = read_artifact_library(state_dir)
    artifacts = library.setdefault("artifacts", {})
    existing = artifacts.get(run_name) if isinstance(artifacts.get(run_name), dict) else None
    entry = _library_entry(
        state_dir=state_dir,
        run_name=run_name,
        run_id=run_id,
        identity=identity,
        state=outcome,
        now_iso=now_iso,
        existing=existing,
        artifact_enabled=artifact_enabled,
    )
    new_version = {
        "run_id": run_id,
        "path": str(version_path) if version_path is not None else None,
        "report_path": str(report_path) if report_path is not None else None,
        "finished_at": now_iso,
        "outcome": outcome,
        "tasks_pass": tasks_pass,
        "tasks_fail": tasks_fail,
        "deliverables": [dict(item) for item in deliverables or []],
    }
    versions = [new_version]
    for version in entry["versions"]:
        if version.get("run_id") != run_id:
            versions.append(version)
    entry["versions"] = versions[:ARTIFACT_LIBRARY_MAX_VERSIONS]
    artifacts[run_name] = entry
    write_artifact_library(state_dir, library)
    prune_artifact_versions(state_dir, versions[ARTIFACT_LIBRARY_MAX_VERSIONS:])

def prune_artifact_versions(state_dir: Path, versions: list[dict[str, Any]]) -> None:
    root = artifacts_dir(state_dir).resolve()
    for version in versions:
        for key in ("path", "report_path"):
            raw = version.get(key)
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            with contextlib.suppress(OSError):
                resolved = path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                if resolved.is_file():
                    resolved.unlink()
                    with contextlib.suppress(OSError):
                        resolved.parent.rmdir()

def reconcile_artifact_library_dead_runs(state_dir: Path) -> None:
    library = read_artifact_library(state_dir)
    artifacts = library.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return
    active = read_active_runs()
    changed = False
    now_iso = datetime.now(timezone.utc).isoformat()
    for entry in artifacts.values():
        if not isinstance(entry, dict) or entry.get("state") != "live":
            continue
        run_id = str(entry.get("current_run_id", ""))
        if not run_id or run_id not in active:
            entry["state"] = "died"
            entry["updated_at"] = now_iso
            changed = True
    if changed:
        write_artifact_library(state_dir, library)

def sanitize_artifact_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return sanitized or "artifact"

def state_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = state.get("tasks") or []
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]

def collect_state_deliverables(state: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for task in state_tasks(state):
        task_key = str(task.get("key", "task"))
        deliverables = task.get("deliverables") or []
        if not isinstance(deliverables, list):
            continue
        for item in deliverables:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            path = str(item.get("path", "")).strip()
            if not name or not path:
                continue
            try:
                size = int(item.get("bytes", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            items.append(
                {
                    "task_key": task_key,
                    "name": name,
                    "path": path,
                    "bytes": size,
                }
            )
    return items
