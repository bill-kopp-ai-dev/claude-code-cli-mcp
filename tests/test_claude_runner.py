"""Tests for the Claude subprocess runner and NDJSON stream parser."""

from __future__ import annotations

import pytest

from claude_code_mcp.claude_args import build_claude_argv, build_child_env
from claude_code_mcp.claude_stream import ClaudeStreamParser, RollingLineBuffer
from claude_code_mcp.models import ClaudeRunTaskRequest, ClaudeExecOptions
from claude_code_mcp.settings import Settings


def test_stream_parser_handles_blank_lines():
    parser = ClaudeStreamParser()
    assert parser.feed_line("") is None
    assert parser.feed_line("   ") is None
    assert parser.feed_line("\n") is None
    assert not parser.errors


def test_stream_parser_handles_invalid_json():
    parser = ClaudeStreamParser()
    evt = parser.feed_line("not json")
    assert evt is not None
    assert evt.type == "unknown"
    assert len(parser.errors) == 1
    assert "JSONDecodeError" in parser.errors[0]


def test_stream_parser_classifies_events():
    parser = ClaudeStreamParser()

    # System event
    evt1 = parser.feed_line('{"type":"system","message":"sys init"}')
    assert evt1 is not None
    assert evt1.type == "system"
    assert len(parser.messages) == 1

    # User event
    evt2 = parser.feed_line('{"type":"user","message":"hi"}')
    assert evt2 is not None
    assert evt2.type == "user"
    assert len(parser.messages) == 2

    # Assistant event
    evt3 = parser.feed_line('{"type":"assistant","message":"hello"}')
    assert evt3 is not None
    assert evt3.type == "assistant"
    assert len(parser.messages) == 3

    # Result event
    evt4 = parser.feed_line(
        '{"type":"result","session_id":"abc","total_cost_usd":0.01,"num_turns":1,"duration_ms":100,"result":"hi","modelUsage":{"sonnet":1}}'
    )
    assert evt4 is not None
    assert evt4.type == "result"
    assert parser.result_event is not None
    assert parser.result_event["session_id"] == "abc"
    assert len(parser.messages) == 4


def test_stream_parser_extracts_text_delta():
    parser = ClaudeStreamParser()

    # Case 1: delta at the top level
    evt1 = parser.feed_line('{"type":"stream_event","delta":{"text":"hello"}}')
    assert evt1 is not None
    assert evt1.type == "assistant"
    assert evt1.text_delta == "hello"
    assert parser.partial_text == "hello"

    # Case 2: delta inside nested event object
    evt2 = parser.feed_line('{"type":"stream_event","event":{"delta":{"text":" world"}}}')
    assert evt2 is not None
    assert evt2.type == "assistant"
    assert evt2.text_delta == " world"
    assert parser.partial_text == "hello world"


def test_rolling_line_buffer_splits_correctly():
    buf = RollingLineBuffer()

    # Feed chunk without newline -> held
    assert buf.feed("abc") == []
    assert buf.get_remaining() == "abc"

    # Feed part with newline -> splits
    assert buf.feed("d\nef\n") == ["abcd", "ef"]
    assert buf.get_remaining() == ""

    # Feed multiple newlines
    assert buf.feed("g\nh\ni") == ["g", "h"]
    assert buf.get_remaining() == "i"

    # Finish remaining
    assert buf.feed("j\n") == ["ij"]
    assert buf.get_remaining() == ""


def test_build_argv_safe_mode_rejects_dontAsk():
    settings = Settings(mode="safe")
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        permission_mode="dontAsk",
    )
    with pytest.raises(ValueError, match="dontAsk"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )


def test_build_argv_model_allowlist_enforced():
    settings = Settings(allowed_models={"sonnet", "opus"})
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        model="haiku",
    )
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )


def test_fallback_model_passes_through():
    """When fallback_model is set, --fallback-model appears in argv (sync and async)."""
    settings = Settings()
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        fallback_model="opus",
    )
    argv_sync = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings,
    )
    assert "--fallback-model" in argv_sync
    assert argv_sync[argv_sync.index("--fallback-model") + 1] == "opus"

    argv_async = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="async",
        session_id="sess-1",
        settings=settings,
    )
    assert "--fallback-model" in argv_async
    assert argv_async[argv_async.index("--fallback-model") + 1] == "opus"


def test_fallback_model_omitted_by_default():
    settings = Settings()
    req = ClaudeRunTaskRequest(workspace_path=".", prompt="OK")
    argv = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings,
    )
    assert "--fallback-model" not in argv
    assert "--model" not in argv


def test_fallback_model_allowlist_enforced():
    """fallback_model is checked against allowed_models like model."""
    settings = Settings(allowed_models={"sonnet"})
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        model="sonnet",
        fallback_model="haiku",
    )
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        build_claude_argv(
            claude_path="claude",
            workspace_path=".",
            request=req,
            mode="sync",
            settings=settings,
        )


def test_build_argv_passes_extra_args():
    settings = Settings(allow_extra_args={"--foo"})
    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="OK",
        options=ClaudeExecOptions(extra_args=["--foo", "bar"]),
    )
    argv = build_claude_argv(
        claude_path="claude",
        workspace_path=".",
        request=req,
        mode="sync",
        settings=settings,
    )
    assert argv[-2:] == ["--foo", "bar"]


def test_build_child_env_always_includes_hardening():
    # Safe mode env checks
    settings_safe = Settings(mode="safe")
    env_safe = build_child_env(None, settings_safe)

    # General hardening vars
    assert env_safe["CLAUDE_CODE_HIDE_ACCOUNT_INFO"] == "1"
    assert env_safe["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env_safe["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    # Safe-mode specific vars
    assert env_safe["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env_safe["CLAUDE_CODE_DISABLE_CRON"] == "1"
    assert env_safe["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env_safe

    # Permissive mode env checks
    settings_permissive = Settings(mode="permissive")
    env_perm = build_child_env(None, settings_permissive)

    # General hardening vars still present
    assert env_perm["CLAUDE_CODE_HIDE_ACCOUNT_INFO"] == "1"
    assert env_perm["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env_perm["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    # Permissive-mode specific vars
    assert env_perm["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in env_perm
    assert "CLAUDE_CODE_DISABLE_CRON" not in env_perm
    assert "CLAUDE_CODE_DISABLE_TERMINAL_TITLE" not in env_perm


def test_stdin_set_to_none_after_finally(monkeypatch):
    """Regression test for commit 87933e5: proc.stdin must be set to None
    in the finally block, not just closed. Otherwise FastMCP wrapper re-reads
    the closed stream and returns parse_error: 'Expecting value'.
    """
    from unittest.mock import MagicMock, patch
    from claude_code_mcp.claude_runner import start_sync_run
    from claude_code_mcp.models import ClaudeRunTaskRequest
    from claude_code_mcp.settings import Settings

    # Create a mock stdin that we can verify is set to None
    mock_stdin = MagicMock()
    mock_proc = MagicMock()
    mock_proc.stdin = mock_stdin
    # Simulate that Popen returns this mocked process
    mock_proc.pid = 12345

    req = ClaudeRunTaskRequest(
        workspace_path=".",
        prompt="hello",
    )
    settings = Settings()

    # Mock the subprocess.Popen call
    with patch("subprocess.Popen", return_value=mock_proc):
        result = start_sync_run(
            claude_path="claude",
            workspace_path=".",
            request=req,
            settings=settings,
        )

    # The critical assertion: stdin was set to None in finally
    assert mock_proc.stdin is None, (
        "REGRESSION: proc.stdin was not set to None in finally block. "
        "This will cause FastMCP to re-read a closed stdin stream and "
        "return parse_error: 'Expecting value'."
    )

