from __future__ import annotations

import contextlib
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from ringer_core.state_files import utc_now_iso
from ringer_core.catalog import default_catalog_path, load_catalog_snapshot
from ringer_core.read_model import (db_attempt_rows, db_catalog_models, default_read_model_db_path, should_use_read_model_db, sync_read_model_db)
from ringer_core.models import (aggregate_model_log_rows, aggregate_model_scoreboard_rows, catalog_models_by_id, default_model_notes_path, default_model_registry_path, enrich_model_groups_with_identity, enrich_model_groups_with_notes, load_model_identity_registry, order_model_scoreboard_rows, parse_model_notes_sections, read_model_log_rows)

MODEL_SCOREBOARD_COLUMNS = (
    "Model",
    "Lab",
    "Harness",
    "API/Plan",
    "Tier",
    "Tasks",
    "First try",
    "Pass",
    "Tokens (median)",
    "Speed (median)",
    "Last used",
    "Notes",
)

def build_models_api_payload(
    *,
    log_path: Path,
    default_log_path: Path | None = None,
    db_path: Path | None = None,
    catalog_path: Path | None = None,
    registry_path: Path | None = None,
    notes_path: Path | None = None,
) -> dict[str, Any]:
    log_path = log_path.expanduser().resolve()
    default_log_path = (default_log_path or log_path).expanduser().resolve()
    explicit_db = db_path is not None
    resolved_db_path = (db_path or default_read_model_db_path()).expanduser().resolve()
    catalog_path = (catalog_path or default_catalog_path()).expanduser().resolve()
    registry_path = (registry_path or default_model_registry_path()).expanduser().resolve()
    notes_path = (notes_path or default_model_notes_path()).expanduser().resolve()
    using_db = should_use_read_model_db(
        log_path=log_path,
        default_log_path=default_log_path,
        explicit_db=explicit_db,
    )
    catalog_models: list[dict[str, Any]] = []
    if using_db:
        try:
            sync_read_model_db(
                resolved_db_path,
                log_path,
                catalog_path=catalog_path,
                registry_path=registry_path,
            )
            rows, identity_registry = db_attempt_rows(resolved_db_path)
            disk_registry = load_model_identity_registry(registry_path)
            identity_registry = dataclass_replace(
                identity_registry,
                noncanonical_routes=disk_registry.noncanonical_routes,
            )
            catalog_models = db_catalog_models(resolved_db_path)
        except Exception:
            using_db = False
            rows, _skipped = read_model_log_rows(log_path)
            identity_registry = load_model_identity_registry(registry_path)
    else:
        rows, _skipped = read_model_log_rows(log_path)
        identity_registry = load_model_identity_registry(registry_path)
    if not using_db:
        with contextlib.suppress(Exception):
            catalog_models = load_catalog_snapshot(catalog_path)
    notes_sections = parse_model_notes_sections(notes_path)
    groups = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_log_rows(rows),
            rows,
            identity_registry,
            include_task_type=True,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    rollup = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_scoreboard_rows(rows),
            rows,
            identity_registry,
            include_task_type=False,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    catalog_by_id = catalog_models_by_id(catalog_models)
    ordered_rollup: list[dict[str, Any]] = []
    for row in order_model_scoreboard_rows(rollup, catalog_by_id):
        item = dict(row)
        item.setdefault("task_type", "(all)")
        ordered_rollup.append(item)
    return {
        "generated_at": utc_now_iso(),
        "columns": list(MODEL_SCOREBOARD_COLUMNS),
        "groups": groups,
        "rollup": ordered_rollup,
    }
