"""Safe local JSONL evidence appends for cooperating Ringer processes."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append one serialized record, refusing to extend a corrupt final row."""
    payload = json.dumps(row, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    except OSError as exc:
        raise OSError(exc.errno, f"could not open evidence file {path}: {exc}", str(path)) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        original_size = os.fstat(fd).st_size
        if original_size:
            with open(path, "rb") as existing:
                existing.seek(original_size - 1)
                if existing.read(1) != b"\n":
                    raise ValueError(f"evidence tail in {path} is incomplete; preserve and repair it manually")
                start = max(0, original_size - 2)
                while start > 0:
                    existing.seek(start - 1)
                    if existing.read(1) == b"\n":
                        break
                    start -= 1
                existing.seek(start)
                tail = existing.read(original_size - start - 1)
                try:
                    final = json.loads(tail)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"evidence tail in {path} is malformed; preserve and repair it manually") from exc
                if not isinstance(final, dict):
                    raise ValueError(f"evidence tail in {path} is not a JSON object")
        written = 0
        try:
            while written < len(payload):
                try:
                    count = os.write(fd, payload[written:])
                except InterruptedError:
                    continue
                if count <= 0:
                    raise OSError("write returned no progress")
                written += count
        except OSError as exc:
            try:
                os.ftruncate(fd, original_size)
            except OSError as rollback:
                raise OSError(f"evidence append failed for {path}: {exc}; rollback also failed: {rollback}") from exc
            raise OSError(f"evidence append failed for {path}: {exc}") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
