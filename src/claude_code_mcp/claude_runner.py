from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from claude_code_mcp.claude_args import build_claude_argv, build_child_env
from claude_code_mcp.claude_stream import ClaudeStreamParser


@dataclass
class ClaudeRun:
    run_id: str
    workspace_path: str
    argv: list[str]
    env: dict[str, str]
    started_at: datetime
    proc: subprocess.Popen
    session_id: str | None = None  # async only
    parser: ClaudeStreamParser | None = None  # async only; None for sync


def start_sync_run(
    *, claude_path: str, workspace_path: str, request: Any, settings: Any
) -> ClaudeRun:
    """Build argv + env, Popen the process, return ClaudeRun.

    Uses subprocess.Popen with shell=False, start_new_session=True,
    stdin=PIPE (closed immediately), stdout=PIPE (text mode, bufsize=1),
    stderr=PIPE. For sync mode, no parser is attached.
    """
    import uuid

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    argv = build_claude_argv(
        claude_path=claude_path,
        workspace_path=workspace_path,
        request=request,
        mode="sync",
        settings=settings,
    )
    env = build_child_env(request.options.env, settings)

    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"CLAUDE_NOT_FOUND: the claude binary at {claude_path!r} was not found: {e}"
        ) from e

    if proc.stdin:
        try:
            proc.stdin.write(request.prompt)
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            proc.stdin = None

    return ClaudeRun(
        run_id=run_id,
        workspace_path=workspace_path,
        argv=argv,
        env=env,
        started_at=started_at,
        proc=proc,
        session_id=None,
        parser=None,
    )


def start_async_run(
    *, claude_path: str, workspace_path: str, request: Any, settings: Any, session_id: str
) -> ClaudeRun:
    """Like start_sync_run but for async mode: attaches a ClaudeStreamParser
    and spawns a daemon thread that reads stdout line-by-line into the parser.
    """
    import uuid

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    argv = build_claude_argv(
        claude_path=claude_path,
        workspace_path=workspace_path,
        request=request,
        mode="async",
        session_id=session_id,
        settings=settings,
    )
    env = build_child_env(request.options.env, settings)

    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"CLAUDE_NOT_FOUND: the claude binary at {claude_path!r} was not found: {e}"
        ) from e

    if proc.stdin:
        try:
            proc.stdin.write(request.prompt)
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            proc.stdin = None

    parser = ClaudeStreamParser()

    def _reader_thread(stdout_stream: Any, parser_inst: ClaudeStreamParser) -> None:
        try:
            for line in stdout_stream:
                parser_inst.feed_line(line)
        except Exception:
            pass
        finally:
            try:
                stdout_stream.close()
            except Exception:
                pass

    t = threading.Thread(target=_reader_thread, args=(proc.stdout, parser), daemon=True)
    t.start()

    return ClaudeRun(
        run_id=run_id,
        workspace_path=workspace_path,
        argv=argv,
        env=env,
        started_at=started_at,
        proc=proc,
        session_id=session_id,
        parser=parser,
    )


def terminate_run(run: ClaudeRun, *, force: bool) -> None:
    """SIGTERM (or SIGKILL if force=True) the process group.
    Fallback to proc.terminate()/proc.kill() if getpgid fails.
    """
    proc = run.proc
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
    except Exception:
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass


def wait_sync(
    run: ClaudeRun, *, timeout_s: int, settings: Any
) -> tuple[int | None, bool, str, str]:
    """Wait for the sync process. Returns (exit_code, timed_out, stdout, stderr).

    Reads remaining stdout/stderr after wait. Truncates each to
    ``settings.max_stdout_bytes`` / ``settings.max_stderr_bytes`` and appends
    a "[truncated]" warning note to stderr if hit.
    """
    proc = run.proc
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code: int | None = None

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (
            e.stdout
            if isinstance(e.stdout, str)
            else (e.stdout or b"").decode("utf-8", errors="replace")
        )
        stderr = (
            e.stderr
            if isinstance(e.stderr, str)
            else (e.stderr or b"").decode("utf-8", errors="replace")
        )
        terminate_run(run, force=True)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        exit_code = proc.returncode
    except Exception:
        exit_code = proc.returncode

    if stdout is None:
        stdout = ""
    if stderr is None:
        stderr = ""

    stdout_bytes = stdout.encode("utf-8", errors="replace")
    stderr_bytes = stderr.encode("utf-8", errors="replace")

    truncated_stdout = len(stdout_bytes) > settings.max_stdout_bytes
    truncated_stderr = len(stderr_bytes) > settings.max_stderr_bytes

    if truncated_stdout:
        stdout = stdout_bytes[: settings.max_stdout_bytes].decode("utf-8", errors="replace")
    if truncated_stderr:
        stderr = stderr_bytes[: settings.max_stderr_bytes].decode("utf-8", errors="replace")

    if truncated_stdout or truncated_stderr:
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        stderr += "[truncated]"

    return exit_code, timed_out, stdout, stderr
