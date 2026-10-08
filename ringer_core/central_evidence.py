"""Attempt identity, conversion, and transactional central evidence writes."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ATTEMPT_COLUMNS: tuple[str, ...] = (
    "attempt_uid", "source_host", "logged_at", "log_sink", "fallback_reason", "run_id",
    "task_key", "pattern", "task_type", "orchestrator", "worker_engine", "model",
    "expected_model", "reported_model", "reasoning_effort", "shepherd_model",
    "verify_method", "verdict", "retry", "duration_ms", "worker_tokens", "notes",
    "spec", "spec_sha256",
)
INSERT_SQL: str = (
    f"INSERT INTO ringer.attempts ({', '.join(ATTEMPT_COLUMNS)}) VALUES ("
    + ", ".join(f"%({column})s" for column in ATTEMPT_COLUMNS)
    + ") ON CONFLICT DO NOTHING"
)


def attempt_uid(source_host: str, row: Mapping[str, Any]) -> str:
    """Hash the exact identity fields recorded in local evidence."""
    identity = "|".join((source_host, row["logged_at"], row["run_id"], row["task_key"],
                         row.get("worker_engine") or ""))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def stamp(row: Mapping[str, Any], source_host: str,
          now: Callable[[], datetime] | None = None) -> dict:
    """Copy and stamp a row before sending it to either evidence sink."""
    out = dict(row)
    if "logged_at" not in out:
        timestamp = now() if now is not None else datetime.now(timezone.utc)
        out["logged_at"] = timestamp.astimezone(timezone.utc).isoformat()
    out["source_host"] = source_host
    out["attempt_uid"] = attempt_uid(source_host, out)
    return out


def apply_spec_policy(row: Mapping[str, Any], mode: str) -> dict:
    """Copy a row with the requested central prompt storage policy."""
    if mode not in ("hash", "excerpt"):
        raise ValueError("invalid spec storage mode; valid modes: hash, excerpt")
    out = dict(row)
    spec = row.get("spec")
    if spec == "[redacted request packet]":
        spec = None
    out["spec_sha256"] = hashlib.sha256(spec.encode("utf-8")).hexdigest() if spec else None
    out["spec"] = spec if mode == "excerpt" else None
    return out


def to_params(row: Mapping[str, Any], source_host: str,
              spec_storage: str = "hash") -> dict[str, Any]:
    """Validate a local attempt and convert it to exactly the insert columns."""
    for field in ("logged_at", "run_id", "task_key", "verdict"):
        if not isinstance(row.get(field), str) or not row[field]:
            raise ValueError(f"missing/invalid {field}")
    try:
        datetime.fromisoformat(row["logged_at"])
    except ValueError as exc:
        raise ValueError(f"invalid logged_at: {exc}") from exc
    for field in ("duration_ms", "worker_tokens"):
        value = row.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{field} not an integer: {value!r}")
    if row.get("retry") is not None and not isinstance(row["retry"], bool):
        raise ValueError(f"retry not boolean: {row['retry']!r}")
    out = {column: row.get(column) for column in ATTEMPT_COLUMNS}
    out["source_host"] = source_host
    out["attempt_uid"] = attempt_uid(source_host, row)
    out["log_sink"] = out["log_sink"] or "jsonl"
    return apply_spec_policy(out, spec_storage)


@dataclass(frozen=True)
class Credentials:
    host: str
    port: int
    user: str
    password: str
    dbname: str

    def connect_kwargs(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "user": self.user,
                "password": self.password, "dbname": self.dbname, "connect_timeout": 5}


def resolve_credentials(env: Mapping[str, str]) -> Credentials:
    """Resolve each database setting independently, preferring neutral names."""
    values = {suffix: env.get(f"RINGER_DB_{suffix}", "") or env.get(f"SUPABASE_DB_{suffix}", "")
              for suffix in ("HOST", "PORT", "USER", "PASSWORD", "NAME")}
    missing = [f"RINGER_DB_{suffix}" for suffix, value in values.items() if not value]
    if missing:
        raise ValueError(f"missing database settings: {', '.join(missing)}")
    try:
        port = int(values["PORT"])
    except ValueError as exc:
        raise ValueError("RINGER_DB_PORT not an integer") from exc
    return Credentials(values["HOST"], port, values["USER"], values["PASSWORD"], values["NAME"])


def connect(credentials: Credentials) -> Any:
    """Open a Postgres connection only when the backend is requested."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            'psycopg is required for the postgres backend; install with pip install "psycopg[binary]"'
        ) from exc
    return psycopg.connect(**credentials.connect_kwargs())


def read_jsonl_rows(path: Path) -> list[dict]:
    """Read objects strictly, reporting malformed rows without changing the file."""
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: not a JSON object")
            rows.append(row)
    return rows


@dataclass(frozen=True)
class PushResult:
    total: int
    inserted: int
    present: int


def push_rows(conn: Any, rows: Sequence[Mapping[str, Any]], source_host: str,
              spec_storage: str = "hash") -> PushResult:
    """Validate the entire batch before writing it in one transaction."""
    params = [to_params(row, source_host, spec_storage) for row in rows]
    inserted = 0
    try:
        with conn.cursor() as cur:
            for row in params:
                cur.execute(INSERT_SQL, row)
                if cur.rowcount > 0:
                    inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return PushResult(len(params), inserted, len(params) - inserted)
