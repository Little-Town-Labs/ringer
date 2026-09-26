from __future__ import annotations
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_NAME = "ringer"
STATE_DIR_NAME = ".ringer"
ENV_VAR_PREFIX = "RINGER"
CONFIG_DIR_NAME = TOOL_NAME
CONFIG_FILE_NAME = "config.toml"
DEFAULT_ENGINE_NAME = "codex"
DEFAULT_DASHBOARD_PORT_BASE = 8787
DEFAULT_HUD_PORT = 8700
DEFAULT_UPDATE_CHECK_INTERVAL_S = 3600
DEFAULT_TOKEN_REGEX = r"tokens\s+used\s*:?\s*([0-9][0-9,]*)"
DEFAULT_CODEX_MODEL_REPORT_REGEX = r"(?m)^model:[ \t]*([^ \t\r\n]+)[ \t]*\r?$"
@dataclass(frozen=True)
class EngineConfig:
    name: str
    bin: str
    args_template: tuple[str, ...]
    full_access_args: tuple[str, ...]
    sandbox_args: tuple[str, ...]
    token_regex: str | None = DEFAULT_TOKEN_REGEX
    model_report_regex: str | None = None
    # Fills the {model} placeholder in args_template when a task does not set
    # its own "model" — this is what makes a harness engine (OpenCode) model
    # agnostic instead of hard-coding one model into the command line.
    model_default: str = ""

    @property
    def process_name(self) -> str:
        return Path(self.bin).name or self.name
@dataclass(frozen=True)
class PostgresEvalConfig:
    env_file: Path
@dataclass(frozen=True)
class EvalConfig:
    backend: str
    jsonl_path: Path
    postgres: PostgresEvalConfig | None = None
@dataclass(frozen=True)
class ArtifactConfig:
    """Tier 0 zero-LLM HTML artifacts: live status page + final report + multi-run index.

    See ringer-live-artifacts-plan.md. Templates support {run_id}, {run_name} substitutions.
    """

    enabled: bool
    out_template: str
    report_template: str
    index_out: Path

    def artifact_path(self, run_id: str, run_name: str) -> Path:
        return Path(format_artifact_template(self.out_template, run_id, run_name))

    def report_path(self, run_id: str, run_name: str) -> Path:
        return Path(format_artifact_template(self.report_template, run_id, run_name))
@dataclass(frozen=True)
class SteeringConfig:
    dir: Path | None = None
    inject_candidates: bool = True
@dataclass(frozen=True)
class UpdateConfig:
    auto: bool = True
    check_interval_s: int = DEFAULT_UPDATE_CHECK_INTERVAL_S
def load_steering_config(raw: Any) -> SteeringConfig:
    """Load optional steering settings without allowing them to break config load."""
    try:
        section = raw if isinstance(raw, dict) else {}
        env_dir = os.environ.get(f"{ENV_VAR_PREFIX}_STEERING_DIR")
        directory = optional_path(env_dir) if env_dir and env_dir.strip() else optional_path(
            section.get("dir")
        )
        return SteeringConfig(
            dir=directory,
            inject_candidates=bool(section.get("inject_candidates", True)),
        )
    except Exception:
        return SteeringConfig()
def format_artifact_template(template: str, run_id: str, run_name: str) -> str:
    text = template.replace("{run_id}", run_id).replace("{run_name}", run_name)
    return str(Path(text).expanduser())
def load_artifact_config(raw: Any, state_dir: Path) -> ArtifactConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("artifact must be a TOML table")
    default_dir = state_dir / "artifacts"
    enabled = bool(raw.get("enabled", True))
    out_template = str(raw.get("out", str(default_dir / "{run_id}.html")))
    report_template = str(raw.get("report_out", str(default_dir / "{run_id}-report.html")))
    index_out = expand_path(raw.get("index_out"), default_dir / "index.html")
    return ArtifactConfig(
        enabled=enabled,
        out_template=out_template,
        report_template=report_template,
        index_out=index_out,
    )
@dataclass(frozen=True)
class AppConfig:
    path: Path | None
    identity_default: str | None
    state_dir: Path
    dashboard_port_base: int
    hud_port: int
    hud_app_path: Path | None
    allow_full_access: bool
    eval: EvalConfig
    engines: dict[str, EngineConfig]
    artifact: ArtifactConfig
    steering: SteeringConfig = field(default_factory=SteeringConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or env_config_path() or default_config_path()
        explicit = path is not None or env_config_path() is not None
        data: dict[str, Any] = {}
        if config_path.exists():
            with config_path.open("rb") as fh:
                loaded = tomllib.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a TOML table")
            data = loaded
        elif explicit:
            raise ValueError(f"config file not found: {config_path}")

        state_dir = expand_path(data.get("state_dir"), default_state_dir())
        dashboard_port_base = int(data.get("dashboard_port_base", DEFAULT_DASHBOARD_PORT_BASE))
        if dashboard_port_base <= 0:
            raise ValueError("dashboard_port_base must be positive")
        hud_port = load_hud_port(data.get("hud"))
        identity_default = optional_string(data.get("identity_default"))
        hud_app_path = optional_path(data.get("hud_app_path"))
        allow_full_access = bool(data.get("allow_full_access", False))
        eval_config = load_eval_config(data.get("eval"), state_dir)
        engines = load_engines(data.get("engines"))
        artifact_config = load_artifact_config(data.get("artifact"), state_dir)
        update_config = load_update_config(data.get("update"))
        try:
            steering_config = load_steering_config(data.get("steering"))
        except Exception:
            # Steering is optional and must never make the base config unusable,
            # including when its loader itself is replaced or extended later.
            steering_config = SteeringConfig()
        return cls(
            path=config_path if config_path.exists() else None,
            identity_default=identity_default,
            state_dir=state_dir,
            dashboard_port_base=dashboard_port_base,
            hud_port=hud_port,
            hud_app_path=hud_app_path,
            allow_full_access=allow_full_access,
            eval=eval_config,
            engines=engines,
            artifact=artifact_config,
            steering=steering_config,
            update=update_config,
        )
def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(base).expanduser() if base else Path.home() / ".config"
    return config_home / CONFIG_DIR_NAME / CONFIG_FILE_NAME
def env_config_path() -> Path | None:
    value = os.environ.get(f"{ENV_VAR_PREFIX}_CONFIG")
    if not value or not value.strip():
        return None
    return Path(value).expanduser().resolve()
def default_state_dir() -> Path:
    return Path.home() / STATE_DIR_NAME
def load_update_config(raw: Any) -> UpdateConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("update must be a TOML table")
    interval = int(raw.get("check_interval_s", DEFAULT_UPDATE_CHECK_INTERVAL_S))
    if interval <= 0:
        raise ValueError("update.check_interval_s must be positive")
    return UpdateConfig(auto=bool(raw.get("auto", True)), check_interval_s=interval)
def expand_path(value: Any, default: Path) -> Path:
    if value is None:
        return default.expanduser().resolve()
    return Path(str(value)).expanduser().resolve()
def optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()
def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
def as_string_tuple(value: Any, *, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return tuple(str(item) for item in value)
def built_in_codex_engine() -> EngineConfig:
    resolved = shutil.which(DEFAULT_ENGINE_NAME) or DEFAULT_ENGINE_NAME
    return EngineConfig(
        name=DEFAULT_ENGINE_NAME,
        bin=resolved,
        args_template=(
            "exec",
            "--skip-git-repo-check",
            "{access_args}",
            "{model_args}",
            "{engine_args}",
            "-C",
            "{taskdir}",
            "{spec}",
        ),
        full_access_args=("--dangerously-bypass-approvals-and-sandbox",),
        sandbox_args=("--sandbox", "workspace-write"),
        token_regex=DEFAULT_TOKEN_REGEX,
        model_report_regex=DEFAULT_CODEX_MODEL_REPORT_REGEX,
    )
def load_eval_config(raw: Any, state_dir: Path) -> EvalConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("eval must be a TOML table")
    backend = str(raw.get("backend", "jsonl")).strip().lower()
    if backend not in {"jsonl", "postgres"}:
        raise ValueError("eval.backend must be 'jsonl' or 'postgres'")
    jsonl_path = expand_path(raw.get("jsonl_path"), state_dir / "runs.jsonl")
    postgres: PostgresEvalConfig | None = None
    postgres_raw = raw.get("postgres")
    if postgres_raw is not None:
        if not isinstance(postgres_raw, dict):
            raise ValueError("eval.postgres must be a TOML table")
        env_file_raw = optional_string(postgres_raw.get("env_file"))
        if env_file_raw is None:
            raise ValueError("eval.postgres.env_file is required")
        env_file = Path(env_file_raw).expanduser().resolve()
        postgres = PostgresEvalConfig(env_file=env_file)
    if backend == "postgres" and postgres is None:
        raise ValueError("eval.backend='postgres' requires [eval.postgres].env_file")
    return EvalConfig(backend=backend, jsonl_path=jsonl_path, postgres=postgres)
def load_hud_port(raw: Any) -> int:
    if raw is None:
        return DEFAULT_HUD_PORT
    if not isinstance(raw, dict):
        raise ValueError("hud must be a TOML table")
    port = int(raw.get("port", DEFAULT_HUD_PORT))
    if port <= 0:
        raise ValueError("hud.port must be positive")
    return port
def load_engines(raw: Any) -> dict[str, EngineConfig]:
    engines: dict[str, EngineConfig] = {DEFAULT_ENGINE_NAME: built_in_codex_engine()}
    if raw is None:
        return engines
    if not isinstance(raw, dict):
        raise ValueError("engines must be a TOML table")
    for name, section in raw.items():
        if not isinstance(section, dict):
            raise ValueError(f"engines.{name} must be a TOML table")
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("engine name must not be empty")
        base = engines.get(clean_name)
        default_bin = base.bin if base else clean_name
        bin_path = str(section.get("bin", default_bin)).strip()
        if not bin_path:
            raise ValueError(f"engines.{clean_name}.bin must not be empty")
        args_template = as_string_tuple(
            section.get("args_template", list(base.args_template) if base else None),
            key=f"engines.{clean_name}.args_template",
        )
        if not args_template:
            raise ValueError(f"engines.{clean_name}.args_template must not be empty")
        full_access_args = as_string_tuple(
            section.get("full_access_args", list(base.full_access_args) if base else []),
            key=f"engines.{clean_name}.full_access_args",
        )
        sandbox_args = as_string_tuple(
            section.get("sandbox_args", list(base.sandbox_args) if base else []),
            key=f"engines.{clean_name}.sandbox_args",
        )
        token_regex = optional_string(section.get("token_regex"))
        if token_regex is None and base is not None:
            token_regex = base.token_regex
        if token_regex:
            try:
                re.compile(token_regex, flags=re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"engines.{clean_name}.token_regex is invalid: {exc}") from exc
        model_report_regex = optional_string(section.get("model_report_regex"))
        if model_report_regex is None and base is not None:
            model_report_regex = base.model_report_regex
        if model_report_regex:
            try:
                compiled_report = re.compile(model_report_regex, flags=re.IGNORECASE)
            except re.error as exc:
                raise ValueError(
                    f"engines.{clean_name}.model_report_regex is invalid: {exc}"
                ) from exc
            if compiled_report.groups < 1:
                raise ValueError(
                    f"engines.{clean_name}.model_report_regex must have a capture group"
                )
        model_default = str(
            section.get("model_default", base.model_default if base else "")
        ).strip()
        engines[clean_name] = EngineConfig(
            name=clean_name,
            bin=bin_path,
            args_template=args_template,
            full_access_args=full_access_args,
            sandbox_args=sandbox_args,
            token_regex=token_regex,
            model_report_regex=model_report_regex,
            model_default=model_default,
        )
    return engines
