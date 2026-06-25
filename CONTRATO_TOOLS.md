# MCP Contract (Tools & Prompts)

This document describes the public contract for tools and prompts exposed by this MCP server.

## Tools

### claude_health

Input: `ClaudeHealthRequest`
- `expected_version: str | None` (default: None)

Output: `ClaudeHealthResponse`
- `claude_path: str`
- `claude_version: str`
- `ok: bool`
- `notes: list[str]`
- `auth_status: str | None`

Notes:
- Resolves the `claude` CLI binary and returns its version.
- Optionally checks authentication status by executing `claude auth status --text`.

### claude_run_task

Input: `ClaudeRunTaskRequest`
- `workspace_path: str`
- `prompt: str`
- `options: ClaudeExecOptions` (nested object, default: defaults)
- `capture_changes: bool` (default: True)
- `change_scope: "workspace" | "git_only"` (default: "workspace")
- `model: str | None` (default: None)
- `max_turns: int | None` (default: None)
- `max_budget_usd: float | None` (default: None)
- `effort: "low" | "medium" | "high" | "xhigh" | "max" | None` (default: None)
- `permission_mode: "default" | "acceptEdits" | "plan" | "dontAsk" | "bypassPermissions" | None` (default: None)
- `allowed_tools: list[str] | None` (default: None)
- `disallowed_tools: list[str] | None` (default: None)
- `system_prompt_append: str | None` (default: None)
- `resume_session_id: str | None` (default: None)

`ClaudeExecOptions` Fields:
- `sandbox: bool` (default: True)
- `dangerously_skip_permissions: bool` (default: False)
- `timeout_s: int` (default: 600, range: 1-3600)
- `env: dict[str, str] | None` (default: None)
- `extra_args: list[str]` (default: empty list)

Output: `ClaudeRunTaskResponse`
- `run_id: str`
- `status: "done" | "error" | "timeout" | "cancelled"`
- `exit_code: int | None`
- `final_text: str`
- `result: dict[str, Any]` (parsed JSON result events from `claude`)
- `changes: WorkspaceChanges | None`
- `session_id: str | None`
- `total_cost_usd: float | None`
- `duration_ms: int | None`
- `num_turns: int | None`
- `model_usage: dict[str, Any] | None`
- `notes: list[str]`

`WorkspaceChanges` Fields:
- `method: "git" | "snapshot" | "none"`
- `changed_files: list[str]`
- `diff: str | None` (Unified diff, only available when `method` is "git")

Notes:
- Executes a blocking synchronous `claude` CLI execution using `--output-format json`.
- Injects persistent memory context automatically at the start of the prompt if persistence is enabled and initialized.
- Extracts standard result fields (`session_id`, `total_cost_usd`, `duration_ms`, `num_turns`, `modelUsage` from the final JSON events).

### claude_start_task

Input: `ClaudeStartTaskRequest` (Same schema as `ClaudeRunTaskRequest`)

Output: `ClaudeStartTaskResponse`
- `run_id: str`
- `started_at: datetime`
- `session_id: str`

Notes:
- Starts `claude` in an asynchronous background process using `--output-format stream-json --verbose --include-partial-messages`.
- Automatically enforces `CLAUDE_MCP_MAX_CONCURRENT_RUNS` limit (default: 10).

### claude_poll_task

Input: `ClaudePollTaskRequest`
- `run_id: str`
- `drain: bool` (default: False. If True, waits synchronously for task completion up to its timeout)
- `wait_seconds: float` (default: 0.5)

Output: `ClaudePollTaskResponse`
- `run_id: str`
- `status: "running" | "done" | "error" | "timeout" | "cancelled"`
- `new_messages: list[dict[str, Any]]` (new stream events received since last poll)
- `result: ClaudeRunResult | None` (populated only when status is not "running")
- `stdout_len: int`
- `stderr_len: int`
- `elapsed_seconds: float`
- `notes: list[str]`

Notes:
- Read-only call that fetches accumulated logs/stream events.
- Once completed, the final workspace changes are automatically calculated and returned inside `result.changes`.

### claude_cancel_task

Input: `ClaudeCancelTaskRequest`
- `run_id: str`
- `force: bool` (default: False. If True, sends SIGKILL; otherwise sends SIGINT to gracefully terminate)

Output: `ClaudeCancelTaskResponse`
- `canceled: bool`
- `status: "cancelled" | "not_found" | "already_done"`

### claude_list_runs

Input: `ClaudeListRunsRequest`
- `limit: int` (default: 50)

Output: `ClaudeListRunsResponse`
- `runs: list[ClaudeRunSummary]`

`ClaudeRunSummary` Fields:
- `run_id: str`
- `workspace_path: str`
- `status: "running" | "done" | "error" | "timeout" | "cancelled"`
- `started_at: datetime`

### claude_init_persistence

Input: `ClaudeInitPersistenceRequest`
- `force: bool` (default: False)
- `seed_templates: bool | None` (default: None)

Output: `ClaudeInitPersistenceResponse`
- `base_dir: str`
- `created: list[str]`
- `already_existed: list[str]`
- `seed_version: str`

Notes:
- Initializes the file-based persistence layer folder at `~/.open-cli-router/claude-code/` (or `CLAUDE_MCP_PERSISTENCE_BASE_DIR/claude-code/`).
- Idempotent: does not overwrite existing files unless `force=true` is supplied.

### claude_read_persistence

Input: `ClaudeReadPersistenceRequest`
- `file: "agents" | "projects" | "memory"`
- `offset: int` (default: 0)
- `limit: int | None` (default: None)

Output: `ClaudeReadPersistenceResponse`
- `file: str`
- `content: str`
- `size_bytes: int`
- `truncated: bool`
- `modified_at: datetime | None`

### claude_append_persistence

Input: `ClaudeAppendPersistenceRequest`
- `file: "agents" | "projects" | "memory"`
- `content: str`
- `section_header: str | None` (default: None. Automatically inserts a `## section_header` if not already present in the file)
- `confirm: bool` (default: False)

Output: `ClaudeAppendPersistenceResponse`
- `file: str`
- `appended_bytes: int`
- `new_size_bytes: int`
- `timestamp: datetime`

Notes:
- Writing/appending to `AGENTS.md` (the system prompt) in safe mode raises a `CONFIRM_REQUIRED` error unless `confirm` is set to `True`.

### claude_update_persistence

Input: `ClaudeUpdatePersistenceRequest`
- `file: "agents" | "projects" | "memory"`
- `section_anchor: str` (the exact heading title without the `## ` prefix)
- `new_content: str`
- `mode: "replace" | "append"` (default: "replace")
- `confirm: bool` (default: False)

Output: `ClaudeUpdatePersistenceResponse`
- `file: str`
- `section_anchor: str`
- `matched: bool`
- `new_size_bytes: int`

Notes:
- Replacing/appending to `AGENTS.md` in safe mode raises `CONFIRM_REQUIRED` unless `confirm` is set to `True`.

### claude_load_persistence_context

Input: `ClaudeLoadPersistenceContextRequest`
- `include: list["agents" | "projects" | "memory"]` (default: ["agents", "projects", "memory"])
- `max_chars_per_file: int` (default: 20000)

Output: `ClaudeLoadPersistenceContextResponse`
- `agents_excerpt: str | None`
- `projects_excerpt: str | None`
- `memory_excerpt: str | None`
- `truncated_flags: dict[str, bool]`
- `total_chars: int`
- `base_dir: str`
- `initialized: bool`

---

## Prompts

The MCP server exposes the following 5 system prompts for coordinating workflows:

- `claude_sync_orchestration`: Instructs orchestrator agents on how to use `claude_run_task` to execute a single synchronous workspace command, inspect results, and handle changes.
- `claude_async_orchestration`: Guides agents through the lifecycle of asynchronous background commands (start, poll with backoff, cancel, and complete).
- `claude_model_selection_guidance`: Informs the client agent how model parameters are passed and lists valid aliases.
- `claude_security_and_workspace_rules`: Enforces path boundaries, `--bare` execution, and restricts parameters in safe vs permissive mode.
- `claude_persistence_protocol`: Details the lifecycle of loading persistence context before a task and appending session learning/summary after a task.

---

## Model Selection

Model selection is controlled per request by specifying the `model` parameter inside `claude_run_task` or `claude_start_task` (which passes `--model <model>` to the CLI process).

Common model aliases include:
- `sonnet`: Claude 3.5 Sonnet (Recommended)
- `opus`: Claude 3 Opus

You can also specify full names like `claude-sonnet-4-6`. If `CLAUDE_MCP_ALLOWED_MODELS` is configured (default: `{"sonnet", "opus"}`), any requested model must reside in that set, otherwise a `MODEL_NOT_ALLOWED` error is raised. If `CLAUDE_MCP_ALLOWED_MODELS` is an empty set, validation is skipped.

---

## Security Configuration

The MCP server operates in two main modes governed by `CLAUDE_MCP_MODE`:

### Safe Mode (`safe`, default)
- **Sandbox Enforced**: If `CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE` is enabled, the CLI process executes in its default sandboxed environment (`sandbox` option must be `True`).
- **No Environment Overrides**: The `env` parameter in `options` must be empty/None.
- **No Extra Arguments**: The `extra_args` list in `options` must be empty.
- **Strict Permissions**: Skipping permissions (`dangerously_skip_permissions=True` or permission modes like `bypassPermissions` or `dontAsk`) is blocked.
- **Hardened Env Variables**: Sets background tasks, cron execution, and terminal title updates to disabled inside the child environment to prevent escape.

### Permissive Mode (`permissive`)
- Allows custom environment variables listed in the `CLAUDE_MCP_ALLOW_ENV_KEYS` allowlist.
- Allows custom CLI arguments listed in the `CLAUDE_MCP_ALLOW_EXTRA_ARGS` allowlist.
- To bypass permissions (`bypassPermissions` mode or `dangerously_skip_permissions=True`), `"--dangerously-skip-permissions"` must be explicitly included in the `CLAUDE_MCP_ALLOW_EXTRA_ARGS` allowlist.

---

## Persistence Settings

The persistent layer is configured using the following environment variables:

- `CLAUDE_MCP_PERSISTENCE_ENABLED`: Master toggle (default: `true`).
- `CLAUDE_MCP_PERSISTENCE_BASE_DIR`: Base path (default: `~/.open-cli-router`). Files resolve to `CLAUDE_MCP_PERSISTENCE_BASE_DIR/claude-code/`.
- `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES`: Hard file size limit (default: `1048576` / 1 MiB).
- `CLAUDE_MCP_PERSISTENCE_BACKUP_ON_WRITE`: If `true`, copies `<FILE>.md` to `.backups/<FILE>.<timestamp>.md` before modifying it (default: `false`).
- `CLAUDE_MCP_PERSISTENCE_SEED_TEMPLATES`: Seeds default template formatting on directory creation (default: `true`).

---

## Validation Rules

1. **Path Traversal Prevention**: `workspace_path` must resolve inside one of the absolute directories listed in `CLAUDE_MCP_ALLOWED_ROOTS`. If `CLAUDE_MCP_ALLOWED_ROOTS` is empty, it defaults to the directory from which the MCP server was launched. Paths cannot escape via `..`.
2. **Safe Mode Enforcement First**: Safe mode parameter checks occur before allowlist validation.
3. **Type Hardening**: Standard boolean parameters (such as `sandbox`, `dangerously_skip_permissions`, `confirm`, `capture_changes`) are validated as strict booleans (`StrictBool`) to block shell parameter injection via string coercion. Extra args and environment keys/values must contain only strings.
4. **Command Isolation**: The server always appends `--bare` (unless `CLAUDE_MCP_FORCE_BARE` is disabled) to isolate the CLI run from local workspace profiles and hooks.
