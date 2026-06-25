"""Tests for the provider prefix standardization module."""

from __future__ import annotations

import pytest

from claude_code_mcp.provider import (
    PROVIDER_PREFIX,
    PERSISTENCE_NAMESPACE,
    prompt_name,
    tool_name,
)


def test_provider_prefix_is_claude():
    """The provider prefix must be the Claude CLI short name."""
    assert PROVIDER_PREFIX == "claude"


def test_tool_name_basic():
    """Tool names follow the pattern `{prefix}_{suffix}`."""
    assert tool_name("health") == "claude_health"
    assert tool_name("run_task") == "claude_run_task"
    assert tool_name("init_persistence") == "claude_init_persistence"


def test_tool_name_uppercase_suffix_normalized():
    """Suffixes are lowercased to keep tool names stable across callers."""
    assert tool_name("HEALTH") == "claude_health"
    assert tool_name("Run_Task") == "claude_run_task"


def test_tool_name_strips_whitespace():
    """Leading/trailing whitespace is removed before building the name."""
    assert tool_name("  health  ") == "claude_health"


def test_tool_name_empty_suffix_raises():
    """An empty suffix must fail loudly, never produce a bare prefix."""
    with pytest.raises(ValueError):
        tool_name("")


def test_tool_name_invalid_chars_raise():
    """Suffixes with non-snake_case characters must fail."""
    for bad in ["foo bar", "foo-bar", "foo.bar", "foo/bar", "ação"]:
        with pytest.raises(ValueError):
            tool_name(bad)


def test_prompt_name_matches_tool_name():
    """Prompt names follow the same convention as tool names."""
    assert prompt_name("sync_orchestration") == tool_name("sync_orchestration")


def test_all_registered_tools_use_standardized_naming():
    """All tools registered on the MCP server follow the `{prefix}_*` naming."""
    import asyncio

    from claude_code_mcp.server import mcp

    async def _list():
        return await mcp.list_tools()

    tools = asyncio.run(_list())
    assert len(tools) >= 5, f"Expected at least 5 tools, found {len(tools)}"
    for t in tools:
        assert t.name.startswith(f"{PROVIDER_PREFIX}_"), (
            f"Tool {t.name!r} does not follow the {PROVIDER_PREFIX}_* naming"
        )


def test_forking_provider_prefix_renames_all_tools(monkeypatch):
    """Simulate a fork: changing PROVIDER_PREFIX renames every tool."""
    import claude_code_mcp.provider as provider_mod

    monkeypatch.setattr(provider_mod, "PROVIDER_PREFIX", "x")

    assert provider_mod.tool_name("health") == "x_health"
    assert provider_mod.tool_name("run_task") == "x_run_task"
    assert provider_mod.tool_name("init_persistence") == "x_init_persistence"


def test_persistence_namespace_constant():
    """Verify PERSISTENCE_NAMESPACE is "claude-code"."""
    assert PERSISTENCE_NAMESPACE == "claude-code"


def test_persistence_namespace_independent_of_prefix(monkeypatch):
    """Verify namespace is independent of prefix."""
    import claude_code_mcp.provider as provider_mod
    monkeypatch.setattr(provider_mod, "PROVIDER_PREFIX", "x")
    assert provider_mod.PERSISTENCE_NAMESPACE == "claude-code"


def test_tool_name_does_not_include_namespace(monkeypatch):
    """Verify that tool name doesn't include the persistence namespace, only prefix."""
    import claude_code_mcp.provider as provider_mod
    monkeypatch.setattr(provider_mod, "PROVIDER_PREFIX", "x")
    assert provider_mod.tool_name("health") == "x_health"
