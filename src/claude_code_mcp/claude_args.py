from __future__ import annotations

from typing import Literal, Any


def build_claude_argv(
    *,
    claude_path: str,
    workspace_path: str,
    request: Any,                   # ClaudeRunTaskRequest
    mode: Literal["sync", "async"],   # sync uses --output-format json; async uses stream-json + --include-partial-messages + --verbose
    session_id: str | None = None,
    settings: Any,                  # claude_code_mcp.settings.Settings instance
) -> list[str]:
    """Return the full argv list for `subprocess.Popen`.

    Raises ValueError("PERMISSION_MODE_NOT_ALLOWED: ...") when permission_mode
    violates the safe-mode/permissive-mode rules from PLAN.md §8.
    """
    # 1. Determine permission mode
    if settings.force_default_permission_mode:
        permission_mode = settings.default_permission_mode
    else:
        permission_mode = request.permission_mode or settings.default_permission_mode

    # 2. Safe-mode and permissive-mode checks (Validation order: safe-mode checks first)
    if settings.mode == "safe":
        if request.options.dangerously_skip_permissions:
            raise ValueError("NOT_ALLOWED: --dangerously-skip-permissions is not allowed in safe mode")
        if permission_mode == "bypassPermissions" or request.permission_mode == "bypassPermissions":
            raise ValueError("NOT_ALLOWED: bypassPermissions requires permissive mode")
        if permission_mode == "dontAsk" or request.permission_mode == "dontAsk":
            raise ValueError("PERMISSION_MODE_NOT_ALLOWED: dontAsk is not allowed in safe mode")

    if settings.mode == "permissive":
        if (
            permission_mode == "bypassPermissions"
            or request.permission_mode == "bypassPermissions"
            or request.options.dangerously_skip_permissions
        ):
            if "--dangerously-skip-permissions" not in (settings.allow_extra_args or set()):
                raise ValueError(
                    "PERMISSION_MODE_NOT_ALLOWED: bypassPermissions/dangerously_skip_permissions requires "
                    "'--dangerously-skip-permissions' to be allowed in allow_extra_args"
                )

    if permission_mode == "auto" or request.permission_mode == "auto":
        raise ValueError("PERMISSION_MODE_NOT_ALLOWED: auto permission mode is not allowed")

    # 3. Model allowlist check (applies to both --model and --fallback-model)
    if settings.allowed_models:
        for label, value in (("model", request.model), ("fallback_model", request.fallback_model)):
            if value and value not in settings.allowed_models:
                raise ValueError(
                    f"MODEL_NOT_ALLOWED: {label} {value!r} is not in "
                    f"settings.allowed_models: {settings.allowed_models}"
                )

    # 4. Build argv
    argv: list[str] = [claude_path, "-p", request.prompt]

    if settings.force_bare:
        argv.append("--bare")

    argv.extend(["--add-dir", workspace_path])
    argv.extend(["--permission-mode", permission_mode])

    if mode == "sync":
        argv.extend(["--output-format", "json", "--no-session-persistence"])
        if request.options.dangerously_skip_permissions:
            argv.append("--dangerously-skip-permissions")
    elif mode == "async":
        argv.extend(["--output-format", "stream-json", "--verbose", "--include-partial-messages"])
        if session_id:
            argv.extend(["--session-id", session_id])
        if request.resume_session_id:
            argv.extend(["--resume", request.resume_session_id])

    if request.model:
        argv.extend(["--model", request.model])
    if request.fallback_model:
        # Docs: "print mode only" — our sync and async modes both use `-p`
        # so this flag is safe in both.
        argv.extend(["--fallback-model", request.fallback_model])
    if request.max_turns is not None:
        argv.extend(["--max-turns", str(request.max_turns)])
    if request.max_budget_usd is not None:
        argv.extend(["--max-budget-usd", str(request.max_budget_usd)])
    if request.effort:
        argv.extend(["--effort", request.effort])
    if request.system_prompt_append:
        argv.extend(["--append-system-prompt", request.system_prompt_append])
    if request.allowed_tools:
        argv.extend(["--allowedTools", ",".join(request.allowed_tools)])
    if request.disallowed_tools:
        argv.extend(["--disallowedTools", ",".join(request.disallowed_tools)])

    if request.options.extra_args:
        argv.extend(request.options.extra_args)

    return argv


def build_child_env(overrides: dict[str, str] | None, settings: Any) -> dict[str, str]:
    """Build the child-process env.

    Always sets CLAUDE_CODE_DISABLE_* hardening vars per PLAN.md §7. In
    safe mode additionally sets CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1,
    CLAUDE_CODE_DISABLE_CRON=1, CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1.
    In permissive mode sets CLAUDE_CODE_ENABLE_TELEMETRY=1.

    Always sets:
        CLAUDE_CODE_HIDE_ACCOUNT_INFO=1
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
        CLAUDE_CODE_DISABLE_CLAUDE_MDS=1
        CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
        CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING=1
    """
    import os
    env = os.environ.copy()
    env.update({
        "CLAUDE_CODE_HIDE_ACCOUNT_INFO": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
    })
    if settings.mode == "safe":
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        env["CLAUDE_CODE_DISABLE_CRON"] = "1"
        env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] = "1"
    else:
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    if overrides:
        env.update({str(k): str(v) for k, v in overrides.items()})
    return env
