from __future__ import annotations

import base64
import contextlib
import mimetypes
import os
import re
import urllib.parse
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any

from ringer_core.artifact_store import sanitize_artifact_name, state_tasks
from ringer_core.state_files import atomic_write_text
from ringer_core.worker_logs import shorten


ARTIFACT_WRAPPER_TAIL_BYTES = 256 * 1024


TASK_REPORT_FILENAMES = ("report.md", "report.html")


TEXT_DELIVERABLE_SUFFIXES = {".md", ".txt", ".log"}


IMAGE_DELIVERABLE_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


CSP_META_TAG = (
    '<meta http-equiv="Content-Security-Policy" '
    'content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">'
)


STATUS_COLORS = {
    "pass": "var(--pass)",
    "fail": "var(--fail)",
    "error": "var(--fail)",
    "timeout": "var(--fail)",
    "running": "var(--running)",
    "retrying": "var(--running)",
    "verifying": "var(--running)",
    "queued": "var(--waiting)",
    "died": "var(--fail)",
    "live": "var(--running)",
    "finished": "var(--pass)",
}


def status_color(status: str) -> str:
    return STATUS_COLORS.get(str(status).lower(), "var(--waiting)")


def fmt_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_compact_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def fmt_plain_ago(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"
    minutes, seconds_left = divmod(total, 60)
    if minutes < 60:
        if seconds_left == 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return (
            f"{minutes} minute{'s' if minutes != 1 else ''} "
            f"{seconds_left} second{'s' if seconds_left != 1 else ''}"
        )
    hours, minutes_left = divmod(minutes, 60)
    if minutes_left == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return (
        f"{hours} hour{'s' if hours != 1 else ''} "
        f"{minutes_left} minute{'s' if minutes_left != 1 else ''}"
    )


ARTIFACT_BASE_CSS = """
  :root {
    color-scheme: dark;
    --ground: #0b0e14;
    --surface: #141a26;
    --ink: #e9eef7;
    --muted: #8fa0b6;
    --hairline: rgba(143, 160, 182, .22);
    --accent: #35d0ff;
    --pass: #45d17e;
    --fail: #ff5f6b;
    --waiting: #6f7c92;
    --quote-bg: rgba(255, 95, 107, .08);
  }
  @media (prefers-color-scheme: light) {
    :root {
      color-scheme: light;
      --ground: #f2f5f9;
      --surface: #ffffff;
      --ink: #17202e;
      --muted: #5a6a7e;
      --hairline: rgba(90, 106, 126, .28);
      --accent: #007fb0;
      --pass: #178a4c;
      --fail: #cc3340;
      --waiting: #7d8ba0;
      --quote-bg: rgba(204, 51, 64, .07);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --ground: #0b0e14; --surface: #141a26; --ink: #e9eef7; --muted: #8fa0b6;
    --hairline: rgba(143,160,182,.22); --accent: #35d0ff; --pass: #45d17e;
    --fail: #ff5f6b; --waiting: #6f7c92; --quote-bg: rgba(255,95,107,.08);
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --ground: #f2f5f9; --surface: #ffffff; --ink: #17202e; --muted: #5a6a7e;
    --hairline: rgba(90,106,126,.28); --accent: #007fb0; --pass: #178a4c;
    --fail: #cc3340; --waiting: #7d8ba0; --quote-bg: rgba(204,51,64,.07);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    min-height: 100%;
    overflow-x: hidden;
    background: var(--ground);
    color: var(--ink);
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    line-height: 1.5;
  }
  body {
    padding: clamp(18px, 4vw, 52px);
  }
  .page {
    max-width: 860px;
    margin: 0 auto;
  }
  .corner {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: clamp(14px, 3vw, 26px);
  }
  .live-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--accent);
    align-self: center;
    flex: 0 0 9px;
  }
  .live-dot.pass { background: var(--pass); }
  .live-dot.fail, .live-dot.retry { background: var(--fail); }
  .live-dot.waiting { background: var(--waiting); }
  @media (prefers-reduced-motion: no-preference) {
    .live-dot.is-live { animation: pulse 1.4s ease-in-out infinite; }
    @keyframes pulse { 50% { opacity: .35; } }
  }
  .eyebrow {
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .12em;
    text-transform: uppercase;
  }
  .eyebrow b {
    color: var(--ink);
  }
  .clock {
    margin-left: auto;
    color: var(--muted);
    font-size: 12px;
  }
  .briefing {
    max-width: 30ch;
    margin: 0 0 clamp(16px, 3vw, 24px);
    font-size: clamp(20px, 3.4vw, 30px);
    font-weight: 800;
    letter-spacing: 0;
    line-height: 1.25;
    text-wrap: balance;
  }
  .briefing .n-pass { color: var(--pass); }
  .briefing .n-fail { color: var(--fail); }
  .rounds {
    display: flex;
    gap: 5px;
    margin-bottom: 8px;
  }
  .rounds span {
    flex: 1;
    height: 7px;
    border-radius: 4px;
    background: var(--waiting);
    opacity: .45;
  }
  .rounds .pass { background: var(--pass); opacity: 1; }
  .rounds .working { background: var(--accent); opacity: 1; }
  .rounds .retry, .rounds .fail { background: var(--fail); opacity: 1; }
  @media (prefers-reduced-motion: no-preference) {
    .rounds .working, .rounds .retry { animation: pulse 1.4s ease-in-out infinite; }
  }
  .legend {
    margin: 0;
    margin-bottom: clamp(26px, 5vw, 40px);
    color: var(--muted);
    font-size: 12.5px;
  }
  .work {
    margin-bottom: clamp(28px, 5vw, 44px);
  }
  .work-list {
    display: grid;
    gap: 10px;
    margin-top: 10px;
  }
  .work-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 0;
    border-bottom: 1px solid var(--hairline);
  }
  .work-main {
    min-width: 0;
  }
  .work-link {
    color: var(--ink);
    font-size: 15px;
    font-weight: 750;
  }
  .work-kind {
    margin-top: 2px;
    color: var(--muted);
    font-size: 12.5px;
  }
  .work-task {
    display: inline-block;
    margin-left: 6px;
  }
  .work-thumb-link {
    flex: 0 0 auto;
  }
  .work-thumb {
    display: block;
    max-width: 132px;
    max-height: 96px;
    border: 1px solid var(--hairline);
    border-radius: 6px;
    object-fit: cover;
  }
  .work.is-primary .work-list {
    gap: 12px;
  }
  .work.is-primary .work-link {
    font-size: clamp(17px, 2.6vw, 22px);
  }
  .work-group .worker {
    border-bottom: none;
    padding-bottom: 6px;
  }
  .work-group-body {
    padding: 0 0 14px 30px;
    border-bottom: 1px solid var(--hairline);
  }
  .work-group:last-child .work-group-body { border-bottom: none; }
  .work-group-body .work-item:last-of-type { border-bottom: none; }
  .work-group-body .empty-note { margin: 4px 0 8px; }
  .work-group-body .verified {
    display: block;
    margin-top: 6px;
    color: var(--muted);
    font-size: 13px;
    overflow-wrap: break-word;
  }
  .work-group-body .proof {
    margin-top: 4px;
    font-size: 12px;
  }
  .work-group-body .proof summary {
    cursor: pointer;
    color: var(--accent);
  }
  .work-group-body .proof pre {
    margin: 6px 0 0;
    padding: 10px 12px;
    max-height: 200px;
    overflow: auto;
    border-left: 2px solid var(--hairline);
    background: var(--surface);
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .work-group-body .links {
    display: block;
    margin-top: 8px;
    font-size: 13px;
  }
  .work-group-body .links a {
    color: var(--accent);
    text-decoration: none;
  }
  .work-group-body .links a:hover,
  .work-group-body .links a:focus-visible {
    text-decoration: underline;
  }
  .work.is-primary .work-group {
    padding: 12px 16px 2px;
    border: 1px solid var(--hairline);
    border-radius: 8px;
    background: var(--surface);
  }
  .work.is-primary .work-group-body { border-bottom: none; }
  section h2 {
    margin: 0 0 4px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--hairline);
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .1em;
    text-transform: uppercase;
  }
  .timeline {
    margin-bottom: clamp(28px, 5vw, 44px);
  }
  details.timeline > summary {
    cursor: pointer;
    list-style: none;
  }
  details.timeline > summary::-webkit-details-marker { display: none; }
  details.timeline > summary h2::after {
    content: " ▸";
    color: var(--muted);
    font-size: 11px;
  }
  details.timeline[open] > summary h2::after { content: " ▾"; }
  details.timeline > summary:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .tl-row {
    display: grid;
    grid-template-columns: 76px minmax(0,1fr);
    gap: 14px;
    padding: 10px 0;
    border-bottom: 1px solid var(--hairline);
    font-size: 14px;
  }
  .tl-row time {
    color: var(--muted);
    font-size: 12px;
    padding-top: 2px;
  }
  .tl-row .catch {
    margin: 6px 0 0;
    padding: 8px 12px;
    background: var(--quote-bg);
    border-left: 2px solid var(--fail);
    border-radius: 0 6px 6px 0;
    color: var(--muted);
    font-size: 13px;
    overflow-wrap: break-word;
  }
  .tl-row .catch b {
    color: var(--fail);
    font-weight: 650;
  }
  .workers {
    margin-bottom: clamp(28px, 5vw, 44px);
  }
  .worker {
    display: grid;
    grid-template-columns: 18px minmax(0,1fr) auto auto;
    gap: 4px 12px;
    align-items: baseline;
    padding: 12px 0;
    border-bottom: 1px solid var(--hairline);
  }
  .glyph {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    align-self: center;
  }
  .glyph.pass { background: var(--pass); }
  .glyph.working { background: var(--accent); }
  .glyph.retry, .glyph.fail { background: var(--fail); }
  .glyph.waiting {
    background: transparent;
    border: 1.5px solid var(--waiting);
  }
  @media (prefers-reduced-motion: no-preference) {
    .glyph.working, .glyph.retry { animation: pulse 1.4s ease-in-out infinite; }
  }
  .worker .name {
    min-width: 0;
    overflow: hidden;
    font-size: 15px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .worker .state {
    font-size: 13px;
    font-weight: 650;
    white-space: nowrap;
  }
  .state.pass { color: var(--pass); }
  .state.working { color: var(--accent); }
  .state.retry, .state.fail { color: var(--fail); }
  .state.waiting { color: var(--waiting); }
  .worker .time {
    color: var(--muted);
    font-size: 12.5px;
    white-space: nowrap;
  }
  .worker .activity {
    grid-column: 2 / -1;
    min-width: 0;
    overflow: hidden;
    color: var(--muted);
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .worker .verified {
    grid-column: 2 / -1;
    min-width: 0;
    color: var(--muted);
    font-size: 13px;
    overflow-wrap: break-word;
  }
  .worker .proof {
    grid-column: 2 / -1;
    min-width: 0;
    font-size: 12px;
  }
  .worker .proof summary {
    cursor: pointer;
    color: var(--accent);
  }
  .worker .proof pre {
    margin: 6px 0 0;
    padding: 10px 12px;
    max-height: 200px;
    overflow: auto;
    border-left: 2px solid var(--hairline);
    background: var(--surface);
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .worker .links {
    grid-column: 2 / -1;
    font-size: 13px;
  }
  .worker .links a {
    color: var(--accent);
    text-decoration: none;
  }
  .worker .links a:hover,
  .worker .links a:focus-visible {
    text-decoration: underline;
  }
  .runs {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .omitted-note,
  .empty-note {
    max-width: 65ch;
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.45;
  }
  .run-row {
    display: grid;
    gap: 16px;
    align-items: center;
    padding: 12px 0;
    border-top: 1px solid var(--hairline);
  }
  .run-row {
    grid-template-columns: minmax(0, 1.35fr) minmax(112px, .55fr) minmax(76px, .4fr) minmax(150px, .8fr);
  }
  .run-name {
    min-width: 0;
    overflow: hidden;
    color: var(--ink);
    font-weight: 700;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .run-state {
    color: var(--state-color);
    font-weight: 800;
  }
  .run-duration {
    color: var(--muted);
  }
  .run-links {
    display: flex;
    min-width: 0;
    flex-wrap: wrap;
    gap: 8px 14px;
  }
  .run-links .muted {
    color: var(--muted);
  }
  .state-pass { --state-color: var(--pass); }
  .state-fail { --state-color: var(--fail); }
  .state-running { --state-color: var(--accent); }
  .state-waiting { --state-color: var(--waiting); }
  .meta {
    max-width: 65ch;
    margin: 0 0 18px;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.55;
  }
  .meta b { color: var(--ink); }
  .mono,
  time {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }
  .muted { color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  th { color: var(--muted); font-weight: 700; font-size: 10px; letter-spacing: 0; }
  .chip { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 10px; font-weight: 800; color: var(--ground); white-space: nowrap; }
  pre {
    width: 100%;
    max-width: 100%;
    margin: 0;
    overflow: auto;
    border: 1px solid var(--hairline);
    border-radius: 6px;
    background: var(--surface);
    color: var(--ink);
    padding: clamp(14px,3vw,24px);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    line-height: 1.65;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  footer,
  .page-foot {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.5;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  @media (max-width: 640px) {
    .worker,
    .run-row {
      grid-template-columns: minmax(0, 1fr);
      gap: 6px;
    }
    .glyph {
      display: none;
    }
    .worker .activity,
    .worker .links {
      grid-column: 1 / -1;
    }
    .work-item,
    .work.is-primary .work-item {
      align-items: flex-start;
      padding: 12px 0;
      border-width: 0 0 1px;
      border-radius: 0;
      background: transparent;
    }
    .work-thumb {
      max-width: 96px;
      max-height: 72px;
    }
    .run-links {
      gap: 6px 12px;
    }
  }
"""


def file_href(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except ValueError:
        return "file://" + urllib.parse.quote(str(path))


def is_html_artifact(path: Path) -> bool:
    return path.suffix.lower() in {".html", ".htm"}


def deliverable_title(path: Path) -> str:
    name = path.name.lower()
    if name == "worker.log":
        return "Work log"
    if name in TASK_REPORT_FILENAMES:
        return "What this worker produced"
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem.capitalize() if stem else "Worker output"


class ArtifactRenderer:
    def __init__(self, artifact_path: Path) -> None:
        self.artifact_dir = artifact_path.parent
        self._wrapper_cache: dict[tuple[Path, Path], tuple[int, int]] = {}
        self._last_task_status: dict[str, str] = {}
        self._last_run_state: str | None = None
        self._seen_transition_keys: set[tuple[str, str]] = set()
        self._transition_log: list[dict[str, str]] = []

    def render_status_html(self, state: dict[str, Any], *, page_path: Path | None = None) -> str:
        return render_status_html(state, renderer=self, force_wrappers=False, page_path=page_path)

    def render_final_report_html(self, state: dict[str, Any], *, page_path: Path | None = None) -> str:
        return render_final_report_html(state, renderer=self, force_wrappers=True, page_path=page_path)

    def render_artifact_index_html(self, entries: list[dict[str, Any]]) -> str:
        return render_artifact_index_html(entries, renderer=self, force_wrappers=False)

    def transition_feed(self, state: dict[str, Any], *, limit: int | None = None) -> list[dict[str, str]]:
        self.record_transitions(state)
        if limit is None:
            return list(reversed(self._transition_log))
        return list(reversed(self._transition_log[-limit:]))

    def omitted_transition_count(self, limit: int) -> int:
        return max(0, len(self._transition_log) - limit)

    def record_transitions(self, state: dict[str, Any]) -> None:
        run_state = str(state.get("state", "live"))
        if self._last_run_state is None:
            if run_state == "live":
                self._append_transition(("run", "live"), "Ringer started")
        elif self._last_run_state != run_state and run_state == "finished":
            self._append_transition(("run", "finished"), "Ringer finished")
        self._last_run_state = run_state

        current_status: dict[str, str] = {}
        tasks = state.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_key = str(task.get("key", "task"))
            status = str(task.get("status", "queued"))
            previous = self._last_task_status.get(task_key)
            current_status[task_key] = status
            if previous == status:
                continue
            event = plain_transition_event(task_key, previous, status, task)
            if event:
                self._append_transition((task_key, status), event)
        self._last_task_status = current_status

    def _append_transition(self, key: tuple[str, str], event: str | dict[str, str]) -> None:
        if key in self._seen_transition_keys:
            return
        self._seen_transition_keys.add(key)
        if isinstance(event, str):
            event = {"line": event}
        self._transition_log.append({"time": datetime.now().strftime("%H:%M:%S"), **event})

    def link_for_source(
        self,
        source_path: Path,
        *,
        state: dict[str, Any] | None = None,
        run_id: str | None = None,
        run_name: str | None = None,
        task_key: str,
        force: bool = False,
    ) -> str:
        if is_html_artifact(source_path):
            return file_href(source_path)
        if not source_path.exists():
            return file_href(source_path)

        wrapper_path = self.wrapper_path(
            run_id=str(run_id or (state or {}).get("run_id") or "run"),
            task_key=task_key,
            source_name=source_path.name,
        )
        self.write_wrapper(
            source_path,
            wrapper_path,
            run_name=str(run_name or (state or {}).get("run_name") or "ringer"),
            task_key=task_key,
            force=force,
        )
        return file_href(wrapper_path)

    def wrapper_path(self, *, run_id: str, task_key: str, source_name: str) -> Path:
        filename = f"{sanitize_artifact_name(task_key)}--{sanitize_artifact_name(source_name)}.html"
        return self.artifact_dir / "view" / sanitize_artifact_name(run_id) / filename

    def write_wrapper(
        self,
        source_path: Path,
        wrapper_path: Path,
        *,
        run_name: str,
        task_key: str,
        force: bool = False,
    ) -> None:
        stat = source_path.stat()
        cache_key = (source_path.resolve(), wrapper_path)
        current = (stat.st_mtime_ns, stat.st_size)
        if not force and wrapper_path.exists() and self._wrapper_cache.get(cache_key) == current:
            return

        html = render_file_wrapper_html(
            source_path=source_path,
            source_stat=stat,
            run_name=run_name,
            task_key=task_key,
        )
        atomic_write_text(wrapper_path, html)
        self._wrapper_cache[cache_key] = current


def render_file_wrapper_html(
    *,
    source_path: Path,
    source_stat: os.stat_result,
    run_name: str,
    task_key: str,
) -> str:
    size = int(source_stat.st_size)
    truncated = size > ARTIFACT_WRAPPER_TAIL_BYTES
    start = max(0, size - ARTIFACT_WRAPPER_TAIL_BYTES)
    with source_path.open("rb") as fh:
        if start:
            fh.seek(start)
        raw = fh.read()
    content = raw.decode("utf-8", errors="replace")
    source_mtime = datetime.fromtimestamp(source_stat.st_mtime).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    truncation_note = (
        f" Showing the last <b>{ARTIFACT_WRAPPER_TAIL_BYTES:,}</b> bytes"
        f" of <b>{size:,}</b>."
        if truncated
        else ""
    )
    title = html_escape(deliverable_title(source_path))
    safe_run_name = html_escape(run_name)
    safe_task_key = html_escape(task_key)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{CSP_META_TAG}
<title>{title}</title>
<style>{ARTIFACT_BASE_CSS}</style>
</head>
<body>
<div class="page">
  <header class="corner">
    <span class="live-dot waiting" aria-hidden="true"></span>
    <span class="eyebrow">Ringer &nbsp;·&nbsp; <b>{safe_run_name}</b> &nbsp;·&nbsp; {safe_task_key}</span>
    <span class="clock mono">artifact</span>
  </header>
  <section class="timeline" aria-label="{title}">
    <h1 class="briefing">{title}</h1>
    <p class="meta">{safe_task_key} produced this on <b>{source_mtime}</b>.{truncation_note}</p>
  </section>
  <pre>{html_escape(content)}</pre>
</div>
</body>
</html>
"""


def task_status_counts(state: dict[str, Any]) -> dict[str, int]:
    tasks = state_tasks(state)
    buckets = [task_state_bucket(str(task.get("status", "queued"))) for task in tasks]
    pass_n = sum(1 for bucket in buckets if bucket == "pass")
    fail_n = sum(1 for bucket in buckets if bucket == "fail")
    running_n = sum(1 for bucket in buckets if bucket == "working")
    retry_n = sum(1 for bucket in buckets if bucket == "retry")
    waiting_n = sum(1 for bucket in buckets if bucket == "waiting")
    return {
        "total": len(tasks),
        "pass": pass_n,
        "fail": fail_n,
        "running": running_n,
        "retry": retry_n,
        "waiting": waiting_n,
    }


def task_word(count: int) -> str:
    return "task" if count == 1 else "tasks"


def passed_phrase(count: int) -> str:
    if count == 1:
        return "1 finished and checked"
    return f"{count} finished and checked"


def failed_phrase(count: int) -> str:
    if count == 1:
        return "1 failed"
    return f"{count} failed"


def running_phrase(count: int) -> str:
    if count == 1:
        return "1 working"
    return f"{count} working"


def retry_phrase(count: int) -> str:
    if count == 1:
        return "1 sent back"
    return f"{count} sent back"


def waiting_phrase(count: int) -> str:
    if count == 1:
        return "1 is waiting"
    return f"{count} are waiting"


def live_briefing_sentence(state: dict[str, Any]) -> str:
    return html_to_text(live_briefing_html(state))


def live_briefing_html(state: dict[str, Any]) -> str:
    counts = task_status_counts(state)
    elapsed = fmt_plain_ago(state.get("elapsed_s"))
    total = counts["total"]
    if total == 0:
        return f"Ringer has no tasks. Started {html_escape(elapsed)} ago."
    parts = []
    if counts["pass"]:
        parts.append(f'<span class="n-pass">{html_escape(passed_phrase(counts["pass"]))}</span>')
    if counts["running"]:
        parts.append(html_escape(running_phrase(counts["running"])))
    if counts["retry"]:
        parts.append(f'<span class="n-fail">{html_escape(retry_phrase(counts["retry"]))}</span>')
    if counts["waiting"]:
        parts.append(html_escape(waiting_phrase(counts["waiting"])))
    if counts["fail"]:
        parts.append(f'<span class="n-fail">{html_escape(failed_phrase(counts["fail"]))}</span>')
    status_sentence = join_plain_html_parts(parts)
    return (
        f"Ringer is working on {total} {task_word(total)} — "
        f"{status_sentence}, started {html_escape(elapsed)} ago."
    )


def final_briefing_sentence(state: dict[str, Any]) -> str:
    return html_to_text(final_briefing_html(state))


def final_briefing_html(state: dict[str, Any]) -> str:
    counts = task_status_counts(state)
    total = counts["total"]
    pass_n = counts["pass"]
    fail_n = counts["fail"]
    elapsed = fmt_compact_duration(state.get("elapsed_s"))
    first = f"Ringer finished {total} {task_word(total)} in {elapsed}."
    if fail_n == 0:
        return f"{html_escape(first)} <span class=\"n-pass\">All {total} finished and checked.</span>"
    return (
        f"{html_escape(first)} <span class=\"n-pass\">{pass_n} finished and checked</span>, "
        f"<span class=\"n-fail\">{fail_n} failed after retry.</span>"
    )


def join_plain_html_parts(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def html_to_text(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def plain_transition_line(
    task_key: str,
    previous_status: str | None,
    status: str,
    task: dict[str, Any],
) -> str | None:
    event = plain_transition_event(task_key, previous_status, status, task)
    if not event:
        return None
    return event["line"]


def plain_transition_event(
    task_key: str,
    previous_status: str | None,
    status: str,
    task: dict[str, Any],
) -> dict[str, str] | None:
    attempts = int(task.get("attempts") or 0)
    timed_out = bool(task.get("check_timed_out")) or status == "timeout"
    check_excerpt = first_check_output_line(task)
    if status == "running" and previous_status in {None, "queued"}:
        return {"line": f"{task_key} started"}
    if status == "retrying":
        if timed_out:
            return {"line": f"{task_key} timed out — trying again"}
        if check_excerpt:
            return {
                "line": f"{task_key} didn't finish cleanly — sent back to redo the work.",
                "catch": check_excerpt,
            }
        return {"line": f"{task_key} did not finish cleanly — trying again"}
    if status == "pass":
        if attempts > 1:
            return {"line": f"{task_key} passed on the second try, {fmt_compact_duration(task.get('elapsed_s'))}"}
        return {"line": f"{task_key} finished and checked, {fmt_compact_duration(task.get('elapsed_s'))}"}
    if status == "fail":
        if timed_out:
            return {"line": f"{task_key} timed out"}
        if check_excerpt:
            return {"line": f"{task_key} could not finish.", "catch": check_excerpt}
        if attempts > 1:
            return {"line": f"{task_key} failed after the second try"}
        return {"line": f"{task_key} failed"}
    if status == "timeout":
        return {"line": f"{task_key} timed out"}
    return None


def first_check_output_line(task: dict[str, Any]) -> str:
    raw = task.get("check_output_tail") or task.get("check_output") or ""
    for line in str(raw).splitlines():
        clean = line.strip()
        if clean:
            return shorten(clean, 120)
    return ""


def task_state_bucket(status: str) -> str:
    status = str(status).lower()
    if status == "pass":
        return "pass"
    if status in {"fail", "error", "timeout", "died"}:
        return "fail"
    if status == "retrying":
        return "retry"
    if status in {"running", "verifying"}:
        return "working"
    return "waiting"


def task_state_word(status: str) -> str:
    bucket = task_state_bucket(status)
    if bucket == "pass":
        return "finished & checked"
    if bucket == "working":
        return "working"
    if bucket == "retry":
        return "sent back — redoing"
    if bucket == "fail":
        return "failed"
    return "waiting"


def local_time_label() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S %Z")


def render_progress_bar(tasks: list[dict[str, Any]], counts: dict[str, int]) -> str:
    segments = []
    for task in tasks:
        key = html_escape(str(task.get("key", "task")))
        bucket = task_state_bucket(str(task.get("status", "queued")))
        state_word = html_escape(task_state_word(str(task.get("status", "queued"))))
        css_class = "" if bucket == "waiting" else f' class="{bucket}"'
        segments.append(
            f'<span{css_class} aria-label="{key}: {state_word}"></span>'
        )
    bar = "".join(segments) if segments else ""
    legend_parts = []
    if counts["pass"]:
        legend_parts.append(f'{counts["pass"]} finished')
    if counts["running"]:
        legend_parts.append(f'{counts["running"]} working')
    if counts["retry"]:
        legend_parts.append(f'{counts["retry"]} sent back')
    if counts["fail"]:
        legend_parts.append(f'{counts["fail"]} failed')
    if counts["waiting"]:
        legend_parts.append(f'{counts["waiting"]} waiting')
    legend = " · ".join(legend_parts) if legend_parts else "No tasks"
    aria = (
        f'{counts["total"]} tasks: {counts["pass"]} passed, {counts["running"]} working, '
        f'{counts["retry"]} retrying, {counts["waiting"]} waiting, {counts["fail"]} failed'
    )
    return f"""<div class="rounds" role="img" aria-label="{html_escape(aria)}">{bar}</div>
    <p class="legend">{html_escape(legend)}</p>"""


def render_work_section(
    state: dict[str, Any],
    *,
    renderer: ArtifactRenderer | None,
    page_path: Path | None,
    force_wrappers: bool = False,
    primary: bool = False,
    finished_only: bool = False,
) -> str:
    # One section carries the whole story: each worker, what it delivered,
    # how the delivery was checked, and where the raw log lives. The old
    # separate "The workers" strip and "What's happening" timeline repeated
    # this information; per-worker live detail belongs to Ringside's agent
    # accordion, not the artifact.
    tasks = state_tasks(state)
    if finished_only:
        tasks = [
            task
            for task in tasks
            if task_state_bucket(str(task.get("status", "queued"))) in {"pass", "fail"}
        ]
    section_class = "work is-primary" if primary else "work"
    if not tasks:
        empty_note = "Deliverables appear here as workers finish." if finished_only else "No tasks."
        body = f'<p class="empty-note">{empty_note}</p>'
    else:
        groups = "".join(
            render_work_group(
                task,
                state=state,
                renderer=renderer,
                page_path=page_path,
                force_wrappers=force_wrappers,
            )
            for task in tasks
        )
        body = f'<div class="work-list">{groups}</div>'
    return f"""<section class="{section_class}" aria-labelledby="the-work-heading">
    <h2 id="the-work-heading">The work</h2>
    {body}
  </section>"""


def render_work_group(
    task: dict[str, Any],
    *,
    state: dict[str, Any],
    renderer: ArtifactRenderer | None,
    page_path: Path | None,
    force_wrappers: bool = False,
) -> str:
    task_key = str(task.get("key", "task"))
    key = html_escape(task_key)
    status = str(task.get("status", "queued"))
    bucket = task_state_bucket(status)
    css_bucket = "working" if bucket == "working" else bucket
    state_word = html_escape(task_state_word(status))
    elapsed = html_escape(fmt_compact_duration(task.get("elapsed_s")))

    activity = task_activity_line(task, bucket)
    activity_html = (
        f'<span class="activity" title="{html_escape(activity)}">{html_escape(activity)}</span>'
        if activity
        else ""
    )

    deliverables = [item for item in (task.get("deliverables") or []) if isinstance(item, dict)]
    rows = [
        render_work_item(
            item,
            task_key=task_key,
            state=state,
            renderer=renderer,
            page_path=page_path,
            force_wrappers=force_wrappers,
        )
        for item in deliverables
    ]
    if rows:
        items_html = "".join(rows)
    elif bucket == "pass":
        items_html = '<p class="empty-note">Finished and checked — this worker filed nothing to the shelf.</p>'
    elif bucket == "fail":
        items_html = '<p class="empty-note">Failed its check — nothing was delivered.</p>'
    elif bucket in {"working", "retry"}:
        items_html = '<p class="empty-note">Nothing delivered yet — still on it.</p>'
    else:
        items_html = '<p class="empty-note">Waiting its turn.</p>'

    # Close the trust loop where the results live: say in plain English what
    # the check proved, and keep the raw evidence one click away. The proof
    # stands on its own — a failed task shows why it failed even when no
    # 'verified' sentence was written.
    verified_html = ""
    if bucket in {"pass", "fail"}:
        verified_text = str(task.get("verified") or "").strip()
        proof_tail = str(task.get("check_output_tail") or "").strip()
        if verified_text:
            how_label = "How it was checked" if bucket == "pass" else "What the check demanded"
            verified_html += f'<span class="verified">{how_label}: {html_escape(verified_text)}</span>'
        if proof_tail:
            proof_label = "See the proof" if bucket == "pass" else "See why it failed"
            verified_html += (
                f'<details class="proof"><summary>{proof_label}</summary>'
                f"<pre>{html_escape(shorten(proof_tail, 1200))}</pre></details>"
            )

    links_html = render_task_links(
        task,
        state=state,
        renderer=renderer,
        force_wrappers=force_wrappers,
        page_path=page_path,
    )

    return f"""<div class="work-group">
      <div class="worker">
        <span class="glyph {css_bucket}" aria-hidden="true"></span>
        <span class="name" title="{key}">{key}</span>
        <span class="state {css_bucket}">{state_word}</span>
        <span class="time mono">{elapsed}</span>
        {activity_html}
      </div>
      <div class="work-group-body">
        {items_html}
        {verified_html}
        <span class="links">{links_html}</span>
      </div>
    </div>"""


def render_work_item(
    item: dict[str, Any],
    *,
    task_key: str,
    state: dict[str, Any],
    renderer: ArtifactRenderer | None,
    page_path: Path | None,
    force_wrappers: bool = False,
) -> str:
    name = str(item.get("name", "")).strip() or "work"
    source_path = Path(str(item.get("path", "")))
    label, kind = work_label_and_kind(name)
    href = work_item_href(
        source_path,
        state=state,
        task_key=task_key,
        renderer=renderer,
        page_path=page_path,
        force_wrappers=force_wrappers,
    )
    thumb = ""
    if is_image_deliverable(source_path):
        thumb_src = image_data_uri(source_path)
        if thumb_src:
            thumb = (
                f'<a class="work-thumb-link" href="{html_escape(href)}">'
                f'<img class="work-thumb" src="{html_escape(thumb_src)}" alt=""></a>'
            )
    return f"""<div class="work-item">
      {thumb}
      <div class="work-main">
        <a class="work-link" href="{html_escape(href)}">{html_escape(label)}</a>
        <div class="work-kind">{html_escape(kind)}</div>
      </div>
    </div>"""


def work_item_href(
    source_path: Path,
    *,
    state: dict[str, Any],
    task_key: str,
    renderer: ArtifactRenderer | None,
    page_path: Path | None,
    force_wrappers: bool,
) -> str:
    if renderer is None:
        return "#"
    if is_text_deliverable(source_path) and source_path.exists():
        wrapper_path = renderer.wrapper_path(
            run_id=str(state.get("run_id") or "run"),
            task_key=task_key,
            source_name=source_path.name,
        )
        renderer.write_wrapper(
            source_path,
            wrapper_path,
            run_name=str(state.get("run_name") or "ringer"),
            task_key=task_key,
            force=force_wrappers,
        )
        return artifact_relative_href(
            wrapper_path,
            page_path=page_path,
            artifact_root=renderer.artifact_dir,
        )
    return artifact_relative_href(source_path, page_path=page_path, artifact_root=renderer.artifact_dir)


def artifact_relative_href(target: Path, *, page_path: Path | None, artifact_root: Path) -> str:
    try:
        root = artifact_root.resolve()
        resolved_target = target.resolve()
        if resolved_target != root and root not in resolved_target.parents:
            return "#"
        start = (page_path.parent if page_path is not None else artifact_root).resolve()
        rel = os.path.relpath(resolved_target, start)
    except (OSError, ValueError):
        return "#"
    return urllib.parse.quote(Path(rel).as_posix(), safe="/._-~")


def work_label_and_kind(name: str) -> tuple[str, str]:
    path = Path(name)
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    pretty = stem[:1].upper() + stem[1:] if stem else "Work"
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        kind = "web page"
    elif suffix in IMAGE_DELIVERABLE_SUFFIXES:
        kind = "image"
    elif suffix in TEXT_DELIVERABLE_SUFFIXES:
        kind = "document"
    else:
        kind = "download"
    return f"{pretty} — {kind}", kind


def is_text_deliverable(path: Path) -> bool:
    return path.suffix.lower() in TEXT_DELIVERABLE_SUFFIXES


def is_image_deliverable(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_DELIVERABLE_SUFFIXES


def image_data_uri(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    guessed, _encoding = mimetypes.guess_type(str(path))
    mime = guessed if guessed and guessed.startswith("image/") else "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def render_corner_header(state: dict[str, Any], *, live: bool) -> str:
    run_name = html_escape(str(state.get("run_name", "ringer")))
    identity = html_escape(str(state.get("identity", "unknown")))
    elapsed = html_escape(fmt_compact_duration(state.get("elapsed_s")))
    dot_class = "live-dot is-live" if live else f"live-dot {final_dot_bucket(state)}"
    clock_label = f"{elapsed} elapsed" if live else f"{elapsed} total"
    return f"""<header class="corner">
    <span class="{dot_class}" aria-hidden="true"></span>
    <span class="eyebrow">Ringer &nbsp;·&nbsp; <b>{run_name}</b> &nbsp;·&nbsp; {identity}</span>
    <span class="clock mono">{clock_label}</span>
  </header>"""


def final_dot_bucket(state: dict[str, Any]) -> str:
    counts = task_status_counts(state)
    if counts["fail"]:
        return "fail"
    if counts["pass"]:
        return "pass"
    return "waiting"


def render_status_html(
    state: dict[str, Any],
    renderer: ArtifactRenderer | None = None,
    *,
    force_wrappers: bool = False,
    page_path: Path | None = None,
) -> str:
    """Tier 0 zero-LLM live status artifact. Rendered on every state flush (~1s)."""
    run_name = html_escape(str(state.get("run_name", "ringer")))
    tasks = state_tasks(state)
    counts = task_status_counts(state)
    briefing = live_briefing_html(state)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{CSP_META_TAG}
<title>ringer &middot; {run_name}</title>
<meta http-equiv="refresh" content="2">
<style>{ARTIFACT_BASE_CSS}</style>
</head>
<body>
<div class="page">
  {render_corner_header(state, live=True)}
  <h1 id="right-now-heading" class="briefing">{briefing}</h1>
  {render_progress_bar(tasks, counts)}
  {render_work_section(state, renderer=renderer, page_path=page_path, force_wrappers=force_wrappers, finished_only=True)}
  <footer>
    <span class="mono">Updated {html_escape(local_time_label())}</span>
    <span>·</span>
    <span>This page updates itself while the work runs.</span>
  </footer>
</div>
</body>
</html>
"""


def render_final_report_html(
    state: dict[str, Any],
    renderer: ArtifactRenderer | None = None,
    *,
    force_wrappers: bool = True,
    page_path: Path | None = None,
) -> str:
    """Feature 4: self-contained final report, rendered once when a run finishes."""
    run_name = html_escape(str(state.get("run_name", "ringer")))
    tasks = state_tasks(state)
    counts = task_status_counts(state)
    briefing = final_briefing_html(state)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{CSP_META_TAG}
<title>ringer report &middot; {run_name}</title>
<style>{ARTIFACT_BASE_CSS}</style>
</head>
<body>
<div class="page">
  {render_corner_header(state, live=False)}
  <h1 id="what-happened-heading" class="briefing">What happened — {briefing}</h1>
  {render_progress_bar(tasks, counts)}
  {render_work_section(state, renderer=renderer, page_path=page_path, force_wrappers=force_wrappers, primary=True)}
  <footer>
    <span class="mono">Finished {html_escape(local_time_label())}</span>
  </footer>
</div>
</body>
</html>
"""


def task_activity_line(task: dict[str, Any], bucket: str) -> str:
    if bucket not in {"working", "retry"}:
        return ""
    activity = task.get("activity") or task.get("last_action") or task.get("last-action") or ""
    return str(activity).strip()


def render_task_links(
    task: dict[str, Any],
    *,
    state: dict[str, Any],
    renderer: ArtifactRenderer | None = None,
    force_wrappers: bool = False,
    page_path: Path | None = None,
) -> str:

    def portable(href_path: Path) -> str:
        # A page viewed over http cannot follow file:// links — resolve
        # anything inside the artifact store to a relative href instead.
        if renderer is not None:
            with contextlib.suppress(Exception):
                resolved = href_path.resolve()
                if resolved.is_relative_to(renderer.artifact_dir.resolve()):
                    return artifact_relative_href(
                        resolved, page_path=page_path, artifact_root=renderer.artifact_dir
                    )
        return file_href(href_path)
    links: list[str] = []
    taskdir_path: Path | None = None
    taskdir = task.get("taskdir")
    if taskdir:
        taskdir_path = Path(str(taskdir))

    task_key = str(task.get("key", "task"))

    report_paths = task.get("report_paths") or {}
    if not isinstance(report_paths, dict):
        report_paths = {}
    for report_name in TASK_REPORT_FILENAMES:
        report_value = report_paths.get(report_name)
        report_file = Path(str(report_value)) if report_value else None
        if report_file is None and taskdir_path is not None:
            report_file = taskdir_path / report_name
        if report_file is not None and report_file.exists():
            if renderer:
                renderer.link_for_source(report_file, state=state, task_key=task_key, force=force_wrappers)
                href = portable(renderer.wrapper_path(run_id=str(state.get("run_id") or "run"), task_key=task_key, source_name=report_file.name)) if not is_html_artifact(report_file) else portable(report_file)
            else:
                href = file_href(report_file)
            links.append(f'<a href="{html_escape(href)}">Read what it found</a>')
            break

    log_path = task.get("log_path")
    worker_log = Path(str(log_path)) if log_path else None
    if worker_log is None and taskdir_path is not None:
        worker_log = taskdir_path / "worker.log"
    if worker_log is not None and worker_log.exists():
        if renderer:
            renderer.link_for_source(worker_log, state=state, task_key=task_key, force=force_wrappers)
            href = portable(renderer.wrapper_path(run_id=str(state.get("run_id") or "run"), task_key=task_key, source_name=worker_log.name))
        else:
            href = file_href(worker_log)
        links.append(f'<a href="{html_escape(href)}">view the work log</a>')

    return " &middot; ".join(links) if links else '<span class="muted">—</span>'


def render_artifact_index_html(
    entries: list[dict[str, Any]],
    renderer: ArtifactRenderer | None = None,
    *,
    force_wrappers: bool = False,
) -> str:
    """Multi-run index: one pane of glass across every run under this state_dir."""
    rows = []
    for entry in entries:
        state_label = str(entry.get("state", "live"))
        fail_n = entry.get("fail", 0) or 0
        color = status_color(state_label if state_label in STATUS_COLORS else ("fail" if fail_n else "pass"))
        run_name = html_escape(str(entry.get("run_name", "ringer")))
        identity = html_escape(str(entry.get("identity", "unknown")))
        elapsed = fmt_duration(entry.get("elapsed_s"))
        pass_n = entry.get("pass", 0)
        links: list[str] = []
        artifact_path = entry.get("artifact_path")
        if artifact_path:
            links.append(f'<a href="{html_escape(file_href(Path(str(artifact_path))))}">live</a>')
        if entry.get("report_ready") and entry.get("report_path"):
            report_path = Path(str(entry["report_path"]))
            href = (
                renderer.link_for_source(
                    report_path,
                    run_id=str(entry.get("run_id") or "run"),
                    run_name=str(entry.get("run_name") or "ringer"),
                    task_key="run",
                    force=force_wrappers,
                )
                if renderer
                else file_href(report_path)
            )
            links.append(f'<a href="{html_escape(href)}">report</a>')
        links_html = " &middot; ".join(links) if links else '<span class="muted">—</span>'
        rows.append(
            f"""<tr>
          <td><span class="chip" style="background:{color}">{html_escape(state_label)}</span></td>
          <td class="mono">{run_name}</td>
          <td class="mono">{identity}</td>
          <td class="mono">{pass_n} pass / {fail_n} fail</td>
          <td class="mono">{elapsed}</td>
          <td class="mono">{links_html}</td>
        </tr>"""
        )
    body = "".join(rows) if rows else '<tr><td colspan="6" class="muted">no runs recorded yet</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{CSP_META_TAG}
<title>ringer &middot; all runs</title>
<meta http-equiv="refresh" content="5">
<style>{ARTIFACT_BASE_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>ringer &mdash; all runs</h1>
  <p class="meta">One pane of glass across every run with state under this state_dir.</p>
  <table>
    <thead><tr><th>State</th><th>Run</th><th>Identity</th><th>Result</th><th>Elapsed</th><th>Artifacts</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>
</body>
</html>
"""
