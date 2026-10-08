#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        f"ringer requires Python 3.12+; found {sys.version.split()[0]} at {sys.executable}"
    )

from ringer_core import central_evidence
from ringer_core.evidence_cli import run_evidence_command
from ringer_core.steering import (STEERING_STATUSES, STEERING_AUDIENCES, STEERING_RULE_HEADING_RE, SteeringProfile, SteeringRule, _steering_yaml_values, inject_steering_spec, load_steering_profile, parse_steering_profile, resolve_steering_profile, steering_profile_candidates, steering_worker_rules)
from ringer_core.runner import (DELIVERABLE_MAX_BYTES, FALLBACK_HARVEST_MAX_FILES, FALLBACK_HARVEST_SUFFIXES, SHEPHERD_MODEL, VERIFY_METHOD, RingerRunner, print_summary)

from ringer_core.presentation import (
    DASHBOARD_HTML_PATH, MINIMAL_DASHBOARD_HTML, RINGSIDE_HTML_PATH, WORKER_LOG_TAIL_BYTES,
    Dashboard, PersistentHudServer, ReusableThreadingHTTPServer, artifact_content_type,
    hud_task_log_path, inject_models_tab_into_ringside_html, open_in_browser,
    read_dashboard_html, read_ringside_html, resolve_artifact_http_path, send_json_response,
    send_response_body, serve_artifact_path, run_state_path_for_id, task_log_path_from_state,
)

import tempfile

from ringer_core.models_api import MODEL_SCOREBOARD_COLUMNS, build_models_api_payload
from ringer_core.model_views import (
    MODEL_SCOREBOARD_CSS, MODEL_SCOREBOARD_IDENTITY, catalog_model_display_name,
    compact_context_label, derived_quality_text, fmt_int, fmt_percent, fmt_scoreboard_duration,
    fmt_short_task_cost, fmt_task_cost, humanize_dates_in_text, humanized_catalog_event_line,
    humanized_log_date, humanized_short_date, model_task_cost_label, normalized_judgment_note,
    rate_bar_html, rate_cell_html, render_free_watchlist, render_model_scoreboard_html,
    render_model_table_pair, render_notes_list, render_task_breakdown_table, source_file_link,
    watchlist_chip_html, write_model_scoreboard_html,
)

from ringer_core.read_model import (
    ReadModelSyncResult, connect_read_model_db, connect_read_model_db_readonly,
    create_read_model_schema, db_attempt_rows, db_catalog_events, db_catalog_models,
    default_read_model_db_path, drop_read_model_tables, ensure_sqlite_available,
    file_sync_metadata, insert_attempt_rows, insert_catalog_event_rows,
    load_identity_registry_from_db, read_catalog_events_from_offset,
    read_log_rows_from_offset, read_model_column_exists, read_model_table_exists,
    read_sync_state_int, read_sync_state_value, rebuild_read_model_db,
    refresh_catalog_tables, refresh_identity_tables, should_use_read_model_db,
    sqlite3, sync_read_model_db, write_sync_state_values,
)

from ringer_core.evidence import append_jsonl
from ringer_core.artifact_store import (
    ARTIFACT_LIBRARY_MAX_VERSIONS,
    _library_entry,
    append_artifact_library_version,
    artifact_deliverables_dir,
    artifact_library_path,
    artifact_live_path,
    artifact_outcome_from_state,
    artifact_version_path,
    artifacts_dir,
    collect_state_deliverables,
    prune_artifact_versions,
    read_artifact_library,
    reconcile_artifact_library_dead_runs,
    sanitize_artifact_name,
    state_tasks,
    update_artifact_library_live,
    write_artifact_library,
)
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field, replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from ringer_core.context import (
    BROAD_TASK_TERMS, ContextChunk, ContextPacket, ENDING_TERMS, OPENING_TERMS,
    SENSITIVE_FILENAME_PARTS, SKIP_DIR_NAMES, STOPWORDS, SUPPORTED_SUFFIXES,
    build_context_packet, chunk_block, chunk_lines, read_source, request_terms,
    score_chunks, source_files,
)
from ringer_core.config import (
    AppConfig, ArtifactConfig, CONFIG_DIR_NAME, CONFIG_FILE_NAME, DEFAULT_CODEX_MODEL_REPORT_REGEX,
    DEFAULT_DASHBOARD_PORT_BASE, DEFAULT_ENGINE_NAME, DEFAULT_HUD_PORT,
    DEFAULT_TOKEN_REGEX, DEFAULT_UPDATE_CHECK_INTERVAL_S, ENV_VAR_PREFIX, EngineConfig,
    EvalConfig, PostgresEvalConfig, STATE_DIR_NAME, SteeringConfig, TOOL_NAME, UpdateConfig,
    as_string_tuple, built_in_codex_engine, default_config_path, default_state_dir,
    env_config_path, expand_path, format_artifact_template, load_artifact_config,
    load_engines, load_eval_config, load_hud_port, load_steering_config, load_update_config,
    optional_path, optional_string,
)
from ringer_core.manifests import (
    DEFAULT_TIMEOUT_S, MODEL_SCOREBOARD_RUN_NAME, Manifest, TaskSpec, require_bool,
)
from ringer_core.verification import (
    CHECK_TIMEOUT_S, VerifyResult, Verifier, kill_process_group, terminate_process_group,
)
from ringer_core.state_files import (
    atomic_write_json, atomic_write_text, active_runs_path, pid_is_alive,
    read_active_runs, read_active_runs_file, read_json_object, register_active_run,
    ringer_home, scan_hud_run_states,
    scan_run_states, unregister_active_run, utc_now_iso, _prune_active_runs,
    _read_active_runs_raw, _write_active_runs,
)
from ringer_core.worker_logs import (
    ACTIVITY_TAIL_BYTES, ACTIVITY_TEXT_LIMIT, ANSI_RE, ASSISTANT_PREFIX_RE,
    CMD_JSON_DOUBLE_RE, CMD_JSON_SINGLE_RE, CMD_LABEL_RE, CMD_PROMPT_RE, CMD_RAN_RE,
    PATCH_FILE_RE, WRITE_FILE_RE, WRITE_QUOTED_FILE_RE, activity_fallback, append_text,
    build_failure_context, clean_command, clean_log_text, extract_shell_command,
    extract_written_file, last_assistant_activity, last_shell_command_activity,
    last_written_file_activity, looks_like_assistant_text, looks_like_shell_command,
    non_empty_log_lines, normalize_activity_path, shorten, tail_file_text, tail_lines,
    tail_text, worker_activity,
)

from ringer_core.runtime import (
    AsyncFileCloser, ProcessTree, RollingBytes, TaskRuntime, WorkerResult,
    build_run_id, build_worker_command, effective_model_from_command,
    effective_reasoning_effort_from_command, parse_reported_model, parse_token_count,
    resolved_task_model, shell_command_for_display, verdict_for,
)
from ringer_core.state import StateWriter
from ringer_core.catalog import (
    CATALOG_AUTO_REFRESH_MAX_AGE_S, CATALOG_FETCH_TIMEOUT_S, DEFAULT_CATALOG_SOURCE,
    RESERVED_FIXTURE_MODELS, CatalogRefreshResult, append_catalog_events,
    catalog_changes_path, catalog_decimal, catalog_decimal_or_none, catalog_event_model_details,
    catalog_explore_candidates, catalog_model_is_text_candidate, catalog_per_m,
    catalog_per_m_decimal, catalog_price_equal, catalog_price_is_negative, catalog_refresh_lock,
    catalog_snapshot_is_fresh, catalog_sort_key, default_catalog_path, diff_catalog_snapshots,
    fetch_catalog_payload, load_catalog_snapshot, normalize_catalog_model,
    normalize_catalog_payload, read_catalog_events, refresh_openrouter_catalog,
    start_catalog_auto_refresh,
)


from ringer_core.models import (
    EMPTY_MODEL_IDENTITY_REGISTRY,
    ModelIdentity,
    ModelIdentityRegistry,
    NoncanonicalRoute,
    PROVEN_MIN_FIRST_TRY,
    PROVEN_MIN_TASKS,
    UNATTRIBUTED_MODEL_DISPLAY,
    aggregate_model_log_rows,
    aggregate_model_scoreboard_rows,
    catalog_identity_fields,
    catalog_models_by_id,
    default_model_notes_path,
    default_model_registry_path,
    enrich_model_groups_with_identity,
    enrich_model_groups_with_notes,
    estimated_task_cost,
    group_model_log_tasks,
    load_model_identity_registry,
    median_int,
    model_judgment_notes,
    model_judgment_notes_for_row,
    model_log_int,
    model_log_row_engine,
    model_log_row_is_reserved_fixture,
    model_log_row_is_retry,
    model_log_row_is_unattributed,
    model_log_row_model,
    model_log_row_reasoning_effort,
    model_log_row_task_type,
    model_log_task_base_key,
    model_log_text,
    model_reasoning_effort_keys,
    model_scoreboard_tier,
    model_scoreboard_tier_rank,
    model_sort_cost,
    normalize_catalog_for_scoreboard,
    normalize_notes_match_text,
    note_date_key,
    order_model_scoreboard_rows,
    parse_log_date,
    parse_model_notes_sections,
    proven_model_group,
    read_model_log_rows,
    row_identity_fields,
    scoreboard_safe_note,
    short_model_name,
    strip_inline_markdown,
    task_final_rows,
    validate_since_date
)



SELF_UPDATE_FETCH_TIMEOUT_S = 10
SELF_UPDATE_STATE_FILE = "self-update.json"
# When a task declares no expect_files, these are the file types worth
# rescuing from the top of its task directory so the work still shows up
# on the results page instead of silently staying invisible. Logs are
# excluded — the worker log is linked separately, it is not a deliverable.
from ringer_core.artifact_views import (
    ARTIFACT_BASE_CSS, ARTIFACT_WRAPPER_TAIL_BYTES, CSP_META_TAG,
    IMAGE_DELIVERABLE_SUFFIXES, STATUS_COLORS, TASK_REPORT_FILENAMES,
    TEXT_DELIVERABLE_SUFFIXES, ArtifactRenderer, artifact_relative_href,
    deliverable_title, failed_phrase, file_href, final_briefing_html,
    final_briefing_sentence, final_dot_bucket, first_check_output_line,
    fmt_compact_duration, fmt_datetime, fmt_duration, fmt_plain_ago,
    html_to_text, image_data_uri, is_html_artifact, is_image_deliverable,
    is_text_deliverable, join_plain_html_parts, live_briefing_html,
    live_briefing_sentence, local_time_label, passed_phrase, plain_transition_event,
    plain_transition_line, render_artifact_index_html, render_corner_header,
    render_file_wrapper_html, render_final_report_html, render_progress_bar,
    render_status_html, render_task_links, render_work_group, render_work_item,
    render_work_section, retry_phrase, running_phrase, status_color,
    task_activity_line, task_state_bucket, task_state_word, task_status_counts,
    task_word, waiting_phrase, work_item_href, work_label_and_kind,
)


























@dataclass(frozen=True)
class SelfUpdateResult:
    status: str
    behind: int = 0
    reason: str | None = None
    old_head: str | None = None
    new_head: str | None = None

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def applied(self) -> bool:
        return self.status == "applied"













def self_update_state_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / SELF_UPDATE_STATE_FILE


def _read_self_update_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _self_update_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _record_self_update_state(
    path: Path,
    repo_dir: Path,
    *,
    behind: int,
    reason: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    try:
        state = _read_self_update_state(path)
        state[str(repo_dir.resolve())] = {
            "last_check": _self_update_timestamp(now),
            "behind": max(0, int(behind)),
            "reason": reason,
            "error": error,
        }
        atomic_write_json(path, state)
    except Exception:
        pass


def _self_update_is_throttled(
    path: Path,
    repo_dir: Path,
    interval_s: int,
    *,
    now: datetime | None = None,
) -> bool:
    entry = _read_self_update_state(path).get(str(repo_dir.resolve()))
    if not isinstance(entry, dict):
        return False
    try:
        checked = datetime.fromisoformat(str(entry.get("last_check", "")))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current.astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() < interval_s
    except (TypeError, ValueError):
        return False


def self_update_dashboard_status(state_dir: Path, repo_dir: Path) -> dict[str, Any] | None:
    entry = _read_self_update_state(self_update_state_path(state_dir)).get(str(repo_dir.resolve()))
    if not isinstance(entry, dict):
        return None
    try:
        behind = int(entry.get("behind") or 0)
    except (TypeError, ValueError):
        return None
    reason = str(entry.get("reason") or "").strip()
    if behind <= 0 or not reason:
        return None
    return {"behind": behind, "reason": reason}


def decide_self_update(
    *,
    behind: int,
    branch: str,
    tracked_changes: bool,
    fast_forward_possible: bool,
) -> str | None:
    """Return the concrete block reason, or None when an ff-only update is safe."""
    if behind <= 0:
        return None
    if branch != "main":
        return f"current branch is {branch or 'detached HEAD'}, not main"
    if tracked_changes:
        return "tracked files are modified"
    if not fast_forward_possible:
        return "local main has diverged from origin/main"
    return None


def _git_result_text(result: Any) -> str:
    text = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    return " ".join(text.split())


def _run_self_update_git(
    runner: Any,
    git_bin: str,
    repo_dir: Path,
    *args: str,
) -> Any:
    return runner(
        [git_bin, "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=SELF_UPDATE_FETCH_TIMEOUT_S,
    )


def _self_update_error_reason(action: str, result: Any) -> str:
    detail = _git_result_text(result)
    return f"{action} failed" + (f": {detail}" if detail else "")


def perform_self_update(
    *,
    config: AppConfig,
    argv: list[str],
    repo_dir: Path | None = None,
    script_path: Path | None = None,
    force: bool = False,
    allow_reexec: bool = True,
    warn_blocked: bool = False,
    runner: Any = subprocess.run,
    execve: Any = os.execve,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
) -> SelfUpdateResult:
    """Fetch origin/main and apply only a clean, main-branch fast-forward."""
    repo = (repo_dir or Path(__file__).resolve().parent).resolve()
    script = (script_path or Path(__file__).resolve()).resolve()
    state_path = self_update_state_path(config.state_dir)
    known_behind = 0
    git_bin = shutil.which("git")
    if not (repo / ".git").exists():
        return SelfUpdateResult("skipped", reason="not a git checkout")
    if git_bin is None:
        return SelfUpdateResult("skipped", reason="git binary not found")
    if not force and _self_update_is_throttled(
        state_path, repo, config.update.check_interval_s, now=now
    ):
        return SelfUpdateResult("throttled")

    try:
        fetched = _run_self_update_git(runner, git_bin, repo, "fetch", "--quiet", "origin", "main")
        if fetched.returncode != 0:
            reason = _self_update_error_reason("fetch", fetched)
            _record_self_update_state(state_path, repo, behind=0, error=reason, now=now)
            return SelfUpdateResult("error", reason=reason)

        behind_result = _run_self_update_git(
            runner, git_bin, repo, "rev-list", "--count", "HEAD..origin/main"
        )
        if behind_result.returncode != 0:
            reason = _self_update_error_reason("behind check", behind_result)
            _record_self_update_state(state_path, repo, behind=0, error=reason, now=now)
            return SelfUpdateResult("error", reason=reason)
        known_behind = int(str(behind_result.stdout).strip())
        if known_behind <= 0:
            _record_self_update_state(state_path, repo, behind=0, reason="up to date", now=now)
            return SelfUpdateResult("up_to_date")

        branch_result = _run_self_update_git(
            runner, git_bin, repo, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if branch_result.returncode == 0:
            branch = str(branch_result.stdout).strip()
        elif branch_result.returncode == 1:
            branch = ""
        else:
            reason = _self_update_error_reason("branch check", branch_result)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=reason, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=reason)
        status_result = _run_self_update_git(
            runner, git_bin, repo, "status", "--porcelain", "--untracked-files=no"
        )
        if status_result.returncode != 0:
            reason = _self_update_error_reason("status check", status_result)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=reason, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=reason)
        ancestor_result = _run_self_update_git(
            runner, git_bin, repo, "merge-base", "--is-ancestor", "HEAD", "origin/main"
        )
        if ancestor_result.returncode not in {0, 1}:
            reason = _self_update_error_reason("fast-forward check", ancestor_result)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=reason, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=reason)
        reason = decide_self_update(
            behind=known_behind,
            branch=branch,
            tracked_changes=bool(str(status_result.stdout).strip()),
            fast_forward_possible=ancestor_result.returncode == 0,
        )
        if reason is not None:
            _record_self_update_state(
                state_path, repo, behind=known_behind, reason=reason, now=now
            )
            if warn_blocked:
                print(
                    f"[ringer] self-update: {known_behind} commit(s) behind; {reason}",
                    file=sys.stderr,
                    flush=True,
                )
            return SelfUpdateResult("blocked", behind=known_behind, reason=reason)

        old_result = _run_self_update_git(runner, git_bin, repo, "rev-parse", "--short", "HEAD")
        if old_result.returncode != 0 or not str(old_result.stdout).strip():
            reason = _self_update_error_reason("current HEAD check", old_result)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=reason, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=reason)
        old_head = str(old_result.stdout).strip()
        new_result = _run_self_update_git(
            runner, git_bin, repo, "rev-parse", "--short", "origin/main"
        )
        if new_result.returncode != 0 or not str(new_result.stdout).strip():
            reason = _self_update_error_reason("origin/main HEAD check", new_result)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=reason, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=reason)
        new_head = str(new_result.stdout).strip()
        merged = _run_self_update_git(runner, git_bin, repo, "merge", "--ff-only", "origin/main")
        if merged.returncode != 0:
            failure = _self_update_error_reason("fast-forward", merged)
            _record_self_update_state(
                state_path, repo, behind=known_behind, error=failure, now=now
            )
            return SelfUpdateResult("error", behind=known_behind, reason=failure)
        _record_self_update_state(state_path, repo, behind=0, reason="applied", now=now)
        result = SelfUpdateResult(
            "applied", behind=known_behind, old_head=old_head, new_head=new_head
        )
        if allow_reexec:
            print(
                f"[ringer] self-update: applied {known_behind} commit(s) "
                f"{old_head}..{new_head}; restarting",
                file=sys.stderr,
                flush=True,
            )
            next_env = dict(environ if environ is not None else os.environ)
            next_env["RINGER_SELF_UPDATED"] = "1"
            execve(
                sys.executable,
                [sys.executable, str(script), *argv[1:]],
                next_env,
            )
        return result
    except subprocess.TimeoutExpired:
        reason = f"git command timed out after {SELF_UPDATE_FETCH_TIMEOUT_S}s"
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
    _record_self_update_state(state_path, repo, behind=known_behind, error=reason, now=now)
    return SelfUpdateResult("error", behind=known_behind, reason=reason)


def _config_path_from_argv(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv[1:]):
        if value == "--config" and index + 2 < len(argv):
            return Path(argv[index + 2])
        if value.startswith("--config="):
            return Path(value.split("=", 1)[1])
    return None


def _self_update_command_requested(argv: list[str]) -> bool:
    """Recognize the explicit command without mistaking an argument value for it."""
    skip_next = False
    for value in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if value == "--config":
            skip_next = True
            continue
        if value == "--no-self-update" or value.startswith("--config="):
            continue
        return value == "self-update"
    return False


def maybe_self_update(
    argv: list[str],
    *,
    config: AppConfig | None = None,
    repo_dir: Path | None = None,
    script_path: Path | None = None,
    runner: Any = subprocess.run,
    execve: Any = os.execve,
    environ: dict[str, str] | None = None,
    now: datetime | None = None,
) -> SelfUpdateResult:
    """Run the fail-open startup update check before command dispatch."""
    env = environ if environ is not None else os.environ
    if env.get("RINGER_SELF_UPDATED") == "1":
        return SelfUpdateResult("skipped", reason="already restarted")
    if env.get("RINGER_NO_SELF_UPDATE") == "1":
        return SelfUpdateResult("skipped", reason="disabled by environment")
    if "--no-self-update" in argv:
        return SelfUpdateResult("skipped", reason="disabled for this invocation")
    if _self_update_command_requested(argv):
        return SelfUpdateResult("skipped", reason="explicit self-update command")
    try:
        resolved_config = config or AppConfig.load(_config_path_from_argv(argv))
    except Exception:
        return SelfUpdateResult("skipped", reason="config unavailable")
    if not resolved_config.update.auto:
        return SelfUpdateResult("skipped", reason="disabled by config")
    return perform_self_update(
        config=resolved_config,
        argv=argv,
        repo_dir=repo_dir,
        script_path=script_path,
        warn_blocked=True,
        runner=runner,
        execve=execve,
        environ=env,
        now=now,
    )




















FILE_TEST_OPS = {"-e", "-f", "-s", "-d", "-r", "-w", "-x", "-L"}


def lint_manifest(
    manifest: Manifest,
    *,
    include_model_log_nudges: bool = False,
    config: AppConfig | None = None,
    identity_registry: ModelIdentityRegistry | None = None,
    allow_noncanonical_route: bool = False,
) -> list[str]:
    findings: list[str] = []
    if manifest.run_name == MODEL_SCOREBOARD_RUN_NAME:
        findings.append("manifest: run_name model-scoreboard is reserved for the scoreboard page.")

    # An engine name that resolves to nothing is the one manifest error that
    # costs a whole dispatch to discover: `run` fails at spawn time, once the
    # run row, the worktree and the dashboard entry already exist. Lint used to
    # call a manifest naming `no-such-engine-xyz` clean, because nothing here
    # ever looked the name up: the only code that touched it,
    # noncanonical_route_findings, does a `.get()` that returns None and then
    # `continue`s, so an unknown engine took the quiet path out.
    if config is not None:
        known = ", ".join(sorted(config.engines))
        selected_config = str(config.path) if config.path is not None else "(safe defaults)"
        for task in manifest.tasks:
            if task.engine not in config.engines:
                findings.append(
                    f"ERROR: {task.key}: engine {task.engine!r} is not configured in "
                    f"{selected_config}; "
                    f"engines available here: {known}."
                )

    for task in manifest.tasks:
        if check_cannot_fail(task.check):
            findings.append(f"{task.key}: check cannot fail, so the task cannot be verified.")
        if check_may_fail_silently(task.check):
            findings.append(
                f"{task.key}: check may fail without printing why; retry prompt and eval log depend on failure output."
            )
        if manifest.worktrees and any(is_relative_expect_file(path) for path in task.expect_files):
            findings.append(
                f"{task.key}: deliverable would be deleted with the worktree; write it outside the worktree or export it in the check."
            )
        if manifest.worktrees and instructs_git_commit(task.spec):
            findings.append(
                f"{task.key}: worker commits die with the worktree; have the worker leave changes uncommitted and export the diff in the check."
            )
        if len(task.spec.strip()) < 80:
            findings.append(
                f"{task.key}: spec is probably underspecified; workers are stateless and cannot ask questions."
            )
        if spec_is_file_pointer(task.spec):
            findings.append(
                f"{task.key}: spec is a pointer to an instruction file; anyone watching Ringside "
                "sees no real brief and the retry prompt loses context — put the instructions in the spec itself."
            )
        if not task.expect_files and not manifest.worktrees:
            findings.append(
                f"{task.key}: no expect_files; the results page will guess deliverables from the "
                "task folder — declare them so the reader sees exactly the right work."
            )
        if not task.verified:
            findings.append(
                f"{task.key}: no 'verified' description; a reader of the results page sees "
                "'checked' but not what the check proves — add one plain-English sentence."
            )
        if include_model_log_nudges and not task.task_type:
            findings.append(
                f"{task.key}: no task_type; the model log buckets this as (untyped) — "
                "name one (e.g. code-feature, research, image-gen) so './ringer.py models' can guide routing."
            )

    if len(manifest.tasks) >= 3 and manifest.max_parallel == 1:
        findings.append("manifest: tasks will run serially; set max_parallel.")

    if not manifest.worktrees:
        # Relative expect_files resolve inside each task's own directory and
        # cannot collide; only a shared absolute path is a real collision.
        paths_to_tasks: dict[str, list[str]] = {}
        for task in manifest.tasks:
            for path in task.expect_files:
                if not Path(path).expanduser().is_absolute():
                    continue
                paths_to_tasks.setdefault(path, []).append(task.key)
        for path, task_keys in paths_to_tasks.items():
            if len(task_keys) >= 2:
                findings.append(
                    f"manifest: write collision on {path}: listed by {', '.join(task_keys)}."
                )

    if not allow_noncanonical_route:
        findings.extend(
            noncanonical_route_findings(
                manifest,
                config=config,
                registry=identity_registry,
            )
        )

    return findings


FILE_POINTER_SPEC_RE = re.compile(
    r"\b(read|open|follow|see)\b[^\n.]{0,100}?/[\w~][\w./~-]*",
    re.IGNORECASE,
)


def spec_is_file_pointer(spec: str) -> bool:
    """True when the spec's substance lives in some other file.

    'Read /path/to/instructions.md and do what it says' hides the brief from
    everyone watching the run and starves the retry prompt. Long specs that
    merely reference files for CONTEXT are fine — the heuristic only fires
    when the spec is short enough that the pointer must be the whole plan.
    """
    text = spec.strip()
    if re.search(r"do (exactly )?what (it|the file|that file) says", text, re.IGNORECASE):
        return True
    if len(text) >= 600:
        return False
    return bool(FILE_POINTER_SPEC_RE.search(text))


def check_cannot_fail(check: str) -> bool:
    stripped = strip_shell_comments(check).strip()
    if stripped in {"true", ":", "exit 0"}:
        return True
    return consists_only_of_echo_commands(stripped)


def strip_shell_comments(command: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            result.append(char)
            escaped = False
            i += 1
            continue
        if char == "\\" and not in_single:
            result.append(char)
            escaped = True
            i += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            result.append(char)
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
            i += 1
            continue
        if (
            char == "#"
            and not in_single
            and not in_double
            and (not result or result[-1].isspace())
        ):
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        result.append(char)
        i += 1
    return "".join(result)


def consists_only_of_echo_commands(command: str) -> bool:
    if not command or "||" in command or re.search(r"[|<>]", command):
        return False
    parts = [part.strip() for part in re.split(r"(?:&&|;|\n)+", command) if part.strip()]
    if not parts:
        return False
    for part in parts:
        try:
            tokens = shlex.split(part)
        except ValueError:
            return False
        if not tokens or tokens[0] != "echo":
            return False
    return True


def check_may_fail_silently(check: str) -> bool:
    stripped = strip_shell_comments(check).strip()
    if has_quiet_diff_probe(stripped):
        return not has_failure_output_branch(stripped)
    if not stripped or "||" in stripped:
        return False
    if re.search(r"(?:;|\n|\|)", stripped):
        return False
    parts = [part.strip() for part in stripped.split("&&") if part.strip()]
    return bool(parts) and all(is_silent_probe(part) for part in parts)


def has_quiet_diff_probe(command: str) -> bool:
    return any(has_command_prefix(part, ("diff", "-q")) for part in command_parts(command))


def has_failure_output_branch(command: str) -> bool:
    if "||" not in command:
        return False
    branch = command.split("||", 1)[1]
    return any(
        has_command_prefix(part, (prefix,))
        for part in command_parts(branch)
        for prefix in ("echo", "printf", "cat", "diff", "ls")
    )


def command_parts(command: str) -> list[str]:
    return [part.strip(" \t{}()") for part in re.split(r"(?:&&|\|\||;|\n)+", command) if part.strip()]


def has_command_prefix(command: str, prefix: tuple[str, ...]) -> bool:
    try:
        tokens = shlex.split(strip_common_redirections(command))
    except ValueError:
        return False
    return len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == prefix


def is_silent_probe(command: str) -> bool:
    return is_file_existence_test(command) or is_quiet_grep(command)


def is_quiet_grep(command: str) -> bool:
    command = strip_common_redirections(command.strip())
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(tokens) and tokens[0] == "grep" and any(
        token == "-q" or (token.startswith("-") and "q" in token[1:]) for token in tokens[1:]
    )


def is_file_existence_test(command: str) -> bool:
    command = strip_common_redirections(command.strip())
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) >= 3 and tokens[0] == "test" and tokens[1] in FILE_TEST_OPS:
        return True
    return len(tokens) >= 4 and tokens[0] == "[" and tokens[1] in FILE_TEST_OPS and tokens[-1] == "]"


def strip_common_redirections(command: str) -> str:
    command = re.sub(r"\s+\d?>&\d+\s*$", "", command)
    command = re.sub(r"\s+\d?>\S+\s*$", "", command)
    return command.strip()


def is_relative_expect_file(path: str) -> bool:
    return bool(path.strip()) and not path.startswith("~") and not Path(path).is_absolute()


def instructs_git_commit(spec: str) -> bool:
    lower = spec.lower()
    start = 0
    while True:
        index = lower.find("git commit", start)
        if index == -1:
            return False
        prefix = lower[max(0, index - 48) : index]
        if not is_negated_git_commit(prefix):
            return True
        start = index + len("git commit")


def is_negated_git_commit(prefix: str) -> bool:
    separators = r"[\s`'\"()\[\]{}:;,.!?-]*"
    return bool(
        re.search(
            rf"(?:do\s+not|don't|never|no){separators}(?:run{separators})?$",
            prefix,
        )
    )


























































def format_catalog_price(value: Any, *, variable: bool = False) -> str:
    if variable or value is None:
        return "var"
    amount = float(value or 0)
    if amount == 0:
        return "0"
    if amount < 0.01:
        return f"{amount:.4f}".rstrip("0").rstrip(".")
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def print_catalog_table(models: list[dict[str, Any]]) -> None:
    header = f"{'id':<48} {'$/M in':>9} {'$/M out':>9} {'ctx':>8} {'FREE':<4}"
    print(header)
    print("-" * len(header))
    for model in sorted(models, key=catalog_sort_key):
        variable_pricing = bool(model.get("variable_pricing"))
        marker = "FREE" if model.get("free") else ""
        print(
            f"{shorten(str(model.get('id', '')), 48):<48} "
            f"{format_catalog_price(model.get('prompt_per_m'), variable=variable_pricing):>9} "
            f"{format_catalog_price(model.get('completion_per_m'), variable=variable_pricing):>9} "
            f"{int(model.get('context_length') or 0):>8} {marker:<4}"
        )




def describe_catalog_event(event: dict[str, Any]) -> str:
    kind = str(event.get("kind", "event"))
    model_id = str(event.get("id", ""))
    ts = str(event.get("ts", ""))
    if kind == "price_change":
        return (
            f"{ts} {model_id} price_change: "
            f"in {format_catalog_price(event.get('old_prompt_per_m'))}"
            f" -> {format_catalog_price(event.get('new_prompt_per_m'))}, "
            f"out {format_catalog_price(event.get('old_completion_per_m'))}"
            f" -> {format_catalog_price(event.get('new_completion_per_m'))}"
        )
    if kind in {"went_free", "went_paid"}:
        return f"{ts} {model_id} {kind}"
    if kind == "pricing_variable":
        return f"{ts} {model_id} pricing_variable"
    if kind == "pricing_fixed":
        return (
            f"{ts} {model_id} pricing_fixed: "
            f"in {format_catalog_price(event.get('new_prompt_per_m'))}, "
            f"out {format_catalog_price(event.get('new_completion_per_m'))}"
        )
    if kind == "added":
        marker = " FREE" if event.get("free") else ""
        return f"{ts} {model_id} added{marker}"
    if kind == "removed":
        return f"{ts} {model_id} removed"
    return f"{ts} {model_id} {kind}"


def describe_catalog_event_humanized(event: dict[str, Any]) -> str:
    text = describe_catalog_event(event)
    ts = str(event.get("ts", ""))
    if ts and text.startswith(ts):
        return humanized_log_date(ts) + text[len(ts) :]
    return text


def run_catalog_command(args: argparse.Namespace) -> int:
    snapshot_path = (args.file or default_catalog_path()).expanduser().resolve()
    if args.refresh:
        models = refresh_openrouter_catalog(
            snapshot_path,
            source=args.source or DEFAULT_CATALOG_SOURCE,
        ).models
    else:
        models = load_catalog_snapshot(snapshot_path)

    if args.changes:
        for event in read_catalog_events(catalog_changes_path(snapshot_path)):
            print(describe_catalog_event(event))
        return 0

    if args.free:
        models = [model for model in models if model.get("free")]

    if args.json:
        print(json.dumps(sorted(models, key=catalog_sort_key)))
        return 0

    if not models:
        print(f"No catalog snapshot at {snapshot_path}. Run './ringer.py catalog --refresh'.", file=sys.stderr)
        return 1
    print_catalog_table(models)
    return 0






# Promotion ladder: 3+ tasks and first-try >= 2/3 ("2 of 3"). The exact
# fraction matters — 0.67 would misclassify a literal 2-of-3 record.








def print_model_explore(
    *,
    log_path: Path,
    rows_read: int,
    skipped: int,
    groups: list[dict[str, Any]],
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
) -> None:
    print(f"TIERS from {log_path} ({rows_read} rows, {skipped} skipped lines)")
    if not groups:
        print("  no local evidence")
    for group in groups:
        label = (
            "unranked"
            if group.get("unattributed") or group.get("misrouted")
            else ("proven" if proven_model_group(group) else "probation")
        )
        display = (
            f"{group.get('model_display') or UNATTRIBUTED_MODEL_DISPLAY} "
            f"[{group.get('engine') or 'unknown'}]"
            if group.get("unattributed")
            else str(group.get("model_display") or group["model"])
        )
        if group.get("misrouted"):
            display = f"{display} [misrouted]"
        print(
            f"  {label:<9} {display} "
            f"task_type={group['task_type']} tasks={group['tasks']} "
            f"first={group['first_try_pass_rate']:.2f} pass={group['pass_rate']:.2f}"
        )

    tested_models = {str(group.get("model")) for group in groups if group.get("model")}
    candidates = catalog_explore_candidates(catalog_models, tested_models=tested_models)
    print(f"CANDIDATES from {catalog_path}")
    if not candidates:
        print("  no untested text->text candidates with context >= 32000")
    for model in candidates:
        marker = " FREE" if model.get("free") else ""
        print(
            f"  untested  {model['id']} "
            f"in={format_catalog_price(model.get('prompt_per_m'))}/M "
            f"out={format_catalog_price(model.get('completion_per_m'))}/M "
            f"ctx={int(model.get('context_length') or 0)}{marker}"
        )


















































































































































class EvalLogger:
    def __init__(self, config: EvalConfig) -> None:
        self.config = config
        self._conn: Any | None = None
        self._fallback_path = config.jsonl_path
        self._fallback_reason: str | None = None
        self._fallback_warned = False
        self._secrets: tuple[str, ...] = ()
        if config.backend == "postgres":
            self._connect()
            if self._fallback_reason is not None:
                self._warn_fallback()

    def log_attempt(self, row: dict[str, Any]) -> None:
        postgres = self.config.postgres
        source_host = central_evidence.resolve_source_host(postgres.source_host if postgres else None)
        spec_storage = postgres.spec_storage if postgres else "hash"
        stamped = central_evidence.stamp(row, source_host)
        log_sink = "jsonl"
        if self._conn is not None:
            try:
                central_row = dict(stamped, log_sink="postgres", fallback_reason=None)
                params = central_evidence.to_params(central_row, source_host, spec_storage)
                self._conn.execute(central_evidence.INSERT_SQL, params)
                log_sink = "postgres"
            except Exception as exc:
                self._fallback_reason = central_evidence.scrub(f"postgres insert failed: {exc}", self._secrets)
                self._close_conn()
                self._warn_fallback()
        self._write_jsonl(stamped, log_sink)

    def close(self) -> None:
        self._close_conn()

    def _connect(self) -> None:
        if self.config.postgres is None:
            self._fallback_reason = "postgres config missing"
            return
        try:
            credentials = central_evidence.resolve_credentials(parse_env_file(self.config.postgres.env_file))
            self._secrets = (credentials.password,)
            self._conn = central_evidence.connect(credentials, autocommit=True)
        except Exception as exc:
            self._fallback_reason = central_evidence.scrub(f"postgres connect failed: {exc}", self._secrets)

    def _write_jsonl(self, row: dict[str, Any], log_sink: str) -> None:
        payload = dict(row)
        payload["log_sink"] = log_sink
        payload["fallback_reason"] = None if log_sink == "postgres" else self._fallback_reason
        append_jsonl(self._fallback_path, payload)

    def _warn_fallback(self) -> None:
        if not self._fallback_warned:
            print(
                f"ringer: central evidence write failed ({self._fallback_reason}); "
                f"attempt rows are still saved to {self._fallback_path}",
                file=sys.stderr,
            )
            self._fallback_warned = True

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None






















































def noncanonical_route_findings(
    manifest: Manifest,
    *,
    config: AppConfig | None = None,
    registry: ModelIdentityRegistry | None = None,
) -> list[str]:
    identity_registry = registry or load_model_identity_registry()
    findings: list[str] = []
    for task in manifest.tasks:
        engine = config.engines.get(task.engine) if config is not None else None
        model_key = task.model or (engine.model_default if engine is not None else "")
        if not model_key:
            model_key = identity_registry.defaults.get(task.engine, "")
        route = identity_registry.noncanonical_routes.get((task.engine, model_key))
        if route is None:
            continue
        findings.append(
            f"ERROR: {task.key}: {task.engine}:{model_key} is a noncanonical route for "
            f"{route.identity.model_display}; canonical route is {route.canonical_route}. "
            "Use --allow-noncanonical-route only for a deliberate bakeoff."
        )
    return findings



























































def run_db_command(config: AppConfig, args: argparse.Namespace) -> int:
    db_path = (args.db or default_read_model_db_path()).expanduser().resolve()
    log_path = (args.log or config.eval.jsonl_path).expanduser().resolve()
    catalog_path = (getattr(args, "catalog_file", None) or default_catalog_path()).expanduser().resolve()
    registry_path = (getattr(args, "registry", None) or default_model_registry_path()).expanduser().resolve()
    if args.db_command == "rebuild":
        result = rebuild_read_model_db(
            db_path,
            log_path,
            catalog_path=catalog_path,
            registry_path=registry_path,
        )
    else:
        result = sync_read_model_db(
            db_path,
            log_path,
            catalog_path=catalog_path,
            registry_path=registry_path,
        )
    action = "rebuild" if result.rebuilt else "sync"
    print(
        f"db {action}: {result.db_path} "
        f"attempts={result.attempts_inserted} skipped={result.skipped} offset={result.offset}"
    )
    return 0
























































































def print_model_log_table(path: Path, rows_read: int, skipped: int, groups: list[dict[str, Any]]) -> None:
    print(f"Model log: {path} ({rows_read} rows, {skipped} skipped lines)")
    widths = (32, 20, 18, 18, 10, 7, 10, 7, 15, 14, 14, 60)
    header = " | ".join(
        f"{name:<{width}}" for name, width in zip(MODEL_SCOREBOARD_COLUMNS, widths)
    )
    current_task_type: str | None = None
    if not groups:
        print(header)
        print("-" * len(header))
    for group in groups:
        task_type = str(group.get("task_type") or "(untyped)")
        if task_type != current_task_type:
            if current_task_type is not None:
                print()
            current_task_type = task_type
            print(f"Task type: {task_type}")
            print(header)
            print("-" * len(header))
        display = str(group.get("model_display") or group["model"])
        if group.get("unattributed"):
            display = UNATTRIBUTED_MODEL_DISPLAY
        if group.get("misrouted"):
            display = f"{display} [misrouted]"
        if group.get("unregistered"):
            display = f"{display} [unregistered]"
        values = (
            display,
            str(group.get("lab") or "(unknown)"),
            str(group.get("harness") or "unknown"),
            str(group.get("access") or "unknown"),
            "not ranked" if group.get("tier") == "unranked" else str(group.get("tier") or ""),
            fmt_int(group.get("tasks")),
            fmt_percent(group.get("first_try_pass_rate")),
            fmt_percent(group.get("pass_rate")),
            "" if group.get("median_tokens") is None else fmt_int(group.get("median_tokens")),
            fmt_scoreboard_duration(group.get("median_duration_ms")),
            humanized_log_date(group.get("last_seen")),
            shorten(str(group.get("latest_note") or ""), 60),
        )
        print(" | ".join(f"{shorten(value, width):<{width}}" for value, width in zip(values, widths)))
    print("Judgment layer: docs/MODEL-NOTES.md")
    unregistered_slugs = sorted(
        {str(group.get("model") or "") for group in groups if group.get("unregistered") and group.get("model")}
    )
    if unregistered_slugs:
        print(
            f"Unregistered model slug(s): {', '.join(unregistered_slugs)} "
            "— run the identity procedure in docs/TAXONOMY.md."
        )


def run_models_command(config: AppConfig, args: argparse.Namespace) -> int:
    default_log_path = config.eval.jsonl_path.expanduser().resolve()
    log_path = (args.log or default_log_path).expanduser().resolve()
    since = validate_since_date(args.since)
    explicit_db = getattr(args, "db", None) is not None
    db_path = (getattr(args, "db", None) or default_read_model_db_path()).expanduser().resolve()
    catalog_path = (getattr(args, "catalog_file", None) or default_catalog_path()).expanduser().resolve()
    registry_path = (getattr(args, "registry", None) or default_model_registry_path()).expanduser().resolve()
    notes_path = (getattr(args, "notes_file", None) or default_model_notes_path()).expanduser().resolve()
    using_db = should_use_read_model_db(
        log_path=log_path,
        default_log_path=default_log_path,
        explicit_db=explicit_db,
    )
    catalog_events: list[dict[str, Any]] | None = None
    if using_db:
        try:
            sync_result = sync_read_model_db(
                db_path,
                log_path,
                catalog_path=catalog_path,
                registry_path=registry_path,
            )
            rows, identity_registry = db_attempt_rows(db_path, since=since, engine=args.engine)
            disk_registry = load_model_identity_registry(registry_path)
            identity_registry = dataclass_replace(
                identity_registry,
                noncanonical_routes=disk_registry.noncanonical_routes,
            )
            skipped = sync_result.skipped
            catalog_models_from_db = db_catalog_models(db_path)
            catalog_events = db_catalog_events(db_path, limit=6)
        except Exception as exc:
            using_db = False
            print(f"models: SQLite read model unavailable; using JSONL fallback ({exc})", file=sys.stderr)
            rows, skipped = read_model_log_rows(log_path, since=since, engine=args.engine)
            identity_registry = load_model_identity_registry(registry_path)
            catalog_models_from_db = []
    else:
        rows, skipped = read_model_log_rows(log_path, since=since, engine=args.engine)
        identity_registry = load_model_identity_registry(registry_path)
        catalog_models_from_db = []
    catalog_models = catalog_models_from_db if using_db else load_catalog_snapshot(catalog_path)
    notes_sections = parse_model_notes_sections(notes_path)
    groups = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_log_rows(rows, task_type=args.task_type, model=args.model),
            rows,
            identity_registry,
            include_task_type=True,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    if args.explore:
        print_model_explore(
            log_path=log_path,
            rows_read=len(rows),
            skipped=skipped,
            groups=groups,
            catalog_path=catalog_path,
            catalog_models=catalog_models,
        )
        return 0
    html_arg = getattr(args, "html", None)
    open_requested = bool(getattr(args, "open", False))
    if html_arg is not None or open_requested:
        scoreboard_rows = enrich_model_groups_with_notes(
            enrich_model_groups_with_identity(
                aggregate_model_scoreboard_rows(rows, task_type=args.task_type, model=args.model),
                rows,
                identity_registry,
                include_task_type=False,
                catalog_models=catalog_models,
            ),
            notes_sections,
        )
        explicit_path = None
        if html_arg not in {None, ""}:
            explicit_path = Path(str(html_arg))
        page_path = write_model_scoreboard_html(
            config,
            path=explicit_path,
            rows=scoreboard_rows,
            log_path=log_path,
            rows_read=len(rows),
            skipped=skipped,
            catalog_path=catalog_path,
            catalog_models=catalog_models,
            notes_path=notes_path,
            notes_sections=notes_sections,
            catalog_events=catalog_events,
        )
        print(page_path)
        if open_requested:
            open_in_browser(file_href(page_path))
        return 0
    if args.json:
        print(json.dumps(groups))
    else:
        print_model_log_table(log_path, len(rows), skipped, groups)
    return 0














def find_repo_identity(start: Path | None = None) -> str | None:
    """Per-repo agent identity: nearest .fleet-agent file walking up from start.

    Jon's fleet convention (2026-07-02): each repo has its own agent name
    (projects.agent_name in the fleet DB); a .fleet-agent file in the repo
    root mirrors it so stdlib-only tools like ringer resolve it without a
    database connection.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".fleet-agent"
        try:
            if candidate.is_file():
                name = re.sub(r"[^A-Za-z0-9_-]", "", candidate.read_text(encoding="utf-8", errors="replace").strip())
                if name:
                    return name
        except OSError:
            continue
    return None


def resolve_identity(
    value: str | None,
    config: AppConfig,
    identity_start_paths: Iterable[Path] = (),
) -> str:
    repo_identities = [find_repo_identity(start) for start in identity_start_paths]
    for candidate in (
        value,
        os.environ.get("FLEET_IDENTITY"),
        os.environ.get(f"{ENV_VAR_PREFIX}_IDENTITY"),
        *repo_identities,
        find_repo_identity(),
        config.identity_default,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return socket.gethostname().split(".", 1)[0] or TOOL_NAME


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values














def print_steering_notes(manifest: Manifest, config: AppConfig) -> None:
    """Surface driver-audience guidance once per resolved model. Never raise."""
    try:
        if config.steering.dir is None:
            return
        seen_models: set[str] = set()
        for task in manifest.tasks:
            try:
                engine = config.engines.get(task.engine)
                command: list[str] = []
                if engine is not None:
                    command = build_worker_command(
                        engine,
                        taskdir=(manifest.workdir / task.key).resolve(),
                        spec=task.spec,
                        full_access=task.full_access,
                        engine_args=task.engine_args,
                        model=task.model,
                    )
                model = resolved_task_model(task, engine, command)
                if not model or model in seen_models:
                    continue
                seen_models.add(model)
                profile = resolve_steering_profile(config.steering.dir, model)
                if profile is None:
                    continue
                rules = tuple(
                    rule
                    for rule in profile.rules
                    if rule.audience == "driver"
                    and rule.status in {"confirmed", "candidate", "stale-pending-reverify"}
                )
                if not rules:
                    continue
                print(
                    f"Steering notes for {model} (v{profile.profile_version}) "
                    "— apply when writing specs/feedback:"
                )
                for rule in rules:
                    print(f"- ({rule.status}) {rule.inject}")
            except Exception:
                continue
    except Exception:
        return


ENGINE_INSTALL_HINTS = {
    "codex": "install it with `npm install -g @openai/codex` (or `brew install --cask codex`), then run `codex login`",
    "opencode": "install it with `curl -fsSL https://opencode.ai/install | bash`, then run `opencode auth login`",
}


def preflight_engine_bins(manifest: Manifest, config: AppConfig) -> None:
    """Fail before spawning anything if a worker binary is missing.

    Without this, a fresh install dies mid-run with a bare
    "worker spawn failed: [Errno 2]" in the task log — the least helpful
    possible first experience.
    """
    for name in sorted({task.engine for task in manifest.tasks}):
        engine = config.engines.get(name)
        if engine is None:
            continue  # validate_manifest_engines already rejected this
        bin_path = engine.bin
        if os.sep in bin_path:
            found = Path(bin_path).expanduser()
            missing = not (found.is_file() and os.access(found, os.X_OK))
        else:
            missing = shutil.which(bin_path) is None
        if missing:
            hint = ENGINE_INSTALL_HINTS.get(name, f"install it or fix engines.{name}.bin in config.toml")
            raise ValueError(
                f"engine '{name}' binary not found ({bin_path}) — {hint}"
            )


def validate_manifest_engines(manifest: Manifest, config: AppConfig) -> None:
    missing = sorted({task.engine for task in manifest.tasks if task.engine not in config.engines})
    if missing:
        raise ValueError(f"unknown worker engine(s): {', '.join(missing)}")
    for task in manifest.tasks:
        engine = config.engines[task.engine]
        requires_model = any("{model}" in item for item in engine.args_template)
        accepts_model = requires_model or "{model_args}" in engine.args_template
        if requires_model and not (task.model or engine.model_default):
            raise ValueError(
                f"task {task.key}: engine {engine.name} needs a model — set the task's "
                f"\"model\" field or engines.{engine.name}.model_default in config.toml"
            )
        if task.model and not accepts_model:
            raise ValueError(
                f"task {task.key}: \"model\" is set but engine {engine.name} has no "
                "{model} placeholder in its args_template, so it would be silently ignored"
            )










































async def run_baseline(manifest: Manifest, *, config: AppConfig) -> int:
    """Execute every task's CHECK against the unmodified tree. Spawn nothing.

    The point: a check assertion that encodes NEW behavior is *expected* to
    fail here, but an assertion that encodes UNCHANGED behavior and fails
    here is a bug in the check itself — and at run time it will burn a
    worker's attempts against something no model can satisfy. Running the
    checks once, before any worker spawns, makes that question answerable in
    one command. The harness only reports; deciding which failures are
    expected is the orchestrator's judgment.

    Checks run for real — including any exports or side effects they perform
    (e.g. a fix-swarm check writing its patch file). Each task gets a fresh
    scratch taskdir (a detached worktree when the manifest uses worktrees),
    removed afterwards, so no state leaks between checks or into a later run.
    """
    del config  # engines are irrelevant: baseline spawns no workers
    verifier = Verifier()
    worktrees = manifest.worktrees and manifest.repo is not None
    baseline_root = Path(tempfile.mkdtemp(prefix="ringer-baseline-"))
    total = len(manifest.tasks)
    print(f"Baseline: executing {total} check(s) with no workers spawned.")
    failures = 0
    errors = 0
    leaked_worktrees: list[str] = []
    try:
        for task in manifest.tasks:
            taskdir = (baseline_root / task.key).resolve()
            # Same containment rule as the real run path: a key must not
            # escape its scratch root.
            if not taskdir.is_relative_to(baseline_root.resolve()) or taskdir == baseline_root.resolve():
                errors += 1
                print(f"{task.key:<24} baseline: ERROR (task key escapes the baseline scratch root)")
                continue
            if worktrees:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(manifest.repo),
                    "worktree",
                    "add",
                    "--detach",
                    str(taskdir),
                    "HEAD",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode != 0:
                    errors += 1
                    print(f"{task.key:<24} baseline: ERROR (git worktree add failed)")
                    message = stdout.decode("utf-8", errors="replace").strip()
                    for line in message.splitlines()[:4]:
                        print(f"    {line}")
                    continue
            else:
                taskdir.mkdir(parents=True, exist_ok=True)
            try:
                verify = await verifier.verify(task, taskdir)
                status = "pass" if verify.ok else "FAIL"
                timed_out = ", timed out" if verify.check_timed_out else ""
                print(
                    f"{task.key:<24} baseline: {status} "
                    f"(rc={verify.check_returncode}{timed_out})"
                )
                if not verify.ok:
                    failures += 1
                    excerpt = verify.raw_output_excerpt.strip()
                    for line in excerpt.splitlines()[:6]:
                        print(f"    {line}")
            finally:
                if worktrees:
                    proc = await asyncio.create_subprocess_exec(
                        "git",
                        "-C",
                        str(manifest.repo),
                        "worktree",
                        "remove",
                        "--force",
                        str(taskdir),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode != 0:
                        # A clean summary must not hide leaked worktree state.
                        leaked_worktrees.append(str(taskdir))
                        message = stdout.decode("utf-8", errors="replace").strip()
                        print(f"{task.key:<24} baseline: WARNING (worktree remove failed, leaked {taskdir})")
                        for line in message.splitlines()[:2]:
                            print(f"    {line}")
    finally:
        shutil.rmtree(baseline_root, ignore_errors=True)
    passed = total - failures - errors
    print(f"\nbaseline: {passed} pass, {failures} fail, {errors} error of {total} check(s).")
    if leaked_worktrees:
        print(
            f"WARNING: {len(leaked_worktrees)} baseline worktree(s) could not be removed; "
            f"clean up with `git -C {shlex.quote(str(manifest.repo))} worktree prune` after "
            "removing the directories above."
        )
    print(
        "Reading the results: a FAIL is EXPECTED for assertions that demand the\n"
        "NEW behavior workers will build. A FAIL on an assertion about UNCHANGED\n"
        "behavior means the check itself is broken and will burn worker attempts\n"
        "against something no model can satisfy — fix the check before spawning."
    )
    return 0










def dry_run(
    manifest: Manifest,
    config: AppConfig,
    identity: str,
    dashboard_enabled: bool,
    force_browser: bool,
) -> None:
    print("DRY RUN: no codex workers will be spawned.")
    print(f"Run: {manifest.run_name}")
    print(f"Identity: {identity}")
    print(f"Config: {config.path if config.path else '(safe defaults)'}")
    print(f"Workdir: {manifest.workdir}")
    print(f"Max parallel: {manifest.max_parallel}")
    print(f"Worktrees: {manifest.worktrees} repo={manifest.repo}")
    print(f"State dir: {config.state_dir}")
    print(f"Eval backend: {config.eval.backend}")
    print(f"Dashboard: {'on' if dashboard_enabled else 'off'}")
    if dashboard_enabled:
        mode = "browser"
        if not force_browser and config.hud_app_path is not None:
            mode = f"HUD app {config.hud_app_path} when available, browser fallback"
        print(f"Dashboard opener: {mode}")
        print(f"Dashboard port base: {config.dashboard_port_base}")
    print(f"Artifacts: {'on' if config.artifact.enabled else 'off'}")
    if config.artifact.enabled:
        run_id_preview = build_run_id(manifest.run_name)
        print(f"  live status page: {config.artifact.artifact_path(run_id_preview, manifest.run_name)}")
        print(f"  final report:     {config.artifact.report_path(run_id_preview, manifest.run_name)}")
        print(f"  runs index:       {config.artifact.index_out}")
    print("Tasks:")
    for task in manifest.tasks:
        taskdir = (manifest.workdir / task.key).resolve()
        engine = config.engines.get(task.engine)
        full_access_allowed = task.full_access and config.allow_full_access
        cmd = (
            build_worker_command(
                engine,
                taskdir=taskdir,
                spec=task.spec,
                full_access=task.full_access,
                engine_args=task.engine_args,
                model=task.model,
            )
            if engine is not None
            else []
        )
        print(f"  - {task.key}")
        print(f"    engine: {task.engine}")
        print(f"    dir: {taskdir}")
        print(f"    timeout_s: {task.timeout_s}")
        print(f"    max_attempts: {task.max_attempts}")
        if task.full_access:
            print(f"    full_access: true allowed={full_access_allowed}")
        else:
            print("    full_access: false")
        print(f"    expect_files: {list(task.expect_files)}")
        print(f"    check: {task.check}")
        if engine is None:
            print("    command: ERROR unknown engine")
        elif task.full_access and not config.allow_full_access:
            print("    command: ERROR full_access requires allow_full_access=true in config")
        else:
            print(f"    command: {shell_command_for_display(cmd)} < /dev/null")


def print_lint_findings(findings: list[str]) -> None:
    for finding in findings:
        print(f"lint: {finding}")




def create_demo_manifest() -> Path:
    root = Path(tempfile.mkdtemp(prefix="ringer-demo-"))
    workdir = root / "work"
    manifest = {
        "run_name": "ringer-demo",
        "workdir": str(workdir),
        "max_parallel": 3,
        "worktrees": False,
        "repo": None,
        "tasks": [
            {
                "key": "alpha",
                "spec": "Create alpha.txt in the current working directory containing exactly: alpha ready\nDo not add punctuation. Do not write any other files.",
                "check": "test \"$(cat alpha.txt 2>/dev/null)\" = \"alpha ready\" || { echo 'FAIL: alpha.txt missing or content is not alpha ready'; exit 1; }",
                "verified": "alpha.txt exists and contains exactly the expected text",
                "expect_files": ["alpha.txt"],
                "task_type": "probe",
            },
            {
                "key": "bravo",
                "spec": "Create bravo.txt in the current working directory containing exactly: bravo ready\nDo not add punctuation. Do not write any other files.",
                "check": "test \"$(cat bravo.txt 2>/dev/null)\" = \"bravo ready\" || { echo 'FAIL: bravo.txt missing or content is not bravo ready'; exit 1; }",
                "verified": "bravo.txt exists and contains exactly the expected text",
                "expect_files": ["bravo.txt"],
                "task_type": "probe",
            },
            {
                "key": "charlie",
                "spec": "Create charlie.txt in the current working directory containing exactly: charlie ready\nDo not add punctuation. Do not write any other files.",
                "check": "test \"$(cat charlie.txt 2>/dev/null)\" = \"charlie ready\" || { echo 'FAIL: charlie.txt missing or content is not charlie ready'; exit 1; }",
                "verified": "charlie.txt exists and contains exactly the expected text",
                "expect_files": ["charlie.txt"],
                "task_type": "probe",
            },
        ],
    }
    path = root / "ringer.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def read_one_request(request: str | None, request_file: Path | None) -> str:
    if request and request_file is not None:
        raise ValueError("give the request as text or with --request-file, not both")
    if request_file is not None:
        try:
            text = request_file.expanduser().resolve().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"could not read request file {request_file}: {exc}"
            ) from exc
    else:
        text = request or ""
    text = text.strip()
    if not text:
        raise ValueError("a request is required")
    return text


def one_request_workdir(config: AppConfig, supplied: Path | None) -> Path:
    if supplied is not None:
        workdir = supplied.expanduser().resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        workdir = (
            config.state_dir / "requests" / f"{stamp}-p{os.getpid()}"
        ).resolve()
    taskdir = workdir / "answer"
    if taskdir.exists():
        raise ValueError(
            f"refusing to reuse an existing answer directory: {taskdir}"
        )
    return workdir


def one_request_manifest(
    *,
    packet: ContextPacket,
    workdir: Path,
    engine: str,
    timeout_s: int,
    reasoning_effort: str,
    model: str | None,
    redact: bool,
) -> Manifest:
    engine_args: list[str] = []
    if engine == DEFAULT_ENGINE_NAME:
        engine_args.extend(("-c", f"model_reasoning_effort={reasoning_effort}"))
    elif model:
        raise ValueError("--model is currently supported only by the codex engine")
    return Manifest(
        run_name="one-request",
        workdir=workdir,
        max_parallel=1,
        worktrees=False,
        repo=None,
        tasks=(
            TaskSpec(
                key="answer",
                spec=packet.text,
                check=(
                    "test -s answer.md || "
                    "{ echo 'FAIL: answer.md was not created or is empty'; exit 1; }"
                ),
                engine=engine,
                expect_files=("answer.md",),
                timeout_s=timeout_s,
                max_attempts=1,
                redact_spec=redact,
                engine_args=tuple(engine_args),
                model=model or "",
                verified=(
                    "answer.md exists and is not empty; this does not prove "
                    "that the answer is correct"
                ),
                task_type="one-request",
            ),
        ),
    )


def print_packet_report(packet: ContextPacket, workdir: Path) -> None:
    selected_source_bytes = sum(
        len(chunk.text.encode("utf-8")) for chunk in packet.selected
    )
    removed = max(0, packet.source_bytes - selected_source_bytes)
    percent = (
        removed / packet.source_bytes * 100.0
        if packet.source_bytes
        else 0.0
    )
    print(
        f"Built a {packet.packet_bytes:,}-byte request packet. It contains "
        f"{selected_source_bytes:,} of {packet.source_bytes:,} source bytes "
        f"({percent:.1f}% of source text left out before the model call)."
    )
    for chunk in packet.selected:
        kind = "state" if chunk.state else "source"
        location = f"{chunk.path}:{chunk.start_line}-{chunk.end_line}"
        if chunk.start_line == chunk.end_line:
            location += f" chars {chunk.start_char}-{chunk.end_char}"
        print(f"  {kind}: {location}")
    for item in packet.skipped:
        print(f"  skipped: {item}")
    print(f"Saved the selection report in {workdir}")


def codex_usage_from_log(path: Path) -> dict[str, int] | None:
    try:
        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return None
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    found = False
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        found = True
        for key in totals:
            value = usage.get(key, 0)
            if isinstance(value, int):
                totals[key] += value
    return totals if found else None


def run_one_request(config: AppConfig, args: argparse.Namespace) -> int:
    request = read_one_request(args.request, args.request_file)
    workdir = one_request_workdir(config, args.workdir)
    packet = build_context_packet(
        request,
        sources=args.source,
        state_files=args.state,
        max_packet_bytes=args.max_packet_bytes,
        max_file_bytes=args.max_file_bytes,
        max_files=args.max_files,
    )
    supplied_sources = bool(args.source or args.state)
    if supplied_sources and not packet.selected:
        skipped = "; ".join(packet.skipped) or "no passage matched the request"
        raise ValueError(
            "none of the supplied source text was selected, so no model call "
            f"was made: {skipped}"
        )
    workdir.mkdir(parents=True, exist_ok=False)
    if args.keep_packet:
        (workdir / "packet.txt").write_text(packet.text, encoding="utf-8")
    packet.write_report(workdir / "packet-report.json")
    print_packet_report(packet, workdir)
    if args.dry_run:
        print("No model call was made.")
        return 0

    manifest = one_request_manifest(
        packet=packet,
        workdir=workdir,
        engine=args.engine,
        timeout_s=args.timeout_s,
        reasoning_effort=args.reasoning_effort,
        model=args.model,
        redact=args.redact,
    )
    validate_manifest_engines(manifest, config)
    preflight_engine_bins(manifest, config)
    identity = resolve_identity(
        args.identity,
        config,
        [workdir, *args.source, *args.state],
    )
    presentation = bool(args.dashboard or args.browser) and not args.no_dashboard
    if not presentation and config.artifact.enabled:
        config = dataclass_replace(config, artifact=dataclass_replace(config.artifact, enabled=False))
    if args.no_artifact and config.artifact.enabled:
        config = dataclass_replace(config, artifact=dataclass_replace(config.artifact, enabled=False))
    if args.dashboard and not args.browser and presentation:
        ensure_hud_running(config, open_browser=True)
    result = asyncio.run(
        run_manifest(
            manifest,
            config=config,
            identity=identity,
            dashboard_enabled=presentation,
            force_browser=bool(args.browser and presentation),
        )
    )
    answer_path = workdir / "answer" / "answer.md"
    if result == 0 and answer_path.is_file():
        print("\nAnswer\n")
        print(answer_path.read_text(encoding="utf-8").rstrip())
    usage = codex_usage_from_log(workdir / "answer" / "worker.log")
    if usage is not None:
        print(
            "\nModel use: "
            f"{usage['input_tokens']:,} input, "
            f"{usage['cached_input_tokens']:,} reused input, "
            f"{usage['output_tokens']:,} output."
        )
    return result


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def claude_root(project: bool) -> Path:
    return (Path.cwd() if project else Path.home()) / ".claude"


def ringer_skill_source() -> Path:
    return repo_root() / ".claude" / "skills" / "ringer" / "SKILL.md"


def ringer_hook_command(action: str) -> str:
    hook_path = repo_root() / "hooks" / "ringer_nudge.py"
    return f"python3 {shlex.quote(str(hook_path))} {action}"


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"settings file must contain a JSON object: {path}")
    return data


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def hook_command_contains(value: Any, needle: str = "ringer_nudge.py") -> bool:
    return isinstance(value, dict) and needle in str(value.get("command", ""))


def event_has_ringer_hook(groups: Any) -> bool:
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if isinstance(handlers, list) and any(hook_command_contains(handler) for handler in handlers):
            return True
    return False


def merge_ringer_hook(settings: dict[str, Any], event: str, matcher: str, command: str) -> bool:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings hooks field must be a JSON object")
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"settings hooks.{event} field must be a JSON array")
    if event_has_ringer_hook(groups):
        return False
    groups.append(
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ],
        }
    )
    return True


def remove_ringer_hooks(settings: dict[str, Any]) -> int:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept_handlers = []
            for handler in handlers:
                if hook_command_contains(handler):
                    removed += 1
                else:
                    kept_handlers.append(handler)
            if kept_handlers:
                new_group = dict(group)
                new_group["hooks"] = kept_handlers
                kept_groups.append(new_group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def install_agent(project: bool = False) -> int:
    root = claude_root(project)
    skill_source = ringer_skill_source()
    skill_target = root / "skills" / "ringer" / "SKILL.md"
    if not skill_source.exists():
        raise ValueError(f"ringer skill source not found: {skill_source}")
    skill_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_source, skill_target)

    settings_path = root / "settings.json"
    settings = load_settings(settings_path)
    changed = False
    changed |= merge_ringer_hook(
        settings,
        "PreToolUse",
        "Bash",
        ringer_hook_command("pre-bash"),
    )
    changed |= merge_ringer_hook(
        settings,
        "PostToolUse",
        "Edit|Write",
        ringer_hook_command("post-edit"),
    )
    if changed or not settings_path.exists():
        write_settings(settings_path, settings)

    scope = "project" if project else "user"
    print(f"Installed ringer agent for {scope} scope.")
    print(f"Skill: {skill_target}")
    if changed:
        print(f"Hooks: added PreToolUse Bash and PostToolUse Edit|Write in {settings_path}")
    else:
        print(f"Hooks: already present in {settings_path}")
    return 0


def uninstall_agent(project: bool = False) -> int:
    root = claude_root(project)
    settings_path = root / "settings.json"
    removed_hooks = 0
    if settings_path.exists():
        settings = load_settings(settings_path)
        removed_hooks = remove_ringer_hooks(settings)
        if removed_hooks:
            write_settings(settings_path, settings)

    skill_dir = root / "skills" / "ringer"
    removed_skill = False
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        removed_skill = True

    scope = "project" if project else "user"
    print(f"Uninstalled ringer agent for {scope} scope.")
    print(f"Hooks removed: {removed_hooks}")
    print(f"Skill removed: {'yes' if removed_skill else 'no'}")
    return 0


async def run_manifest(
    manifest: Manifest,
    config: AppConfig,
    identity: str,
    dashboard_enabled: bool,
    force_browser: bool,
) -> int:
    logger = EvalLogger(config.eval)
    try:
        runner = RingerRunner(
            manifest,
            config=config,
            identity=identity,
            dashboard_enabled=dashboard_enabled,
            force_browser=force_browser,
            logger=logger,
        )
    except BaseException:
        logger.close()
        raise
    register_active_run(
        runner.run_id,
        identity,
        manifest.run_name,
        manifest.workdir,
        started_at=runner.started_at,
    )
    task = asyncio.create_task(runner.run())
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    shutdown_started = False

    def request_shutdown() -> None:
        # One-shot: a repeat signal must not cancel the in-progress worker
        # cleanup and state flush, or it recreates the orphan problem.
        nonlocal shutdown_started
        if shutdown_started:
            print(
                "ringer.py: shutdown already in progress; waiting on worker cleanup",
                file=sys.stderr,
            )
            return
        shutdown_started = True
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, request_shutdown)
            registered_signals.append(sig)
    try:
        return await task
    except asyncio.CancelledError:
        return 130
    finally:
        for sig in registered_signals:
            loop.remove_signal_handler(sig)
        unregister_active_run(runner.run_id)


def hud_is_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.4) as response:
            return response.status == 200
    except Exception:
        return False




def current_repo_head(
    repo_dir: Path | None = None,
    *,
    runner: Any = subprocess.run,
) -> str | None:
    repo = (repo_dir or Path(__file__).resolve().parent).resolve()
    git_bin = shutil.which("git")
    if git_bin is None or not (repo / ".git").exists():
        return None
    try:
        result = _run_self_update_git(runner, git_bin, repo, "rev-parse", "HEAD")
    except Exception:
        return None
    head = str(result.stdout).strip() if result.returncode == 0 else ""
    return head or None


def hud_should_restart(
    recorded_running_head: str | None,
    disk_head: str | None,
    update_result: SelfUpdateResult | None = None,
) -> bool:
    if update_result is not None and update_result.applied:
        return True
    return disk_head is not None and disk_head != recorded_running_head


def start_hud_update_maintenance(
    config: AppConfig,
    server: PersistentHudServer,
    *,
    recorded_running_head: str | None,
    argv: list[str] | None = None,
    repo_dir: Path | None = None,
    script_path: Path | None = None,
    runner: Any = subprocess.run,
    execve: Any = os.execve,
    environ: dict[str, str] | None = None,
) -> threading.Thread | None:
    repo = (repo_dir or Path(__file__).resolve().parent).resolve()
    script = (script_path or Path(__file__).resolve()).resolve()
    invocation = list(argv if argv is not None else sys.argv)
    env = environ if environ is not None else os.environ

    def worker() -> None:
        while True:
            time.sleep(config.update.check_interval_s)
            try:
                result: SelfUpdateResult | None = None
                if (
                    config.update.auto
                    and env.get("RINGER_NO_SELF_UPDATE") != "1"
                    and env.get("RINGER_SELF_UPDATED") != "1"
                ):
                    result = perform_self_update(
                        config=config,
                        argv=invocation,
                        repo_dir=repo,
                        script_path=script,
                        force=True,
                        allow_reexec=False,
                        runner=runner,
                        environ=env,
                    )
                    if result.blocked:
                        server.update_status = {
                            "behind": result.behind,
                            "reason": result.reason or "update is blocked",
                        }
                    elif result.status in {"up_to_date", "applied"}:
                        server.update_status = None
                disk_head = current_repo_head(repo, runner=runner)
                if not hud_should_restart(recorded_running_head, disk_head, result):
                    continue
                server.stop()
                next_env = dict(env)
                next_env["RINGER_SELF_UPDATED"] = "1"
                execve(
                    sys.executable,
                    [sys.executable, str(script), *invocation[1:]],
                    next_env,
                )
                return
            except Exception:
                continue

    try:
        thread = threading.Thread(
            target=worker,
            name="ringer-hud-self-update",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception:
        return None


def ensure_hud_running(config: AppConfig, *, open_browser: bool) -> None:
    """Make sure the persistent Ringside page is up before a run starts.

    The human should never have to remember a second command to watch the
    fight: if no hud answers on the configured port, spawn one detached.
    """
    port = config.hud_port
    url = f"http://127.0.0.1:{port}"
    already_alive = hud_is_alive(port)
    if not already_alive:
        log_path = config.state_dir / "hud.log"
        with contextlib.suppress(Exception):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log_file:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "hud", "--no-open", "--port", str(port)],
                    stdout=log_file,
                    stderr=log_file,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
        for _ in range(20):
            if hud_is_alive(port):
                break
            time.sleep(0.15)
    if open_browser and not already_alive and hud_is_alive(port):
        open_in_browser(url)
    print(f"Ringside: {url}", flush=True)


def run_persistent_hud(config: AppConfig, *, port: int | None, open_viewer: bool) -> int:
    chosen_port = port if port is not None else config.hud_port
    if hud_is_alive(chosen_port):
        url = f"http://127.0.0.1:{chosen_port}"
        print(f"Ringside is already running: {url}")
        if open_viewer:
            open_in_browser(url)
        return 0
    server = PersistentHudServer(
        config.state_dir,
        preferred_port=chosen_port,
        open_viewer=open_viewer,
    )
    server.model_log_path = config.eval.jsonl_path
    server.default_model_log_path = config.eval.jsonl_path
    repo_dir = Path(__file__).resolve().parent
    running_head = current_repo_head(repo_dir)
    server.update_status = self_update_dashboard_status(config.state_dir, repo_dir)
    server.start()
    start_hud_update_maintenance(
        config,
        server,
        recorded_running_head=running_head,
        repo_dir=repo_dir,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nRingside stopped.")
        return 0
    finally:
        server.stop()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ringer.py",
        description=(
            "Ringer: deterministic parallel AI-agent orchestrator. Runs manifest tasks in parallel, "
            "verifies artifacts with executed checks, retries failures once, logs eval rows, "
            "and serves a live dashboard."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.toml (default: XDG config path)")
    parser.add_argument(
        "--no-self-update",
        action="store_true",
        help="skip the startup self-update check for this invocation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser(
        "self-update", help="check and apply an ff-only update from origin/main"
    )
    update_parser.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    run_parser = subparsers.add_parser("run", help="run a ringer manifest")
    run_parser.add_argument("manifest", type=Path, help="path to ringer.json")
    run_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    run_parser.add_argument("--max-parallel", type=int, help="override manifest max_parallel")
    run_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    run_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    run_parser.add_argument("--dashboard", action="store_true", help="start the Ringside dashboard")
    run_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    run_parser.epilog = "Set RINGER_NO_CATALOG_REFRESH=1 to skip the non-blocking OpenRouter catalog auto-refresh."
    run_parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="disable zero-LLM HTML status/report artifacts (see [artifact] in config.toml)",
    )
    run_parser.add_argument("--dry-run", action="store_true", help="print the plan without spawning codex")
    run_parser.add_argument(
        "--baseline",
        action="store_true",
        help=(
            "execute every task's CHECK against the unmodified tree and report, "
            "spawning no workers — assertions about unchanged behavior that fail "
            "baseline are bugs in the check, not work for a model"
        ),
    )
    run_parser.add_argument(
        "--allow-noncanonical-route",
        action="store_true",
        help="allow a registry-marked noncanonical model route for a deliberate bakeoff",
    )

    ask_parser = subparsers.add_parser(
        "ask",
        help="answer one normal request with a small, clean worker",
    )
    ask_parser.add_argument("request", nargs="?", help="the normal-language request")
    ask_parser.add_argument(
        "--request-file",
        type=Path,
        help="read the request from a text file",
    )
    ask_parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help=(
            "file or directory to search for relevant passages; "
            "repeat as needed"
        ),
    )
    ask_parser.add_argument(
        "--state",
        type=Path,
        action="append",
        default=[],
        help=(
            "small file with settled decisions that must take priority; "
            "repeat as needed"
        ),
    )
    ask_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    ask_parser.add_argument(
        "--engine",
        default=DEFAULT_ENGINE_NAME,
        help=f"worker engine (default: {DEFAULT_ENGINE_NAME})",
    )
    ask_parser.add_argument("--model", help="Codex model override")
    ask_parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high"),
        default="low",
        help="Codex reasoning effort (default: low)",
    )
    ask_parser.add_argument(
        "--timeout-s",
        type=int,
        default=300,
        help="worker timeout (default: 300)",
    )
    ask_parser.add_argument(
        "--max-packet-bytes",
        type=int,
        default=16_000,
        help=(
            "hard limit for request plus selected source text "
            "(default: 16000)"
        ),
    )
    ask_parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=4_000_000,
        help="skip any one source larger than this (default: 4000000)",
    )
    ask_parser.add_argument(
        "--max-files",
        type=int,
        default=200,
        help="source file limit (default: 200)",
    )
    ask_parser.add_argument(
        "--workdir",
        type=Path,
        help="where to save the packet, answer, and log",
    )
    ask_parser.add_argument(
        "--keep-packet",
        action="store_true",
        help="save the full request packet for debugging (off by default)",
    )
    ask_parser.add_argument(
        "--redact",
        action="store_true",
        help="hide the request packet from state, command, and eval records",
    )
    ask_parser.add_argument(
        "--dashboard", action="store_true", help="start Ringside presentation for this run"
    )
    ask_parser.add_argument(
        "--browser", action="store_true", help="open the per-run dashboard in a browser"
    )
    ask_parser.add_argument(
        "--no-dashboard", action="store_true", help="disable dashboard even when requested"
    )
    ask_parser.add_argument(
        "--no-artifact", action="store_true", help="disable generated HTML artifacts"
    )
    ask_parser.add_argument(
        "--identity",
        help="orchestrator identity for the local run record",
    )
    ask_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "select passages and show the packet size without making "
            "a model call"
        ),
    )

    lint_parser = subparsers.add_parser("lint", help="lint a ringer manifest")
    lint_parser.add_argument("manifest", type=Path, help="path to ringer.json")
    # Lint reads the config now (engine names, model routes), so it takes the
    # same suppressed --config every other config-consuming command takes.
    lint_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    lint_parser.add_argument(
        "--allow-noncanonical-route",
        action="store_true",
        help="allow a registry-marked noncanonical model route for a deliberate bakeoff",
    )

    hud_parser = subparsers.add_parser("hud", help="start the persistent Ringside page in your browser")
    hud_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    hud_parser.add_argument("--port", type=int, help=f"port to bind on 127.0.0.1 (default: {DEFAULT_HUD_PORT})")
    hud_parser.add_argument("--no-open", action="store_true", help="start the server without opening a browser")

    db_parser = subparsers.add_parser("db", help="manage the derived SQLite read model")
    db_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    for name in ("rebuild", "sync"):
        sub = db_subparsers.add_parser(name, help=f"{name} the derived SQLite read model")
        sub.add_argument("--db", type=Path, help="path to SQLite read model (default: ~/.ringer/ringer.db)")
        sub.add_argument("--log", type=Path, help="path to local eval JSONL log")
        sub.add_argument("--catalog-file", type=Path, help="path to local OpenRouter catalog snapshot")
        sub.add_argument("--registry", type=Path, help="path to model identity registry")

    models_parser = subparsers.add_parser("models", help="show the local per-model performance scoreboard")
    models_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    models_parser.add_argument("--log", type=Path, help="path to local eval JSONL log")
    models_parser.add_argument("--db", type=Path, help="path to SQLite read model (default: ~/.ringer/ringer.db)")
    models_parser.add_argument("--task-type", help="only include one task_type bucket")
    models_parser.add_argument("--model", help="only include one resolved model bucket")
    models_parser.add_argument("--engine", help="only include rows from one worker engine")
    models_parser.add_argument("--since", help="only include rows logged on or after YYYY-MM-DD")
    models_parser.add_argument("--explore", action="store_true", help="show proven/probation tiers plus cheap untested catalog candidates")
    models_parser.add_argument("--catalog-file", type=Path, help="path to local OpenRouter catalog snapshot")
    models_parser.add_argument("--notes-file", type=Path, default=default_model_notes_path(), help="path to MODEL-NOTES.md judgment layer")
    models_parser.add_argument("--registry", type=Path, default=default_model_registry_path(), help=argparse.SUPPRESS)
    models_parser.add_argument("--html", nargs="?", const="", help="render a self-contained HTML scoreboard; optional output path")
    models_parser.add_argument("--open", action="store_true", help="render the HTML scoreboard to the artifact library and open it")
    models_parser.add_argument("--json", action="store_true", help="print the scoreboard as JSON")

    evidence_parser = subparsers.add_parser("evidence", help="inspect and push local evaluation evidence")
    evidence_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    for name in ("push", "status"):
        evidence_sub = evidence_subparsers.add_parser(name, help=f"{name} local evaluation evidence")
        evidence_sub.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        evidence_sub.add_argument("--file", type=Path, action="append", dest="file", help="evidence JSONL files; default is the configured [eval] jsonl_path")
        if name == "push":
            evidence_sub.add_argument("--since", help="only include rows logged on or after this ISO-8601 date or datetime")
            evidence_sub.add_argument("--source-host", help="source host name for central evidence")
            evidence_sub.add_argument("--spec-storage", choices=("hash", "excerpt"), help="central prompt storage policy")
            evidence_sub.add_argument("--dry-run", action="store_true", help="validate and report without sending rows")

    catalog_parser = subparsers.add_parser("catalog", help="show or refresh the local OpenRouter model catalog")
    catalog_parser.add_argument("--refresh", action="store_true", help="fetch source and rewrite the local snapshot")
    catalog_parser.add_argument("--source", help=f"OpenRouter models URL or fixture file (default: {DEFAULT_CATALOG_SOURCE})")
    catalog_parser.add_argument("--file", type=Path, help="catalog snapshot path (default: ~/.ringer/openrouter-catalog.json)")
    catalog_parser.add_argument("--free", action="store_true", help="show free models only")
    catalog_parser.add_argument("--changes", action="store_true", help="show recent catalog changes newest first")
    catalog_parser.add_argument("--json", action="store_true", help="print the model list as JSON and nothing else")

    demo_parser = subparsers.add_parser("demo", help="generate and run a 3-task toy manifest in /tmp")
    demo_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    demo_parser.add_argument("--max-parallel", type=int, help="override demo max_parallel")
    demo_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    demo_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    demo_parser.add_argument("--dashboard", action="store_true", help="start the Ringside dashboard")
    demo_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    demo_parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="disable zero-LLM HTML status/report artifacts (see [artifact] in config.toml)",
    )
    demo_parser.add_argument("--dry-run", action="store_true", help="print the demo plan without spawning codex")

    install_parser = subparsers.add_parser("install-agent", help="install the ringer Claude Code skill and hooks")
    install_parser.add_argument("--project", action="store_true", help="install into ./.claude instead of ~/.claude")

    uninstall_parser = subparsers.add_parser("uninstall-agent", help="remove the ringer Claude Code skill and hooks")
    uninstall_parser.add_argument("--project", action="store_true", help="remove from ./.claude instead of ~/.claude")
    return parser


def main(argv: list[str] | None = None) -> int:
    invocation_argv = list(sys.argv) if argv is None else [str(Path(__file__).resolve()), *argv]
    maybe_self_update(invocation_argv)
    # The guard is only for this process start. Clearing it lets a restarted,
    # long-running HUD discover a later update during its lifetime.
    os.environ.pop("RINGER_SELF_UPDATED", None)
    # Keep progress lines live when stdout is a pipe (tee, orchestrators).
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    parser = build_parser()
    parse_argv = [value for value in invocation_argv[1:] if value != "--no-self-update"]
    args = parser.parse_args(parse_argv)
    try:
        if args.command == "self-update":
            config = AppConfig.load(args.config)
            result = perform_self_update(
                config=config,
                argv=invocation_argv,
                force=True,
                allow_reexec=False,
            )
            if result.status == "up_to_date":
                print("Ringer is up to date.")
                return 0
            if result.applied:
                print(
                    f"Applied {result.behind} commit(s) "
                    f"{result.old_head}..{result.new_head}."
                )
                return 0
            if result.blocked:
                print(
                    f"Ringer is {result.behind} commit(s) behind but blocked because "
                    f"{result.reason}."
                )
                return 1
            if result.status == "error":
                print(f"Self-update check failed: {result.reason}.")
                return 0
            print(f"Self-update skipped: {result.reason or 'not available'}.")
            return 0
        if args.command == "install-agent":
            return install_agent(project=args.project)
        if args.command == "uninstall-agent":
            return uninstall_agent(project=args.project)

        if args.command == "lint":
            manifest = Manifest.from_path(args.manifest)
            # Lint ran with config=None until 2026-08-15 - the shared
            # `AppConfig.load` below sits AFTER this branch returns. That made
            # two checks vacuous at once: engine names were never resolvable,
            # and noncanonical_route_findings fell back to the identity
            # registry's defaults instead of the engine's real model_default
            # for every task that does not name a model. Load it here.
            lint_config: AppConfig | None
            config_error = ""
            try:
                lint_config = AppConfig.load(args.config)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                lint_config = None
                config_error = str(exc)
            findings = lint_manifest(
                manifest,
                config=lint_config,
                allow_noncanonical_route=args.allow_noncanonical_route,
            )
            if config_error:
                # Say so rather than degrade quietly: without a config the
                # engine and route checks did not run, and a bare "clean" would
                # claim more than was actually checked.
                findings.insert(
                    0,
                    f"ERROR: manifest: ringer config could not be loaded ({config_error}); "
                    "engine names and model routes were NOT checked.",
                )
            if findings:
                print_lint_findings(findings)
                return 1
            print(f"lint: clean ({len(manifest.tasks)} tasks)")
            return 0

        if args.command == "catalog":
            return run_catalog_command(args)

        config = AppConfig.load(args.config)
        if args.command == "db":
            return run_db_command(config, args)
        if args.command == "models":
            return run_models_command(config, args)
        if args.command == "evidence":
            return run_evidence_command(config, args, read_env=parse_env_file)
        if args.command == "hud":
            return run_persistent_hud(
                config,
                port=args.port,
                open_viewer=not args.no_open,
            )
        if args.command == "ask":
            if args.timeout_s <= 0:
                raise ValueError("--timeout-s must be positive")
            return run_one_request(config, args)

        if args.command == "demo":
            manifest_path = create_demo_manifest()
            print(f"Demo manifest: {manifest_path}")
        else:
            manifest_path = args.manifest
        manifest = Manifest.from_path(manifest_path).with_max_parallel(args.max_parallel)
        with contextlib.suppress(Exception):
            print_steering_notes(manifest, config)
        lint_findings = lint_manifest(
            manifest,
            include_model_log_nudges=True,
            config=config,
            allow_noncanonical_route=bool(
                getattr(args, "allow_noncanonical_route", False)
            ),
        )
        print_lint_findings(lint_findings)
        if any(finding.startswith("ERROR:") for finding in lint_findings):
            return 1
        validate_manifest_engines(manifest, config)
        identity_start_paths = [manifest.workdir]
        if manifest.source_path is not None:
            identity_start_paths.append(manifest.source_path.parent)
        identity = resolve_identity(args.identity, config, identity_start_paths)
        dashboard_enabled = (
            bool(args.browser or getattr(args, "dashboard", False)) and not args.no_dashboard
        )
        if not dashboard_enabled and config.artifact.enabled:
            config = dataclass_replace(config, artifact=dataclass_replace(config.artifact, enabled=False))
        if getattr(args, "no_artifact", False) and config.artifact.enabled:
            config = dataclass_replace(config, artifact=dataclass_replace(config.artifact, enabled=False))
        if args.dry_run:
            dry_run(
                manifest,
                config=config,
                identity=identity,
                dashboard_enabled=dashboard_enabled,
                force_browser=args.browser,
            )
            return 0
        if getattr(args, "baseline", False):
            # Deliberately before preflight_engine_bins: baseline spawns no
            # workers, so a missing engine binary must not block it.
            return asyncio.run(run_baseline(manifest, config=config))
        preflight_engine_bins(manifest, config)
        if args.command == "run":
            start_catalog_auto_refresh()
        if dashboard_enabled and not args.browser:
            ensure_hud_running(config, open_browser=True)
        return asyncio.run(
            run_manifest(
                manifest,
                config=config,
                identity=identity,
                dashboard_enabled=dashboard_enabled,
                force_browser=args.browser,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ringer.py: error: {exc}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(main())
