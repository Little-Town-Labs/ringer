from __future__ import annotations

import base64
import json
import re
import tomllib
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ringer_core.catalog import RESERVED_FIXTURE_MODELS, normalize_catalog_model

UNATTRIBUTED_MODEL_DISPLAY = "(unattributed legacy rows)"

PROVEN_MIN_TASKS = 3

PROVEN_MIN_FIRST_TRY = 2 / 3

def proven_model_group(group: dict[str, Any]) -> bool:
    return (
        int(group.get("tasks") or 0) >= PROVEN_MIN_TASKS
        and float(group.get("first_try_pass_rate") or 0) >= PROVEN_MIN_FIRST_TRY
    )

def parse_log_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        return ""
    candidate = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return ""
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return ""
    return candidate

def validate_since_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not parse_log_date(value):
        raise ValueError("--since must be YYYY-MM-DD")
    return value

def model_log_row_is_retry(row: dict[str, Any]) -> bool:
    retry = row.get("retry")
    if isinstance(retry, bool):
        return retry
    if isinstance(retry, str) and retry.strip().lower() in {"true", "false"}:
        return retry.strip().lower() == "true"
    notes = row.get("notes", "")
    return isinstance(notes, str) and "retry=true" in notes

def model_log_text(value: Any) -> str:
    return "" if value is None else str(value).strip()

def model_log_row_model(row: dict[str, Any]) -> str:
    model = model_log_text(row.get("model"))
    if model:
        return model
    return model_log_text(row.get("worker_engine"))

def model_log_row_is_unattributed(row: dict[str, Any]) -> bool:
    return not model_log_text(row.get("model"))

def model_log_row_reasoning_effort(row: dict[str, Any]) -> str | None:
    effort = model_log_text(row.get("reasoning_effort"))
    return effort or None

def model_log_row_is_reserved_fixture(row: dict[str, Any]) -> bool:
    return model_log_text(row.get("model")) in RESERVED_FIXTURE_MODELS

def model_reasoning_effort_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str, bool]]:
    keys: set[tuple[str, str, bool]] = set()
    for row in rows:
        if model_log_row_is_reserved_fixture(row):
            continue
        if (
            not model_log_row_is_unattributed(row)
            and model_log_row_reasoning_effort(row) is not None
        ):
            keys.add(
                (
                    model_log_row_engine(row),
                    model_log_row_model(row),
                    model_log_row_is_unattributed(row),
                )
            )
    return keys

def model_log_row_task_type(row: dict[str, Any]) -> str:
    task_type = model_log_text(row.get("task_type"))
    return task_type or "(untyped)"

def model_log_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def median_int(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2

def model_log_task_base_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, bool] | None:
    run_id = model_log_text(row.get("run_id"))
    task_key = model_log_text(row.get("task_key"))
    if not run_id or not task_key:
        return None
    unattributed = model_log_row_is_unattributed(row)
    return (
        run_id,
        task_key,
        model_log_row_model(row),
        model_log_row_task_type(row),
        "" if unattributed else (model_log_row_reasoning_effort(row) or ""),
        unattributed,
    )

def group_model_log_tasks(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: list[list[dict[str, Any]]] = []
    active_by_key: dict[tuple[str, str, str, str, str, bool], int] = {}
    for row in rows:
        key = model_log_task_base_key(row)
        if key is not None and model_log_row_is_retry(row) and key in active_by_key:
            grouped[active_by_key[key]].append(row)
            continue
        grouped.append([row])
        if key is not None:
            active_by_key[key] = len(grouped) - 1
    return grouped

def read_model_log_rows(
    path: Path,
    *,
    since: str | None = None,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        fh = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return rows, skipped
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue
            if engine is not None and model_log_text(row.get("worker_engine")) != engine:
                continue
            rows.append(row)
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
    return rows, skipped

def aggregate_model_log_rows(
    rows: list[dict[str, Any]],
    *,
    task_type: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, bool], dict[str, Any]] = {}
    effort_keys = model_reasoning_effort_keys(rows)
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        first = ordered[0]
        final = ordered[-1]
        if model_log_row_is_reserved_fixture(final):
            continue
        group_engine = model_log_row_engine(final)
        group_model = model_log_row_model(final)
        group_task_type = model_log_row_task_type(final)
        unattributed = model_log_row_is_unattributed(final)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(final)
        if model is not None and group_model != model:
            continue
        if task_type is not None and group_task_type != task_type:
            continue
        key = (
            group_engine,
            group_model,
            group_task_type,
            reasoning_effort or "",
            unattributed,
        )
        group = groups.setdefault(
            key,
            {
                "engine": group_engine,
                "model": group_model,
                "task_type": group_task_type,
                "reasoning_effort": reasoning_effort,
                "show_reasoning_effort": (
                    (group_engine, group_model, unattributed) in effort_keys
                ),
                "unattributed": unattributed,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
                "first_try_pass_rate": 0.0,
                "median_duration_ms": None,
                "median_tokens": None,
                "last_seen": "",
                "_first_try_passed": 0,
                "_duration_ms": [],
                "_tokens": [],
            },
        )
        group["tasks"] += 1
        group["attempts"] += len(ordered)
        if model_log_text(final.get("verdict")).upper() == "PASS":
            group["passed"] += 1
        else:
            group["failed"] += 1
        if model_log_text(first.get("verdict")).upper() == "PASS":
            group["_first_try_passed"] += 1
        duration_ms = model_log_int(final.get("duration_ms"))
        if duration_ms is not None:
            group["_duration_ms"].append(duration_ms)
        for row in ordered:
            tokens = model_log_int(row.get("worker_tokens"))
            if tokens is not None:
                group["_tokens"].append(tokens)
        logged_at = model_log_text(final.get("logged_at"))
        if logged_at > group["last_seen"]:
            group["last_seen"] = logged_at

    finalized: list[dict[str, Any]] = []
    for group in groups.values():
        tasks_count = group["tasks"]
        group["pass_rate"] = group["passed"] / tasks_count if tasks_count else 0.0
        group["first_try_pass_rate"] = (
            group["_first_try_passed"] / tasks_count if tasks_count else 0.0
        )
        group["median_duration_ms"] = median_int(group["_duration_ms"])
        group["median_tokens"] = median_int(group["_tokens"])
        finalized.append(
            {
                "engine": group["engine"],
                "model": group["model"],
                "task_type": group["task_type"],
                "reasoning_effort": group["reasoning_effort"],
                "show_reasoning_effort": group["show_reasoning_effort"],
                "unattributed": group["unattributed"],
                "tasks": group["tasks"],
                "attempts": group["attempts"],
                "passed": group["passed"],
                "failed": group["failed"],
                "pass_rate": group["pass_rate"],
                "first_try_pass_rate": group["first_try_pass_rate"],
                "median_duration_ms": group["median_duration_ms"],
                "median_tokens": group["median_tokens"],
                "last_seen": group["last_seen"],
            }
        )
    return sorted(
        finalized,
        key=lambda item: (
            1 if item["unattributed"] else 0,
            item["task_type"],
            -item["pass_rate"],
            -item["first_try_pass_rate"],
            item["engine"],
            item["model"],
            item["reasoning_effort"] or "",
        ),
    )

def default_model_notes_path() -> Path:
    return Path(__file__).resolve().parents[1] / "docs" / "MODEL-NOTES.md"

def default_model_registry_path() -> Path:
    return Path(__file__).resolve().parents[1] / "registry" / "model-identity.toml"

@dataclass(frozen=True)
class ModelIdentity:
    model_display: str
    lab: str
    harness: str
    access: str
    alias: bool = False
    confidence: str = ""
    source: str = ""
    last_verified: str = ""
    unregistered: bool = False
    misrouted: bool = False
    canonical_engine: str = ""
    canonical_model_key: str = ""
    canonical_harness: str = ""
    canonical_access: str = ""

@dataclass(frozen=True)
class NoncanonicalRoute:
    engine: str
    model_key: str
    canonical_engine: str
    canonical_model_key: str
    identity: ModelIdentity

    @property
    def canonical_route(self) -> str:
        return (
            f"{self.canonical_engine}:{self.canonical_model_key} via "
            f"{self.identity.harness} on {self.identity.access}"
        )

@dataclass(frozen=True)
class ModelIdentityRegistry:
    identities: dict[tuple[str, str], ModelIdentity]
    defaults: dict[str, str]
    engine_meta: dict[str, ModelIdentity]
    noncanonical_routes: dict[tuple[str, str], NoncanonicalRoute]

    def resolve(self, engine: str, model_key: str) -> ModelIdentity:
        engine_key = model_log_text(engine)
        raw_model_key = model_log_text(model_key)
        lookup_key = raw_model_key or self.defaults.get(engine_key, "")
        noncanonical = self.noncanonical_routes.get((engine_key, lookup_key))
        if noncanonical is not None:
            actual_meta = self.engine_meta.get(engine_key)
            canonical = noncanonical.identity
            return dataclass_replace(
                canonical,
                harness=actual_meta.harness if actual_meta else (engine_key or "unknown"),
                access=actual_meta.access if actual_meta else "unknown",
                misrouted=True,
                canonical_engine=noncanonical.canonical_engine,
                canonical_model_key=noncanonical.canonical_model_key,
                canonical_harness=canonical.harness,
                canonical_access=canonical.access,
            )
        identity = self.identities.get((engine_key, lookup_key))
        if identity is not None:
            return identity
        meta = self.engine_meta.get(engine_key)
        if raw_model_key.startswith("openrouter/"):
            slug = raw_model_key.removeprefix("openrouter/")
            org = slug.split("/", 1)[0] if "/" in slug else ""
            return ModelIdentity(
                model_display=raw_model_key,
                lab=f"{org}?" if org else "(unverified)",
                harness=(meta.harness if meta else "OpenCode"),
                access=(meta.access if meta else "OpenRouter API"),
                confidence="fallback",
                source="unlisted OpenRouter slug",
                unregistered=True,
            )
        if raw_model_key:
            return ModelIdentity(
                model_display=raw_model_key,
                lab="(unverified)",
                harness=meta.harness if meta else (engine_key or "unknown"),
                access=meta.access if meta else "unknown",
                confidence="fallback",
                source="unregistered model slug",
                unregistered=True,
            )
        unknown = engine_key or "unknown"
        return ModelIdentity(
            model_display=unknown,
            lab="(unknown)",
            harness=unknown,
            access="unknown",
            confidence="unknown",
            source="",
        )

EMPTY_MODEL_IDENTITY_REGISTRY = ModelIdentityRegistry({}, {}, {}, {})

def load_model_identity_registry(path: Path | None = None) -> ModelIdentityRegistry:
    registry_path = (path or default_model_registry_path()).expanduser().resolve()
    try:
        with registry_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return EMPTY_MODEL_IDENTITY_REGISTRY
    engines_raw = data.get("engines", {})
    if not isinstance(engines_raw, dict):
        return EMPTY_MODEL_IDENTITY_REGISTRY
    identities: dict[tuple[str, str], ModelIdentity] = {}
    defaults: dict[str, str] = {}
    engine_meta: dict[str, ModelIdentity] = {}
    pending_noncanonical: list[tuple[str, str, str]] = []
    for engine_name, raw_engine in engines_raw.items():
        if not isinstance(raw_engine, dict):
            continue
        engine = str(engine_name).strip()
        if not engine:
            continue
        harness = model_log_text(raw_engine.get("harness")) or engine
        access = model_log_text(raw_engine.get("access")) or "unknown"
        default_key = model_log_text(raw_engine.get("default_model_key"))
        if default_key:
            defaults[engine] = default_key
        engine_meta[engine] = ModelIdentity(
            model_display=engine,
            lab="(unknown)",
            harness=harness,
            access=access,
            confidence="engine",
            source="",
        )
        models_raw = raw_engine.get("models", {})
        if not isinstance(models_raw, dict):
            continue
        for model_key_raw, raw_model in models_raw.items():
            if not isinstance(raw_model, dict):
                continue
            model_key = str(model_key_raw).strip()
            if not model_key:
                continue
            identities[(engine, model_key)] = ModelIdentity(
                model_display=model_log_text(raw_model.get("display")) or model_key,
                lab=model_log_text(raw_model.get("lab")) or "(unknown)",
                harness=harness,
                access=access,
                alias=bool(raw_model.get("alias", False)),
                confidence=model_log_text(raw_model.get("confidence")),
                source=model_log_text(raw_model.get("source")),
                last_verified=model_log_text(raw_model.get("last_verified")),
            )
            raw_noncanonical = raw_model.get("noncanonical_slugs", [])
            if isinstance(raw_noncanonical, list):
                for value in raw_noncanonical:
                    route_key = model_log_text(value)
                    if route_key:
                        pending_noncanonical.append((engine, model_key, route_key))
    noncanonical_routes: dict[tuple[str, str], NoncanonicalRoute] = {}
    for canonical_engine, canonical_model_key, route_key in pending_noncanonical:
        route_engine, separator, route_model_key = route_key.partition(":")
        route_engine = route_engine.strip()
        route_model_key = route_model_key.strip()
        canonical_identity = identities.get((canonical_engine, canonical_model_key))
        if not separator or not route_engine or not route_model_key or canonical_identity is None:
            continue
        noncanonical_routes[(route_engine, route_model_key)] = NoncanonicalRoute(
            engine=route_engine,
            model_key=route_model_key,
            canonical_engine=canonical_engine,
            canonical_model_key=canonical_model_key,
            identity=canonical_identity,
        )
    return ModelIdentityRegistry(identities, defaults, engine_meta, noncanonical_routes)

def model_log_row_engine(row: dict[str, Any]) -> str:
    return model_log_text(row.get("worker_engine") if "worker_engine" in row else row.get("engine"))

def row_identity_fields(row: dict[str, Any], registry: ModelIdentityRegistry) -> dict[str, Any]:
    if model_log_row_is_unattributed(row):
        engine = model_log_row_engine(row)
        meta = registry.engine_meta.get(engine)
        return {
            "model_display": UNATTRIBUTED_MODEL_DISPLAY,
            "lab": "(unknown)",
            "harness": meta.harness if meta else (engine or "unknown"),
            "access": meta.access if meta else "unknown",
            "alias": False,
            "last_verified": "",
            "unregistered": False,
            "misrouted": False,
            "identity_key": "",
            "canonical_route": "",
        }
    identity = registry.resolve(model_log_row_engine(row), model_log_text(row.get("model")))
    return {
        "model_display": identity.model_display,
        "lab": identity.lab,
        "harness": identity.harness,
        "access": identity.access,
        "alias": identity.alias,
        "last_verified": identity.last_verified,
        "unregistered": identity.unregistered,
        "misrouted": identity.misrouted,
        "identity_key": identity.canonical_model_key or model_log_text(row.get("model")),
        "canonical_route": (
            f"{identity.canonical_engine}:{identity.canonical_model_key} via "
            f"{identity.canonical_harness} on {identity.canonical_access}"
            if identity.misrouted
            else ""
        ),
    }

def task_final_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finals: list[dict[str, Any]] = []
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        if ordered:
            finals.append(ordered[-1])
    return finals

def enrich_model_groups_with_identity(
    groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    registry: ModelIdentityRegistry,
    *,
    include_task_type: bool,
    catalog_models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    catalog_by_id = catalog_models_by_id(catalog_models or [])
    identity_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    latest: dict[tuple[Any, ...], str] = {}
    for row in task_final_rows(rows):
        if model_log_row_is_reserved_fixture(row):
            continue
        group_engine = model_log_row_engine(row)
        group_model = model_log_row_model(row)
        group_task_type = model_log_row_task_type(row)
        unattributed = model_log_row_is_unattributed(row)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(row)
        key: tuple[Any, ...]
        key = (
            (group_engine, group_model, group_task_type, reasoning_effort, unattributed)
            if include_task_type
            else (group_engine, group_model, reasoning_effort, unattributed)
        )
        logged_at = model_log_text(row.get("logged_at"))
        if key not in latest or logged_at >= latest[key]:
            latest[key] = logged_at
            identity = row_identity_fields(row, registry)
            if identity.get("unregistered"):
                identity.update(
                    catalog_identity_fields(model_log_text(row.get("model")), catalog_by_id)
                )
            identity_rows[key] = identity
    enriched: list[dict[str, Any]] = []
    for group in groups:
        key = (
            (
                str(group.get("engine") or ""),
                str(group.get("model") or ""),
                str(group.get("task_type") or ""),
                group.get("reasoning_effort"),
                bool(group.get("unattributed")),
            )
            if include_task_type
            else (
                str(group.get("engine") or ""),
                str(group.get("model") or ""),
                group.get("reasoning_effort"),
                bool(group.get("unattributed")),
            )
        )
        item = dict(group)
        item.update(
            identity_rows.get(
                key,
                {
                    "model_display": str(group.get("model") or ""),
                    "lab": "(unknown)",
                    "harness": "unknown",
                    "access": "unknown",
                    "alias": False,
                    "last_verified": "",
                    "unregistered": bool(group.get("model")),
                    "misrouted": False,
                    "identity_key": str(group.get("model") or ""),
                    "canonical_route": "",
                },
            )
        )
        if item.get("unregistered") and str(item.get("model") or "").startswith("openrouter/"):
            if item.get("model_display") == item.get("model"):
                item["model_display"] = short_model_name(item.get("model"))
        if item.get("show_reasoning_effort") and not item.get("unattributed"):
            effort = item.get("reasoning_effort") or "(effort unrecorded)"
            item["model_display"] = f"{item['model_display']} · {effort}"
        if item.get("unattributed"):
            # The aggregation helper retains the engine as a legacy grouping key.
            # Public payloads must not expose harness branding as a model identity.
            item["model"] = ""
        if item.get("unattributed") or item.get("misrouted"):
            item["tier"] = "unranked"
        elif "tier" not in item:
            item["tier"] = model_scoreboard_tier(
                int(item.get("tasks") or 0), float(item.get("first_try_pass_rate") or 0)
            )
        item["bucket_id"] = "|".join(
            (
                str(item.get("engine") or ""),
                str(item.get("model") or ""),
                str(item.get("reasoning_effort") or ""),
                "unattributed" if item.get("unattributed") else "model",
            )
        )
        item["display_bucket_id"] = "bucket-" + base64.urlsafe_b64encode(
            item["bucket_id"].encode("utf-8")
        ).decode("ascii").rstrip("=")
        enriched.append(item)
    return enriched

def normalize_notes_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()

def parse_model_notes_sections(path: Path) -> dict[str, list[str]]:
    """Return dated bullet blocks keyed by the raw level-2 heading text."""
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    sections: dict[str, list[str]] = {}
    current_heading: str | None = None
    current_bullets: list[str] = []
    active_bullet: list[str] | None = None

    def flush_bullet() -> None:
        nonlocal active_bullet
        if active_bullet is not None:
            text = "\n".join(active_bullet).strip()
            if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
                current_bullets.append(text)
            active_bullet = None

    def flush_section() -> None:
        flush_bullet()
        if current_heading is not None:
            sections[current_heading] = list(current_bullets)

    for line in lines:
        heading_match = re.match(r"^##\s+(.+?)\s*$", line)
        if heading_match:
            flush_section()
            current_heading = heading_match.group(1).strip()
            current_bullets = []
            active_bullet = None
            continue
        if current_heading is None:
            continue
        if line.startswith("## "):
            flush_section()
            current_heading = None
            current_bullets = []
            active_bullet = None
            continue
        if line.startswith("- "):
            flush_bullet()
            active_bullet = [line[2:].strip()]
            continue
        if active_bullet is not None and (line.startswith("  ") or not line.strip()):
            active_bullet.append(line.strip())
            continue
        flush_bullet()
    flush_section()
    return sections

def model_judgment_notes(model_id: str, notes_sections: dict[str, list[str]]) -> list[str]:
    needle = normalize_notes_match_text(model_id)
    if not needle:
        return []
    id_boundary = r"A-Za-z0-9._/:-"
    needle_re = re.compile(rf"(?<![{id_boundary}]){re.escape(needle)}(?![{id_boundary}])")
    matches: list[tuple[int, int, int, list[str]]] = []
    for index, (heading, bullets) in enumerate(notes_sections.items()):
        normalized_heading = normalize_notes_match_text(heading)
        if not needle_re.search(normalized_heading):
            continue
        exact_score = 1 if normalized_heading == needle else 0
        matches.append((exact_score, len(normalized_heading), -index, bullets))
    if not matches:
        return []
    return max(matches, key=lambda item: (item[0], item[1], item[2]))[3]

def model_judgment_notes_for_row(
    row: dict[str, Any], notes_sections: dict[str, list[str]]
) -> list[str]:
    display = re.sub(
        r"\s+·\s+(?:[^·]+)$", "", str(row.get("model_display") or "")
    ).strip()
    candidates = (
        str(row.get("identity_key") or "").strip(),
        display,
        str(row.get("model") or "").strip(),
    )
    for candidate in candidates:
        if not candidate:
            continue
        notes = model_judgment_notes(candidate, notes_sections)
        if notes:
            return notes
    return []

def enrich_model_groups_with_notes(
    groups: list[dict[str, Any]], notes_sections: dict[str, list[str]]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for group in groups:
        item = dict(group)
        notes = [
            scoreboard_safe_note(note)
            for note in sorted(
                model_judgment_notes_for_row(item, notes_sections),
                key=note_date_key,
                reverse=True,
            )
        ]
        item["notes"] = notes
        item["latest_note"] = strip_inline_markdown(notes[0]) if notes else ""
        enriched.append(item)
    return enriched

def strip_inline_markdown(value: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()

def scoreboard_safe_note(value: str) -> str:
    return re.sub(
        r"openrouter/[A-Za-z0-9._/+:-]+",
        lambda match: short_model_name(match.group(0)),
        value,
    )

def note_date_key(item: str) -> str:
    match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", item)
    return match.group(0) if match else ""

def normalize_catalog_for_scoreboard(model: dict[str, Any]) -> dict[str, Any]:
    if "prompt_per_m" in model and "completion_per_m" in model:
        return model
    if isinstance(model.get("pricing"), dict):
        return normalize_catalog_model(model, fetched_at=str(model.get("fetched_at") or ""))
    return model

def catalog_models_by_id(catalog_models: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for model in catalog_models:
        model_id = str(model.get("id") or "").strip()
        if model_id:
            by_id[model_id] = normalize_catalog_for_scoreboard(model)
    return by_id

def catalog_identity_fields(
    model_key: str,
    catalog_by_id: dict[str, dict[str, Any]],
) -> dict[str, str]:
    if not model_key.startswith("openrouter/"):
        return {}
    catalog_id = model_key.removeprefix("openrouter/")
    model = catalog_by_id.get(catalog_id) or catalog_by_id.get(model_key)
    if model is None:
        return {}
    name = model_log_text(model.get("name"))
    if not name or name in {catalog_id, model_key}:
        return {}
    display = name.removesuffix(" (free)").strip()
    org = catalog_id.split("/", 1)[0] if "/" in catalog_id else ""
    lab = f"{org}?" if org else "(unverified)"
    if ":" in display:
        prefix, candidate = (part.strip() for part in display.split(":", 1))
        if candidate:
            display = candidate
        if prefix:
            lab = f"{prefix}?"
    return {"model_display": display, "lab": lab}

def model_scoreboard_tier(tasks: int, first_try_pass_rate: float) -> str:
    # Same promotion rule as proven_model_group: volume alone never proves a
    # model — a 0% pass rate with many tasks is evidence against, not for.
    if tasks >= PROVEN_MIN_TASKS and first_try_pass_rate >= PROVEN_MIN_FIRST_TRY:
        return "proven"
    return "probation"

def model_scoreboard_tier_rank(tier: str) -> int:
    return {"proven": 0, "probation": 1}.get(tier, 3)

def aggregate_model_scoreboard_rows(
    rows: list[dict[str, Any]],
    *,
    task_type: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    models: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    effort_keys = model_reasoning_effort_keys(rows)
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        first = ordered[0]
        final = ordered[-1]
        if model_log_row_is_reserved_fixture(final):
            continue
        group_engine = model_log_row_engine(final)
        group_model = model_log_row_model(final)
        group_task_type = model_log_row_task_type(final)
        unattributed = model_log_row_is_unattributed(final)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(final)
        if model is not None and group_model != model:
            continue
        if task_type is not None and group_task_type != task_type:
            continue
        model_key = (group_engine, group_model, reasoning_effort or "", unattributed)
        model_entry = models.setdefault(
            model_key,
            {
                "engine": group_engine,
                "model": group_model,
                "reasoning_effort": reasoning_effort,
                "show_reasoning_effort": (
                    (group_engine, group_model, unattributed) in effort_keys
                ),
                "unattributed": unattributed,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "retries": 0,
                "first_try_passed": 0,
                "last_seen": "",
                "_duration_ms": [],
                "_tokens": [],
                "_task_types": {},
            },
        )
        breakdown = model_entry["_task_types"].setdefault(
            group_task_type,
            {
                "task_type": group_task_type,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "first_try_passed": 0,
                "last_seen": "",
            },
        )
        passed = model_log_text(final.get("verdict")).upper() == "PASS"
        first_passed = model_log_text(first.get("verdict")).upper() == "PASS"
        for target in (model_entry, breakdown):
            target["tasks"] += 1
            target["attempts"] += len(ordered)
            target["passed"] += 1 if passed else 0
            target["failed"] += 0 if passed else 1
            target["first_try_passed"] += 1 if first_passed else 0
            logged_at = model_log_text(final.get("logged_at"))
            if logged_at > target["last_seen"]:
                target["last_seen"] = logged_at
        model_entry["retries"] += max(0, len(ordered) - 1)
        duration_ms = model_log_int(final.get("duration_ms"))
        if duration_ms is not None:
            model_entry["_duration_ms"].append(duration_ms)
        for row in ordered:
            tokens = model_log_int(row.get("worker_tokens"))
            if tokens is not None:
                model_entry["_tokens"].append(tokens)

    finalized: list[dict[str, Any]] = []
    for entry in models.values():
        tasks_count = int(entry["tasks"])
        breakdown_rows = []
        for breakdown in entry["_task_types"].values():
            b_tasks = int(breakdown["tasks"])
            breakdown_rows.append(
                {
                    "task_type": breakdown["task_type"],
                    "tasks": b_tasks,
                    "attempts": breakdown["attempts"],
                    "passed": breakdown["passed"],
                    "failed": breakdown["failed"],
                    "first_try_pass_rate": breakdown["first_try_passed"] / b_tasks if b_tasks else 0.0,
                    "pass_rate": breakdown["passed"] / b_tasks if b_tasks else 0.0,
                    "last_seen": breakdown["last_seen"],
                }
            )
        breakdown_rows.sort(key=lambda item: (-item["tasks"], item["task_type"]))
        first_try_rate = entry["first_try_passed"] / tasks_count if tasks_count else 0.0
        tier = (
            "unranked"
            if entry["unattributed"]
            else model_scoreboard_tier(tasks_count, first_try_rate)
        )
        finalized.append(
            {
                "engine": entry["engine"],
                "model": entry["model"],
                "reasoning_effort": entry["reasoning_effort"],
                "show_reasoning_effort": entry["show_reasoning_effort"],
                "unattributed": entry["unattributed"],
                "tier": tier,
                "tasks": tasks_count,
                "attempts": entry["attempts"],
                "retries": entry["retries"],
                "passed": entry["passed"],
                "failed": entry["failed"],
                "first_try_pass_rate": entry["first_try_passed"] / tasks_count if tasks_count else 0.0,
                "pass_rate": entry["passed"] / tasks_count if tasks_count else 0.0,
                "median_duration_ms": median_int(entry["_duration_ms"]),
                "median_tokens": median_int(entry["_tokens"]),
                "last_seen": entry["last_seen"],
                "task_types": breakdown_rows,
            }
        )
    return finalized

def estimated_task_cost(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> float | None:
    median_tokens = row.get("median_tokens")
    if median_tokens is None or catalog_model is None or catalog_model.get("variable_pricing"):
        return None
    if catalog_model.get("free"):
        return 0.0
    try:
        tokens = float(median_tokens)
        prompt_per_m = float(catalog_model.get("prompt_per_m") or 0)
        completion_per_m = float(catalog_model.get("completion_per_m") or 0)
    except (TypeError, ValueError):
        return None
    return tokens * ((prompt_per_m + completion_per_m) / 2.0) / 1_000_000

def model_sort_cost(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> float:
    cost = estimated_task_cost(row, catalog_model)
    if cost is not None:
        return cost
    if row.get("median_tokens") is None:
        return 0.0
    return float("inf")

def order_model_scoreboard_rows(
    rows: list[dict[str, Any]],
    catalog_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            1 if row.get("unattributed") else 0,
            model_scoreboard_tier_rank(str(row.get("tier") or "")),
            -float(row.get("first_try_pass_rate") or 0),
            -float(row.get("pass_rate") or 0),
            model_sort_cost(row, catalog_by_id.get(str(row.get("model") or ""))),
            str(row.get("engine") or ""),
            str(row.get("model") or ""),
            str(row.get("reasoning_effort") or ""),
        ),
    )

def short_model_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    text = text.removeprefix("openrouter/")
    text = text.removesuffix(":free")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "unknown"
    parts = []
    for part in text.split():
        parts.append(part.upper() if part.lower() in {"gpt", "glm", "ai", "llm"} else part.capitalize())
    return " ".join(parts)
