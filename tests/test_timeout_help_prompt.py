import pytest
from claude_code_mcp.server import prompt_timeout_help
from claude_code_mcp.models import TaskClass


def test_prompt_returns_recommendation_for_sonnet_smoke():
    """Call with task_class='smoke_test', model_alias='sonnet', assert timeout_s: 120 and must_use_async: False."""
    res = prompt_timeout_help(task_class="smoke_test", model_alias="sonnet")
    assert "`timeout_s`: **120**" in res
    assert "`must_use_async`: **False**" in res
    assert "ERROR" not in res


def test_prompt_returns_recommendation_for_opus_architecture():
    """Call with task_class='architecture', model_alias='opus', assert must_use_async: True and timeout_s >= 1800."""
    res = prompt_timeout_help(task_class="architecture", model_alias="opus")
    assert "`must_use_async`: **True**" in res
    assert "ERROR" not in res
    # Let's extract the recommended timeout from the text.
    # The format is: - `timeout_s`: **1800**
    # Let's find "**" and parse the number between them.
    part = res.split("`timeout_s`: **")[1].split("**")[0]
    timeout_val = int(part)
    assert timeout_val >= 1800


def test_prompt_handles_unknown_model():
    """Call with model_alias='gpt-4', assert output contains 'ERROR' and the valid aliases list."""
    res = prompt_timeout_help(task_class="smoke_test", model_alias="gpt-4")
    assert "ERROR: unknown model_alias='gpt-4'" in res
    assert "sonnet" in res
    assert "fable" in res
    assert "opus" in res
    assert "haiku" in res


def test_prompt_handles_unknown_task_class():
    """Call with task_class='unicorn_ride', assert output contains 'ERROR'."""
    res = prompt_timeout_help(task_class="unicorn_ride", model_alias="sonnet")
    assert "ERROR: unknown task_class='unicorn_ride'" in res
    assert "smoke_test" in res


def test_prompt_includes_decision_matrix_table():
    """Assert '## Decision matrix' and all 10 TaskClass values appear in output."""
    res = prompt_timeout_help(task_class="smoke_test", model_alias="sonnet")
    assert "## Decision matrix" in res
    for tc in TaskClass:
        assert tc.value in res


def test_prompt_includes_model_registry_section():
    """Assert '## Model registry' and all 4 aliases appear."""
    res = prompt_timeout_help(task_class="smoke_test", model_alias="sonnet")
    assert "## Model registry" in res
    assert "sonnet" in res
    assert "fable" in res
    assert "opus" in res
    assert "haiku" in res


def test_prompt_includes_sync_async_rules():
    """Assert 'SYNC_CEILING_S' and 'ASYNC_CEILING_S' appear in output."""
    res = prompt_timeout_help(task_class="smoke_test", model_alias="sonnet")
    assert "SYNC_CEILING_S" in res
    assert "ASYNC_CEILING_S" in res


def test_prompt_normalizes_case():
    """Call with task_class='SMOKE_TEST', model_alias='Sonnet', assert it still works (no error)."""
    res = prompt_timeout_help(task_class="SMOKE_TEST", model_alias="Sonnet")
    assert "`timeout_s`: **120**" in res
    assert "`must_use_async`: **False**" in res
    assert "ERROR" not in res
