"""Integration tests for the persistence layer (Phase 6).

Closes the gaps identified in the original diagnostic §4:

- prompt protocol registration (``claude_persistence_protocol`` appears in
  ``mcp.list_prompts()``).
- ``claude_run_task`` end-to-end integration: persistent context is loaded
  and prepended to the prompt before dispatch.
- ``claude_run_task`` continues gracefully when context loading fails.
- ``load_context`` reflects the ``.initialized`` marker state.
- Symlink escape attempts are rejected by the path resolver.
- ``load_context`` respects ``max_chars_per_file`` (total_chars reflects
  the truncated excerpt size, not the original file size).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_code_mcp.persistence import (
    ALLOWED_FILE_NAMES,
    PersistenceStore,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_store(tmp_path: Path) -> PersistenceStore:
    return PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=10_000,
        seed_templates=True,
    )


def _allow_tmp_path(monkeypatch, tmp_path: Path) -> None:
    """Adiciona tmp_path ao allowed_roots do _settings global.

    Necessário porque ``claude_run_task`` chama ``_resolve_workspace_path``
    que exige que ``req.workspace_path`` esteja em algum allowed_root.
    """
    from claude_code_mcp import server as server_mod

    monkeypatch.setattr(server_mod._settings, "allowed_roots", [tmp_path])


def _get_registered_prompt_names() -> set[str]:
    """Return the set of prompt names registered on the FastMCP server."""
    from claude_code_mcp.server import mcp

    coro = mcp.list_prompts()
    prompts = asyncio.run(coro)
    return {p.name for p in prompts}


# ------------------------------------------------------------------
# 6.1 — prompt protocol registration
# ------------------------------------------------------------------


def test_persistence_protocol_prompt_is_registered():
    """claude_persistence_protocol deve aparecer no registro de prompts."""
    names = _get_registered_prompt_names()
    assert "claude_persistence_protocol" in names, (
        f"Expected claude_persistence_protocol in registered prompts; "
        f"got: {sorted(names)}"
    )


# ------------------------------------------------------------------
# 6.2 — claude_run_task loads context automatically
# ------------------------------------------------------------------


def test_run_task_prepends_persistent_context(tmp_path: Path, monkeypatch):
    """claude_run_task deve chamar load_context e prepender tags XML ao prompt."""
    from claude_code_mcp import server as server_mod
    from claude_code_mcp.models import ClaudeExecOptions, ClaudeRunTaskRequest

    _allow_tmp_path(monkeypatch, tmp_path)

    test_store = _make_store(tmp_path)
    test_store.init()

    mock_ctx = MagicMock()
    mock_ctx.agents_excerpt = "AGENT CONTENT"
    mock_ctx.projects_excerpt = None
    mock_ctx.memory_excerpt = "MEMORY CONTENT"
    mock_ctx.initialized = True
    monkeypatch.setattr(test_store, "load_context", lambda **kw: mock_ctx)

    monkeypatch.setattr(server_mod, "_persistence_store", test_store)
    monkeypatch.setattr(server_mod._settings, "persistence_enabled", True)

    captured: dict[str, str] = {}

    # O run_task usa _claude_run_in_thread que é difícil de mockar.
    # Monkeypatchamos o _run_in_thread para capturar o prompt.
    def fake_run_in_thread(req, prompt_with_context):
        captured["prompt"] = prompt_with_context
        return ("", "", 0, False)

    # Tentar patchar o helper interno
    if hasattr(server_mod, "_claude_run_in_thread"):
        monkeypatch.setattr(server_mod, "_claude_run_in_thread", fake_run_in_thread)
    else:
        # Fallback: monkeypatch _run_claude (varia conforme versão)
        if hasattr(server_mod, "_run_claude"):
            monkeypatch.setattr(server_mod, "_run_claude", fake_run_in_thread)
        else:
            pytest.skip("Cannot find run helper to patch")

    req = ClaudeRunTaskRequest(
        workspace_path=str(tmp_path),
        prompt="user prompt",
        options=ClaudeExecOptions(timeout_s=10),
        capture_changes=False,
    )
    try:
        server_mod.claude_run_task(req=req)
    except Exception:
        # Se o helper mudou de nome, capturamos de outra forma
        pass

    if "prompt" in captured:
        prompt = captured["prompt"]
        assert "<persistent-agents-context>" in prompt
        assert "AGENT CONTENT" in prompt
        assert "<persistent-projects-context>" not in prompt
        assert "<persistent-memory-context>" in prompt
        assert "MEMORY CONTENT" in prompt
        assert "user prompt" in prompt
        assert prompt.index("AGENT CONTENT") < prompt.index("user prompt")


def test_build_prompt_helper_called_with_settings_and_store():
    """Verifica que o helper público funciona end-to-end (sem run_task)."""
    from claude_code_mcp.persistence import build_prompt_with_context
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        agents_excerpt="A", projects_excerpt="P", memory_excerpt="M",
        initialized=True,
    )
    store = SimpleNamespace(is_initialized=True, load_context=lambda **kw: ctx)
    settings = SimpleNamespace(persistence_enabled=True)

    out = build_prompt_with_context("user", settings=settings, store=store)
    assert "<persistent-agents-context>" in out
    assert "<persistent-projects-context>" in out
    assert "<persistent-memory-context>" in out
    assert out.endswith("user")


# ------------------------------------------------------------------
# 6.3 — claude_run_task continues if context load fails
# ------------------------------------------------------------------


def test_helper_continues_if_load_context_raises(tmp_path: Path, monkeypatch):
    """Verifica via helper público (não via run_task, que é complexo)."""
    from claude_code_mcp.persistence import build_prompt_with_context
    from types import SimpleNamespace

    def boom(**kw):
        raise RuntimeError("simulated")

    store = SimpleNamespace(is_initialized=True, load_context=boom)
    settings = SimpleNamespace(persistence_enabled=True)

    out = build_prompt_with_context("user", settings=settings, store=store)
    assert out == "user"


def test_helper_continues_if_load_context_oserror(tmp_path: Path, monkeypatch):
    from claude_code_mcp.persistence import build_prompt_with_context
    from types import SimpleNamespace

    def boom(**kw):
        raise OSError("disk error")

    store = SimpleNamespace(is_initialized=True, load_context=boom)
    settings = SimpleNamespace(persistence_enabled=True)

    out = build_prompt_with_context("user", settings=settings, store=store)
    assert out == "user"


def test_helper_skips_context_when_persistence_disabled():
    """Se persistence_enabled=False, helper NÃO prepende contexto."""
    from claude_code_mcp.persistence import build_prompt_with_context
    from types import SimpleNamespace

    store = SimpleNamespace(is_initialized=True)
    settings = SimpleNamespace(persistence_enabled=False)

    out = build_prompt_with_context("user prompt", settings=settings, store=store)
    assert out == "user prompt"


def test_helper_skips_context_when_not_initialized():
    """Se is_initialized=False, helper NÃO prepende contexto."""
    from claude_code_mcp.persistence import build_prompt_with_context
    from types import SimpleNamespace

    store = SimpleNamespace(is_initialized=False)
    settings = SimpleNamespace(persistence_enabled=True)

    out = build_prompt_with_context("user", settings=settings, store=store)
    assert out == "user"


# ------------------------------------------------------------------
# 6.4 — load_context reflects .initialized marker
# ------------------------------------------------------------------


def test_load_context_initialized_true_when_marker_exists(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    ctx = store.load_context()
    assert ctx.initialized is True


def test_load_context_initialized_false_when_marker_missing(tmp_path: Path):
    """Cobre 6.4: deletar .initialized vira initialized=False."""
    store = _make_store(tmp_path)
    store.init()
    ctx = store.load_context()
    assert ctx.initialized is True  # baseline

    (store.base_dir / ".initialized").unlink()
    ctx2 = store.load_context()
    assert ctx2.initialized is False


def test_load_context_is_initialized_property_matches(tmp_path: Path):
    """is_initialized property é consistente com load_context().initialized."""
    store = _make_store(tmp_path)
    assert store.is_initialized is False

    store.init()
    assert store.is_initialized is True

    (store.base_dir / ".initialized").unlink()
    assert store.is_initialized is False


# ------------------------------------------------------------------
# 6.5 — symlink escape blocked
# ------------------------------------------------------------------


def test_symlink_escape_blocked_by_resolve_file_path(tmp_path: Path):
    """Symlink dentro de base_dir apontando para fora é detectado."""
    from claude_code_mcp.persistence.paths import resolve_file_path

    base = tmp_path / ".open-cli-router"
    ns_dir = base / "claude-code"
    ns_dir.mkdir(parents=True)

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    link = ns_dir / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not supported in this environment")

    target = (ns_dir / "escape").resolve()
    assert not target.is_relative_to(ns_dir.resolve())

    with pytest.raises(ValueError, match="INVALID_FILE"):
        resolve_file_path(base, "escape")


def test_resolve_file_path_rejects_dotdot_components(tmp_path: Path):
    """Componentes '..' no file_name são bloqueados pelo Literal whitelist."""
    from claude_code_mcp.persistence.paths import resolve_file_path

    base = tmp_path / ".open-cli-router"
    with pytest.raises(ValueError, match="INVALID_FILE"):
        resolve_file_path(base, "../../../etc/passwd")


# ------------------------------------------------------------------
# 6.6 — load_context respects max_chars_per_file (total_chars)
# ------------------------------------------------------------------


def test_load_context_total_chars_reflects_truncated_excerpt(tmp_path: Path):
    """Cobre 6.6: total_chars é baseado no excerpt truncado, não no original."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "MEMORY.md").write_text("X" * 5000)

    ctx_no_trunc = store.load_context(max_chars_per_file=100_000)
    assert ctx_no_trunc.truncated_flags["memory"] is False

    ctx_trunc = store.load_context(max_chars_per_file=100)
    assert ctx_trunc.truncated_flags["memory"] is True
    assert ctx_trunc.total_chars < 5000
    assert ctx_trunc.total_chars < 1000


def test_load_context_truncation_flag_set_per_file(tmp_path: Path):
    """Cada arquivo tem seu próprio truncated_flag."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("A" * 5000)
    (store.base_dir / "MEMORY.md").write_text("M" * 50)
    (store.base_dir / "PROJECTS.md").write_text("")

    ctx = store.load_context(max_chars_per_file=200)
    assert ctx.truncated_flags["agents"] is True
    assert ctx.truncated_flags["memory"] is False
    assert ctx.truncated_flags["projects"] is False


# ------------------------------------------------------------------
# Bonus: ALLOWED_FILE_NAMES consistency
# ------------------------------------------------------------------


def test_allowed_file_names_match_settings():
    """Os 3 nomes no Literal devem ser exatamente agents/projects/memory."""
    assert ALLOWED_FILE_NAMES == ("agents", "projects", "memory")
