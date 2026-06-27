"""Persistent memory layer for the MCP server.

This package provides file-based persistence at
``~/.open-cli-router/{PERSISTENCE_NAMESPACE}/`` for three editable markdown
files:

- ``AGENTS.md`` — editable system prompt
- ``PROJECTS.md`` — project summaries
- ``MEMORY.md`` — permanent memory updated after each session

The namespace (``claude-code``, ``agy``, ``codex-cli``) is derived from
``PERSISTENCE_NAMESPACE`` in :mod:`claude_code_mcp.provider`, so forking
the server to another CLI provider automatically remaps the persistence
directory.

The :func:`build_prompt_with_context` helper is the single source of
truth for injecting persistent context into user prompts (used by both
``claude_run_task`` and ``claude_start_task``).

Forked from agy_mcp_server.persistence v0.0.1.
"""

from __future__ import annotations

from claude_code_mcp.persistence.context import build_prompt_with_context
from claude_code_mcp.persistence.paths import (
    ALLOWED_FILE_NAMES,
    PersistenceFileName,
    get_persistence_base_dir,
    get_persistence_dir,
    resolve_file_path,
)
from claude_code_mcp.persistence.store import PersistenceStore

__all__ = [
    "ALLOWED_FILE_NAMES",
    "PersistenceFileName",
    "PersistenceStore",
    "build_prompt_with_context",
    "get_persistence_base_dir",
    "get_persistence_dir",
    "resolve_file_path",
]
