import pytest
from claude_code_mcp.models import (
    MODEL_REGISTRY,
    ModelTier,
    TaskClass,
    resolve_model_alias,
    is_model_allowed,
)


def test_registry_has_all_four_models():
    """Garante que a lista de modelos suportados possui exatamente as chaves mapeadas."""
    expected_keys = {"sonnet", "fable", "opus", "haiku"}
    assert set(MODEL_REGISTRY.keys()) == expected_keys


def test_each_profile_has_required_fields():
    """Valida as propriedades internas e a tipagem de cada perfil de modelo."""
    for alias, profile in MODEL_REGISTRY.items():
        assert profile.alias == alias
        assert isinstance(profile.cli_string, str)
        assert isinstance(profile.display_name, str)
        assert isinstance(profile.tier, ModelTier)
        assert isinstance(profile.best_for, list)
        assert len(profile.best_for) > 0
        assert all(isinstance(tc, TaskClass) for tc in profile.best_for)
        assert profile.typical_latency_min > 0.0
        assert profile.typical_cost_per_run_usd >= 0.0
        assert isinstance(profile.multi_file_safe, bool)
        assert profile.max_context_k_tokens > 0


def test_resolve_model_alias_returns_cli_string():
    """Verifica se aliases válidos retornam suas correspondentes strings de CLI oficiais."""
    assert resolve_model_alias("sonnet") == "claude-sonnet-5-2026"
    assert resolve_model_alias("haiku") == "claude-haiku-4-5-2026"
    assert resolve_model_alias("SONNET") == "claude-sonnet-5-2026"  # Case-insensitive


def test_resolve_model_alias_raises_for_unknown():
    """Garante que a passagem de apelidos inválidos gera a exceção KeyError esperada."""
    with pytest.raises(KeyError) as exc_info:
        resolve_model_alias("unknown_model")
    assert "não é suportado pelo servidor MCP" in str(exc_info.value)


def test_is_model_allowed_with_empty_set():
    """Verifica que, se a allowlist do servidor estiver vazia, qualquer modelo do registro é aceito."""
    assert is_model_allowed("haiku", set()) is True
    assert is_model_allowed("invalid_model", set()) is False


def test_is_model_allowed_with_haiku_in_set():
    """Valida a filtragem da allowlist quando explicitamente populada por variáveis de ambiente."""
    allowlist = {"sonnet", "haiku"}
    assert is_model_allowed("sonnet", allowlist) is True
    assert is_model_allowed("haiku", allowlist) is True


def test_is_model_allowed_rejects_unknown():
    allowlist = {"sonnet", "haiku"}
    assert is_model_allowed("opus", allowlist) is False


@pytest.mark.parametrize(
    "alias,expected_tier",
    [
        ("sonnet", ModelTier.STANDARD),
        ("fable", ModelTier.MID_TIER),
        ("opus", ModelTier.FLAGSHIP),
        ("haiku", ModelTier.CHEAP),
    ],
)
def test_model_tier_assignment(alias, expected_tier):
    assert MODEL_REGISTRY[alias].tier == expected_tier
