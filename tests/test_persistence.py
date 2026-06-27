"""Tests for the persistence layer."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from claude_code_mcp.persistence import (
    ALLOWED_FILE_NAMES,
    PersistenceStore,
    get_persistence_dir,
    resolve_file_path,
)


# ------------------------------------------------------------------
# paths
# ------------------------------------------------------------------


def test_persistence_dir_uses_namespace(tmp_path: Path):
    base = tmp_path / ".open-cli-router"
    d = get_persistence_dir(base)
    # PERSISTENCE_NAMESPACE is "claude-code" by default.
    assert d == base / "claude-code"
    assert d.name == "claude-code"


def test_provider_namespace_uses_namespace_constant(monkeypatch, tmp_path: Path):
    import claude_code_mcp.provider as provider_mod
    monkeypatch.setattr(provider_mod, "PERSISTENCE_NAMESPACE", "x")

    base = tmp_path / ".open-cli-router"
    d = get_persistence_dir(base)
    assert d == base / "x"
    assert d.name == "x"


def test_resolve_file_path_accepts_allowed_names(tmp_path: Path):
    base = tmp_path / ".open-cli-router"
    for name in ALLOWED_FILE_NAMES:
        p = resolve_file_path(base, name)
        assert p.suffix == ".md"
        assert p.name == f"{name.upper()}.md"


def test_resolve_file_path_rejects_unknown_name(tmp_path: Path):
    base = tmp_path / ".open-cli-router"
    with pytest.raises(ValueError, match="INVALID_FILE"):
        resolve_file_path(base, "secrets")


def test_resolve_file_path_rejects_traversal(tmp_path: Path):
    base = tmp_path / ".open-cli-router"
    with pytest.raises(ValueError, match="INVALID_FILE"):
        resolve_file_path(base, "../../etc/passwd")


# ------------------------------------------------------------------
# Phase 1: AGENTS template references real namespace (not PROVIDER_PREFIX)
# ------------------------------------------------------------------


def test_agents_template_references_real_namespace():
    """Bug A1: AGENTS template deve usar PERSISTENCE_NAMESPACE (não PROVIDER_PREFIX).

    PROVIDER_PREFIX="claude" é wire-format; PERSISTENCE_NAMESPACE="claude-code"
    é o path real. Apontar o agente para o path errado ensina-o a violar
    segurança do diretório real.
    """
    from claude_code_mcp.persistence.templates import render_agents_template
    from claude_code_mcp.provider import PROVIDER_PREFIX, PERSISTENCE_NAMESPACE

    # Sanity: garante que namespace e prefix realmente divergem
    # (se a próxima assertion falhar, o bug foi corrigido em provider.py
    # sem atualizar o template — investigar).
    assert PERSISTENCE_NAMESPACE != PROVIDER_PREFIX

    text = render_agents_template(PROVIDER_PREFIX)

    # Path correto
    assert f"~/.open-cli-router/{PERSISTENCE_NAMESPACE}/" in text
    # Path errado NÃO deve aparecer
    assert f"~/.open-cli-router/{PROVIDER_PREFIX}/" not in text


def test_persistence_init_seeds_correct_namespace_path(tmp_path: Path):
    """init() deve criar AGENTS.md com o path correto (PERSISTENCE_NAMESPACE)."""
    from claude_code_mcp.persistence import PersistenceStore
    from claude_code_mcp.provider import PERSISTENCE_NAMESPACE

    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        seed_templates=True,
    )
    store.init()

    agents_md = store.base_dir / "AGENTS.md"
    assert agents_md.exists()
    text = agents_md.read_text()
    assert f"~/.open-cli-router/{PERSISTENCE_NAMESPACE}/" in text


# ------------------------------------------------------------------
# store: init / read
# ------------------------------------------------------------------


def _make_store(tmp_path: Path) -> PersistenceStore:
    return PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=10_000,
        seed_templates=True,
    )


def test_init_creates_three_files_and_marker(tmp_path: Path):
    store = _make_store(tmp_path)
    result = store.init()
    assert (store.base_dir / "AGENTS.md").exists()
    assert (store.base_dir / "PROJECTS.md").exists()
    assert (store.base_dir / "MEMORY.md").exists()
    assert (store.base_dir / ".initialized").exists()
    assert len(result.created) >= 3  # 3 seed files
    assert result.seed_version


def test_init_is_idempotent(tmp_path: Path):
    store = _make_store(tmp_path)
    first = store.init()
    second = store.init()
    # Second call reports everything as already existed.
    assert second.already_existed
    assert not second.created
    # Files weren't overwritten.
    content = (store.base_dir / "AGENTS.md").read_text()
    assert "AGENTS" in content  # from the first seed


def test_init_force_overwrites(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("custom content")
    store.init(force=True)
    assert "custom content" not in (store.base_dir / "AGENTS.md").read_text()
    assert "AGENTS" in (store.base_dir / "AGENTS.md").read_text()


def test_init_no_seed_creates_empty_files(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init(seed_templates=False)
    for name in ALLOWED_FILE_NAMES:
        text = (store.base_dir / f"{name.upper()}.md").read_text()
        assert text == ""


def test_init_fails_on_unwritable_base_dir(tmp_path: Path):
    # Create a file at the path where the directory should be created.
    base = tmp_path / "blocker"
    base.write_text("not a directory")
    store = PersistenceStore(base_dir=base / ".open-cli-router")
    with pytest.raises(ValueError, match="PERSISTENCE_BASE_DIR_NOT_WRITABLE"):
        store.init()


# ------------------------------------------------------------------
# store: read
# ------------------------------------------------------------------


def test_read_returns_content(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    result = store.read("agents")
    assert "AGENTS" in result.content
    assert result.size_bytes > 0
    assert not result.truncated
    assert result.modified_at is not None


def test_read_offset_and_limit(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    # Replace seed with ASCII content so byte limit == char limit.
    target = store.base_dir / "AGENTS.md"
    target.write_text("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    limited = store.read("agents", offset=0, limit=10)
    assert limited.content == "ABCDEFGHIJ"
    assert limited.truncated

    # Offset works too.
    chunk = store.read("agents", offset=5, limit=5)
    assert chunk.content == "FGHIJ"


def test_read_rejects_unknown_file(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    with pytest.raises(ValueError, match="INVALID_FILE"):
        store.read("nope")  # type: ignore[arg-type]


def test_read_rejects_too_large(tmp_path: Path):
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=10,
        seed_templates=False,
    )
    store.init()
    (store.base_dir / "AGENTS.md").write_text("x" * 50)
    with pytest.raises(ValueError, match="PERSISTENCE_FILE_TOO_LARGE"):
        store.read("agents")


# ------------------------------------------------------------------
# store: append
# ------------------------------------------------------------------


def test_append_grows_file(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    before = store.read("memory").size_bytes
    result = store.append("memory", "Session summary: did X.")
    after = store.read("memory").size_bytes
    assert after > before
    assert result.appended_bytes > 0


def test_append_with_section_header_inserts_once(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    store.append("memory", "first body", section_header="2026-06-21")
    store.append("memory", "second body", section_header="2026-06-21")
    text = store.read("memory").content
    assert text.count("## 2026-06-21") == 1


def test_append_rejects_overflow(tmp_path: Path):
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=50,
        seed_templates=False,
    )
    store.init()
    (store.base_dir / "MEMORY.md").write_text("seed\n")
    with pytest.raises(ValueError, match="PERSISTENCE_FILE_TOO_LARGE"):
        store.append("memory", "x" * 1000)


# ------------------------------------------------------------------
# store: update
# ------------------------------------------------------------------


def test_update_replaces_section(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    store.append("projects", "## A\nold\n\n## B\nkeep\n")
    result = store.update("projects", "A", "## A\nnew\n", mode="replace")
    assert result.matched
    text = store.read("projects").content
    assert "new" in text
    assert "old" not in text
    assert "keep" in text


def test_update_miss_returns_matched_false(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    result = store.update("memory", "NoSuchSection", "## NoSuchSection\nx\n")
    assert not result.matched


def test_update_append_mode_ignores_anchor(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    result = store.update(
        "memory", "ignored", "trailing text", mode="append"
    )
    assert result.matched
    assert "trailing text" in store.read("memory").content


# ------------------------------------------------------------------
# store: load_context
# ------------------------------------------------------------------


def test_load_context_returns_excerpts_when_initialized(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    ctx = store.load_context()
    assert ctx.initialized
    assert ctx.agents_excerpt is not None
    assert ctx.projects_excerpt is not None
    assert ctx.memory_excerpt is not None
    assert ctx.base_dir.endswith("claude-code")


def test_load_context_returns_none_when_not_initialized(tmp_path: Path):
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        seed_templates=False,
    )
    store.init()  # marker exists
    (store.base_dir / ".initialized").unlink()
    ctx = store.load_context()
    assert not ctx.initialized
    # Files still exist (created by init), so excerpts are strings (empty).
    assert ctx.agents_excerpt is not None
    assert ctx.agents_excerpt == ""


def test_load_context_truncates_large_files(tmp_path: Path):
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=1_000_000,
        seed_templates=False,
    )
    store.init()
    (store.base_dir / "MEMORY.md").write_text("X" * 5000)
    ctx = store.load_context(max_chars_per_file=200)
    assert ctx.memory_excerpt is not None
    assert ctx.truncated_flags["memory"] is True
    # Phase 2 (C4): marker agora inclui número de chars omitidos.
    assert "[truncated 4800 chars]" in ctx.memory_excerpt


# ------------------------------------------------------------------
# store: atomic write + concurrency
# ------------------------------------------------------------------


def test_atomic_write_no_partial_file_on_failure(tmp_path: Path, monkeypatch):
    store = _make_store(tmp_path)
    store.init()
    target = store.base_dir / "AGENTS.md"
    original = target.read_text()

    # Force a failure inside the atomic write by patching os.replace to raise.
    def boom(*_args, **_kwargs):
        raise OSError("simulated atomic-write failure")

    monkeypatch.setattr("claude_code_mcp.persistence.store.os.replace", boom)

    with pytest.raises(OSError, match="simulated atomic-write failure"):
        store.append("agents", "should not land")

    # The original content should be intact (atomic write left nothing behind).
    assert target.read_text() == original

    # And no orphan temp files remain in the directory.
    leftover = [
        p for p in target.parent.iterdir()
        if p.name.startswith(".AGENTS.md.") and p.name.endswith(".tmp")
    ]
    assert leftover == []


def test_concurrent_appends_serialize(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    n_threads = 10
    per_thread = 20

    def worker(i: int) -> None:
        for j in range(per_thread):
            store.append("memory", f"t{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = store.read("memory").content
    for i in range(n_threads):
        for j in range(per_thread):
            assert f"t{i}-{j}" in text, f"lost entry t{i}-{j}"


# ------------------------------------------------------------------
# Integration: provider prefix remaps namespace
# ------------------------------------------------------------------


def test_provider_prefix_remaps_namespace(monkeypatch, tmp_path: Path):
    """Simulate a fork: changing PERSISTENCE_NAMESPACE remaps the persistence dir."""
    import claude_code_mcp.provider as provider_mod

    monkeypatch.setattr(provider_mod, "PERSISTENCE_NAMESPACE", "x")

    d = get_persistence_dir(tmp_path / ".open-cli-router")
    assert d.name == "x"

    store = PersistenceStore(base_dir=tmp_path / ".open-cli-router")
    store.init()
    assert (store.base_dir / "AGENTS.md").exists()
    text = (store.base_dir / "AGENTS.md").read_text()
    # Template uses the provider namespace name
    assert "x" in text.lower()


# ------------------------------------------------------------------
# Phase 2: robustness improvements (C1-C5)
# ------------------------------------------------------------------


def _make_store(tmp_path: Path) -> PersistenceStore:
    """Factory for test PersistenceStore (uses tmp_path)."""
    return PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        max_file_bytes=10_000,
        seed_templates=True,
    )


# --- C1: section_header normalization ---

def test_append_section_header_normalizes_double_hash(tmp_path: Path):
    """C1: cliente envia '## foo' não deve duplicar prefixo."""
    store = _make_store(tmp_path)
    store.init()
    store.append("memory", "body", section_header="## foo")
    text = store.read("memory").content
    assert text.count("## foo") == 1
    assert "## ## foo" not in text


def test_append_section_header_normalizes_whitespace(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    store.append("memory", "body", section_header="  ##  bar  ")
    text = store.read("memory").content
    assert text.count("## bar") == 1


def test_append_section_header_dedup_case_insensitive(tmp_path: Path):
    """C1: dedup case-insensitive (foo vs Foo vs FOO)."""
    store = _make_store(tmp_path)
    store.init()
    store.append("memory", "body1", section_header="foo")
    store.append("memory", "body2", section_header="FOO")
    store.append("memory", "body3", section_header="Foo")
    text = store.read("memory").content
    assert sum(1 for line in text.splitlines() if line.rstrip().lower() == "## foo") == 1


def test_append_empty_header_after_normalization_raises(tmp_path: Path):
    """C1: header que vira vazio após normalização lança ValueError."""
    store = _make_store(tmp_path)
    store.init()
    with pytest.raises(ValueError, match="INVALID_SECTION_HEADER"):
        store.append("memory", "body", section_header="##")
    with pytest.raises(ValueError, match="INVALID_SECTION_HEADER"):
        store.append("memory", "body", section_header="   ")


def test_append_existing_section_header_is_deduped_case_insensitively(tmp_path: Path):
    store = _make_store(tmp_path)
    store.init()
    store.append("memory", "## Foo\ncontent\n")
    store.append("memory", "more", section_header="foo")
    text = store.read("memory").content
    assert sum(1 for line in text.splitlines() if line.rstrip().lower() == "## foo") == 1


# --- C2: case-insensitive anchor matching ---

def test_update_section_anchor_is_case_insensitive(tmp_path: Path):
    """C2: anchor 'foo' deve encontrar '## Foo'."""
    store = _make_store(tmp_path)
    store.init()
    target = store.base_dir / "PROJECTS.md"
    target.write_text("# Projects\n\n## MyProject\nold body\n\n## Other\nkeep\n")
    result = store.update("projects", "myproject", "## MyProject\nnew body\n")
    assert result.matched
    text = store.read("projects").content
    assert "new body" in text
    assert "old body" not in text
    assert "## Other" in text


def test_update_section_anchor_strips_hash_prefix(tmp_path: Path):
    """C2: anchor '## Foo' (com prefixo) normalizado para 'Foo'."""
    store = _make_store(tmp_path)
    store.init()
    target = store.base_dir / "PROJECTS.md"
    target.write_text("## Foo\nold\n")
    result = store.update("projects", "## Foo", "## Foo\nnew\n")
    assert result.matched
    assert "new" in store.read("projects").content


def test_update_section_anchor_empty_after_normalization_returns_miss(tmp_path: Path):
    """C2: anchor que normaliza para vazio → matched=False (não erro)."""
    store = _make_store(tmp_path)
    store.init()
    result = store.update("memory", "##", "x")
    assert result.matched is False


# --- C3: backup rotation ---

def test_backup_rotation_keeps_last_n(tmp_path: Path):
    """C3: rotação mantém apenas últimos N backups."""
    import time
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        backup_on_write=True,
        backup_keep=3,
        seed_templates=False,
    )
    store.init()

    for i in range(5):
        store.append("memory", f"entry {i}")
        time.sleep(1.05)

    backup_dir = store.base_dir / ".backups"
    backups = sorted(backup_dir.glob("MEMORY.md.*.bak"))
    assert len(backups) == 3


def test_backup_rotation_disabled_keeps_all(tmp_path: Path):
    """C3: backup_keep alto preserva todos."""
    import time
    store = PersistenceStore(
        base_dir=tmp_path / ".open-cli-router",
        backup_on_write=True,
        backup_keep=100,
        seed_templates=False,
    )
    store.init()
    for i in range(3):
        store.append("memory", f"entry {i}")
        time.sleep(1.05)

    backup_dir = store.base_dir / ".backups"
    backups = sorted(backup_dir.glob("MEMORY.md.*.bak"))
    assert len(backups) == 3


# --- C4: asymmetric truncation ---

def test_load_context_truncation_favors_tail(tmp_path: Path):
    """C4: head_ratio=0.2 → ~20% head, ~80% tail."""
    store = _make_store(tmp_path)  # default head_ratio=0.2
    store.init()
    text = ("A" * 500) + ("B" * 500)
    (store.base_dir / "MEMORY.md").write_text(text)

    ctx = store.load_context(max_chars_per_file=100)
    assert ctx.truncated_flags["memory"] is True
    excerpt = ctx.memory_excerpt
    assert excerpt.startswith("A" * 20)
    assert excerpt.endswith("B" * 80)


def test_load_context_truncation_head_ratio_validation(tmp_path: Path):
    """C4: head_ratio fora de [0,1] é rejeitado."""
    with pytest.raises(ValueError, match="INVALID_HEAD_RATIO"):
        PersistenceStore(base_dir=tmp_path, head_ratio=-0.1)
    with pytest.raises(ValueError, match="INVALID_HEAD_RATIO"):
        PersistenceStore(base_dir=tmp_path, head_ratio=1.5)


def test_load_context_truncation_omitted_chars_in_marker(tmp_path: Path):
    """C4: marker de truncamento inclui número de chars omitidos."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "MEMORY.md").write_text("X" * 1000)
    ctx = store.load_context(max_chars_per_file=100)
    excerpt = ctx.memory_excerpt
    assert "[truncated 900 chars]" in excerpt


# --- C5: read() truncated flag logic ---

def test_read_truncated_when_limit_exhausts_file(tmp_path: Path):
    """C5: read com limit < tamanho do arquivo → truncated=True."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("X" * 1000)

    result = store.read("agents", limit=100)
    assert result.truncated is True
    assert len(result.content) == 100


def test_read_not_truncated_when_limit_covers_file(tmp_path: Path):
    """C5: read com limit >= tamanho do arquivo → truncated=False."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("X" * 100)

    result = store.read("agents", limit=200)
    assert result.truncated is False
    assert len(result.content) == 100


def test_read_with_offset_and_limit(tmp_path: Path):
    """C5: offset+limit — truncated se ainda há dados após."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    result = store.read("agents", offset=10, limit=5)
    assert result.content == "KLMNO"
    assert result.truncated is True


def test_read_offset_at_end_returns_not_truncated(tmp_path: Path):
    """C5: offset no fim do arquivo + limit suficiente → não truncated."""
    store = _make_store(tmp_path)
    store.init()
    (store.base_dir / "AGENTS.md").write_text("ABCDE")

    result = store.read("agents", offset=5, limit=10)
    assert result.content == ""
    assert result.truncated is False
