import pytest
from claude_code_mcp.models import MODEL_REGISTRY, TaskClass
from claude_code_mcp.timeout_policy import (
    compute_timeout,
    ASYNC_CEILING_S,
)


def test_trivial_edit_with_haiku_uses_sync():
    """Garante recomendações rápidas de timeout síncrono para edições triviais."""
    profile = MODEL_REGISTRY["haiku"]
    rec = compute_timeout(TaskClass.TRIVIAL_EDIT, profile, files_to_edit=1)
    assert rec.timeout_s == 180
    assert rec.must_use_async is False
    assert rec.warning is None


def test_smoke_test_is_120s():
    """Garante que smoke_test seja 120s síncrono."""
    profile = MODEL_REGISTRY["haiku"]
    rec = compute_timeout(TaskClass.SMOKE_TEST, profile, files_to_edit=1)
    assert rec.timeout_s == 120
    assert rec.must_use_async is False
    assert rec.warning is None


def test_multi_file_refactor_uses_async_when_above_sync_cap():
    """Verifica a promoção obrigatória para fluxo assíncrono em grandes edições."""
    profile = MODEL_REGISTRY["sonnet"]
    rec = compute_timeout(TaskClass.MULTI_FILE_REFACTOR, profile, files_to_edit=25)
    assert rec.timeout_s == 900
    assert rec.must_use_async is True
    assert rec.warning is None


def test_haiku_on_50_files_emits_warning():
    """Valida o alerta de segurança emitido ao selecionar o modelo Haiku para múltiplos arquivos."""
    profile = MODEL_REGISTRY["haiku"]
    rec = compute_timeout(TaskClass.MULTI_FILE_REFACTOR, profile, files_to_edit=10)
    assert rec.warning is not None
    assert "não é recomendado para tarefas que alteram múltiplos arquivos" in rec.warning


def test_max_budget_warning_emitted_when_overshoot():
    """Verifica a emissão de alertas de estouro financeiro baseado na estimativa de consumo."""
    profile = MODEL_REGISTRY["opus"]
    rec = compute_timeout(TaskClass.MIGRATION, profile, files_to_edit=5, max_budget_usd=1.00)
    assert rec.warning is not None
    assert "custo estimado desta execução" in rec.warning
    assert "compromete mais de 80%" in rec.warning


@pytest.mark.parametrize("task_class", list(TaskClass))
@pytest.mark.parametrize("profile_name", list(MODEL_REGISTRY.keys()))
def test_no_timeout_exceeds_async_ceiling(task_class, profile_name):
    """Garante que nenhuma recomendação de timeout ultrapasse o limite assíncrono."""
    profile = MODEL_REGISTRY[profile_name]
    rec = compute_timeout(task_class, profile, files_to_edit=10)
    assert rec.timeout_s <= ASYNC_CEILING_S


@pytest.mark.parametrize("task_class", list(TaskClass))
@pytest.mark.parametrize("profile_name", list(MODEL_REGISTRY.keys()))
def test_no_timeout_below_sync_floor(task_class, profile_name):
    """Garante que nenhuma recomendação de timeout fique abaixo do limite mínimo de 60s."""
    profile = MODEL_REGISTRY[profile_name]
    rec = compute_timeout(task_class, profile, files_to_edit=10)
    assert rec.timeout_s >= 60
