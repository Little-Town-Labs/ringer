from __future__ import annotations

import contextlib
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import sqlite3
except Exception:  # pragma: no cover - exercised by monkeypatch in tests.
    sqlite3 = None  # type: ignore[assignment]

from ringer_core.state_files import ringer_home
from ringer_core.catalog import catalog_changes_path, default_catalog_path, load_catalog_snapshot
from ringer_core.models import (
    ModelIdentity, ModelIdentityRegistry, default_model_registry_path,
    group_model_log_tasks, load_model_identity_registry, model_log_int,
    model_log_row_engine, model_log_row_is_retry, model_log_row_reasoning_effort,
    model_log_text, normalize_catalog_for_scoreboard, parse_log_date,
)


def default_read_model_db_path() -> Path:
    return ringer_home() / "ringer.db"


def should_use_read_model_db(
    *,
    log_path: Path,
    default_log_path: Path,
    explicit_db: bool,
) -> bool:
    if explicit_db:
        return True
    return log_path.expanduser().resolve() == default_log_path.expanduser().resolve()


def ensure_sqlite_available() -> Any:
    if sqlite3 is None:
        raise RuntimeError("sqlite3 is unavailable")
    return sqlite3


def connect_read_model_db(path: Path) -> Any:
    sqlite = ensure_sqlite_available()
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite.connect(str(path))
    conn.row_factory = sqlite.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect_read_model_db_readonly(path: Path) -> Any:
    sqlite = ensure_sqlite_available()
    path = path.expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"read model database missing: {path}")
    uri_path = urllib.parse.quote(path.as_posix(), safe="/")
    conn = sqlite.connect(f"file:{uri_path}?mode=ro", uri=True)
    conn.row_factory = sqlite.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def read_model_table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def read_model_column_exists(conn: Any, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def create_read_model_schema(conn: Any) -> None:
    schema_table_exists = read_model_table_exists(conn, "schema_version")
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    schema_version = None
    if schema_table_exists:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is not None:
            schema_version = int(row[0])
    needs_stamp = user_version != 3 or schema_version != 3
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY,
            run_id TEXT,
            task_key TEXT,
            logged_at TEXT,
            engine TEXT,
            model TEXT,
            reported_model TEXT,
            expected_model TEXT,
            reasoning_effort TEXT,
            task_type TEXT,
            retry INTEGER,
            verdict TEXT,
            duration_ms INTEGER,
            worker_tokens INTEGER,
            orchestrator TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_model_task_type
            ON attempts(model, task_type);
        CREATE INDEX IF NOT EXISTS idx_attempts_logged_at
            ON attempts(logged_at);

        CREATE TABLE IF NOT EXISTS catalog_models (
            id TEXT PRIMARY KEY,
            name TEXT,
            context_length INTEGER,
            prompt_per_m REAL,
            completion_per_m REAL,
            free INTEGER,
            variable_pricing INTEGER,
            pricing_unknown INTEGER,
            fetched_at TEXT,
            modality TEXT
        );
        CREATE TABLE IF NOT EXISTS catalog_events (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            kind TEXT,
            model_id TEXT,
            payload TEXT
        );
        CREATE TABLE IF NOT EXISTS identity (
            engine TEXT NOT NULL,
            model_key TEXT NOT NULL,
            model_display TEXT,
            lab TEXT,
            harness TEXT,
            access TEXT,
            alias INTEGER,
            confidence TEXT,
            source TEXT,
            last_verified TEXT,
            PRIMARY KEY (engine, model_key)
        );
        CREATE TABLE IF NOT EXISTS identity_defaults (
            engine TEXT PRIMARY KEY,
            default_model_key TEXT,
            harness TEXT,
            access TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    if not read_model_column_exists(conn, "attempts", "reasoning_effort"):
        conn.execute("ALTER TABLE attempts ADD COLUMN reasoning_effort TEXT")
    if not read_model_column_exists(conn, "attempts", "reported_model"):
        conn.execute("ALTER TABLE attempts ADD COLUMN reported_model TEXT")
    if not read_model_column_exists(conn, "attempts", "expected_model"):
        conn.execute("ALTER TABLE attempts ADD COLUMN expected_model TEXT")
    if not read_model_column_exists(conn, "identity", "lab"):
        conn.execute("ALTER TABLE identity ADD COLUMN lab TEXT")
    if not read_model_column_exists(conn, "identity", "alias"):
        conn.execute("ALTER TABLE identity ADD COLUMN alias INTEGER")
    if not read_model_column_exists(conn, "identity", "last_verified"):
        conn.execute("ALTER TABLE identity ADD COLUMN last_verified TEXT")
    if needs_stamp:
        conn.executescript(
            """
            DELETE FROM schema_version;
            INSERT INTO schema_version(version) VALUES (3);
            PRAGMA user_version = 3;
            """
        )


def drop_read_model_tables(conn: Any) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS attempts;
        DROP TABLE IF EXISTS catalog_models;
        DROP TABLE IF EXISTS catalog_events;
        DROP TABLE IF EXISTS identity;
        DROP TABLE IF EXISTS identity_defaults;
        DROP TABLE IF EXISTS sync_state;
        DROP TABLE IF EXISTS schema_version;
        """
    )


def read_log_rows_from_offset(path: Path, offset: int) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    final_offset = offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            while True:
                line_start = fh.tell()
                raw_line = fh.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    final_offset = line_start
                    break
                final_offset = fh.tell()
                try:
                    line = raw_line.decode("utf-8")
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    skipped += 1
                    continue
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                rows.append(row)
    except FileNotFoundError:
        return [], 0, 0
    return rows, skipped, final_offset


def read_catalog_events_from_offset(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    final_offset = offset
    try:
        with path.expanduser().open("rb") as fh:
            fh.seek(offset)
            while True:
                line_start = fh.tell()
                raw_line = fh.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    final_offset = line_start
                    break
                final_offset = fh.tell()
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except FileNotFoundError:
        return [], 0
    return events, final_offset


def insert_attempt_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    payloads: list[tuple[Any, ...]] = []
    for row in rows:
        payloads.append(
            (
                model_log_text(row.get("run_id")),
                model_log_text(row.get("task_key")),
                model_log_text(row.get("logged_at")),
                model_log_row_engine(row),
                model_log_text(row.get("model")),
                model_log_text(row.get("reported_model")) or None,
                model_log_text(row.get("expected_model")) or None,
                model_log_row_reasoning_effort(row),
                model_log_text(row.get("task_type")),
                1 if model_log_row_is_retry(row) else 0,
                model_log_text(row.get("verdict")),
                model_log_int(row.get("duration_ms")),
                model_log_int(row.get("worker_tokens")),
                model_log_text(row.get("orchestrator")),
            )
        )
    if payloads:
        conn.executemany(
            """
            INSERT INTO attempts (
                run_id, task_key, logged_at, engine, model, reported_model, expected_model,
                reasoning_effort, task_type, retry,
                verdict, duration_ms, worker_tokens, orchestrator
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payloads,
        )
    return len(payloads)


def read_sync_state_value(conn: Any, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def read_sync_state_int(conn: Any, key: str, default: int = 0) -> int:
    value = read_sync_state_value(conn, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def write_sync_state_values(conn: Any, values: dict[str, int | str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, ?)",
        [(key, str(value)) for key, value in values.items()],
    )


def file_sync_metadata(path: Path) -> tuple[int, int]:
    try:
        stat = path.expanduser().stat()
    except FileNotFoundError:
        return -1, 0
    return int(stat.st_mtime_ns), int(stat.st_size)


def insert_catalog_event_rows(conn: Any, events: list[dict[str, Any]]) -> int:
    event_payloads: list[tuple[Any, ...]] = []
    for event in events:
        event_payloads.append(
            (
                model_log_text(event.get("ts")),
                model_log_text(event.get("kind")),
                model_log_text(event.get("id")),
                json.dumps(event, sort_keys=True),
            )
        )
    if event_payloads:
        conn.executemany(
            "INSERT INTO catalog_events(ts, kind, model_id, payload) VALUES (?, ?, ?, ?)",
            event_payloads,
        )
    return len(event_payloads)


def refresh_catalog_tables(conn: Any, catalog_path: Path) -> None:
    catalog_path = catalog_path.expanduser().resolve()
    changes_path = catalog_changes_path(catalog_path)
    catalog_mtime, catalog_size = file_sync_metadata(catalog_path)
    changes_mtime, changes_size = file_sync_metadata(changes_path)
    catalog_unchanged = (
        read_sync_state_int(conn, "catalog_snapshot_mtime_ns", -2) == catalog_mtime
        and read_sync_state_int(conn, "catalog_snapshot_size", -2) == catalog_size
    )
    changes_unchanged = (
        read_sync_state_int(conn, "catalog_changes_mtime_ns", -2) == changes_mtime
        and read_sync_state_int(conn, "catalog_changes_size", -2) == changes_size
    )
    if catalog_unchanged and changes_unchanged:
        return

    if not catalog_unchanged:
        conn.execute("DELETE FROM catalog_models")
        try:
            catalog_models = load_catalog_snapshot(catalog_path)
        except (OSError, json.JSONDecodeError, ValueError):
            catalog_models = []
        payloads: list[tuple[Any, ...]] = []
        for model in catalog_models:
            normalized = normalize_catalog_for_scoreboard(model)
            model_id = model_log_text(normalized.get("id"))
            if not model_id:
                continue
            payloads.append(
                (
                    model_id,
                    model_log_text(normalized.get("name")),
                    model_log_int(normalized.get("context_length")),
                    normalized.get("prompt_per_m"),
                    normalized.get("completion_per_m"),
                    1 if normalized.get("free") else 0,
                    1 if normalized.get("variable_pricing") else 0,
                    1 if normalized.get("pricing_unknown") else 0,
                    model_log_text(normalized.get("fetched_at")),
                    model_log_text(normalized.get("modality")),
                )
            )
        if payloads:
            conn.executemany(
                """
                INSERT INTO catalog_models (
                    id, name, context_length, prompt_per_m, completion_per_m, free,
                    variable_pricing, pricing_unknown, fetched_at, modality
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payloads,
            )
        write_sync_state_values(
            conn,
            {
                "catalog_snapshot_mtime_ns": catalog_mtime,
                "catalog_snapshot_size": catalog_size,
            },
        )

    if not changes_unchanged:
        previous_changes_mtime = read_sync_state_value(conn, "catalog_changes_mtime_ns")
        previous_changes_size = read_sync_state_int(conn, "catalog_changes_size", 0)
        previous_offset = read_sync_state_int(conn, "catalog_changes_offset", 0)
        append_only = (
            previous_changes_mtime is not None
            and changes_size >= previous_changes_size
            and previous_offset <= changes_size
        )
        if append_only:
            events, new_offset = read_catalog_events_from_offset(changes_path, previous_offset)
        else:
            conn.execute("DELETE FROM catalog_events")
            events, new_offset = read_catalog_events_from_offset(changes_path, 0)
        insert_catalog_event_rows(conn, events)
        write_sync_state_values(
            conn,
            {
                "catalog_changes_mtime_ns": changes_mtime,
                "catalog_changes_size": changes_size,
                "catalog_changes_offset": new_offset,
            },
        )


def refresh_identity_tables(conn: Any, registry_path: Path) -> None:
    registry = load_model_identity_registry(registry_path)
    conn.execute("DELETE FROM identity")
    conn.execute("DELETE FROM identity_defaults")
    if registry.identities:
        conn.executemany(
            """
            INSERT INTO identity (
                engine, model_key, model_display, lab, harness, access, alias, confidence, source,
                last_verified
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    engine,
                    model_key,
                    identity.model_display,
                    identity.lab,
                    identity.harness,
                    identity.access,
                    1 if identity.alias else 0,
                    identity.confidence,
                    identity.source,
                    identity.last_verified,
                )
                for (engine, model_key), identity in sorted(registry.identities.items())
            ],
        )
    if registry.engine_meta:
        conn.executemany(
            """
            INSERT INTO identity_defaults(engine, default_model_key, harness, access)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    engine,
                    registry.defaults.get(engine, ""),
                    identity.harness,
                    identity.access,
                )
                for engine, identity in sorted(registry.engine_meta.items())
            ],
        )


@dataclass(frozen=True)
class ReadModelSyncResult:
    db_path: Path
    log_path: Path
    attempts_inserted: int
    skipped: int
    offset: int
    rebuilt: bool


def rebuild_read_model_db(
    db_path: Path,
    log_path: Path,
    *,
    catalog_path: Path | None = None,
    registry_path: Path | None = None,
) -> ReadModelSyncResult:
    db_path = db_path.expanduser().resolve()
    log_path = log_path.expanduser().resolve()
    catalog_path = (catalog_path or default_catalog_path()).expanduser().resolve()
    registry_path = (registry_path or default_model_registry_path()).expanduser().resolve()
    with contextlib.closing(connect_read_model_db(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            drop_read_model_tables(conn)
            create_read_model_schema(conn)
            rows, skipped, offset = read_log_rows_from_offset(log_path, 0)
            inserted = insert_attempt_rows(conn, rows)
            refresh_catalog_tables(conn, catalog_path)
            refresh_identity_tables(conn, registry_path)
            write_sync_state_values(conn, {"log_offset": offset})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ReadModelSyncResult(db_path, log_path, inserted, skipped, offset, True)


def sync_read_model_db(
    db_path: Path,
    log_path: Path,
    *,
    catalog_path: Path | None = None,
    registry_path: Path | None = None,
) -> ReadModelSyncResult:
    db_path = db_path.expanduser().resolve()
    log_path = log_path.expanduser().resolve()
    catalog_path = (catalog_path or default_catalog_path()).expanduser().resolve()
    registry_path = (registry_path or default_model_registry_path()).expanduser().resolve()
    try:
        log_size = log_path.stat().st_size
    except FileNotFoundError:
        log_size = 0
    with contextlib.closing(connect_read_model_db(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            create_read_model_schema(conn)
            offset = read_sync_state_int(conn, "log_offset", 0)
            if log_size < offset:
                conn.rollback()
                return rebuild_read_model_db(
                    db_path,
                    log_path,
                    catalog_path=catalog_path,
                    registry_path=registry_path,
                )
            rows, skipped, new_offset = read_log_rows_from_offset(log_path, offset)
            inserted = insert_attempt_rows(conn, rows)
            refresh_catalog_tables(conn, catalog_path)
            refresh_identity_tables(conn, registry_path)
            write_sync_state_values(conn, {"log_offset": new_offset})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ReadModelSyncResult(db_path, log_path, inserted, skipped, new_offset, False)


def load_identity_registry_from_db(conn: Any) -> ModelIdentityRegistry:
    identities: dict[tuple[str, str], ModelIdentity] = {}
    defaults: dict[str, str] = {}
    engine_meta: dict[str, ModelIdentity] = {}
    for row in conn.execute("SELECT * FROM identity"):
        engine = model_log_text(row["engine"])
        model_key = model_log_text(row["model_key"])
        if not engine or not model_key:
            continue
        identities[(engine, model_key)] = ModelIdentity(
            model_display=model_log_text(row["model_display"]) or model_key,
            lab=model_log_text(row["lab"]) or "(unknown)",
            harness=model_log_text(row["harness"]) or engine,
            access=model_log_text(row["access"]) or "unknown",
            alias=bool(row["alias"]),
            confidence=model_log_text(row["confidence"]),
            source=model_log_text(row["source"]),
            last_verified=model_log_text(row["last_verified"]),
        )
    for row in conn.execute("SELECT * FROM identity_defaults"):
        engine = model_log_text(row["engine"])
        if not engine:
            continue
        defaults[engine] = model_log_text(row["default_model_key"])
        engine_meta[engine] = ModelIdentity(
            model_display=engine,
            lab="(unknown)",
            harness=model_log_text(row["harness"]) or engine,
            access=model_log_text(row["access"]) or "unknown",
            confidence="engine",
            source="",
        )
    return ModelIdentityRegistry(identities, defaults, engine_meta, {})


def db_attempt_rows(
    db_path: Path,
    *,
    since: str | None = None,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], ModelIdentityRegistry]:
    with contextlib.closing(connect_read_model_db_readonly(db_path)) as conn:
        query = """
            SELECT run_id, task_key, logged_at, engine, model, reported_model, expected_model,
                   reasoning_effort, task_type, retry,
                   verdict, duration_ms, worker_tokens, orchestrator
            FROM attempts
        """
        params: list[Any] = []
        if engine is not None:
            query += " WHERE engine = ?"
            params.append(engine)
        query += " ORDER BY id"
        rows = [
            {
                "run_id": row["run_id"],
                "task_key": row["task_key"],
                "logged_at": row["logged_at"],
                "worker_engine": row["engine"],
                "model": row["model"],
                "reported_model": row["reported_model"],
                "expected_model": row["expected_model"],
                "reasoning_effort": row["reasoning_effort"],
                "task_type": row["task_type"],
                "retry": bool(row["retry"]),
                "verdict": row["verdict"],
                "duration_ms": row["duration_ms"],
                "worker_tokens": row["worker_tokens"],
                "orchestrator": row["orchestrator"],
            }
            for row in conn.execute(query, params)
        ]
        registry = load_identity_registry_from_db(conn)
    if since is not None:
        selected_row_ids: set[int] = set()
        for task_rows in group_model_log_tasks(rows):
            ordered = sorted(
                task_rows,
                key=lambda row: (
                    model_log_text(row.get("logged_at")),
                    1 if model_log_row_is_retry(row) else 0,
                ),
            )
            final_date = parse_log_date(ordered[-1].get("logged_at"))
            if final_date and final_date >= since:
                selected_row_ids.update(id(row) for row in task_rows)
        rows = [row for row in rows if id(row) in selected_row_ids]
    return rows, registry


def db_catalog_models(db_path: Path) -> list[dict[str, Any]]:
    with contextlib.closing(connect_read_model_db_readonly(db_path)) as conn:
        rows = [
            {
                "id": row["id"],
                "name": row["name"],
                "context_length": row["context_length"],
                "prompt_per_m": row["prompt_per_m"],
                "completion_per_m": row["completion_per_m"],
                "free": bool(row["free"]),
                "variable_pricing": bool(row["variable_pricing"]),
                "pricing_unknown": bool(row["pricing_unknown"]),
                "fetched_at": row["fetched_at"],
                "modality": row["modality"],
            }
            for row in conn.execute("SELECT * FROM catalog_models ORDER BY id")
        ]
        return rows


def db_catalog_events(db_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    with contextlib.closing(connect_read_model_db_readonly(db_path)) as conn:
        rows = conn.execute(
            "SELECT payload FROM catalog_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            event = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
