from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


ACTIVITY_TAIL_BYTES = 2048


ACTIVITY_TEXT_LIMIT = 80


def tail_lines(path: Path, line_count: int) -> list[str]:
    if line_count <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-line_count:]


def tail_file_text(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0 or not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def tail_text(path: Path, max_bytes: int = 6000, line_count: int = 40) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-line_count:])


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


CMD_JSON_DOUBLE_RE = re.compile(
    r'"(?:cmd|command)"\s*:\s*"(?P<cmd>(?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)


CMD_JSON_SINGLE_RE = re.compile(
    r"'(?:cmd|command)'\s*:\s*'(?P<cmd>(?:\\.|[^'\\])*)'",
    re.IGNORECASE,
)


CMD_LABEL_RE = re.compile(
    r"\b(?:exec(?:_command|/command)?|shell command|command)\b\s*[:=]\s*(?P<cmd>.+)$",
    re.IGNORECASE,
)


CMD_RAN_RE = re.compile(r"^\s*(?:[*>-]\s*)?(?:ran|running)\s+`?(?P<cmd>.+?)`?\s*$", re.IGNORECASE)


CMD_PROMPT_RE = re.compile(r"^\s*(?:\$|\+)\s+(?P<cmd>.+)$")


PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update)\s+File:\s+(?P<path>.+)$", re.IGNORECASE)


WRITE_QUOTED_FILE_RE = re.compile(
    r"\b(?:created|modified|updated|wrote|writing|saved|edited|patched)\b[^`'\"]{0,48}"
    r"[`'\"](?P<path>[^`'\"]+)[`'\"]",
    re.IGNORECASE,
)


WRITE_FILE_RE = re.compile(
    r"\b(?:created|modified|updated|wrote|writing|saved|edited|patched)\s+"
    r"(?:file\s+)?(?P<path>[A-Za-z0-9_./~:-]+\.[A-Za-z0-9][A-Za-z0-9_+-]*)",
    re.IGNORECASE,
)


ASSISTANT_PREFIX_RE = re.compile(
    r"^\s*(?:assistant|codex(?:-[A-Za-z0-9_-]+)?|agent)\s*(?:>|:|-)\s*(?P<text>.+)$",
    re.IGNORECASE,
)


def worker_activity(path: Path, log_tail: list[str]) -> str:
    text = tail_text(path, max_bytes=ACTIVITY_TAIL_BYTES, line_count=80)
    if text:
        for finder in (last_shell_command_activity, last_written_file_activity, last_assistant_activity):
            activity = finder(text)
            if activity:
                return activity
    return activity_fallback(log_tail)


def last_shell_command_activity(text: str) -> str:
    for line in reversed(non_empty_log_lines(text)):
        command = extract_shell_command(line)
        if command:
            return f"ran: {shorten(command, ACTIVITY_TEXT_LIMIT)}"
    return ""


def last_written_file_activity(text: str) -> str:
    for line in reversed(non_empty_log_lines(text)):
        path = extract_written_file(line)
        if path:
            return f"wrote {shorten(path, ACTIVITY_TEXT_LIMIT)}"
    return ""


def last_assistant_activity(text: str) -> str:
    lines = list(reversed(non_empty_log_lines(text)))
    for line in lines:
        match = ASSISTANT_PREFIX_RE.match(line)
        if match:
            candidate = clean_log_text(match.group("text"))
            if candidate:
                return shorten(candidate, ACTIVITY_TEXT_LIMIT)
    for line in lines:
        if looks_like_assistant_text(line):
            return shorten(clean_log_text(line), ACTIVITY_TEXT_LIMIT)
    return ""


def activity_fallback(log_tail: list[str]) -> str:
    for line in reversed(log_tail):
        candidate = clean_log_text(line)
        if candidate:
            return shorten(candidate, ACTIVITY_TEXT_LIMIT)
    return ""


def non_empty_log_lines(text: str) -> list[str]:
    return [line for line in (clean_log_text(raw) for raw in text.splitlines()) if line]


def clean_log_text(value: str) -> str:
    value = ANSI_RE.sub("", value)
    return " ".join(value.strip().split())


def extract_shell_command(line: str) -> str:
    if line.startswith("[ringer.py]"):
        return ""
    for pattern in (CMD_JSON_DOUBLE_RE, CMD_JSON_SINGLE_RE, CMD_LABEL_RE, CMD_RAN_RE, CMD_PROMPT_RE):
        match = pattern.search(line)
        if not match:
            continue
        command = clean_command(match.group("cmd"))
        if command and looks_like_shell_command(command):
            return command
    return ""


def clean_command(value: str) -> str:
    command = value.strip().strip("`")
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"'", '"'}:
        command = command[1:-1]
    command = command.replace(r"\n", " ").replace(r"\t", " ")
    command = command.replace(r"\"", '"').replace(r"\'", "'")
    command = re.split(r"\s+<\s*/dev/null\b", command, maxsplit=1)[0]
    return clean_log_text(command).strip(" ,")


def looks_like_shell_command(command: str) -> bool:
    if not command or command.startswith(("{", "[", "(", "<")):
        return False
    lower = command.lower()
    if lower.startswith(("error ", "unknown ", "none ", "failed ")):
        return False
    if lower.startswith("codex exec "):
        return False
    try:
        first = shlex.split(command)[0]
    except ValueError:
        first = command.split(maxsplit=1)[0]
    return bool(re.match(r"^(?:[A-Za-z0-9_./-]+)(?:\.[A-Za-z0-9_+-]+)?$", first))


def extract_written_file(line: str) -> str:
    if line.startswith("[ringer.py]"):
        return ""
    for pattern in (PATCH_FILE_RE, WRITE_QUOTED_FILE_RE, WRITE_FILE_RE):
        match = pattern.search(line)
        if not match:
            continue
        path = normalize_activity_path(match.group("path"))
        if path:
            return path
    return ""


def normalize_activity_path(value: str) -> str:
    path = value.strip().strip("`'\".,;:)")
    path = re.sub(r":\d+(?::\d+)?$", "", path)
    if not re.search(r"\.[A-Za-z0-9][A-Za-z0-9_+-]*(?:$|[?#])", path):
        return ""
    if path.startswith("/"):
        return Path(path).name
    return path


def looks_like_assistant_text(line: str) -> bool:
    lower = line.lower()
    if not line or line.startswith(("[", "{", "}", "```", "***", "@@", "$", "+")):
        return False
    if re.match(r"^(?:exec|command|stdout|stderr|chunk id|wall time|process exited)\b", lower):
        return False
    if re.match(r"^(?:error|warning|info|debug)[:\s]", lower):
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_./-]*:\d+(?::\d+)?:", line):
        return False
    return bool(re.search(r"[A-Za-z]", line))


def build_failure_context(log_path: Path, raw_check_output: str) -> str:
    worker_tail = tail_text(log_path)
    context = f"{worker_tail}\n{raw_check_output}".strip()
    if len(context) > 6000:
        return context[-6000:]
    return context


def shorten(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
