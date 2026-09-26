from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModuleBoundaryTests(unittest.TestCase):
    def test_model_resource_paths_are_repository_rooted_from_other_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, "-c", "from ringer_core.models import default_model_notes_path, default_model_registry_path; from pathlib import Path; root=Path(%r); assert default_model_notes_path()==root/'docs'/'MODEL-NOTES.md'; assert default_model_registry_path()==root/'registry'/'model-identity.toml'" % str(ROOT)],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_models_import_is_data_only_and_does_not_load_cli_or_views(self) -> None:
        script = r'''
import sys, builtins, subprocess
from pathlib import Path
original_open, original_path_open = builtins.open, Path.open
original_run, original_popen = subprocess.run, subprocess.Popen
fail = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("import caused I/O or process execution"))
builtins.open = Path.open = subprocess.run = subprocess.Popen = fail
import ringer_core.models as models
builtins.open, Path.open = original_open, original_path_open
subprocess.run, subprocess.Popen = original_run, original_popen
assert "ringer" not in sys.modules
assert not any(name.endswith(("readmodel", "views")) for name in sys.modules)
assert models.EMPTY_MODEL_IDENTITY_REGISTRY.identities == {}
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_direct_imports_are_independent_of_cli_and_reexports_keep_identity(self) -> None:
        script = r'''
import sys
import builtins
import ringer_core.context as context
import ringer_core.config as config
import ringer_core.manifests as manifests
import ringer_core.verification as verification
import subprocess
from pathlib import Path
original_run = subprocess.run
original_popen = subprocess.Popen
original_path_open = Path.open
original_builtin_open = builtins.open
subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime import ran a process"))
subprocess.Popen = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime import ran a process"))
Path.open = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime import performed file I/O"))
builtins.open = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime import performed file I/O"))
import ringer_core.state_files as state_files
import ringer_core.artifact_store as artifact_store
import ringer_core.worker_logs as worker_logs
import ringer_core.runtime as runtime
import ringer_core.artifact_views as artifact_views
import ringer_core.state as state
import ringer_core.catalog as catalog
import ringer_core.runner as runner
import ringer_core.steering as steering
subprocess.run = original_run
subprocess.Popen = original_popen
Path.open = original_path_open
builtins.open = original_builtin_open
assert "ringer" not in sys.modules
assert "ringer.py" not in sys.modules
import ringer_core.models as models
assert "ringer" not in sys.modules
assert models.default_model_registry_path() == Path.cwd() / "registry" / "model-identity.toml"
import ringer
assert ringer.ModelIdentity is models.ModelIdentity
assert ringer.aggregate_model_log_rows is models.aggregate_model_log_rows
assert ringer.default_model_registry_path is models.default_model_registry_path
assert ringer.StateWriter is state.StateWriter
assert ringer.RingerRunner is runner.RingerRunner
assert ringer.print_summary is runner.print_summary
for name in ("DELIVERABLE_MAX_BYTES", "FALLBACK_HARVEST_MAX_FILES", "FALLBACK_HARVEST_SUFFIXES", "SHEPHERD_MODEL", "VERIFY_METHOD"):
    assert getattr(ringer, name) is getattr(runner, name), name
assert ringer.SteeringRule is steering.SteeringRule
assert ringer.SteeringProfile is steering.SteeringProfile
assert ringer.inject_steering_spec is steering.inject_steering_spec
for name in ("STEERING_STATUSES", "STEERING_AUDIENCES", "STEERING_RULE_HEADING_RE"):
    assert getattr(ringer, name) is getattr(steering, name), name
assert ringer.TaskRuntime is runtime.TaskRuntime
assert ringer.WorkerResult is runtime.WorkerResult
assert ringer.ProcessTree is runtime.ProcessTree
assert ringer.RollingBytes is runtime.RollingBytes
assert ringer.AsyncFileCloser is runtime.AsyncFileCloser
assert ringer.verdict_for is runtime.verdict_for
assert ringer.build_run_id is runtime.build_run_id
assert ringer.parse_token_count is runtime.parse_token_count
assert ringer.parse_reported_model is runtime.parse_reported_model
assert ringer.effective_model_from_command is runtime.effective_model_from_command
assert ringer.effective_reasoning_effort_from_command is runtime.effective_reasoning_effort_from_command
assert ringer.resolved_task_model is runtime.resolved_task_model
assert ringer.build_worker_command is runtime.build_worker_command
assert ringer.shell_command_for_display is runtime.shell_command_for_display
assert ringer.ContextChunk is context.ContextChunk
assert ringer.build_context_packet is context.build_context_packet
assert ringer.AppConfig is config.AppConfig
assert ringer.load_steering_config is config.load_steering_config
assert ringer.TaskSpec is manifests.TaskSpec
assert ringer.Manifest is manifests.Manifest
assert ringer.Verifier is verification.Verifier
assert ringer.VerifyResult is verification.VerifyResult
assert ringer.terminate_process_group is verification.terminate_process_group
for name in ("CatalogRefreshResult", "default_catalog_path", "catalog_changes_path", "catalog_decimal", "catalog_decimal_or_none", "catalog_per_m", "catalog_per_m_decimal", "catalog_price_equal", "catalog_price_is_negative", "normalize_catalog_model", "normalize_catalog_payload", "catalog_sort_key", "fetch_catalog_payload", "load_catalog_snapshot", "catalog_event_model_details", "diff_catalog_snapshots", "append_catalog_events", "catalog_refresh_lock", "refresh_openrouter_catalog", "read_catalog_events", "catalog_snapshot_is_fresh", "start_catalog_auto_refresh", "catalog_model_is_text_candidate", "catalog_explore_candidates", "DEFAULT_CATALOG_SOURCE", "CATALOG_AUTO_REFRESH_MAX_AGE_S", "CATALOG_FETCH_TIMEOUT_S", "RESERVED_FIXTURE_MODELS"):
    assert getattr(ringer, name) is getattr(catalog, name), name
for name in ("ArtifactRenderer", "status_color", "fmt_duration", "fmt_datetime", "fmt_compact_duration", "fmt_plain_ago", "file_href", "is_html_artifact", "deliverable_title", "render_file_wrapper_html", "task_status_counts", "task_word", "passed_phrase", "failed_phrase", "running_phrase", "retry_phrase", "waiting_phrase", "live_briefing_sentence", "live_briefing_html", "final_briefing_sentence", "final_briefing_html", "join_plain_html_parts", "html_to_text", "plain_transition_line", "plain_transition_event", "first_check_output_line", "task_state_bucket", "task_state_word", "local_time_label", "render_progress_bar", "render_work_section", "render_work_group", "render_work_item", "work_item_href", "artifact_relative_href", "work_label_and_kind", "is_text_deliverable", "is_image_deliverable", "image_data_uri", "render_corner_header", "final_dot_bucket", "render_status_html", "render_final_report_html", "task_activity_line", "render_task_links", "render_artifact_index_html", "ARTIFACT_WRAPPER_TAIL_BYTES", "TASK_REPORT_FILENAMES", "TEXT_DELIVERABLE_SUFFIXES", "IMAGE_DELIVERABLE_SUFFIXES", "CSP_META_TAG", "STATUS_COLORS", "ARTIFACT_BASE_CSS"):
    assert getattr(ringer, name) is getattr(artifact_views, name), name
for name in ("atomic_write_text", "atomic_write_json", "ringer_home", "utc_now_iso", "active_runs_path", "pid_is_alive", "_read_active_runs_raw", "_prune_active_runs", "_write_active_runs", "read_active_runs", "register_active_run", "unregister_active_run", "scan_run_states", "read_json_object", "scan_hud_run_states", "read_active_runs_file"):
    assert getattr(ringer, name) is getattr(state_files, name), name
for name in ("tail_lines", "tail_file_text", "tail_text", "worker_activity", "last_shell_command_activity", "last_written_file_activity", "last_assistant_activity", "activity_fallback", "non_empty_log_lines", "clean_log_text", "extract_shell_command", "clean_command", "looks_like_shell_command", "extract_written_file", "normalize_activity_path", "looks_like_assistant_text", "build_failure_context", "shorten", "append_text", "ACTIVITY_TAIL_BYTES", "ACTIVITY_TEXT_LIMIT", "ANSI_RE", "CMD_JSON_DOUBLE_RE", "CMD_JSON_SINGLE_RE", "CMD_LABEL_RE", "CMD_RAN_RE", "CMD_PROMPT_RE", "PATCH_FILE_RE", "WRITE_QUOTED_FILE_RE", "WRITE_FILE_RE", "ASSISTANT_PREFIX_RE"):
    assert getattr(ringer, name) is getattr(worker_logs, name), name
for name in ("ARTIFACT_LIBRARY_MAX_VERSIONS", "artifacts_dir", "artifact_library_path", "artifact_live_path", "artifact_version_path", "artifact_deliverables_dir", "read_artifact_library", "write_artifact_library", "artifact_outcome_from_state", "_library_entry", "update_artifact_library_live", "append_artifact_library_version", "prune_artifact_versions", "reconcile_artifact_library_dead_runs", "sanitize_artifact_name", "state_tasks", "collect_state_deliverables"):
    assert getattr(ringer, name) is getattr(artifact_store, name), name
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_read_model_import_is_independent_and_cli_reexports_preserve_identity(self) -> None:
        script = r'''
import sys
import sqlite3 as sqlite_module
sqlite_module.connect = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("connected during import"))
import ringer_core.read_model as read_model
assert "ringer" not in sys.modules
assert "ringer.py" not in sys.modules
import ringer
for name in ("ReadModelSyncResult", "connect_read_model_db", "connect_read_model_db_readonly", "create_read_model_schema", "db_attempt_rows", "db_catalog_events", "db_catalog_models", "default_read_model_db_path", "drop_read_model_tables", "ensure_sqlite_available", "file_sync_metadata", "insert_attempt_rows", "insert_catalog_event_rows", "load_identity_registry_from_db", "read_catalog_events_from_offset", "read_log_rows_from_offset", "read_model_column_exists", "read_model_table_exists", "read_sync_state_int", "read_sync_state_value", "rebuild_read_model_db", "refresh_catalog_tables", "refresh_identity_tables", "should_use_read_model_db", "sync_read_model_db", "write_sync_state_values", "sqlite3"):
    assert getattr(ringer, name) is getattr(read_model, name), name
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_model_api_and_views_import_without_cli_or_runtime_io_and_keep_reexport_identity(self) -> None:
        script = r"""
import sys, builtins, subprocess
from pathlib import Path
original_open, original_path_open = builtins.open, Path.open
original_run, original_popen = subprocess.run, subprocess.Popen
fail = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("import caused I/O or process execution"))
builtins.open = Path.open = subprocess.run = subprocess.Popen = fail
import ringer_core.models_api as api
import ringer_core.model_views as views
builtins.open, Path.open = original_open, original_path_open
subprocess.run, subprocess.Popen = original_run, original_popen
assert "ringer" not in sys.modules
import ringer
assert ringer.build_models_api_payload is api.build_models_api_payload
assert ringer.MODEL_SCOREBOARD_COLUMNS is api.MODEL_SCOREBOARD_COLUMNS
for name in ("MODEL_SCOREBOARD_IDENTITY", "MODEL_SCOREBOARD_CSS", "normalized_judgment_note", "fmt_scoreboard_duration", "render_model_scoreboard_html", "write_model_scoreboard_html"):
    assert getattr(ringer, name) is getattr(views, name), name
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_unsupported_python_guard_runs_before_core_imports(self) -> None:
        script = r'''
import sys
sys.version_info = (3, 11)
sys.version = "3.11.9"
try:
    import ringer
except SystemExit as exc:
    assert str(exc) == f"ringer requires Python 3.12+; found 3.11.9 at {sys.executable}"
else:
    raise AssertionError("unsupported Python version did not exit")
assert "ringer_core" not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_presentation_resources_are_repository_rooted_from_other_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            script = "from pathlib import Path; import ringer_core.presentation as p; root=Path(%r); assert p.DASHBOARD_HTML_PATH == root/'dashboard'/'dashboard.html'; assert p.RINGSIDE_HTML_PATH == root/'dashboard'/'ringside.html'" % str(ROOT)
            result = subprocess.run([sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_presentation_import_is_inert_and_cli_reexports_keep_identity(self) -> None:
        script = r'''
import socket, threading, webbrowser, sqlite3, http.server
fail = lambda *a, **k: (_ for _ in ()).throw(AssertionError("presentation import caused side effect"))
socket.socket = fail
threading.Thread.start = fail
webbrowser.open = fail
sqlite3.connect = fail
import ringer_core.presentation as p
assert p.Dashboard
assert "ringer" not in __import__("sys").modules
import ringer
for name in ("artifact_content_type", "inject_models_tab_into_ringside_html", "read_ringside_html", "send_response_body", "send_json_response", "resolve_artifact_http_path", "task_log_path_from_state", "run_state_path_for_id", "hud_task_log_path", "serve_artifact_path", "ReusableThreadingHTTPServer", "PersistentHudServer", "Dashboard", "read_dashboard_html", "open_in_browser", "DASHBOARD_HTML_PATH", "RINGSIDE_HTML_PATH", "MINIMAL_DASHBOARD_HTML", "WORKER_LOG_TAIL_BYTES"):
    assert getattr(ringer, name) is getattr(p, name), name
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_read_model_optional_sqlite_fallback_is_preserved(self) -> None:
        script = r'''
import sys
sys.modules["sqlite3"] = None
import ringer_core.read_model as read_model
assert read_model.sqlite3 is None
try:
    read_model.ensure_sqlite_available()
except RuntimeError as exc:
    assert str(exc) == "sqlite3 is unavailable"
else:
    raise AssertionError("expected sqlite fallback error")
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
