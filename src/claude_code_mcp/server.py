from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP

from claude_code_mcp.changes import (
    diff_snapshots, git_changed_files, git_diff, is_git_repo, snapshot_tree,
)
from claude_code_mcp.claude_args import build_claude_argv, build_child_env
from claude_code_mcp.claude_runner import (
    ClaudeRun, start_async_run, start_sync_run, terminate_run, wait_sync,
)
from claude_code_mcp.claude_stream import ClaudeStreamParser, RollingLineBuffer
from claude_code_mcp.logfire_setup import setup_logfire
from claude_code_mcp.models import (
    ClaudeAppendPersistenceRequestIn, ClaudeAppendPersistenceResponse,
    ClaudeCancelTaskRequest, ClaudeCancelTaskRequestIn, ClaudeCancelTaskResponse,
    ClaudeHealthRequest, ClaudeHealthRequestIn, ClaudeHealthResponse,
    ClaudeInitPersistenceRequestIn, ClaudeInitPersistenceResponse,
    ClaudeListRunsRequest, ClaudeListRunsRequestIn, ClaudeListRunsResponse,
    ClaudeLoadPersistenceContextRequestIn, ClaudeLoadPersistenceContextResponse,
    ClaudePollTaskRequest, ClaudePollTaskRequestIn, ClaudePollTaskResponse,
    ClaudeReadPersistenceRequestIn, ClaudeReadPersistenceResponse,
    ClaudeRunResult, ClaudeRunSummary, ClaudeRunTaskRequest,
    ClaudeRunTaskRequestIn, ClaudeRunTaskResponse,
    ClaudeStartTaskRequest, ClaudeStartTaskRequestIn, ClaudeStartTaskResponse,
    ClaudeUpdatePersistenceRequestIn, ClaudeUpdatePersistenceResponse,
    WorkspaceChanges,
)
from claude_code_mcp.persistence import PersistenceStore
from claude_code_mcp.provider import PROVIDER_PREFIX, prompt_name, tool_name
from claude_code_mcp.rolling_buffer import RollingTextBuffer
from claude_code_mcp.run_store import RunStore, StoredRun
from claude_code_mcp.settings import Settings

mcp = FastMCP(
    "claude-code-cli-mcp",
    instructions="Exposes tools to run Claude Code CLI (claude) in a controlled workspace.",
)

_settings = Settings()
_run_store = RunStore(max_runs=_settings.max_runs)
_active_runs_lock = threading.Lock()
_active_runs: dict[str, "ActiveRun"] = {}
_persistence_store = PersistenceStore(
    base_dir=_settings.persistence_base_dir,
    max_file_bytes=_settings.persistence_max_file_bytes,
    backup_on_write=_settings.persistence_backup_on_write,
    seed_templates=_settings.persistence_seed_templates,
)
setup_logfire(token=_settings.logfire_token)


# Monkeypatch ClaudeStreamParser to capture stdout chunks
_orig_parser_init = ClaudeStreamParser.__init__
_orig_feed_line = ClaudeStreamParser.feed_line

def _custom_parser_init(self, *args, **kwargs):
    _orig_parser_init(self, *args, **kwargs)
    self._stdout_buf = RollingTextBuffer(max_bytes=_settings.max_stdout_bytes)

def _custom_feed_line(self, line: str):
    if hasattr(self, "_stdout_buf"):
        self._stdout_buf.append(line)
    return _orig_feed_line(self, line)

ClaudeStreamParser.__init__ = _custom_parser_init
ClaudeStreamParser.feed_line = _custom_feed_line


@dataclass
class ActiveRun:
    run_id: str
    session_id: str
    workspace: Path
    request: ClaudeRunTaskRequest
    started_at: datetime
    run: ClaudeRun
    parser: ClaudeStreamParser
    stdout_buf: RollingTextBuffer
    stderr_buf: RollingTextBuffer
    before_snapshot: dict[str, Any] | None
    cancel_requested: bool = False
    last_polled_msg_count: int = 0


# Helpers (private functions)

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_claude_path() -> str:
    path = _settings.resolve_claude_path()
    import shutil
    expanded = str(Path(path).expanduser())
    resolved = shutil.which(expanded)
    if resolved is None:
        raise RuntimeError(f"CLAUDE_NOT_FOUND: no claude binary found (path: {path})")
    return resolved


def _resolve_workspace_path(p: str) -> Path:
    resolved_path = Path(p).expanduser().resolve()
    if not resolved_path.exists() or not resolved_path.is_dir():
        raise ValueError("INVALID_WORKSPACE: workspace_path must be an existing directory")

    allowed_roots = _settings.resolved_allowed_roots()
    if not any(str(resolved_path).startswith(str(root) + "/") or resolved_path == root for root in allowed_roots):
        raise ValueError("NOT_ALLOWED: workspace_path is outside allowed roots")

    return resolved_path


def _validate_exec_options(req: Any) -> None:
    if req.options.extra_args is not None:
        for arg in req.options.extra_args:
            if not isinstance(arg, str):
                raise ValueError(f"NOT_ALLOWED: extra_args must contain only strings, got {type(arg).__name__}")
    
    if req.options.env is not None:
        for k, v in req.options.env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(f"NOT_ALLOWED: env keys and values must be strings, got {type(k).__name__}/{type(v).__name__}")

    if _settings.mode == "safe":
        if _settings.force_sandbox_in_safe_mode and not req.options.sandbox:
            raise ValueError("NOT_ALLOWED: sandbox must be enabled in safe mode")
        if req.options.dangerously_skip_permissions:
            raise ValueError("NOT_ALLOWED: dangerously_skip_permissions is not allowed in safe mode")
        if req.options.env:
            raise ValueError("NOT_ALLOWED: custom env is not allowed in safe mode")
        if req.options.extra_args:
            raise ValueError("NOT_ALLOWED: extra_args is not allowed in safe mode")

    if req.options.extra_args:
        unknown = [a for a in req.options.extra_args if a not in _settings.allow_extra_args]
        if unknown:
            raise ValueError("NOT_ALLOWED: extra_args contains disallowed entries")

    if req.options.env:
        unknown = [k for k in req.options.env.keys() if k not in _settings.allow_env_keys]
        if unknown:
            raise ValueError("NOT_ALLOWED: env contains disallowed keys")


def _build_prompt_with_context(prompt_text: str, settings: Any, persistence_store: Any) -> str:
    if settings.persistence_enabled and persistence_store.is_initialized:
        try:
            ctx = persistence_store.load_context()
            header_parts = []
            if ctx.agents_excerpt:
                header_parts.append(
                    "<persistent-agents-context>\n"
                    f"{ctx.agents_excerpt}\n"
                    "</persistent-agents-context>"
                )
            if ctx.projects_excerpt:
                header_parts.append(
                    "<persistent-projects-context>\n"
                    f"{ctx.projects_excerpt}\n"
                    "</persistent-projects-context>"
                )
            if ctx.memory_excerpt:
                header_parts.append(
                    "<persistent-memory-context>\n"
                    f"{ctx.memory_excerpt}\n"
                    "</persistent-memory-context>"
                )
            if header_parts:
                return "\n\n".join(header_parts) + "\n\n" + prompt_text
        except Exception:
            pass
    return prompt_text


def _compute_changes(workspace: Path, request: Any, before_snapshot: dict[str, Any] | None) -> WorkspaceChanges | None:
    if not request.capture_changes:
        return None
    if is_git_repo(workspace):
        diff = git_diff(workspace)
        return WorkspaceChanges(
            method="git",
            changed_files=git_changed_files(workspace),
            diff=diff if diff else None,
        )
    else:
        if before_snapshot is None:
            return WorkspaceChanges(method="none", changed_files=[], diff=None)
        else:
            after_snapshot = snapshot_tree(
                workspace,
                ignore_dir_names=_settings.ignore_dir_names,
                max_file_bytes=_settings.snapshot_max_file_bytes,
            )
            return WorkspaceChanges(
                method="snapshot",
                changed_files=diff_snapshots(before_snapshot, after_snapshot),
                diff=None,
            )


def _auth_status_text() -> str | None:
    try:
        claude_path = _resolve_claude_path()
        return subprocess.check_output([claude_path, "auth", "status", "--text"], text=True).strip()
    except Exception:
        return None


def _finalize_active_run(active: ActiveRun) -> None:
    def _read_stderr(pipe: Any, buf: RollingTextBuffer) -> None:
        try:
            for line in iter(pipe.readline, ""):
                buf.append(line)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    err_t = threading.Thread(
        target=_read_stderr,
        args=(active.run.proc.stderr, active.stderr_buf),
        daemon=True
    )
    err_t.start()

    # Wait for the process to finish
    try:
        timeout_s = max(1, active.request.options.timeout_s or _settings.default_timeout_s)
        active.run.proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        pass

    terminate_run(active.run, force=True)

    try:
        active.run.proc.wait(timeout=5)
    except Exception:
        pass

    if hasattr(active.parser, "_stdout_thread") and active.parser._stdout_thread:
        active.parser._stdout_thread.join(timeout=5)
    err_t.join(timeout=5)

    exit_code = active.run.proc.returncode

    # Extract final results from the parser
    res = active.parser.result_event or {}
    session_id = res.get("session_id")
    total_cost_usd = res.get("total_cost_usd")
    duration_ms = res.get("duration_ms")
    num_turns = res.get("num_turns")
    final_text = res.get("result", "")
    model_usage = res.get("modelUsage")
    is_error = res.get("is_error", False)

    notes: list[str] = []
    if active.parser.errors:
        notes.extend(active.parser.errors)
    if res.get("permission_denials"):
        notes.append(f"permission_denials: {res.get('permission_denials')}")

    run_result = ClaudeRunResult(
        run_id=active.run_id,
        workspace_path=str(active.workspace),
        final_text=final_text,
        result=res,
        stdout=active.stdout_buf.tail(_settings.max_stdout_bytes),
        stderr=active.stderr_buf.tail(_settings.max_stderr_bytes),
        exit_code=exit_code,
        timed_out=False,
        cancelled=active.cancel_requested,
        started_at=active.started_at,
        finished_at=_now(),
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        duration_ms=duration_ms,
        num_turns=num_turns,
        model_usage=model_usage,
        notes=notes,
    )

    changes = _compute_changes(active.workspace, active.request, active.before_snapshot)

    if active.cancel_requested:
        status = "cancelled"
    elif (exit_code is not None and exit_code != 0) or is_error:
        status = "error"
    else:
        status = "done"

    _run_store.put(
        active.run_id,
        StoredRun(
            status=status,
            result=run_result,
            changes=changes,
            started_at=active.started_at,
        )
    )

    with _active_runs_lock:
        _active_runs.pop(active.run_id, None)


# MCP Tools

@mcp.tool(name=tool_name("health"))
def claude_health(req: ClaudeHealthRequestIn) -> ClaudeHealthResponse:
    """Health check for the Claude Code CLI binary."""
    try:
        claude_path = _resolve_claude_path()
        version = subprocess.check_output([claude_path, "--version"], text=True).strip()
    except Exception as e:
        raise RuntimeError(f"CLAUDE_NOT_FOUND: unable to run claude: {e}") from e

    auth_status = _auth_status_text()
    notes: list[str] = []
    ok = True
    if req.expected_version and version != req.expected_version:
        ok = False
        notes.append(f"expected_version_mismatch: expected={req.expected_version} got={version}")

    return ClaudeHealthResponse(
        claude_path=claude_path,
        claude_version=version,
        ok=ok,
        notes=notes,
        auth_status=auth_status,
    )


@mcp.tool(name=tool_name("run_task"))
def claude_run_task(req: ClaudeRunTaskRequestIn) -> ClaudeRunTaskResponse:
    """Run a single Claude task synchronously."""
    workspace = _resolve_workspace_path(req.workspace_path)
    _validate_exec_options(req)

    prompt_with_context = _build_prompt_with_context(req.prompt, _settings, _persistence_store)

    before_snapshot = None
    if req.capture_changes and not is_git_repo(workspace) and req.change_scope == "workspace":
        before_snapshot = snapshot_tree(
            workspace,
            ignore_dir_names=_settings.ignore_dir_names,
            max_file_bytes=_settings.snapshot_max_file_bytes,
        )

    req_for_run = req.model_copy(update={"prompt": prompt_with_context})
    claude_path = _resolve_claude_path()

    started_at = _now()
    run = start_sync_run(
        claude_path=claude_path,
        workspace_path=str(workspace),
        request=req_for_run,
        settings=_settings,
    )

    timeout_s = max(1, req.options.timeout_s or _settings.default_timeout_s)
    exit_code, timed_out, stdout, stderr = wait_sync(run, timeout_s=timeout_s, settings=_settings)
    finished_at = _now()

    # Parse stdout as JSON
    import json
    result_dict = {}
    parse_error = None
    try:
        result_dict = json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        parse_error = str(e)
        result_dict = {"result": stdout, "parse_error": parse_error}

    if not isinstance(result_dict, dict):
        result_dict = {"result": stdout, "parse_error": "Parsed JSON was not a dictionary"}

    session_id = result_dict.get("session_id")
    total_cost_usd = result_dict.get("total_cost_usd")
    duration_ms = result_dict.get("duration_ms")
    num_turns = result_dict.get("num_turns")
    final_text = result_dict.get("result", "")
    model_usage = result_dict.get("modelUsage")
    is_error = result_dict.get("is_error", False)

    run_id = f"run-{uuid4()}"
    notes = []
    if result_dict.get("permission_denials"):
        notes.append(f"permission_denials: {result_dict.get('permission_denials')}")
    if parse_error:
        notes.append(f"json_parse_error: {parse_error}")

    run_result = ClaudeRunResult(
        run_id=run_id,
        workspace_path=str(workspace),
        final_text=final_text,
        result=result_dict,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=False,
        started_at=started_at,
        finished_at=finished_at,
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        duration_ms=duration_ms,
        num_turns=num_turns,
        model_usage=model_usage,
        notes=notes,
    )

    changes = _compute_changes(workspace, req, before_snapshot)

    if timed_out:
        status = "timeout"
    elif (exit_code is not None and exit_code != 0) or is_error:
        status = "error"
    else:
        status = "done"

    _run_store.put(
        run_id,
        StoredRun(status=status, result=run_result, changes=changes, started_at=started_at),
    )

    return ClaudeRunTaskResponse(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        final_text=final_text,
        result=result_dict,
        changes=changes,
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        duration_ms=duration_ms,
        num_turns=num_turns,
        model_usage=model_usage,
        notes=notes,
    )


@mcp.tool(name=tool_name("start_task"))
def claude_start_task(req: ClaudeStartTaskRequestIn) -> ClaudeStartTaskResponse:
    """Start a Claude Code CLI task asynchronously."""
    with _active_runs_lock:
        if len(_active_runs) >= _settings.max_concurrent_runs:
            raise RuntimeError(
                f"MAX_CONCURRENT_RUNS_EXCEEDED: already running {len(_active_runs)} tasks "
                f"(limit {_settings.max_concurrent_runs})"
            )

    workspace = _resolve_workspace_path(req.workspace_path)
    _validate_exec_options(req)

    prompt_with_context = _build_prompt_with_context(req.prompt, _settings, _persistence_store)

    before_snapshot = None
    if req.capture_changes and not is_git_repo(workspace) and req.change_scope == "workspace":
        before_snapshot = snapshot_tree(
            workspace,
            ignore_dir_names=_settings.ignore_dir_names,
            max_file_bytes=_settings.snapshot_max_file_bytes,
        )

    claude_path = _resolve_claude_path()
    session_id = str(uuid4())
    req_for_run = req.model_copy(update={"prompt": prompt_with_context})

    # Temporarily patch Thread to capture the reader thread
    _orig_thread_init = threading.Thread.__init__
    _last_spawned_thread = None

    def _custom_thread_init(self_thread, *args, **kwargs):
        nonlocal _last_spawned_thread
        _orig_thread_init(self_thread, *args, **kwargs)
        _last_spawned_thread = self_thread

    threading.Thread.__init__ = _custom_thread_init
    try:
        run = start_async_run(
            claude_path=claude_path,
            workspace_path=str(workspace),
            request=req_for_run,
            settings=_settings,
            session_id=session_id,
        )
    finally:
        threading.Thread.__init__ = _orig_thread_init

    if run.parser is not None and _last_spawned_thread is not None:
        run.parser._stdout_thread = _last_spawned_thread

    run_id = run.run_id
    started_at = run.started_at

    active = ActiveRun(
        run_id=run_id,
        session_id=session_id,
        workspace=workspace,
        request=req,
        started_at=started_at,
        run=run,
        parser=run.parser,
        stdout_buf=run.parser._stdout_buf if hasattr(run.parser, "_stdout_buf") else RollingTextBuffer(max_bytes=_settings.max_stdout_bytes),
        stderr_buf=RollingTextBuffer(max_bytes=_settings.max_stderr_bytes),
        before_snapshot=before_snapshot,
    )

    with _active_runs_lock:
        _active_runs[run_id] = active

    t = threading.Thread(target=_finalize_active_run, args=(active,), daemon=True)
    t.start()

    return ClaudeStartTaskResponse(
        run_id=run_id,
        started_at=started_at,
        session_id=session_id,
    )


@mcp.tool(name=tool_name("poll_task"))
def claude_poll_task(req: ClaudePollTaskRequestIn) -> ClaudePollTaskResponse:
    """Poll an asynchronous task."""
    import time

    if req.drain:
        while True:
            with _active_runs_lock:
                active = _active_runs.get(req.run_id)
            if active is None:
                break
            time.sleep(0.1)

    with _active_runs_lock:
        active = _active_runs.get(req.run_id)

    if active is not None:
        all_msgs = list(active.parser.messages)
        total_msgs = len(all_msgs)

        if active.last_polled_msg_count == 0:
            new_msgs = all_msgs[-50:]
        else:
            new_msgs = all_msgs[active.last_polled_msg_count:]

        active.last_polled_msg_count = total_msgs

        stdout_len = len(active.stdout_buf.get().encode("utf-8", errors="replace"))
        stderr_len = len(active.stderr_buf.get().encode("utf-8", errors="replace"))
        elapsed = (datetime.now(timezone.utc) - active.started_at).total_seconds()

        return ClaudePollTaskResponse(
            run_id=req.run_id,
            status="running",
            new_messages=new_msgs,
            result=None,
            stdout_len=stdout_len,
            stderr_len=stderr_len,
            elapsed_seconds=elapsed,
            notes=[],
        )

    stored = _run_store.get(req.run_id)
    if stored is None:
        raise RuntimeError("RUN_NOT_FOUND: unknown run_id")

    exit_code_str = f"exit={stored.result.exit_code}" if stored.result else "exit=None"
    return ClaudePollTaskResponse(
        run_id=req.run_id,
        status=stored.status,
        new_messages=[],
        result=stored.result,
        stdout_len=len(stored.result.stdout.encode("utf-8")) if (stored.result and stored.result.stdout) else 0,
        stderr_len=len(stored.result.stderr.encode("utf-8")) if (stored.result and stored.result.stderr) else 0,
        elapsed_seconds=stored.result.duration_ms / 1000.0 if (stored.result and stored.result.duration_ms) else 0.0,
        notes=[f"final: {exit_code_str}"],
    )


@mcp.tool(name=tool_name("cancel_task"))
def claude_cancel_task(req: ClaudeCancelTaskRequestIn) -> ClaudeCancelTaskResponse:
    """Cancel a running task."""
    with _active_runs_lock:
        active = _active_runs.get(req.run_id)

    if active is not None:
        active.cancel_requested = True
        terminate_run(active.run, force=req.force)
        return ClaudeCancelTaskResponse(canceled=True, status="cancelled")

    stored = _run_store.get(req.run_id)
    if stored is not None:
        return ClaudeCancelTaskResponse(canceled=False, status="already_done")

    return ClaudeCancelTaskResponse(canceled=False, status="not_found")


@mcp.tool(name=tool_name("list_runs"))
def claude_list_runs(req: ClaudeListRunsRequestIn) -> ClaudeListRunsResponse:
    """List recent runs."""
    runs: list[ClaudeRunSummary] = []

    with _active_runs_lock:
        active_items = list(_active_runs.values())
    active_items.sort(key=lambda r: r.started_at, reverse=True)

    for r in active_items[: max(0, req.limit)]:
        runs.append(
            ClaudeRunSummary(
                run_id=r.run_id,
                workspace_path=str(r.workspace),
                status="running",
                started_at=r.started_at,
            )
        )

    remaining = max(0, req.limit - len(runs))
    if remaining > 0:
        items = _run_store.list(remaining)
        for run_id, stored in items:
            runs.append(
                ClaudeRunSummary(
                    run_id=run_id,
                    workspace_path=stored.result.workspace_path if stored.result else "",
                    status=stored.status,
                    started_at=stored.started_at,
                )
            )

    return ClaudeListRunsResponse(runs=runs)


# Persistence tools

@mcp.tool(name=tool_name("init_persistence"))
def claude_init_persistence(req: ClaudeInitPersistenceRequestIn) -> ClaudeInitPersistenceResponse:
    """Initialize the persistence directory and seed the three markdown files."""
    if not _settings.persistence_enabled:
        raise ValueError("PERSISTENCE_DISABLED: persistence is disabled via settings")

    result = _persistence_store.init(force=req.force, seed_templates=req.seed_templates)
    return ClaudeInitPersistenceResponse(
        base_dir=result.base_dir,
        created=result.created,
        already_existed=result.already_existed,
        seed_version=result.seed_version,
    )


@mcp.tool(name=tool_name("read_persistence"))
def claude_read_persistence(req: ClaudeReadPersistenceRequestIn) -> ClaudeReadPersistenceResponse:
    """Read one of the three persistence files."""
    if not _settings.persistence_enabled:
        raise ValueError("PERSISTENCE_DISABLED: persistence is disabled via settings")

    result = _persistence_store.read(req.file, offset=req.offset, limit=req.limit)
    return ClaudeReadPersistenceResponse(
        file=result.file,
        content=result.content,
        size_bytes=result.size_bytes,
        truncated=result.truncated,
        modified_at=result.modified_at,
    )


@mcp.tool(name=tool_name("append_persistence"))
def claude_append_persistence(req: ClaudeAppendPersistenceRequestIn) -> ClaudeAppendPersistenceResponse:
    """Append content to one of the persistence files."""
    if not _settings.persistence_enabled:
        raise ValueError("PERSISTENCE_DISABLED: persistence is disabled via settings")

    if req.file == "agents" and _settings.mode == "safe" and not req.confirm:
        raise ValueError("CONFIRM_REQUIRED: updating AGENTS.md in safe mode requires confirm=true")

    result = _persistence_store.append(req.file, req.content, section_header=req.section_header)
    return ClaudeAppendPersistenceResponse(
        file=result.file,
        appended_bytes=result.appended_bytes,
        new_size_bytes=result.new_size_bytes,
        timestamp=result.timestamp,
    )


@mcp.tool(name=tool_name("update_persistence"))
def claude_update_persistence(req: ClaudeUpdatePersistenceRequestIn) -> ClaudeUpdatePersistenceResponse:
    """Replace or append to a section in one of the persistence files."""
    if not _settings.persistence_enabled:
        raise ValueError("PERSISTENCE_DISABLED: persistence is disabled via settings")

    if req.file == "agents" and _settings.mode == "safe" and not req.confirm:
        raise ValueError("CONFIRM_REQUIRED: updating AGENTS.md in safe mode requires confirm=true")

    result = _persistence_store.update(
        req.file,
        req.section_anchor,
        req.new_content,
        mode=req.mode,
    )
    return ClaudeUpdatePersistenceResponse(
        file=result.file,
        section_anchor=result.section_anchor,
        matched=result.matched,
        new_size_bytes=result.new_size_bytes,
    )


@mcp.tool(name=tool_name("load_persistence_context"))
def claude_load_persistence_context(req: ClaudeLoadPersistenceContextRequestIn) -> ClaudeLoadPersistenceContextResponse:
    """Load the persistence files as context for the current session."""
    if not _settings.persistence_enabled:
        raise ValueError("PERSISTENCE_DISABLED: persistence is disabled via settings")

    result = _persistence_store.load_context(
        include=req.include,
        max_chars_per_file=req.max_chars_per_file,
    )
    return ClaudeLoadPersistenceContextResponse(
        agents_excerpt=result.agents_excerpt,
        projects_excerpt=result.projects_excerpt,
        memory_excerpt=result.memory_excerpt,
        truncated_flags=result.truncated_flags,
        total_chars=result.total_chars,
        base_dir=result.base_dir,
        initialized=result.initialized,
    )


# Prompts

@mcp.prompt(name=prompt_name("sync_orchestration"))
def prompt_sync_orchestration(*, workspace_path: str, goal: str) -> str:
    """Orchestration playbook for running a single synchronous task safely."""
    return (
        "You are orchestrating an MCP server that executes Claude Code CLI (`claude`) inside a controlled workspace.\n"
        "\n"
        "Goal:\n"
        f"- {goal}\n"
        "\n"
        "Workspace:\n"
        f"- workspace_path: {workspace_path}\n"
        "\n"
        "Constraints and important behavior:\n"
        "- Model selection is done per-run via the `model` field (or the CLI `--model` flag).\n"
        "- `workspace_path` must be an existing directory inside the server's allowed roots.\n"
        "- Prefer safe defaults: sandbox enabled, no custom env, no extra_args.\n"
        "\n"
        "Recommended execution plan (synchronous):\n"
        "1) Call `claude_health` once if you haven't verified the binary for this environment.\n"
        "2) Call `claude_run_task` with:\n"
        "   - workspace_path set to the provided value\n"
        "   - prompt containing clear instructions and acceptance criteria\n"
        "   - capture_changes=true\n"
        "   - change_scope=\"workspace\"\n"
        "3) Use the returned `changes` field to decide what to do next:\n"
        "   - If changes.method==\"git\": inspect `diff` (unified git diff) and changed_files.\n"
        "   - If changes.method==\"snapshot\": inspect changed_files; open files as needed to review.\n"
        "4) If the run failed or timed out:\n"
        "   - Review stderr and stdout for actionable error text.\n"
        "   - Retry with a more constrained prompt, or break down the task.\n"
        "\n"
        "JSON example (claude_run_task):\n"
        "{\n"
        "  \"workspace_path\": \"" + workspace_path.replace('\"', '\\\\\"') + "\",\n"
        "  \"prompt\": \"<write a precise task here with steps and success criteria>\",\n"
        "  \"capture_changes\": true,\n"
        "  \"change_scope\": \"workspace\",\n"
        "  \"model\": \"sonnet\",\n"
        "  \"permission_mode\": \"acceptEdits\",\n"
        "  \"options\": {\n"
        "    \"sandbox\": true,\n"
        "    \"dangerously_skip_permissions\": false,\n"
        "    \"timeout_s\": 300,\n"
        "    \"env\": null,\n"
        "    \"extra_args\": []\n"
        "  }\n"
        "}\n"
    )


@mcp.prompt(name=prompt_name("async_orchestration"))
def prompt_async_orchestration(*, workspace_path: str, goal: str) -> str:
    """Orchestration playbook for running start_task + poll_task + cancel_task."""
    return (
        "You are orchestrating an MCP server that executes Claude Code CLI (`claude`) inside a controlled workspace.\n"
        "\n"
        "Goal:\n"
        f"- {goal}\n"
        "\n"
        "Workspace:\n"
        f"- workspace_path: {workspace_path}\n"
        "\n"
        "Constraints and important behavior:\n"
        "- While a run is active, `claude_poll_task` returns partial messages, stdout/stderr tails, and status=\"running\".\n"
        "- After completion, `claude_poll_task` returns status in {done, error, timeout, cancelled} and includes `result` (and `changes` if enabled).\n"
        "\n"
        "Recommended execution plan (async):\n"
        "1) Call `claude_start_task` with capture_changes=true (unless you explicitly don't need it).\n"
        "2) Store run_id.\n"
        "3) Poll with backoff:\n"
        "   - Poll with `claude_poll_task` using `drain=false` and optional `wait_seconds`.\n"
        "   - Or poll with `drain=true` to wait synchronously for completion.\n"
        "   - Always stop when status != \"running\".\n"
        "4) If you detect a stuck run or need to stop:\n"
        "   - Call `claude_cancel_task` with force=false first.\n"
        "   - If it does not exit promptly, call again with force=true.\n"
        "5) Once done:\n"
        "   - Inspect result.stdout/result.stderr and changes.\n"
        "\n"
        "JSON example (claude_start_task):\n"
        "{\n"
        "  \"workspace_path\": \"" + workspace_path.replace('\"', '\\\\\"') + "\",\n"
        "  \"prompt\": \"<write a precise task here with steps and success criteria>\",\n"
        "  \"capture_changes\": true,\n"
        "  \"change_scope\": \"workspace\",\n"
        "  \"options\": {\n"
        "    \"sandbox\": true,\n"
        "    \"dangerously_skip_permissions\": false,\n"
        "    \"timeout_s\": 300,\n"
        "    \"env\": null,\n"
        "    \"extra_args\": []\n"
        "  }\n"
        "}\n"
        "\n"
        "JSON example (claude_poll_task):\n"
        "{ \"run_id\": \"run-<uuid>\", \"drain\": false }\n"
        "\n"
        "JSON example (claude_cancel_task):\n"
        "{ \"run_id\": \"run-<uuid>\", \"force\": false }\n"
    )


@mcp.prompt(name=prompt_name("model_selection_guidance"))
def prompt_model_selection_guidance() -> str:
    """Guidance for model selection when using this MCP server."""
    return (
        "The Claude Code CLI (`claude`) allows selecting the model per-run via "
        "the `--model` flag, and an optional automatic fallback via "
        "`--fallback-model` (used when the primary model is overloaded).\n"
        "\n"
        "Model selection constraints:\n"
        "- Pass `model` (and optionally `fallback_model`) directly to "
        "`claude_run_task` or `claude_start_task`.\n"
        "  Examples: `\"sonnet\"`, `\"opus\"`, `\"claude-sonnet-4-6\"`.\n"
        "- The MCP server has an `allowed_models` setting (env var "
        "`CLAUDE_MCP_ALLOWED_MODELS`) which restricts permitted models for "
        "both `--model` and `--fallback-model`.\n"
        "- Attempting to use a model not in the allowlist raises a "
        "`MODEL_NOT_ALLOWED` error.\n"
        "\n"
        "Recommended aliases (per CLI docs):\n"
        "- sonnet: latest Claude Sonnet model\n"
        "- opus: latest Claude Opus model\n"
        "- haiku: only valid for subagent `--agents` definitions\n"
        "\n"
        "If the request does not set `model`, the CLI uses its built-in "
        "default (typically `sonnet` on Pro/Team accounts). `fallback_model` "
        "is opt-in — set it explicitly when you want graceful degradation "
        "during overload.\n"
    )


@mcp.prompt(name=prompt_name("security_and_workspace_rules"))
def prompt_security_and_workspace_rules() -> str:
    """Safety rules and workspace constraints for orchestrators."""
    return (
        "Safety and workspace rules for claude-code-cli-mcp:\n"
        "\n"
        "Workspace constraints:\n"
        "- workspace_path must be an existing directory under allowed roots.\n"
        "\n"
        "Safe mode constraints:\n"
        "- Enforces --bare to disable local workspace configurations.\n"
        "- Rejects custom `env` overrides.\n"
        "- Rejects `extra_args`.\n"
        "- Rejects `dangerously_skip_permissions` and `bypassPermissions` or `dontAsk` permission modes.\n"
        "\n"
        "Permissive mode constraints:\n"
        "- Custom `env` keys are restricted by settings allowlist.\n"
        "- Custom `extra_args` are restricted by settings allowlist.\n"
        "- `bypassPermissions` / `dangerously_skip_permissions` requires explicit allowlist configuration.\n"
        "\n"
        "Configuration notes:\n"
        "- The `~/.claude.json` file is immutable and cannot be edited by this server.\n"
    )


@mcp.prompt(name=prompt_name("persistence_protocol"))
def prompt_persistence_protocol() -> str:
    """Instruct the orchestrator on how to maintain the persistence layer."""
    return (
        "You have access to a persistent memory layer at "
        "~/.open-cli-router/claude/ with three editable files:\n"
        "- AGENTS.md (your editable system prompt)\n"
        "- PROJECTS.md (project summaries)\n"
        "- MEMORY.md (permanent memory)\n"
        "\n"
        "Lifecycle:\n"
        "1. On the first run, call `claude_init_persistence` to "
        "create the directory and seed files.\n"
        "2. At the start of each session, call "
        "`claude_load_persistence_context` to load the latest state.\n"
        "3. After each meaningful session, append a concise summary to "
        "MEMORY.md using `claude_append_persistence`.\n"
        "4. When the user explicitly changes AGENTS.md or PROJECTS.md, "
        "use `claude_update_persistence` to persist.\n"
        "\n"
        "Do not store secrets, credentials, or full file dumps in "
        "MEMORY.md — keep entries small and high-signal.\n"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
