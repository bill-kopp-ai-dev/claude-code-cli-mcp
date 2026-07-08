from __future__ import annotations
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from claude_code_mcp.changes import (
    diff_snapshots, git_changed_files, git_diff, is_git_repo, snapshot_tree,
)
from claude_code_mcp.claude_runner import (
    ClaudeRun, start_async_run, start_sync_run, terminate_run, wait_sync,
)
from claude_code_mcp.claude_stream import ClaudeStreamParser
from claude_code_mcp.logfire_setup import setup_logfire
from claude_code_mcp.models import (
    ClaudeAppendPersistenceRequest, ClaudeAppendPersistenceRequestIn,
    ClaudeAppendPersistenceResponse, ClaudeCancelTaskRequest,
    ClaudeCancelTaskRequestIn, ClaudeCancelTaskResponse, ClaudeHealthRequest,
    ClaudeHealthRequestIn, ClaudeHealthResponse, ClaudeInitPersistenceRequest,
    ClaudeInitPersistenceRequestIn, ClaudeInitPersistenceResponse,
    ClaudeListRunsRequest, ClaudeListRunsRequestIn, ClaudeListRunsResponse,
    ClaudeLoadPersistenceContextRequest, ClaudeLoadPersistenceContextRequestIn,
    ClaudeLoadPersistenceContextResponse, ClaudePollTaskRequest,
    ClaudePollTaskRequestIn, ClaudePollTaskResponse, ClaudeReadPersistenceRequest,
    ClaudeReadPersistenceRequestIn, ClaudeReadPersistenceResponse,
    ClaudeRunResult, ClaudeRunSummary, ClaudeRunTaskRequest,
    ClaudeRunTaskRequestIn, ClaudeRunTaskResponse, ClaudeSelfTestRequest,
    ClaudeSelfTestRequestIn, ClaudeSelfTestResponse, ClaudeToolSchemaReport,
    ClaudeStartTaskRequest, ClaudeStartTaskRequestIn, ClaudeStartTaskResponse,
    ClaudeUpdatePersistenceRequest, ClaudeUpdatePersistenceRequestIn,
    ClaudeUpdatePersistenceResponse, WorkspaceChanges,
)
from claude_code_mcp.persistence import PersistenceStore, build_prompt_with_context
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
# Persistence store — file-based memory layer for AGENTS.md, PROJECTS.md, MEMORY.md.
# base_dir resolution: Settings.resolve_persistence_base_dir() honors
# persistence_location ("global" vs "workspace") and the $cwd_parent
# escape hatch in persistence_base_dir.
_persistence_store = PersistenceStore(
    base_dir=_settings.resolve_persistence_base_dir(),
    max_file_bytes=_settings.persistence_max_file_bytes,
    backup_on_write=_settings.persistence_backup_on_write,
    backup_keep=_settings.persistence_backup_keep,
    seed_templates=_settings.persistence_seed_templates,
    head_ratio=_settings.persistence_truncation_head_ratio,
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
    if not any(
        str(root) == "/" or resolved_path == root or str(resolved_path).startswith(str(root) + "/")
        for root in allowed_roots
    ):
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
        allowed_extra = _settings.allow_extra_args or set()
        if _settings.mode == "permissive":
            allowed_extra = allowed_extra | {"--dangerously-skip-permissions"}
        unknown = [a for a in req.options.extra_args if a not in allowed_extra]
        if unknown:
            raise ValueError("NOT_ALLOWED: extra_args contains disallowed entries")

    if req.options.env:
        unknown = [k for k in req.options.env.keys() if k not in _settings.allow_env_keys]
        if unknown:
            raise ValueError("NOT_ALLOWED: env contains disallowed keys")


def _build_prompt_with_context(prompt_text: str, settings: Any, persistence_store: Any) -> str:
    """Backwards-compatible wrapper around build_prompt_with_context.

    Kept for backwards compatibility with any external caller; the
    canonical implementation now lives in
    :mod:`claude_code_mcp.persistence.context`.
    """
    return build_prompt_with_context(
        prompt_text, settings=settings, store=persistence_store
    )


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
def claude_health(req: ClaudeHealthRequestIn | None = None) -> ClaudeHealthResponse:
    """Health check for the Claude Code CLI binary.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeHealthRequest()
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
def claude_run_task(req: ClaudeRunTaskRequestIn | None = None) -> ClaudeRunTaskResponse:
    """Run a single Claude task synchronously.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeRunTaskRequest()
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
def claude_start_task(req: ClaudeStartTaskRequestIn | None = None) -> ClaudeStartTaskResponse:
    """Start a Claude Code CLI task asynchronously.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeStartTaskRequest()
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
def claude_poll_task(req: ClaudePollTaskRequestIn | None = None) -> ClaudePollTaskResponse:
    """Poll an asynchronous task.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudePollTaskRequest()
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
def claude_cancel_task(req: ClaudeCancelTaskRequestIn | None = None) -> ClaudeCancelTaskResponse:
    """Cancel a running task.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeCancelTaskRequest()
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
def claude_list_runs(req: ClaudeListRunsRequestIn | None = None) -> ClaudeListRunsResponse:
    """List recent runs.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeListRunsRequest()
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
def claude_init_persistence(req: ClaudeInitPersistenceRequestIn | None = None) -> ClaudeInitPersistenceResponse:
    """Initialize the persistence directory and seed the three markdown files.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeInitPersistenceRequest()
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
def claude_read_persistence(req: ClaudeReadPersistenceRequestIn | None = None) -> ClaudeReadPersistenceResponse:
    """Read one of the three persistence files.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeReadPersistenceRequest()
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
def claude_append_persistence(req: ClaudeAppendPersistenceRequestIn | None = None) -> ClaudeAppendPersistenceResponse:
    """Append content to one of the persistence files.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeAppendPersistenceRequest()
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
def claude_update_persistence(req: ClaudeUpdatePersistenceRequestIn | None = None) -> ClaudeUpdatePersistenceResponse:
    """Replace or append to a section in one of the persistence files.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeUpdatePersistenceRequest()
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
def claude_load_persistence_context(req: ClaudeLoadPersistenceContextRequestIn | None = None) -> ClaudeLoadPersistenceContextResponse:
    """Load the persistence files as context for the current session.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"field1": value1, "field2": value2, ...}}`
        For backwards-compatibility, the server also accepts `args={}` for
        tools whose request model has all-optional fields; required-field
        errors surface as Pydantic ValidationError.
    """
    if req is None:
        req = ClaudeLoadPersistenceContextRequest()
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


@mcp.prompt(name=prompt_name("timeout_help"))
def prompt_timeout_help(
    task_class: str = "single_feature",
    files_to_edit: int = 1,
    model_alias: str = "sonnet",
) -> str:
    """Decision matrix for picking the right timeout + sync/async per task.

    Parameters
    ----------
    task_class : str
        One of the TaskClass enum values: trivial_edit, smoke_test,
        single_feature, docs_update, test_suite, review,
        multi_file_refactor, architecture, migration, long_running.
    files_to_edit : int
        Estimated number of files the task will modify. Used to bump
        timeout up for refactor-style tasks.
    model_alias : str
        Short alias of the target model: sonnet, fable, opus, haiku.
        Resolved via MODEL_REGISTRY.

    The prompt returns a structured guide that the orchestrator
    (Femto or similar) can read to pick the right timeout_s and
    whether to use claude_run_task (sync) or claude_start_task (async).
    """
    from claude_code_mcp.models import MODEL_REGISTRY, TaskClass
    from claude_code_mcp.timeout_policy import (
        SYNC_CEILING_S, ASYNC_CEILING_S, compute_timeout,
    )

    # Normalize inputs
    tc = task_class.lower().strip()
    fa = model_alias.lower().strip()

    # Build the matrix string (Table-driven — same data the policy uses)
    matrix = "\n".join(
        f"| {member.value:<24} | {compute_timeout(member, MODEL_REGISTRY['sonnet'], files_to_edit=5).timeout_s:>5}s | "
        f"{compute_timeout(member, MODEL_REGISTRY['sonnet'], files_to_edit=5).must_use_async!s:<5} |"
        for member in TaskClass
    )

    # Look up the specific recommendation
    try:
        profile = MODEL_REGISTRY[fa]
    except KeyError:
        return (
            f"ERROR: unknown model_alias='{fa}'. Valid aliases: "
            f"{sorted(MODEL_REGISTRY.keys())}"
        )

    try:
        task_enum = TaskClass(tc)
    except ValueError:
        return (
            f"ERROR: unknown task_class='{tc}'. Valid values: "
            f"{[m.value for m in TaskClass]}"
        )

    rec = compute_timeout(task_enum, profile, files_to_edit=files_to_edit)

    return (
        f"## Timeout recommendation\n"
        f"\n"
        f"- task_class: {task_enum.value}\n"
        f"- model_alias: {profile.alias} (tier={profile.tier.value}, "
        f"display={profile.display_name})\n"
        f"- files_to_edit: {files_to_edit}\n"
        f"\n"
        f"Recommended values:\n"
        f"- `timeout_s`: **{rec.timeout_s}**\n"
        f"- `must_use_async`: **{rec.must_use_async}**\n"
        f"- warning: {rec.warning or '(none)'}\n"
        f"\n"
        f"## How to call\n"
        f"\n"
        f"```python\n"
        f"{'await ' if rec.must_use_async else ''}claude_{'start_task' if rec.must_use_async else 'run_task'}(\n"
        f"    req={{'prompt': '...', 'model': '{profile.alias}', 'timeout_s': {rec.timeout_s}}}\n"
        f")\n"
        f"```\n"
        f"\n"
        f"## Decision matrix (default, files_to_edit=5, sonnet)\n"
        f"\n"
        f"| task_class              | timeout | async |\n"
        f"|-------------------------|---------|-------|\n"
        f"{matrix}\n"
        f"\n"
        f"## Reference constants\n"
        f"\n"
        f"- SYNC_CEILING_S = {SYNC_CEILING_S} (FastMCP sync wrapper hard cap)\n"
        f"- ASYNC_CEILING_S = {ASYNC_CEILING_S} (Pydantic validator upper bound)\n"
        f"\n"
        f"## Rules\n"
        f"\n"
        f"- Use `claude_run_task` (sync) ONLY if `timeout_s <= {SYNC_CEILING_S}`.\n"
        f"- Use `claude_start_task` (async) + `claude_poll_task` if `timeout_s > {SYNC_CEILING_S}`.\n"
        f"- For tasks that touch ≥50 files, expect `timeout_s >= 1800` (async mandatory).\n"
        f"- For tasks that touch ≥100 files or are 'architecture'/'migration' class, "
        f"expect `timeout_s` near the {ASYNC_CEILING_S}s ceiling.\n"
        f"- `haiku` is `multi_file_safe=False`: avoid for >5 file edits "
        f"(compute_timeout emits a warning).\n"
        f"\n"
        f"## Model registry\n"
        f"\n"
        + "\n".join(
            f"- **{a}** (tier={p.tier.value}, ~${p.typical_cost_per_run_usd:.2f}/run, "
            f"latency={p.typical_latency_min:.1f}min, multi_file_safe={p.multi_file_safe})"
            for a, p in MODEL_REGISTRY.items()
        )
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
    from claude_code_mcp.provider import PERSISTENCE_NAMESPACE

    base = _settings.resolve_persistence_base_dir()
    location_note = (
        f"NOTE: persistence is configured with LOCATION="
        f"{_settings.persistence_location} → base_dir={base}\n"
    )
    if _settings.persistence_location == "workspace":
        location_note += (
            "When location='workspace', the files live in your project "
            "directory (one level up from the server's CWD).\n"
            "Consider adding '.open-cli-router/' to .gitignore to avoid "
            "committing agent memory to source control.\n"
        )
    location_note += "\n"

    return (location_note + (
        f"You have access to a persistent memory layer "
        f"(namespace: {PERSISTENCE_NAMESPACE}) with three editable files:\n"
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
        "use `claude_update_persistence` to persist. **Note:** in safe mode, "
        "updating AGENTS.md requires `confirm=true`.\n"
        "\n"
        "Do not store secrets, credentials, or full file dumps in "
        "MEMORY.md — keep entries small and high-signal.\n"
    ))


@mcp.prompt(name=prompt_name("quickstart"))
def prompt_quickstart() -> str:
    """Cheatsheet for using this MCP server. Read this first if confused.

    Returns the canonical contract: args shape, required CLI binary,
    tool catalog, and common gotchas. Static, but kept in sync with
    `claude_self_test` results.
    """
    return (
        f"# {PROVIDER_PREFIX}-code-cli-mcp — Quickstart\n"
        "\n"
        "## Args shape (CRITICAL — most bugs come from this)\n"
        "    run_mcp(args={\"req\": {...}})       # ✓ correct — dict\n"
        "    run_mcp(args=[{\"req\": {...}}])     # ✗ wrong — list wraps as {\"item\": ...}\n"
        "    run_mcp(args={})                    # OK after refactor: req is now optional\n"
        "\n"
        "## CLI binary required\n"
        "    claude must be installed and on PATH (Claude Code CLI).\n"
        "    Verify with claude_health(req={}). For print-mode (-p) runs,\n"
        "    the user must run `claude login` interactively at least once.\n"
        "\n"
        "## Tool catalog (12 tools)\n"
        "    claude_health                  — ping server + check CLI + auth\n"
        "    claude_self_test               — schema robustness probe (run first if unsure)\n"
        "    claude_run_task                — sync execution (supports model + fallback_model)\n"
        "    claude_start_task / claude_poll_task / claude_cancel_task — async lifecycle\n"
        "    claude_list_runs               — list active/completed runs\n"
        "    Persistence (5):\n"
        "        claude_init_persistence, claude_read_persistence,\n"
        "        claude_append_persistence, claude_update_persistence,\n"
        "        claude_load_persistence_context\n"
        "\n"
        "## workspace_path for run_task\n"
        "    Must be inside CLAUDE_MCP_ALLOWED_ROOTS (JSON-array env var).\n"
        "    Default = Path.cwd() of the server process = the server's project dir.\n"
        "\n"
        "## Common gotchas → call troubleshoot prompt with the error string\n"
        "    Use prompt `claude_troubleshoot` with the exact error message.\n"
        "\n"
        "## Restart requirement\n"
        "    After server-side changes that add new tools/prompts, restart the\n"
        "    MCP server in the Trae panel so the registry re-discovers them.\n"
    )


@mcp.prompt(name=prompt_name("contract"))
def prompt_contract() -> str:
    """Full machine-readable JSON contract of every registered tool.

    Builds the catalog from mcp._local_provider._components so it stays
    in sync with the actual registered tool schemas. Read this before
    writing integrations.
    """
    tools_dict: dict[str, Any] = {}
    if hasattr(mcp, "_local_provider") and hasattr(mcp._local_provider, "_components"):
        tools_dict = {
            v.name: v.parameters if hasattr(v, "parameters") else {}
            for k, v in mcp._local_provider._components.items()
            if k.startswith("tool:")
        }
    elif hasattr(mcp, "_tool_manager"):
        tools_dict = getattr(mcp._tool_manager, "_tools", {})

    parts = [f"# {PROVIDER_PREFIX}-code-cli-mcp — Full tool contract\n"]
    for name, schema in sorted(tools_dict.items()):
        parts.append(f"## {name}\n")
        parts.append("```json\n")
        try:
            parts.append(json.dumps(schema, indent=2, default=str))
        except Exception:  # noqa: BLE001
            parts.append(str(schema))
        parts.append("\n```\n")
    return "\n".join(parts)


@mcp.prompt(name=prompt_name("troubleshoot"))
def prompt_troubleshoot(error: str = "") -> str:
    """Diagnose a specific error string and return the fix recipe.

    Pass the exact error message you received (e.g. \"req: Missing required
    argument\" or \"workspace_path is outside allowed roots\") and this
    prompt returns the canonical fix.
    """
    err_lc = (error or "").lower()
    if not err_lc:
        return (
            "Pass the exact error message you received as the `error` arg.\n"
            "Example: prompt `claude_troubleshoot` with error=\"req: Missing required argument\"."
        )
    if "missing required argument" in err_lc and "req" in err_lc:
        return (
            "BUG: args shape wrong. You're sending args as a list or empty dict.\n"
            "FIX: pass args={\"req\": {...}} (a dict with the `req` key)."
        )
    if "not allowed" in err_lc or "outside allowed roots" in err_lc:
        return (
            "BUG: workspace_path is not in the server's allowed roots.\n"
            "FIX: set CLAUDE_MCP_ALLOWED_ROOTS=[\"/your/path\"] (JSON array) in the\n"
            "server's env, or pass a workspace_path inside Path.cwd() of the server process."
        )
    if "tool is not found" in err_lc or "mcp tool is not found" in err_lc:
        return (
            "BUG: Trae MCP registry stale.\n"
            "FIX: user must restart the MCP server in the Trae panel (not retry the call)."
        )
    if "not logged in" in err_lc or "/login" in err_lc or "result.*not logged in" in err_lc:
        return (
            "BUG: claude CLI auth not initialized for print mode (-p).\n"
            "FIX: user must run `claude login` interactively once. After that, the\n"
            "`claude_health` tool reports logged-in but `claude_run_task` may still fail\n"
            "if print-mode auth is missing — this is a known cli-side quirk."
        )
    if "tolerant_count" in err_lc or "requires_req_count" in err_lc:
        return (
            "Schema regression detected. Some tools no longer accept args={}.\n"
            "FIX: run claude_self_test to enumerate, then check the affected tool's signature."
        )
    return (
        f"No specific recipe for: {error!r}.\n"
        "General debug steps:\n"
        "1. Run claude_self_test(req={}) to check server health.\n"
        "2. Read the `claude_quickstart` prompt.\n"
        "3. Check the server's stderr for the actual exception."
    )


@mcp.tool(name=tool_name("self_test"))
def claude_self_test(req: ClaudeSelfTestRequestIn | None = None) -> ClaudeSelfTestResponse:
    """Inspect every registered tool's input schema and report robustness.

    This is a metadata-only check — no tools are actually invoked.

    Args shape:
        The MCP client MUST pass arguments wrapped in a `req` object:
            `{"req": {"include": ["claude_health"], "only_show_tolerant": true}}`
        For backwards-compatibility, the server also accepts `args={}`.
    """
    if req is None:
        req = ClaudeSelfTestRequest()
    
    tools_dict = {}
    if hasattr(mcp, "_local_provider") and hasattr(mcp._local_provider, "_components"):
        tools_dict = {
            v.name: v
            for k, v in mcp._local_provider._components.items()
            if k.startswith("tool:")
        }
    else:
        tool_manager = getattr(mcp, "_tool_manager", None) or getattr(mcp, "_tools", None)
        if tool_manager is None:
            raise RuntimeError("Cannot access FastMCP tool manager")
        if hasattr(tool_manager, "_tools"):
            tools_dict = tool_manager._tools
        else:
            raise RuntimeError(f"Unsupported tool manager: {type(tool_manager)}")
    
    reports: list[ClaudeToolSchemaReport] = []
    for name, tool in tools_dict.items():
        if hasattr(tool, "parameters"):
            schema = tool.parameters
        elif hasattr(tool, "input_schema"):
            schema = tool.input_schema
        else:
            import inspect
            sig = inspect.signature(tool)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            schema = {
                "required": [p.name for p in params if p.default is inspect.Parameter.empty],
                "properties": {p.name: {"type": "object"} for p in params},
            }
        
        required = schema.get("required", []) if isinstance(schema, dict) else []
        properties = list(schema.get("properties", {}).keys()) if isinstance(schema, dict) else []
        
        if req.include is not None:
            if not any(name.startswith(p) for p in req.include):
                continue
        if req.only_show_tolerant and required:
            continue
        
        reports.append(ClaudeToolSchemaReport(
            name=name,
            top_level_required=required,
            top_level_properties=properties,
            accepts_empty_args=len(required) == 0,
            requires_req_wrapper="req" in required,
        ))
    
    tolerant = sum(1 for r in reports if r.accepts_empty_args)
    requires_req = sum(1 for r in reports if r.requires_req_wrapper)
    
    return ClaudeSelfTestResponse(
        total_tools=len(reports),
        tolerant_count=tolerant,
        requires_req_count=requires_req,
        tools=reports,
        server_info={"name": "claude-code-cli-mcp", "version": "3.4.2"},
        summary=f"{len(reports)} tools inspected: {tolerant} tolerant to args={{}}, {requires_req} still require `req` wrapper",
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
