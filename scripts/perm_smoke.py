#!/usr/bin/env python3
"""Smoke test script for validating permissive vs safe mode permission mappings."""

import sys
from pathlib import Path

# Add src directory to path
src_dir = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_dir))

from claude_code_mcp.claude_args import build_claude_argv
from claude_code_mcp.models import ClaudeRunTaskRequest
from claude_code_mcp.settings import Settings


def main():
    print("Running permission smoke test...")

    # 1. Permissive mode with explicit permission_mode="bypassPermissions"
    settings_permissive = Settings(mode="permissive")
    req_bypass = ClaudeRunTaskRequest(
        workspace_path="/tmp",
        prompt="echo hello",
        permission_mode="bypassPermissions",
    )
    argv_bypass = build_claude_argv(
        claude_path="claude",
        workspace_path="/tmp",
        request=req_bypass,
        mode="sync",
        settings=settings_permissive,
    )
    assert "--dangerously-skip-permissions" in argv_bypass, "Expected --dangerously-skip-permissions in argv"
    assert "--permission-mode" not in argv_bypass, "Did not expect --permission-mode in argv when bypassing"
    print("✓ Test 1 Passed: Permissive mode with bypassPermissions correctly emits --dangerously-skip-permissions")

    # 2. Safe mode check: assert bypassPermissions is rejected or dangerous flag is absent
    settings_safe = Settings(mode="safe")
    req_safe = ClaudeRunTaskRequest(
        workspace_path="/tmp",
        prompt="echo hello",
        permission_mode="acceptEdits",
    )
    argv_safe = build_claude_argv(
        claude_path="claude",
        workspace_path="/tmp",
        request=req_safe,
        mode="sync",
        settings=settings_safe,
    )
    assert "--dangerously-skip-permissions" not in argv_safe, "Expected --dangerously-skip-permissions to be absent in safe mode"
    assert "--permission-mode" in argv_safe, "Expected --permission-mode in safe mode"
    idx = argv_safe.index("--permission-mode")
    assert argv_safe[idx + 1] == "acceptEdits"
    print("✓ Test 2 Passed: Safe mode correctly enforces acceptEdits without dangerous flag")

    # 3. Test permission_mode="plan" passthrough
    req_plan = ClaudeRunTaskRequest(
        workspace_path="/tmp",
        prompt="plan task",
        permission_mode="plan",
    )
    argv_plan = build_claude_argv(
        claude_path="claude",
        workspace_path="/tmp",
        request=req_plan,
        mode="sync",
        settings=settings_permissive,
    )
    assert "--permission-mode" in argv_plan
    idx_plan = argv_plan.index("--permission-mode")
    assert argv_plan[idx_plan + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv_plan
    print("✓ Test 3 Passed: permission_mode='plan' passes through correctly as --permission-mode plan")

    # 4. Permissive mode with default permission mode (omitted permission_mode)
    req_default = ClaudeRunTaskRequest(
        workspace_path="/tmp",
        prompt="run script",
    )
    argv_default = build_claude_argv(
        claude_path="claude",
        workspace_path="/tmp",
        request=req_default,
        mode="async",
        settings=settings_permissive,
    )
    assert "--dangerously-skip-permissions" in argv_default, "Expected default permissive mode to bypass permissions"
    print("✓ Test 4 Passed: Omitted permission_mode in permissive mode defaults to bypassPermissions in async mode")

    print("\nAll smoke tests passed successfully!")


if __name__ == "__main__":
    main()
