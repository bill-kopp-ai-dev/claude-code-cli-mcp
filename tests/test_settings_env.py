from claude_code_mcp.settings import Settings


def test_allowed_models_default_has_all_four(monkeypatch):
    """Garante que a allowlist padrão de inicialização contenha os quatro modelos Claude."""
    monkeypatch.delenv("CLAUDE_MCP_ALLOWED_MODELS", raising=False)
    settings = Settings()
    expected_models = {"sonnet", "fable", "opus", "haiku"}
    assert expected_models.issubset(settings.allowed_models)


def test_allowed_models_from_env(monkeypatch):
    """Verifica se a injeção via variável de ambiente restringe corretamente a lista."""
    monkeypatch.setenv("CLAUDE_MCP_ALLOWED_MODELS", "haiku,sonnet")
    settings = Settings()
    assert settings.allowed_models == {"haiku", "sonnet"}


def test_timeout_policy_default_off(monkeypatch):
    """Verifica que a feature flag de política inicia desativada por motivos de segurança."""
    monkeypatch.delenv("CLAUDE_MCP_TIMEOUT_POLICY_ENABLED", raising=False)
    settings = Settings()
    assert settings.timeout_policy_enabled is False


def test_model_aliases_dict_has_all_four(monkeypatch):
    """Garante que o dicionário de aliases possua os quatro modelos e correspondentes cli strings."""
    settings = Settings()
    assert set(settings.claude_model_aliases.keys()) == {"sonnet", "fable", "opus", "haiku"}
    assert settings.claude_model_aliases["sonnet"] == "claude-sonnet-5-2026"
    assert settings.claude_model_aliases["haiku"] == "claude-haiku-4-5-2026"


def test_timeout_policy_enabled_by_env(monkeypatch):
    """Verifica a capacidade de ativar a governança através do ambiente."""
    monkeypatch.setenv("CLAUDE_MCP_TIMEOUT_POLICY_ENABLED", "true")
    settings = Settings()
    assert settings.timeout_policy_enabled is True
