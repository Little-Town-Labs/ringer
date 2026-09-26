from __future__ import annotations

import re
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any

from ringer_core.config import AppConfig
from ringer_core.manifests import MODEL_SCOREBOARD_RUN_NAME
from ringer_core.artifact_views import ARTIFACT_BASE_CSS, CSP_META_TAG, file_href
from ringer_core.artifact_store import artifact_live_path, sanitize_artifact_name, update_artifact_library_live
from ringer_core.state_files import atomic_write_text
from ringer_core.catalog import catalog_changes_path, read_catalog_events
from ringer_core.models import catalog_models_by_id, estimated_task_cost, model_judgment_notes_for_row, model_log_text, normalize_catalog_for_scoreboard, note_date_key, order_model_scoreboard_rows, short_model_name, strip_inline_markdown

MODEL_SCOREBOARD_IDENTITY = "ringer-models"

def normalized_judgment_note(item: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", " ", item).strip()
    match = re.match(r"^-?\s*(\d{4}-\d{2}-\d{2})\s+(?:[-\u2013\u2014]+\s*)?(.*)$", text)
    if not match:
        return None
    body = strip_inline_markdown(match.group(2))
    if not body:
        return None
    date = humanized_log_date(match.group(1))
    short_date = re.sub(r",\s*\d{4}$", "", date)
    return short_date, body

def fmt_scoreboard_duration(value_ms: Any) -> str:
    if value_ms is None:
        return ""
    try:
        total = max(0, int(round(float(value_ms) / 1000.0)))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"

def render_notes_list(items: list[str], *, notes_path: Path | None = None, limit: int = 5) -> str:
    if not items:
        return '<p class="empty-note">no judgment notes yet</p>'
    ordered_items = sorted(items, key=note_date_key, reverse=True)
    rendered = []
    for item in ordered_items[:limit]:
        note = normalized_judgment_note(item)
        if note is None:
            body = strip_inline_markdown(item)
            if not body:
                continue
            rendered.append(f"<li><span>{html_escape(body)}</span></li>")
            continue
        date, body = note
        rendered.append(
            f'<li><time>{html_escape(date)}</time><span>{html_escape(body)}</span></li>'
        )
    if not rendered:
        return '<p class="empty-note">no judgment notes yet</p>'
    more = ""
    if len(ordered_items) > limit and notes_path is not None:
        more = (
            f'<li class="more-notes">{source_file_link(notes_path, "more in model notes")}</li>'
        )
    return f'<ul class="notes-list">{"".join(rendered)}{more}</ul>'

def fmt_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"

def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"

def fmt_task_cost(value: float | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "$0/task"
    if value < 0.01:
        return f"${value:.4f}/task"
    return f"${value:.2f}/task"

def fmt_short_task_cost(value: float | None) -> str:
    if value is None:
        return "in plan"
    if value == 0:
        return "free"
    if value < 0.10:
        cents = value * 100
        if cents < 1:
            return "<1¢"
        rounded = round(cents)
        return f"~{rounded}¢"
    return f"${value:.2f}"

def humanized_log_date(value: Any, *, prefix: str = "") -> str:
    text = model_log_text(value)
    if not text:
        return f"{prefix}unknown" if prefix else "unknown"
    candidate = text[:10]
    try:
        dt = datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return f"{prefix}{text}" if prefix else text
    rendered = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    return f"{prefix}{rendered}" if prefix else rendered

def humanize_dates_in_text(value: str) -> str:
    return re.sub(
        r"\b\d{4}-\d{2}-\d{2}\b",
        lambda match: humanized_log_date(match.group(0)),
        value,
    )

def source_file_link(path: Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    return (
        f'<a class="source-link" href="{html_escape(file_href(resolved))}" '
        f'title="{html_escape(str(resolved))}">{html_escape(label)}</a>'
    )

def model_task_cost_label(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> str:
    median_tokens = row.get("median_tokens")
    if median_tokens is None:
        return "in plan"
    if catalog_model is None:
        return "catalog missing"
    if catalog_model.get("free"):
        return "free"
    if catalog_model.get("variable_pricing"):
        return "var"
    return fmt_short_task_cost(estimated_task_cost(row, catalog_model))

def rate_bar_html(value: Any) -> str:
    try:
        pct = max(0.0, min(100.0, float(value) * 100))
    except (TypeError, ValueError):
        pct = 0.0
    return (
        '<span class="rate-meter" aria-hidden="true">'
        f'<span class="rate-bar bar-fill" style="width: {pct:.0f}%"></span>'
        "</span>"
    )

def rate_cell_html(value: Any) -> str:
    return (
        f'<span class="rate-value">{html_escape(fmt_percent(value))}</span>'
        f"{rate_bar_html(value)}"
    )

def compact_context_label(value: Any) -> str:
    try:
        ctx = int(value)
    except (TypeError, ValueError):
        return "unknown ctx"
    if ctx >= 1_000_000 and ctx % 1_000_000 == 0:
        return f"{ctx // 1_000_000}M ctx"
    if ctx >= 1_000:
        if ctx % 1_000 == 0:
            return f"{ctx // 1_000}K ctx"
        return f"{ctx / 1000:.1f}K ctx"
    return f"{ctx} ctx"

def catalog_model_display_name(model: dict[str, Any]) -> str:
    name = str(model.get("name") or "").strip()
    model_id = str(model.get("id") or "").strip()
    if name and name != model_id:
        return name.removesuffix(" (free)").strip()
    return short_model_name(model_id)

def humanized_short_date(value: Any) -> str:
    return re.sub(r",\s*\d{4}$", "", humanized_log_date(value))

def humanized_catalog_event_line(event: dict[str, Any], catalog_by_id: dict[str, dict[str, Any]]) -> str:
    model_id = str(event.get("id") or "")
    if model_id in catalog_by_id:
        label = catalog_model_display_name(catalog_by_id[model_id])
    else:
        label = short_model_name(model_id)
    kind = str(event.get("kind") or "event")
    date = humanized_short_date(event.get("ts"))
    if kind == "went_free":
        action = "went free"
    elif kind == "went_paid":
        action = "went paid"
    elif kind == "pricing_variable":
        action = "moved to variable pricing"
    elif kind == "pricing_fixed":
        action = "returned to fixed pricing"
    elif kind == "added":
        action = "was added"
    elif kind == "removed":
        action = "was removed"
    else:
        action = kind.replace("_", " ")
    return f"{label} {action} — {date}"

def watchlist_chip_html(model: dict[str, Any]) -> str:
    label = catalog_model_display_name(model)
    context = compact_context_label(model.get("context_length"))
    return (
        '<li class="watch-chip">'
        f'<span title="{html_escape(label)}">'
        f"{html_escape(label)} · {html_escape(context)}</span></li>"
    )

def derived_quality_text(row: dict[str, Any], *, best: bool) -> str:
    candidates = [item for item in row.get("task_types", []) if int(item.get("tasks") or 0) >= 2]
    if not candidates:
        return "not enough per-task evidence yet"
    if best:
        chosen = max(candidates, key=lambda item: (float(item.get("first_try_pass_rate") or 0), int(item.get("tasks") or 0), str(item.get("task_type") or "")))
        prefix = "best derived"
    else:
        chosen = min(candidates, key=lambda item: (float(item.get("first_try_pass_rate") or 0), -int(item.get("tasks") or 0), str(item.get("task_type") or "")))
        prefix = "worst derived"
    return (
        f"{prefix}: {chosen['task_type']} "
        f"({fmt_percent(chosen.get('first_try_pass_rate'))} first-try, "
        f"{fmt_percent(chosen.get('pass_rate'))} pass, n={fmt_int(chosen.get('tasks'))})"
    )

def render_task_breakdown_table(task_rows: list[dict[str, Any]]) -> str:
    if not task_rows:
        return '<p class="empty-note">no task-type breakdown</p>'
    rows = []
    for item in task_rows:
        rows.append(
            f"""<tr>
      <td>{html_escape(str(item.get("task_type") or ""))}</td>
      <td class="num">{fmt_int(item.get("tasks"))}</td>
      <td class="num rate-cell">{rate_cell_html(item.get("first_try_pass_rate"))}</td>
      <td class="num rate-cell">{rate_cell_html(item.get("pass_rate"))}</td>
    </tr>"""
        )
    return f"""<table class="breakdown">
    <thead><tr><th>task_type</th><th>n</th><th>first-try</th><th>pass</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>"""

MODEL_SCOREBOARD_CSS = """
  .scoreboard-page { max-width: 1240px; }
  .scoreboard-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 18px;
    align-items: end;
    margin-bottom: clamp(14px, 2.5vw, 22px);
  }
  .scoreboard-title {
    margin: 0;
    font-size: clamp(28px, 5vw, 54px);
    line-height: .98;
    letter-spacing: 0;
    text-wrap: balance;
  }
  .scoreboard-meta {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 12px;
  }
  .source-links {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .source-link {
    color: var(--ink);
    text-decoration: none;
    border-bottom: 1px solid var(--hairline);
  }
  .source-link:hover { border-bottom-color: var(--ink); }
  .watchlist {
    border-top: 1px solid var(--hairline);
    border-bottom: 1px solid var(--hairline);
    padding: 12px 0;
    margin: 0 0 clamp(18px, 3vw, 28px);
    color: var(--muted);
    font-size: 13px;
  }
  .watchline {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .watch-title { color: var(--ink); font-weight: 650; }
  .watch-chips, .event-list {
    display: contents;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .watch-chip {
    border: 1px solid var(--hairline);
    padding: 3px 7px 4px;
    font-size: 12px;
    color: var(--ink);
    background: rgba(255, 255, 255, .03);
  }
  .event-list li {
    color: var(--muted);
  }
  .event-list li::before {
    content: "/";
    color: var(--hairline);
    margin: 0 8px 0 2px;
  }
  .table-scroll {
    overflow-x: auto;
    border: 1px solid var(--hairline);
    border-left: 0;
    border-right: 0;
  }
  .ranked-table {
    width: 100%;
    min-width: 1500px;
    border-collapse: collapse;
    font-size: 13px;
  }
  .ranked-table th {
    color: var(--muted);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: .08em;
    text-transform: uppercase;
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--hairline);
    white-space: nowrap;
  }
  .ranked-table td {
    padding: 17px 12px;
    border-bottom: 1px solid var(--hairline);
    vertical-align: middle;
    color: var(--ink);
  }
  .ranked-table tr.model-row:hover td {
    background: color-mix(in srgb, var(--surface) 48%, transparent);
  }
  .rank-cell {
    width: 58px;
    font-size: 16px;
    font-weight: 760;
  }
  .model-cell { min-width: 230px; }
  .model-name {
    font-size: 16px;
    font-weight: 760;
    line-height: 1.2;
    overflow-wrap: anywhere;
  }
  .model-id {
    margin-top: 3px;
    color: var(--muted);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .identity-flag, .verified-date {
    display: block;
    margin-top: 3px;
    color: var(--muted);
    font-size: 11px;
  }
  .tier-badge {
    display: inline-flex;
    align-items: center;
    min-width: 78px;
    justify-content: center;
    padding: 3px 8px 4px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 700;
    color: var(--ink);
    border: 1px solid transparent;
  }
  .tier-badge.proven {
    background: color-mix(in srgb, var(--pass) 30%, transparent);
    border-color: color-mix(in srgb, var(--pass) 58%, var(--hairline));
  }
  .tier-badge.probation {
    background: color-mix(in srgb, var(--accent) 24%, transparent);
    border-color: color-mix(in srgb, var(--accent) 48%, var(--hairline));
  }
  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .rate-cell {
    min-width: 108px;
  }
  .rate-value {
    display: inline-block;
    min-width: 38px;
  }
  .rate-meter {
    display: inline-block;
    width: 48px;
    height: 6px;
    margin-left: 8px;
    vertical-align: 1px;
    border-radius: 4px;
    background: color-mix(in srgb, var(--muted) 22%, transparent);
    overflow: hidden;
  }
  .rate-bar {
    display: block;
    height: 100%;
    border-radius: 4px;
    background: var(--pass);
  }
  .detail-row td {
    padding: 0;
    background: color-mix(in srgb, var(--surface) 55%, transparent);
  }
  .model-detail {
    padding: 0;
  }
  .model-detail summary {
    cursor: pointer;
    color: var(--ink);
    padding: 11px 12px;
    font-size: 13px;
    border-bottom: 1px solid var(--hairline);
  }
  .model-detail summary:hover { background: color-mix(in srgb, var(--surface) 70%, transparent); }
  .detail-content {
    display: grid;
    grid-template-columns: minmax(0, .86fr) minmax(260px, 1.14fr);
    gap: 22px;
    padding: 16px 12px 20px;
  }
  .detail-heading {
    margin: 0 0 8px;
    color: var(--muted);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: .08em;
    text-transform: uppercase;
  }
  .quality-lines {
    display: grid;
    gap: 6px;
    margin: 12px 0 0;
    color: var(--muted);
    font-size: 13px;
  }
  .quality-lines b { color: var(--ink); }
  .notes-panel {
    min-width: 0;
  }
  .breakdown {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .breakdown th, .breakdown td {
    border-bottom: 1px solid var(--hairline);
    padding: 8px 6px;
    text-align: left;
  }
  .breakdown th {
    color: var(--muted);
    font-weight: 650;
  }
  .notes-list {
    display: grid;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
    color: var(--ink);
  }
  .notes-list li {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 12px;
    align-items: baseline;
  }
  .notes-list time {
    color: var(--muted);
    font-size: 11px;
    font-variant-caps: all-small-caps;
    letter-spacing: .08em;
    white-space: nowrap;
  }
  .more-notes {
    color: var(--muted);
    font-size: 12px;
  }
  .empty-note { color: var(--muted); margin: 0; }
  .muted { color: var(--muted); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
  footer.scoreboard-footer {
    margin-top: 14px;
    color: var(--muted);
    font-size: 12px;
  }
  @media (prefers-color-scheme: dark) {
    .watch-chip { background: rgba(255, 255, 255, .035); }
  }
  @media (max-width: 820px) {
    body { padding: 18px 14px; }
    .scoreboard-header, .detail-content { grid-template-columns: 1fr; }
    .scoreboard-meta { justify-content: flex-start; }
    .table-scroll { margin-left: -14px; margin-right: -14px; padding-left: 14px; }
    .notes-list li { grid-template-columns: 1fr; gap: 2px; }
  }
"""

def render_free_watchlist(
    catalog_models: list[dict[str, Any]],
    catalog_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
) -> str:
    normalized = [normalize_catalog_for_scoreboard(model) for model in catalog_models]
    catalog_by_id = catalog_models_by_id(normalized)
    free_models = sorted(
        (model for model in normalized if model.get("free")),
        key=lambda item: (-int(item.get("context_length") or 0), str(item.get("id") or "")),
    )
    if free_models:
        free_items = "".join(
            watchlist_chip_html(model)
            for model in free_models[:6]
        )
    else:
        free_items = '<li class="muted">no free catalog models in the current snapshot</li>'
    if events is None:
        events = read_catalog_events(catalog_changes_path(catalog_path), limit=6)
    event_items = "".join(
        f"<li>{html_escape(humanized_catalog_event_line(event, catalog_by_id))}</li>"
        for event in events[:3]
    )
    if not event_items:
        event_items = '<li class="muted">no catalog change log found</li>'
    return f"""<section class="watchlist" aria-label="free model watchlist">
    <div class="watchline">
      <span class="watch-title">{fmt_int(len(free_models))} models free on OpenRouter right now</span>
      <ul class="watch-chips">{free_items}</ul>
      <ul class="event-list">{event_items}</ul>
    </div>
  </section>"""

def render_model_table_pair(
    row: dict[str, Any],
    *,
    notes_sections: dict[str, list[str]],
    notes_path: Path,
) -> str:
    model_id = str(row.get("model") or "")
    model_display = str(row.get("model_display") or model_id)
    lab = str(row.get("lab") or "(unknown)")
    harness = str(row.get("harness") or "unknown")
    access = str(row.get("access") or "unknown")
    last_verified = str(row.get("last_verified") or "")
    notes = list(row.get("notes") or model_judgment_notes_for_row(row, notes_sections))
    latest_note = str(
        row.get("latest_note") or (strip_inline_markdown(notes[0]) if notes else "")
    )
    notes_title = "\n\n".join(strip_inline_markdown(note) for note in notes)
    if row.get("unattributed"):
        model_id_line = (
            f'<div class="model-id">engine: {html_escape(str(row.get("engine") or "unknown"))} '
            '· quarantined legacy data</div>'
        )
    else:
        model_id_line = ""
    tier = str(row.get("tier") or "")
    tier_display = (
        "not ranked" if row.get("unattributed") or row.get("misrouted") else tier
    )
    row_id = str(row.get("display_bucket_id") or "model")
    unregistered_flag = (
        '<span class="identity-flag">unregistered</span>' if row.get("unregistered") else ""
    )
    misrouted_flag = (
        '<span class="identity-flag misrouted">misrouted</span>'
        if row.get("misrouted")
        else ""
    )
    verified_date = (
        f'<span class="verified-date">verified {html_escape(last_verified)}</span>'
        if last_verified
        else ""
    )
    return f"""<tr class="model-row" id="model-{html_escape(sanitize_artifact_name(row_id))}">
      <td class="model-cell"><div class="model-name">{html_escape(model_display)}</div>{model_id_line}{unregistered_flag}{misrouted_flag}</td>
      <td>{html_escape(lab)}{verified_date}</td>
      <td>{html_escape(harness)}</td>
      <td>{html_escape(access)}</td>
      <td><span class="tier-badge {html_escape(tier)}">{html_escape(tier_display)}</span></td>
      <td class="num">{fmt_int(row.get("tasks"))}</td>
      <td class="num rate-cell">{rate_cell_html(row.get("first_try_pass_rate"))}</td>
      <td class="num rate-cell">{rate_cell_html(row.get("pass_rate"))}</td>
      <td class="num">{html_escape(fmt_int(row.get("median_tokens"))) if row.get("median_tokens") is not None else ""}</td>
      <td>{html_escape(fmt_scoreboard_duration(row.get("median_duration_ms")))}</td>
      <td>{html_escape(humanized_log_date(row.get("last_seen")))}</td>
      <td class="notes-cell" title="{html_escape(notes_title)}">{html_escape(latest_note)}</td>
    </tr>
    <tr class="detail-row">
      <td colspan="12">
        <details class="model-detail">
          <summary>details for {html_escape(model_display)}</summary>
          <div class="detail-content">
            <div>
              <h3 class="detail-heading">Task types</h3>
              {render_task_breakdown_table(row.get("task_types", []))}
              <div class="quality-lines">
                <div><b>Best:</b> {html_escape(derived_quality_text(row, best=True))}</div>
                <div><b>Worst:</b> {html_escape(derived_quality_text(row, best=False))}</div>
              </div>
            </div>
            <div class="notes-panel">
              <h3 class="detail-heading">Judgment notes</h3>
              {render_notes_list(notes, notes_path=notes_path)}
            </div>
          </div>
        </details>
      </td>
    </tr>"""

def render_model_scoreboard_html(
    *,
    rows: list[dict[str, Any]],
    log_path: Path,
    rows_read: int,
    skipped: int,
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
    notes_path: Path,
    notes_sections: dict[str, list[str]],
    catalog_events: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> str:
    catalog_by_id = catalog_models_by_id(catalog_models)
    ordered = order_model_scoreboard_rows(rows, catalog_by_id)
    generated = generated_at or datetime.now().astimezone().replace(microsecond=0).isoformat()
    rendered_rows: list[str] = []
    for row in ordered:
        rendered_rows.append(
            render_model_table_pair(
                row,
                notes_sections=notes_sections,
                notes_path=notes_path,
            )
        )
    table_rows = "".join(rendered_rows)
    if not table_rows:
        table_rows = '<tr><td colspan="12" class="muted">No local model evidence matched these filters.</td></tr>'
    unregistered_slugs = sorted(
        {str(row.get("model") or "") for row in ordered if row.get("unregistered") and row.get("model")}
    )
    unregistered_pointer = ""
    if unregistered_slugs:
        pointer = (
            f"Unregistered model slug(s): {', '.join(unregistered_slugs)} "
            "— run the identity procedure in docs/TAXONOMY.md."
        )
        unregistered_pointer = f'<div class="identity-pointer">{html_escape(pointer)}</div>'
    misrouted_routes = sorted(
        {
            f"{row.get('engine')}:{row.get('model')} → {row.get('canonical_route')}"
            for row in ordered
            if row.get("misrouted") and row.get("model")
        }
    )
    misrouted_pointer = ""
    if misrouted_routes:
        misrouted_pointer = (
            '<div class="identity-pointer">Noncanonical route(s): '
            f"{html_escape(', '.join(misrouted_routes))}</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{CSP_META_TAG}
<title>ringer model scoreboard</title>
<style>{ARTIFACT_BASE_CSS}
{MODEL_SCOREBOARD_CSS}</style>
</head>
<body>
<div class="page scoreboard-page">
  <header class="scoreboard-header">
    <h1 class="scoreboard-title">Model performance scoreboard</h1>
    <div class="scoreboard-meta">
      <span>Generated {html_escape(humanized_log_date(generated))}</span>
      <nav class="source-links" aria-label="scoreboard source files">
        {source_file_link(log_path, "eval log")}
        {source_file_link(catalog_path, "catalog")}
        {source_file_link(notes_path, "model notes")}
      </nav>
    </div>
  </header>
  {render_free_watchlist(catalog_models, catalog_path, events=catalog_events)}
  <main>
    <div class="table-scroll">
      <table class="ranked-table">
        <thead>
          <tr>
            <th>Model</th>
            <th>Lab</th>
            <th>Harness</th>
            <th>API/Plan</th>
            <th>Tier</th>
            <th class="num">Tasks</th>
            <th class="num">First try</th>
            <th class="num">Pass</th>
            <th class="num">Tokens (median)</th>
            <th>Speed (median)</th>
            <th>Last used</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </main>
  <footer class="scoreboard-footer">
    <span>{fmt_int(rows_read)} rows read, {fmt_int(skipped)} skipped lines. Ordering sorts by evidence tier first: proven n&gt;=3, then probation; ties use first-try pass rate and pass rate. Misrouted and unattributed legacy rows are not ranked or tiered.</span>
    {unregistered_pointer}
    {misrouted_pointer}
  </footer>
</div>
</body>
</html>
"""

def write_model_scoreboard_html(
    config: AppConfig,
    *,
    path: Path | None,
    rows: list[dict[str, Any]],
    log_path: Path,
    rows_read: int,
    skipped: int,
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
    notes_path: Path,
    notes_sections: dict[str, list[str]],
    catalog_events: list[dict[str, Any]] | None = None,
) -> Path:
    target = path
    if target is None:
        target = artifact_live_path(config.state_dir, MODEL_SCOREBOARD_RUN_NAME)
    target = target.expanduser().resolve()
    html = render_model_scoreboard_html(
        rows=rows,
        log_path=log_path,
        rows_read=rows_read,
        skipped=skipped,
        catalog_path=catalog_path,
        catalog_models=catalog_models,
        catalog_events=catalog_events,
        notes_path=notes_path,
        notes_sections=notes_sections,
    )
    atomic_write_text(target, html)
    if path is None:
        update_artifact_library_live(
            config.state_dir,
            run_name=MODEL_SCOREBOARD_RUN_NAME,
            run_id=MODEL_SCOREBOARD_RUN_NAME,
            identity=MODEL_SCOREBOARD_IDENTITY,
            state="pass",
        )
    return target
