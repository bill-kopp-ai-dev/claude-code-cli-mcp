"""Settings for the Claude Code CLI MCP server.

Environment prefix: ``CLAUDE_MCP_``. Forked from ``agy_mcp_server.settings``
with the following Claude-specific adaptations:

- ``agy_path`` -> ``claude_path`` plus a ``claude_path_fallbacks`` list.
- ``default_permission_mode`` (default ``acceptEdits``) drives the
  ``--permission-mode`` flag when the request does not set one.
- ``force_bare`` is True by default — ``--bare`` is always passed to the
  child ``claude`` process to disable workspace customizations.
- ``max_concurrent_runs`` (default 10) enforces an upper bound on
  in-flight ``claude_start_task`` invocations.
- ``allowed_models`` is a free-form allowlist (``{"sonnet","opus"}``
  defaults). Empty set means "any model".
- ``AGY_MCP_*`` env vars renamed to ``CLAUDE_MCP_*``.
- The Antigravity-specific ``fix_antigravity_mcp_config`` /
  ``antigravity_mcp_config_path`` settings are dropped — ``~/.claude.json``
  is treated as immutable.
- ``logfire_token`` added for optional observability via Logfire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Workspace & mode ----
    allowed_roots: list[Path] = Field(default_factory=list)
    mode: Literal["safe", "permissive"] = "safe"

    # ---- Binary discovery ----
    claude_path: str = "claude"
    claude_path_fallbacks: list[str] = Field(
        default_factory=lambda: [
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            "~/.local/bin/claude",
        ]
    )

    # ---- Hardening ----
    force_bare: bool = True
    force_sandbox_in_safe_mode: bool = True
    default_permission_mode: str = "acceptEdits"
    force_default_permission_mode: bool = False  # if True, ignore request permission_mode and use default

    # ---- Limits ----
    default_timeout_s: int = 600
    poll_default_wait_seconds: float = 0.5
    max_concurrent_runs: int = 10
    max_runs: int = 50
    max_stdout_bytes: int = 1_000_000  # 1 MB
    max_stderr_bytes: int = 200_000
    snapshot_max_file_bytes: int = 512_000

    # ---- Allowlists (permissive mode) ----
    allowed_models: set[str] = Field(default_factory=lambda: {"sonnet", "opus"})
    allow_env_keys: set[str] = Field(default_factory=set)
    allow_extra_args: set[str] = Field(default_factory=set)

    # ---- Snapshot ignore ----
    ignore_dir_names: set[str] = Field(
        default_factory=lambda: {".git", ".claude", ".venv", "__pycache__", ".antigravitycli"}
    )

    # ---- Observability ----
    logfire_token: str | None = None

    # ---- Persistence ----
    persistence_enabled: bool = True
    persistence_base_dir: Path = Field(
        default_factory=lambda: Path("~/.open-cli-router").expanduser()
    )
    persistence_max_file_bytes: int = 1_048_576  # 1 MiB
    persistence_backup_on_write: bool = False
    persistence_seed_templates: bool = True

    def resolved_allowed_roots(self) -> list[Path]:
        if self.allowed_roots:
            return [p.expanduser().resolve() for p in self.allowed_roots]
        return [Path.cwd().resolve()]

    def resolve_claude_path(self) -> str:
        """Resolve the ``claude`` binary path, checking fallbacks."""
        import shutil

        resolved = shutil.which(self.claude_path)
        if resolved is not None:
            return resolved

        for fb in self.claude_path_fallbacks:
            candidate = shutil.which(str(Path(fb).expanduser()))
            if candidate is not None:
                return candidate

        # Nothing found; return the original (caller raises CLAUDE_NOT_FOUND).
        return self.claude_path