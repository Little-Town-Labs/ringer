"""Command-line operations for local and central evaluation evidence."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from ringer_core import central_evidence


def _timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _date_range(rows: list[dict]) -> str:
    if not rows:
        return "date range: none"
    dates = [_timestamp(row["logged_at"]) for row in rows]
    return f"date range: {min(dates).isoformat()} to {max(dates).isoformat()}"


def run_evidence_command(
    config: Any,
    args: Any,
    *,
    read_env: Callable[[Path], dict[str, str]],
    connect: Callable[..., Any] = central_evidence.connect,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    paths = args.file or [config.eval.jsonl_path]
    files: list[tuple[Path, list[dict]]] = []
    for path in paths:
        if not path.is_file():
            print(f"evidence: file not found: {path}", file=err)
            return 2
        try:
            rows = central_evidence.read_jsonl_rows(path)
        except (OSError, ValueError) as exc:
            if args.evidence_command == "status":
                print(f"evidence status: {path}", file=out)
            print(f"evidence: {exc}", file=err)
            if args.evidence_command == "push":
                return 2
            continue
        files.append((path, rows))

    if args.evidence_command == "status":
        totals = Counter()
        latest: list[tuple[datetime, str]] = []
        for path, rows in files:
            print(f"evidence status: {path}", file=out)
            try:
                confirmed = sum(row.get("log_sink") == "postgres" for row in rows)
                local = len(rows) - confirmed
                dates = _date_range(rows)
                reasons = [(_timestamp(row["logged_at"]), row["fallback_reason"])
                           for row in rows if row.get("fallback_reason")]
            except (KeyError, TypeError, ValueError) as exc:
                print(f"evidence status: {path}: invalid row: {exc}", file=err)
                return 2
            totals.update(rows=len(rows), confirmed=confirmed, local=local)
            print(f"rows: {len(rows)}", file=out)
            print(dates, file=out)
            print(f"confirmed central (log_sink=postgres): {confirmed}", file=out)
            print(f"local-only (log_sink=jsonl): {local}", file=out)
            if reasons:
                latest.extend(reasons)
        if len(files) != len(paths):
            return 2
        print(f"rows: {totals['rows']}", file=out)
        print(_date_range([row for _, rows in files for row in rows]), file=out)
        print(f"confirmed central (log_sink=postgres): {totals['confirmed']}", file=out)
        print(f"local-only (log_sink=jsonl): {totals['local']}", file=out)
        print(f"latest fallback_reason: {max(latest, default=(None, 'none'))[1]}", file=out)
        if totals["local"]:
            print("run: ringer evidence push", file=out)
        return 0

    try:
        since = _timestamp(args.since) if args.since else None
    except ValueError as exc:
        print(f"evidence push: invalid --since {args.since!r}: {exc}", file=err)
        return 2
    postgres = config.eval.postgres
    source_host = args.source_host or central_evidence.resolve_source_host(
        postgres.source_host if postgres else None
    )
    spec_storage = args.spec_storage or (postgres.spec_storage if postgres else "hash")
    selected: list[tuple[Path, list[dict], list[dict], list[dict[str, Any]]]] = []
    for path, rows in files:
        try:
            numbered = [(number, row) for number, row in enumerate(rows, 1)
                        if since is None or _timestamp(row["logged_at"]) >= since]
        except (KeyError, TypeError, ValueError) as exc:
            print(f"evidence push: {path}: invalid logged_at: {exc}", file=err)
            return 2
        chosen = [row for _, row in numbered]
        params = []
        for number, row in numbered:
            try:
                params.append(central_evidence.to_params(row, source_host, spec_storage))
            except ValueError as exc:
                print(f"evidence push: {path}:{number}: {exc}", file=err)
                return 2
        selected.append((path, rows, chosen, params))

    if args.dry_run:
        total_rows = 0
        for path, rows, chosen, params in selected:
            print(f"evidence push: {path}: rows={len(rows)} selected={len(params)}", file=out)
            total_rows += len(params)
        print(f"source host: {source_host}", file=out)
        print(f"spec storage: {spec_storage}", file=out)
        all_rows = [row for _, _, chosen, _ in selected for row in chosen]
        print(f"selected rows: {len(all_rows)}", file=out)
        print(_date_range(all_rows), file=out)
        verdicts = Counter(row.get("verdict", "unknown") for _, _, chosen, _ in selected for row in chosen)
        print("verdicts: " + (", ".join(f"{key}={value}" for key, value in sorted(verdicts.items())) or "none"), file=out)
        print("DRY RUN: nothing sent", file=out)
        return 0

    if postgres is None:
        print("evidence push requires [eval.postgres] with an env_file", file=err)
        return 2
    try:
        credentials = central_evidence.resolve_credentials(read_env(postgres.env_file))
    except (ValueError, OSError) as exc:
        print(f"evidence push: {exc}", file=err)
        return 2
    try:
        conn = connect(credentials)
    except RuntimeError as exc:
        print(f"evidence push: {exc}", file=err)
        return 2
    except Exception as exc:
        print(f"evidence push: database connection failed: {exc}", file=err)
        return 3
    inserted_total = present_total = total = 0
    try:
        print(f"connected to {credentials.host}:{credentials.port}/{credentials.dbname} as {credentials.user}", file=out)
        for path, all_rows, rows, params in selected:
            try:
                result = central_evidence.push_rows(conn, rows, source_host, spec_storage)
            except Exception as exc:
                print(f"evidence push: {path}: database write failed: {exc}", file=err)
                return 3
            print(f"evidence push: {path}: inserted {result.inserted}, already present {result.present}", file=out)
            inserted_total += result.inserted
            present_total += result.present
            total += result.total
        print(f"evidence push: inserted {inserted_total}, already present {present_total} (of {total} rows)", file=out)
        return 0
    finally:
        conn.close()
