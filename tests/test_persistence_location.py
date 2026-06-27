"""Tests for Settings.resolve_persistence_base_dir() (Phase 4).

The feature introduces a ``persistence_location`` setting with values
``"global"`` (default) and ``"workspace"`` (parent of server's CWD), plus
a ``$cwd_parent`` escape hatch in ``persistence_base_dir``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture
def restore_cwd():
    """Snapshot and restore the current working directory around a test."""
    original = os.getcwd()
    try:
        yield
    finally:
        os.chdir(original)


def _make_settings(monkeypatch, **overrides):
    """Build a Settings instance with all env vars cleared, then apply overrides.

    pydantic-settings reads env vars AT INSTANTIATION. We clear relevant
    vars first, then set the ones we want.
    """
    for key in [
        "CLAUDE_MCP_PERSISTENCE_BASE_DIR",
        "CLAUDE_MCP_PERSISTENCE_LOCATION",
    ]:
        monkeypatch.delenv(key, raising=False)
    for k, v in overrides.items():
        monkeypatch.setenv(f"CLAUDE_MCP_{k.upper()}", v)
    from claude_code_mcp.settings import Settings

    return Settings()


# ------------------------------------------------------------------
# Default behavior (global)
# ------------------------------------------------------------------


def test_resolve_default_location_is_global():
    from claude_code_mcp.settings import Settings

    s = Settings()
    assert s.persistence_location == "global"


def test_resolve_default_base_dir_is_open_cli_router(monkeypatch):
    s = _make_settings(monkeypatch)
    resolved = s.resolve_persistence_base_dir()
    assert resolved == Path("~/.open-cli-router").expanduser()


def test_resolve_global_returns_explicit_base_dir(monkeypatch, tmp_path):
    custom = tmp_path / "my-data"
    s = _make_settings(
        monkeypatch,
        persistence_base_dir=str(custom),
        persistence_location="global",
    )
    resolved = s.resolve_persistence_base_dir()
    assert resolved == custom


# ------------------------------------------------------------------
# Workspace mode
# ------------------------------------------------------------------


def test_resolve_workspace_returns_cwd_parent(monkeypatch, tmp_path, restore_cwd):
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(monkeypatch, persistence_location="workspace")
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path / ".open-cli-router"


def test_resolve_workspace_ignores_persistence_base_dir(monkeypatch, tmp_path, restore_cwd):
    """When location=workspace, persistence_base_dir é ignorado (cwd_parent tem precedência)."""
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(
        monkeypatch,
        persistence_location="workspace",
        persistence_base_dir="/some/other/path",
    )
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path / ".open-cli-router"


def test_workspace_persistence_creates_files_in_cwd_parent(
    monkeypatch, tmp_path, restore_cwd
):
    """init() em modo workspace cria no cwd_parent (smoke test)."""
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    from claude_code_mcp.persistence import PersistenceStore

    s = _make_settings(monkeypatch, persistence_location="workspace")
    store = PersistenceStore(base_dir=s.resolve_persistence_base_dir())
    store.init()

    assert (tmp_path / ".open-cli-router" / "claude-code" / "AGENTS.md").exists()
    assert (tmp_path / ".open-cli-router" / "claude-code" / "MEMORY.md").exists()
    assert (tmp_path / ".open-cli-router" / "claude-code" / "PROJECTS.md").exists()
    assert (tmp_path / ".open-cli-router" / "claude-code" / ".initialized").exists()


def test_workspace_unwritable_fails_loudly(monkeypatch, tmp_path, restore_cwd):
    """Workspace em FS read-only → falha loudly."""
    import stat

    ro_dir = tmp_path / "read_only"
    ro_dir.mkdir()
    ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

    from claude_code_mcp.persistence import PersistenceStore

    store = PersistenceStore(base_dir=ro_dir / ".open-cli-router")
    try:
        with pytest.raises((ValueError, PermissionError, OSError)):
            store.init()
    finally:
        ro_dir.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


# ------------------------------------------------------------------
# $cwd_parent escape hatch
# ------------------------------------------------------------------


def test_resolve_cwd_parent_token_in_base_dir(monkeypatch, tmp_path, restore_cwd):
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(
        monkeypatch,
        persistence_base_dir="$cwd_parent/.my-custom",
    )
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path / ".my-custom"


def test_resolve_cwd_parent_token_with_workspace_location(monkeypatch, tmp_path, restore_cwd):
    """$cwd_parent funciona mesmo com location=workspace (token tem precedência)."""
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(
        monkeypatch,
        persistence_base_dir="$cwd_parent/.my-custom",
        persistence_location="workspace",
    )
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path / ".my-custom"


def test_cwd_parent_token_empty_suffix(monkeypatch, tmp_path, restore_cwd):
    """$cwd_parent sem sufixo = parent do CWD puro."""
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(monkeypatch, persistence_base_dir="$cwd_parent")
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path


def test_cwd_parent_token_with_path_separator(monkeypatch, tmp_path, restore_cwd):
    """$cwd_parent com / no início é normalizado."""
    fake_server_dir = tmp_path / "fake_server"
    fake_server_dir.mkdir()
    os.chdir(fake_server_dir)

    s = _make_settings(monkeypatch, persistence_base_dir="$cwd_parent/./data")
    resolved = s.resolve_persistence_base_dir()
    assert resolved == tmp_path / "data"


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


def test_invalid_location_raises_validation_error(monkeypatch):
    """persistence_location com valor inválido falha Pydantic ValidationError."""
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_LOCATION", "bogus")
    from claude_code_mcp.settings import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_default_location_unchanged_is_global():
    """Regressão: default é 'global' (não mudou comportamento padrão)."""
    from claude_code_mcp.settings import Settings

    s = Settings()
    assert s.persistence_location == "global"
    resolved = s.resolve_persistence_base_dir()
    assert resolved == Path("~/.open-cli-router").expanduser()


# ------------------------------------------------------------------
# Cross-mode isolation: dados globais não são tocados por workspace
# ------------------------------------------------------------------


def test_workspace_mode_does_not_touch_global_data(monkeypatch, tmp_path, restore_cwd):
    """Mudar para workspace NÃO apaga ~/.open-cli-router/claude-code/ existente."""
    global_dir = tmp_path / "global_data"
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_BASE_DIR", str(global_dir))

    from claude_code_mcp.persistence import PersistenceStore
    from claude_code_mcp.settings import Settings

    s_global = Settings()
    store_global = PersistenceStore(base_dir=s_global.resolve_persistence_base_dir())
    store_global.init()
    assert (global_dir / "claude-code" / "MEMORY.md").exists()

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "fake_server").mkdir()
    os.chdir(workspace_root / "fake_server")
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_LOCATION", "workspace")

    s_workspace = Settings()
    store_workspace = PersistenceStore(
        base_dir=s_workspace.resolve_persistence_base_dir()
    )
    store_workspace.init()

    assert (global_dir / "claude-code" / "MEMORY.md").exists()
    assert (workspace_root / ".open-cli-router" / "claude-code" / "MEMORY.md").exists()
    global_content = (global_dir / "claude-code" / "MEMORY.md").read_text()
    workspace_content = (workspace_root / ".open-cli-router" / "claude-code" / "MEMORY.md").read_text()
    assert global_content == workspace_content


# ------------------------------------------------------------------
# Settings integração: backup_keep e truncation_head_ratio
# ------------------------------------------------------------------


def test_backup_keep_default_is_10():
    from claude_code_mcp.settings import Settings

    s = Settings()
    assert s.persistence_backup_keep == 10


def test_truncation_head_ratio_default_is_0_2():
    from claude_code_mcp.settings import Settings

    s = Settings()
    assert s.persistence_truncation_head_ratio == 0.2


def test_backup_keep_propagates_to_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_BACKUP_KEEP", "5")
    from claude_code_mcp.settings import Settings
    from claude_code_mcp.persistence import PersistenceStore

    s = Settings()
    store = PersistenceStore(
        base_dir=s.resolve_persistence_base_dir(),
        backup_keep=s.persistence_backup_keep,
    )
    assert store._backup_keep == 5


def test_truncation_head_ratio_propagates_to_store(monkeypatch):
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_TRUNCATION_HEAD_RATIO", "0.5")
    from claude_code_mcp.settings import Settings
    from claude_code_mcp.persistence import PersistenceStore

    s = Settings()
    store = PersistenceStore(
        base_dir=s.resolve_persistence_base_dir(),
        head_ratio=s.persistence_truncation_head_ratio,
    )
    assert store._head_ratio == 0.5


# ------------------------------------------------------------------
# Phase 5: max_file_bytes default alignment with agy (512 KiB)
# ------------------------------------------------------------------


def test_default_max_file_bytes_is_512kib():
    """Phase 5: padronizado em 512 KiB (era 1 MiB antes desta refatoração)."""
    from claude_code_mcp.settings import Settings

    s = Settings()
    assert s.persistence_max_file_bytes == 524_288
