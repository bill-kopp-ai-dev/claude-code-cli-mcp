"""Integration tests checking the contract with settings, git, and persistence tools."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from claude_code_mcp.changes import git_changed_files, is_git_repo
from claude_code_mcp.settings import Settings


def _load_server(monkeypatch, *, persistence_base_dir: Path):
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_BASE_DIR", str(persistence_base_dir))
    monkeypatch.setenv("CLAUDE_MCP_PERSISTENCE_ENABLED", "true")

    import claude_code_mcp.claude_stream as claude_stream
    importlib.reload(claude_stream)

    import claude_code_mcp.claude_runner as claude_runner
    importlib.reload(claude_runner)

    import claude_code_mcp.server as server

    return importlib.reload(server)


def test_settings_load_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_MCP_MODE", raising=False)
    (tmp_path / ".env").write_text("CLAUDE_MCP_MODE=permissive\n", encoding="utf-8")

    settings = Settings()

    assert settings.mode == "permissive"


def test_git_repo_detection_works_from_subdirectories(tmp_path):
    repo_root = tmp_path / "repo"
    nested_workspace = repo_root / "nested"
    nested_workspace.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo_root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    changed_file = nested_workspace / "new_file.py"
    changed_file.write_text("print('ok')\n", encoding="utf-8")

    assert is_git_repo(nested_workspace) is True
    assert git_changed_files(nested_workspace) == ["nested/new_file.py"]


def test_persistence_init_creates_files_in_correct_dir(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, persistence_base_dir=tmp_path)

    from claude_code_mcp.models import ClaudeInitPersistenceRequest

    req = ClaudeInitPersistenceRequest(force=True, seed_templates=True)
    res = server.claude_init_persistence(req)

    # Base dir in response should match the namespace-derived persistence directory
    expected_dir = tmp_path / "claude-code"
    assert Path(res.base_dir) == expected_dir

    # Check files exist on disk
    assert (expected_dir / "AGENTS.md").exists()
    assert (expected_dir / "PROJECTS.md").exists()
    assert (expected_dir / "MEMORY.md").exists()


def test_load_persistence_context_returns_initialized(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, persistence_base_dir=tmp_path)

    from claude_code_mcp.models import ClaudeInitPersistenceRequest, ClaudeLoadPersistenceContextRequest

    # Initialize first
    server.claude_init_persistence(ClaudeInitPersistenceRequest(force=True, seed_templates=True))

    req = ClaudeLoadPersistenceContextRequest()
    res = server.claude_load_persistence_context(req)

    assert res.initialized is True
    assert res.agents_excerpt is not None
    assert "AGENTS" in res.agents_excerpt
    assert res.projects_excerpt is not None
    assert res.memory_excerpt is not None


def test_append_persistence_grows_file(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, persistence_base_dir=tmp_path)

    from claude_code_mcp.models import (
        ClaudeInitPersistenceRequest,
        ClaudeAppendPersistenceRequest,
        ClaudeReadPersistenceRequest,
    )

    # Initialize first
    server.claude_init_persistence(ClaudeInitPersistenceRequest(force=True, seed_templates=True))

    read_req = ClaudeReadPersistenceRequest(file="memory")
    before_res = server.claude_read_persistence(read_req)

    append_req = ClaudeAppendPersistenceRequest(file="memory", content="Appended text.", confirm=True)
    append_res = server.claude_append_persistence(append_req)

    assert append_res.appended_bytes > 0

    after_res = server.claude_read_persistence(read_req)
    assert after_res.size_bytes > before_res.size_bytes
    assert "Appended text." in after_res.content
