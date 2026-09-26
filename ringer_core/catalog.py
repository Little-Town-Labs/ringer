from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from ringer_core.state_files import atomic_write_json, ringer_home, utc_now_iso

DEFAULT_CATALOG_SOURCE = "https://openrouter.ai/api/v1/models"


CATALOG_AUTO_REFRESH_MAX_AGE_S = 24 * 60 * 60


CATALOG_FETCH_TIMEOUT_S = 5


RESERVED_FIXTURE_MODELS = frozenset(
    {"proven-model", "probation-model", "mock-model", "test-model"}
)


def default_catalog_path() -> Path:
    return ringer_home() / "openrouter-catalog.json"


def catalog_changes_path(snapshot_path: Path) -> Path:
    text = str(snapshot_path)
    if text.endswith(".json"):
        return Path(text[:-5] + ".changes.jsonl")
    return snapshot_path.with_name(snapshot_path.name + ".changes.jsonl")


def catalog_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def catalog_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def catalog_per_m(value: Any) -> float:
    return float(catalog_decimal(value) * Decimal("1000000"))


def catalog_per_m_decimal(value: Decimal) -> float:
    return float(value * Decimal("1000000"))


def catalog_price_equal(left: Any, right: Any) -> bool:
    return catalog_decimal(left) == catalog_decimal(right)


def catalog_price_is_negative(value: Any) -> bool:
    return catalog_decimal(value) < 0


def normalize_catalog_model(raw: dict[str, Any], *, fetched_at: str) -> dict[str, Any]:
    pricing = raw.get("pricing")
    pricing_obj = pricing if isinstance(pricing, dict) else {}
    architecture = raw.get("architecture")
    architecture_obj = architecture if isinstance(architecture, dict) else {}
    model_id = str(raw.get("id", "")).strip()
    prompt_price = catalog_decimal_or_none(pricing_obj.get("prompt"))
    completion_price = catalog_decimal_or_none(pricing_obj.get("completion"))
    pricing_unknown = prompt_price is None or completion_price is None
    variable_pricing = pricing_unknown or prompt_price < 0 or completion_price < 0
    prompt_per_m = None if variable_pricing else catalog_per_m_decimal(prompt_price)
    completion_per_m = None if variable_pricing else catalog_per_m_decimal(completion_price)
    is_free = not variable_pricing and (
        model_id.endswith(":free")
        or (
            prompt_price == 0
            and completion_price == 0
        )
    )
    if model_id.endswith(":free"):
        is_free = True
    context_length_raw = raw.get("context_length")
    try:
        context_length = int(context_length_raw)
    except (TypeError, ValueError):
        context_length = 0
    return {
        "id": model_id,
        "name": str(raw.get("name", "")).strip() or model_id,
        "context_length": context_length,
        "modality": str(architecture_obj.get("modality", "")).strip(),
        "pricing": dict(pricing_obj),
        "prompt_per_m": prompt_per_m,
        "completion_per_m": completion_per_m,
        "variable_pricing": variable_pricing,
        "pricing_unknown": pricing_unknown,
        "free": is_free,
        "fetched_at": fetched_at,
    }


def normalize_catalog_payload(payload: dict[str, Any], *, fetched_at: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("catalog source must have a JSON object with a data array")
    models: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model = normalize_catalog_model(item, fetched_at=fetched_at)
        if model["id"]:
            models.append(model)
    return sorted(models, key=catalog_sort_key)


def catalog_sort_key(model: dict[str, Any]) -> tuple[bool, float, str]:
    variable_pricing = bool(model.get("variable_pricing"))
    return (
        variable_pricing,
        float("inf")
        if variable_pricing
        else float(model.get("prompt_per_m") or 0) + float(model.get("completion_per_m") or 0),
        str(model.get("id") or ""),
    )


def fetch_catalog_payload(source: str, *, timeout: float = CATALOG_FETCH_TIMEOUT_S) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(source, headers={"User-Agent": "ringer.py"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    else:
        payload = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("catalog source must be a JSON object")
    return payload


def load_catalog_snapshot(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if isinstance(data, dict):
        models = data.get("models")
    else:
        models = data
    if not isinstance(models, list):
        return []
    return [item for item in models if isinstance(item, dict)]


def catalog_event_model_details(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": model.get("name", ""),
        "prompt_per_m": model.get("prompt_per_m", 0),
        "completion_per_m": model.get("completion_per_m", 0),
        "variable_pricing": bool(model.get("variable_pricing")),
        "pricing_unknown": bool(model.get("pricing_unknown")),
        "free": bool(model.get("free")),
        "context_length": model.get("context_length", 0),
        "modality": model.get("modality", ""),
    }


def diff_catalog_snapshots(
    old_models: list[dict[str, Any]],
    new_models: list[dict[str, Any]],
    *,
    ts: str,
) -> list[dict[str, Any]]:
    old_by_id = {str(model.get("id")): model for model in old_models if model.get("id")}
    new_by_id = {str(model.get("id")): model for model in new_models if model.get("id")}
    events: list[dict[str, Any]] = []

    for model_id in sorted(new_by_id.keys() - old_by_id.keys()):
        model = new_by_id[model_id]
        events.append({"ts": ts, "kind": "added", "id": model_id, **catalog_event_model_details(model)})

    for model_id in sorted(old_by_id.keys() - new_by_id.keys()):
        model = old_by_id[model_id]
        events.append({"ts": ts, "kind": "removed", "id": model_id, **catalog_event_model_details(model)})

    for model_id in sorted(old_by_id.keys() & new_by_id.keys()):
        old = old_by_id[model_id]
        new = new_by_id[model_id]
        old_prompt = old.get("prompt_per_m", 0)
        new_prompt = new.get("prompt_per_m", 0)
        old_completion = old.get("completion_per_m", 0)
        new_completion = new.get("completion_per_m", 0)
        old_free = bool(old.get("free"))
        new_free = bool(new.get("free"))
        old_variable = bool(old.get("variable_pricing"))
        new_variable = bool(new.get("variable_pricing"))
        if new_variable:
            if not old_variable:
                events.append(
                    {
                        "ts": ts,
                        "kind": "pricing_variable",
                        "id": model_id,
                        "name": new.get("name", old.get("name", "")),
                        "old_prompt_per_m": old_prompt,
                        "new_prompt_per_m": new_prompt,
                        "old_completion_per_m": old_completion,
                        "new_completion_per_m": new_completion,
                        "old_free": old_free,
                        "new_free": new_free,
                    }
                )
            continue
        if old_variable:
            events.append(
                {
                    "ts": ts,
                    "kind": "pricing_fixed",
                    "id": model_id,
                    "name": new.get("name", old.get("name", "")),
                    "old_prompt_per_m": old_prompt,
                    "new_prompt_per_m": new_prompt,
                    "old_completion_per_m": old_completion,
                    "new_completion_per_m": new_completion,
                    "old_free": old_free,
                    "new_free": new_free,
                }
            )
            if new_free:
                events.append(
                    {
                        "ts": ts,
                        "kind": "went_free",
                        "id": model_id,
                        "name": new.get("name", old.get("name", "")),
                        "old_prompt_per_m": old_prompt,
                        "new_prompt_per_m": new_prompt,
                        "old_completion_per_m": old_completion,
                        "new_completion_per_m": new_completion,
                    }
                )
            continue
        price_changed = old_prompt != new_prompt or old_completion != new_completion
        if price_changed:
            events.append(
                {
                    "ts": ts,
                    "kind": "price_change",
                    "id": model_id,
                    "name": new.get("name", old.get("name", "")),
                    "old_prompt_per_m": old_prompt,
                    "new_prompt_per_m": new_prompt,
                    "old_completion_per_m": old_completion,
                    "new_completion_per_m": new_completion,
                    "old_free": old_free,
                    "new_free": new_free,
                }
            )
        if old_free != new_free:
            events.append(
                {
                    "ts": ts,
                    "kind": "went_free" if new_free else "went_paid",
                    "id": model_id,
                    "name": new.get("name", old.get("name", "")),
                    "old_prompt_per_m": old_prompt,
                    "new_prompt_per_m": new_prompt,
                    "old_completion_per_m": old_completion,
                    "new_completion_per_m": new_completion,
                }
            )
    return events


@dataclass(frozen=True)
class CatalogRefreshResult:
    path: Path
    changes_path: Path
    models: list[dict[str, Any]]
    events: list[dict[str, Any]]


def append_catalog_events(path: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, sort_keys=True) + "\n")


@contextlib.contextmanager
def catalog_refresh_lock(snapshot_path: Path) -> Iterable[None]:
    lock_path = snapshot_path.with_name(snapshot_path.name + ".lock")
    try:
        import fcntl
    except Exception:
        yield
        return
    fh = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a", encoding="utf-8")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def refresh_openrouter_catalog(
    snapshot_path: Path,
    *,
    source: str = DEFAULT_CATALOG_SOURCE,
    timeout: float = CATALOG_FETCH_TIMEOUT_S,
) -> CatalogRefreshResult:
    snapshot_path = snapshot_path.expanduser().resolve()
    changes_path = catalog_changes_path(snapshot_path)
    with catalog_refresh_lock(snapshot_path):
        ts = utc_now_iso()
        old_models = load_catalog_snapshot(snapshot_path)
        payload = fetch_catalog_payload(source, timeout=timeout)
        new_models = normalize_catalog_payload(payload, fetched_at=ts)
        events = diff_catalog_snapshots(old_models, new_models, ts=ts)
        snapshot = {"fetched_at": ts, "models": new_models}
        append_catalog_events(changes_path, events)
        # Append events before replacing the snapshot: a crash here can duplicate
        # events on the next refresh, but duplicated events are recoverable and
        # silently lost catalog changes are not.
        atomic_write_json(snapshot_path, snapshot)
    return CatalogRefreshResult(
        path=snapshot_path,
        changes_path=changes_path,
        models=new_models,
        events=events,
    )


def read_catalog_events(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(events) >= limit:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def catalog_snapshot_is_fresh(
    snapshot_path: Path,
    *,
    max_age_s: int = CATALOG_AUTO_REFRESH_MAX_AGE_S,
    now: float | None = None,
) -> bool:
    try:
        mtime = snapshot_path.expanduser().stat().st_mtime
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) < max_age_s


def start_catalog_auto_refresh(
    *,
    snapshot_path: Path | None = None,
    source: str = DEFAULT_CATALOG_SOURCE,
    print_notice: bool = True,
) -> threading.Thread | None:
    try:
        if os.environ.get("RINGER_NO_CATALOG_REFRESH") == "1":
            return None
        path = (snapshot_path or default_catalog_path()).expanduser().resolve()
        if catalog_snapshot_is_fresh(path):
            return None
    except Exception:
        return None

    def worker() -> None:
        try:
            result = refresh_openrouter_catalog(path, source=source, timeout=CATALOG_FETCH_TIMEOUT_S)
            went_free = [event for event in result.events if event.get("kind") == "went_free"]
            if print_notice and went_free:
                sample = ", ".join(str(event.get("id")) for event in went_free[:3])
                extra = "" if len(went_free) <= 3 else f" and {len(went_free) - 3} more"
                print(f"Catalog refresh: model went FREE: {sample}{extra}", file=sys.stderr, flush=True)
        except Exception:
            pass

    try:
        thread = threading.Thread(target=worker, name="ringer-catalog-refresh", daemon=True)
        thread.start()
        return thread
    except Exception:
        return None


def catalog_model_is_text_candidate(model: dict[str, Any]) -> bool:
    try:
        context_length = int(model.get("context_length") or 0)
    except (TypeError, ValueError):
        context_length = 0
    return (
        not bool(model.get("variable_pricing"))
        and str(model.get("modality", "")).strip().lower() == "text->text"
        and context_length >= 32000
    )


def catalog_explore_candidates(
    catalog_models: list[dict[str, Any]],
    *,
    tested_models: set[str],
    limit: int = 10,
) -> list[dict[str, Any]]:
    candidates = [
        model
        for model in catalog_models
        if str(model.get("id", "")).strip() not in tested_models
        and str(model.get("id", "")).strip() not in RESERVED_FIXTURE_MODELS
        and catalog_model_is_text_candidate(model)
    ]
    return sorted(
        candidates,
        key=lambda model: (
            not bool(model.get("free")),
            float(model.get("prompt_per_m") or 0) + float(model.get("completion_per_m") or 0),
            str(model.get("id") or ""),
        ),
    )[:limit]
