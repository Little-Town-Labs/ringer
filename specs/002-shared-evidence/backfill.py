#!/usr/bin/env python3
"""One-off backfill of local Ringer JSONL evidence into ringer.attempts.

Spec 002-shared-evidence, R2/R3/R5. Dry-run by default; --apply writes.
Uses the identity and spec policy the shipped `evidence push` will use, so it
can be replaced by that command later. Reads JSONL only; never modifies it.

  python3 backfill.py FILE [FILE ...] [--existing uids.txt]
  python3 backfill.py FILE ... --apply --env-file ~/.ringer/ringer-db.env
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import socket
import sys
from datetime import datetime
from pathlib import Path

REQUIRED = ("logged_at", "run_id", "task_key", "verdict")
TEXT = ("pattern", "task_type", "orchestrator", "worker_engine", "model", "expected_model",
        "reported_model", "reasoning_effort", "shepherd_model", "verify_method", "notes",
        "log_sink", "fallback_reason")
COLUMNS = ("attempt_uid", "source_host", "logged_at", "log_sink", "fallback_reason", "run_id",
           "task_key", "pattern", "task_type", "orchestrator", "worker_engine", "model",
           "expected_model", "reported_model", "reasoning_effort", "shepherd_model",
           "verify_method", "verdict", "retry", "duration_ms", "worker_tokens", "notes",
           "spec", "spec_sha256")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def attempt_uid(host: str, row: dict) -> str:
    # R3: exact logged_at string as written to JSONL.
    return sha("|".join((host, row["logged_at"], row["run_id"], row["task_key"],
                         row.get("worker_engine") or "")))


def convert(row: dict, host: str, spec_storage: str) -> dict:
    for key in REQUIRED:
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"missing/invalid {key}")
    datetime.fromisoformat(row["logged_at"])
    out = {k: row.get(k) for k in TEXT}
    for key in ("duration_ms", "worker_tokens"):
        value = row.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{key} not an integer: {value!r}")
        out[key] = value
    if row.get("retry") is not None and not isinstance(row["retry"], bool):
        raise ValueError(f"retry not boolean: {row['retry']!r}")
    out["retry"] = row.get("retry")
    out.update(logged_at=row["logged_at"], run_id=row["run_id"], task_key=row["task_key"],
               verdict=row["verdict"], source_host=host,
               attempt_uid=attempt_uid(host, row))
    spec = row.get("spec")
    out["spec_sha256"] = sha(spec) if spec else None
    out["spec"] = spec if (spec_storage == "full" and spec) else None
    out["log_sink"] = out["log_sink"] or "jsonl"
    return out


def load(paths: list[Path], host: str, spec_storage: str):
    rows, errors = [], []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    rows.append((str(path), convert(json.loads(line), host, spec_storage)))
                except (ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{path}:{number}: {exc}")
    return rows, errors


def report(rows, errors, existing: set[str]) -> int:
    uids = collections.Counter(r["attempt_uid"] for _, r in rows)
    dupes = {u: n for u, n in uids.items() if n > 1}
    new = [r for _, r in rows if r["attempt_uid"] not in existing]
    print(f"rows parsed:        {len(rows)}   errors: {len(errors)}")
    for name, count in collections.Counter(n for n, _ in rows).items():
        print(f"  {name}: {count}")
    stamps = sorted(r["logged_at"] for _, r in rows)
    if stamps:
        print(f"date range:         {stamps[0][:10]} -> {stamps[-1][:10]}")
    print(f"verdicts:           {dict(collections.Counter(r['verdict'] for _, r in rows))}")
    print(f"distinct hosts:     {sorted({r['source_host'] for _, r in rows})}")
    print(f"unique attempt_uid: {len(uids)}   duplicate uids: {len(dupes)}")
    print(f"already in db:      {len(rows) - len(new)}   would insert: {len(new)}")
    print(f"spec stored:        {sum(1 for _, r in rows if r['spec'])}   "
          f"spec_sha256 set: {sum(1 for _, r in rows if r['spec_sha256'])}")
    print(f"null model:         {sum(1 for _, r in rows if not r['model'])}   "
          f"null task_type: {sum(1 for _, r in rows if not r['task_type'])}")
    print(f"max notes length:   {max((len(r['notes'] or '') for _, r in rows), default=0)}")
    for message in errors[:10]:
        print("ERROR", message)
    for uid in list(dupes)[:5]:
        print("DUPLICATE", uid)
    if rows:
        sample = dict(rows[-1][1])
        sample["notes"] = (sample["notes"] or "")[:60] + "..."
        print("sample (last row):", json.dumps(sample, indent=1, default=str))
    return 1 if errors or dupes else 0


def apply(rows, env_file: Path) -> None:
    import psycopg  # imported late: dry-run needs no database driver

    env = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    placeholders = ", ".join(f"%({c})s" for c in COLUMNS)
    sql = (f"INSERT INTO ringer.attempts ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
           "ON CONFLICT DO NOTHING")
    with psycopg.connect(host=env["RINGER_DB_HOST"], port=int(env["RINGER_DB_PORT"]),
                         user=env["RINGER_DB_USER"], password=env["RINGER_DB_PASSWORD"],
                         dbname=env["RINGER_DB_NAME"], connect_timeout=5) as conn:
        inserted = 0
        with conn.cursor() as cur:
            for _, row in rows:
                cur.execute(sql, row)
                inserted += cur.rowcount
        conn.commit()
    print(f"inserted {inserted} of {len(rows)} rows ({len(rows) - inserted} already present)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--source-host", default=socket.gethostname().split(".")[0])
    parser.add_argument("--spec-storage", choices=("hash", "full"), default="hash")
    parser.add_argument("--existing", type=Path, help="file of attempt_uid values already in the db")
    parser.add_argument("--apply", action="store_true", help="write to the database")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    rows, errors = load(args.files, args.source_host, args.spec_storage)
    existing = set(args.existing.read_text().split()) if args.existing else set()
    status = report(rows, errors, existing)
    if not args.apply:
        print("\nDRY RUN: nothing written. Re-run with --apply --env-file to load.")
        return status
    if status or not args.env_file:
        print("refusing to apply: fix errors/duplicates and pass --env-file", file=sys.stderr)
        return 2
    apply(rows, args.env_file.expanduser())
    return 0


if __name__ == "__main__":
    sys.exit(main())
