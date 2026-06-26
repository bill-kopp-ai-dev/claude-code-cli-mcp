from __future__ import annotations

from claude_code_mcp.models import ClaudeSelfTestRequest, ClaudeSelfTestResponse
from claude_code_mcp.server import claude_self_test


def test_claude_self_test_basic():
    """Verify that claude_self_test returns the expected response shape."""
    res = claude_self_test()
    assert isinstance(res, ClaudeSelfTestResponse)
    assert res.total_tools > 0
    assert res.tolerant_count >= 0
    assert res.requires_req_count >= 0
    assert len(res.tools) == res.total_tools
    assert "claude-code-cli-mcp" in res.server_info.get("name", "")
    assert res.summary is not None
    assert len(res.summary) > 0

    # Every report should have expected fields
    for tool_report in res.tools:
        assert tool_report.name.startswith("claude_")
        assert isinstance(tool_report.top_level_required, list)
        assert isinstance(tool_report.top_level_properties, list)
        assert isinstance(tool_report.accepts_empty_args, bool)
        assert isinstance(tool_report.requires_req_wrapper, bool)


def test_claude_self_test_include_filter():
    """Verify that the include filter restricts the tools reported."""
    # Only report tools starting with "claude_health" or "claude_run_task"
    req = ClaudeSelfTestRequest(include=["claude_health", "claude_run_task"])
    res = claude_self_test(req)
    
    assert res.total_tools > 0
    for tool_report in res.tools:
        assert any(tool_report.name.startswith(p) for p in ["claude_health", "claude_run_task"])


def test_claude_self_test_only_show_tolerant():
    """Verify that only_show_tolerant filters out tools that have required arguments."""
    req = ClaudeSelfTestRequest(only_show_tolerant=True)
    res = claude_self_test(req)
    
    for tool_report in res.tools:
        assert tool_report.accepts_empty_args is True
        assert len(tool_report.top_level_required) == 0
