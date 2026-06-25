"""Provider prefix configuration for the MCP server.

This module is the SINGLE source of truth for the provider prefix used in
tool names, prompts, and any other user-visible identifier.

Forked from ``agy_mcp_server.provider`` (v0.0.1 of antigravity-cli-mcp).
Changing ``PROVIDER_PREFIX`` here is the only edit needed to re-skin the
entire server for another CLI backend.

Example:
    Current server (Claude Code CLI):

        >>> from claude_code_mcp.provider import PROVIDER_PREFIX, tool_name
        >>> PROVIDER_PREFIX
        'claude'
        >>> tool_name("health")
        'claude_health'
"""

from __future__ import annotations

# Single source of truth. Change this when forking the server for another CLI.
PROVIDER_PREFIX: str = "claude"

# Persistence namespace. Independent of PROVIDER_PREFIX because the on-disk
# directory layout (~/.open-cli-router/<namespace>/) is intentionally
# human-readable, while the MCP wire format uses the short prefix.
#
# Forks:
#   antigravity-cli-mcp: "agy"
#   claude-code-cli-mcp:  "claude-code"
#   codex-cli-mcp (next): "codex"
PERSISTENCE_NAMESPACE: str = "claude-code"

# Convention: tool names are always ``{PROVIDER_PREFIX}_{suffix}``.
# Keep suffix in snake_case.
_NAME_SEPARATOR: str = "_"


def tool_name(suffix: str) -> str:
    """Build a standardized MCP tool name for this provider.

    Args:
        suffix: The tool's local name (e.g., ``"health"``, ``"run_task"``).
                Must be non-empty and contain only snake_case characters.

    Returns:
        The full MCP tool name (e.g., ``"claude_health"``).

    Raises:
        ValueError: If ``suffix`` is empty or contains characters other than
                    lowercase ASCII letters, digits, or underscores.
    """
    if not suffix:
        raise ValueError("PROVIDER_PREFIX_REQUIRED: tool suffix cannot be empty")

    normalized = suffix.strip().lower()
    if not normalized.replace("_", "").isalnum() or not normalized.replace(
        "_", ""
    ).isascii():
        raise ValueError(
            f"INVALID_TOOL_SUFFIX: suffix must be snake_case ASCII, got {suffix!r}"
        )

    return f"{PROVIDER_PREFIX}{_NAME_SEPARATOR}{normalized}"


def prompt_name(suffix: str) -> str:
    """Build a standardized MCP prompt name for this provider.

    Mirrors :func:`tool_name` so prompt names follow the same convention.
    """
    return tool_name(suffix)