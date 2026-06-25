"""Security and argument validation tests."""

from __future__ import annotations

import importlib
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import threading

import pytest

from claude_code_mcp.claude_args import build_claude_argv
from claude_code_mcp.models import (
    ClaudeRunTaskRequest,
    ClaudeStartTaskRequest,
    ClaudePollTaskRequest,
    ClaudeCancelTaskRequest,
    ClaudeExecOptions,
)
from claude_code_mcp.settings import Settings


def _load_server(monkeypatch, *, mode: str, allowed_roots: str):
    monkeypatch.setenv("CLAUDE_MCP_MODE", mode)
    monkeypatch.setenv("CLAUDE_MCP_ALLOWED_ROOTS", allowed_roots)
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_ENABLED", "false")

    import claude_code_mcp.claude_stream as claude_stream
    importlib.reload(claude_stream)

    import claude_code_mcp.claude_runner as claude_runner
    importlib.reload(claude_runner)

    import claude_code_mcp.server as server

    return importlib.reload(server)


def test_safe_mode_blocks_env_and_extra_args(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, mode="safe", allowed_roots=f'["{tmp_path}"]')
    workspace = tmp_path

    req = ClaudeRunTaskRequest(
        workspace_path=str(workspace),
        prompt="OK",
        capture_changes=False,
    )
    req.options.env = {"X": "1"}
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        server.claude_run_task(req)

    req = ClaudeRunTaskRequest(
        workspace_path=str(workspace),
        prompt="OK",
        capture_changes=False,
    )
    req.options.extra_args = ["--log-file", "/tmp/x"]
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        server.claude_run_task(req)


def test_permissive_mode_enforces_allowlists(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_MCP_ALLOW_ENV_KEYS", '["FOO"]')
    monkeypatch.setenv("CLAUDE_MCP_ALLOW_EXTRA_ARGS", '["--sandbox"]')
    server = _load_server(monkeypatch, mode="permissive", allowed_roots=f'["{tmp_path}"]')

    req = ClaudeRunTaskRequest(
        workspace_path=str(tmp_path),
        prompt="OK",
        capture_changes=False,
    )
    req.options.env = {"BAR": "1"}
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        server.claude_run_task(req)

    req = ClaudeRunTaskRequest(
        workspace_path=str(tmp_path),
        prompt="OK",
        capture_changes=False,
    )
    req.options.extra_args = ["--unknown-flag"]
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        server.claude_run_task(req)


def test_bypassPermissions_rejected_in_safe_mode():
    settings = Settings(mode="safe")
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        permission_mode="bypassPermissions",
    )
    with pytest.raises(ValueError, match="NOT_ALLOWED|PERMISSION_MODE_NOT_ALLOWED"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )


def test_dontAsk_rejected_in_safe_mode():
    settings = Settings(mode="safe")
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        permission_mode="dontAsk",
    )
    with pytest.raises(ValueError, match="NOT_ALLOWED|PERMISSION_MODE_NOT_ALLOWED"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )


def test_bypassPermissions_requires_allowlist_in_permissive():
    # WITHOUT '--dangerously-skip-permissions' in allowed extra args
    settings = Settings(mode="permissive", allow_extra_args=set())
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        permission_mode="bypassPermissions",
    )
    with pytest.raises(ValueError, match="PERMISSION_MODE_NOT_ALLOWED"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )

    # WITH '--dangerously-skip-permissions' allowed
    settings_ok = Settings(mode="permissive", allow_extra_args={"--dangerously-skip-permissions"})
    argv = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings_ok,
    )
    assert "--permission-mode" in argv


def test_bare_is_always_present():
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK")

    settings_bare = Settings(force_bare=True)
    argv_bare = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings_bare,
    )
    assert "--bare" in argv_bare

    settings_no_bare = Settings(force_bare=False)
    argv_no_bare = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings_no_bare,
    )
    assert "--bare" not in argv_no_bare


def test_no_session_persistence_only_for_sync():
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK")
    settings = Settings()

    argv_sync = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings,
    )
    assert "--no-session-persistence" in argv_sync

    argv_async = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        settings=settings,
    )
    assert "--no-session-persistence" not in argv_async


def test_async_uses_stream_json():
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK")
    settings = Settings()

    argv_async = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        settings=settings,
    )
    assert "--output-format" in argv_async
    idx = argv_async.index("--output-format")
    assert argv_async[idx + 1] == "stream-json"
    assert "--verbose" in argv_async
    assert "--include-partial-messages" in argv_async

    argv_sync = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings,
    )
    assert "--output-format" in argv_sync
    idx_sync = argv_sync.index("--output-format")
    assert argv_sync[idx_sync + 1] == "json"


def test_session_id_passed_to_async():
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK")
    settings = Settings()

    argv_session = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        session_id="abc-123",
        settings=settings,
    )
    assert "--session-id" in argv_session
    idx = argv_session.index("--session-id")
    assert argv_session[idx + 1] == "abc-123"

    argv_no_session = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        session_id=None,
        settings=settings,
    )
    assert "--session-id" not in argv_no_session


def test_resume_session_id_passed():
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK", resume_session_id="xyz")
    settings = Settings()

    argv = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        settings=settings,
    )
    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "xyz"


def test_async_start_poll_cancel_flow(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, mode="safe", allowed_roots=f'["{tmp_path}"]')

    from claude_code_mcp.claude_runner import ClaudeRun
    from claude_code_mcp.claude_stream import ClaudeStreamParser

    def fake_start_async_run(*args, **kwargs):
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        parser = ClaudeStreamParser()
        t = threading.Thread(target=lambda: None)
        t.start()
        return ClaudeRun(
            run_id=str(uuid4()),
            workspace_path=kwargs.get("workspace_path", str(tmp_path)),
            argv=[],
            env={},
            started_at=datetime.now(timezone.utc),
            proc=proc,
            session_id=kwargs.get("session_id", "session-123"),
            parser=parser,
        )

    monkeypatch.setattr(server, "start_async_run", fake_start_async_run)

    started = server.claude_start_task(
        ClaudeStartTaskRequest(workspace_path=str(tmp_path), prompt="OK", capture_changes=False)
    )
    run_id = started.run_id

    pol = server.claude_poll_task(ClaudePollTaskRequest(run_id=run_id))
    assert pol.status == "running"

    canceled = server.claude_cancel_task(ClaudeCancelTaskRequest(run_id=run_id, force=False))
    assert canceled.canceled is True

    deadline = time.time() + 5
    while time.time() < deadline:
        pol = server.claude_poll_task(ClaudePollTaskRequest(run_id=run_id))
        if pol.status != "running":
            return
        time.sleep(0.1)

    assert False, "Task did not cancel within timeout"
