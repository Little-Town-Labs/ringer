from __future__ import annotations
import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from .manifests import TaskSpec

CHECK_TIMEOUT_S = 60
@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    check_returncode: int | None
    check_timed_out: bool
    raw_output_excerpt: str
    missing_files: tuple[str, ...] = ()
class Verifier:
    async def verify(self, task: TaskSpec, taskdir: Path) -> VerifyResult:
        check_returncode, check_timed_out, output = await self._run_check(task.check, taskdir)
        missing_files = tuple(
            rel for rel in task.expect_files if not self._is_nonempty_file(self._expect_file_path(taskdir, rel))
        )
        ok = not missing_files and not check_timed_out and check_returncode == 0
        if missing_files:
            missing_message = f"[ringer] missing expected files: {', '.join(missing_files)}"
            output = f"{missing_message}\n{output}" if output.strip() else missing_message
        elif not check_timed_out and check_returncode != 0 and not output.strip():
            # A silent failing check wastes the retry (no failure context to
            # inject) and blinds the eval row. Say so, in both places.
            output = (
                f"[ringer] check failed silently (exit {check_returncode}, no output). "
                "Prefer checks that print WHY they fail — the retry prompt and the "
                "eval log both depend on it."
            )
        return VerifyResult(
            ok=ok,
            check_returncode=check_returncode,
            check_timed_out=check_timed_out,
            raw_output_excerpt=output[:2000],
            missing_files=missing_files,
        )

    @staticmethod
    def _is_nonempty_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _expect_file_path(taskdir: Path, path: str) -> Path:
        candidate = Path(path).expanduser()
        # Keep runtime verification aligned with lint's treatment of "~" paths.
        return candidate if candidate.is_absolute() else taskdir / candidate

    @staticmethod
    async def _run_check(command: str, cwd: Path) -> tuple[int | None, bool, str]:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            timed_out = True
            terminate_process_group(proc)
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                kill_process_group(proc)
                stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        if timed_out:
            output += f"\n[ringer.py] check timed out after {CHECK_TIMEOUT_S}s\n"
        return proc.returncode, timed_out, output
def terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
