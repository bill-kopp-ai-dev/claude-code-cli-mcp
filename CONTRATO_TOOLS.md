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
- Use this as the first call in any orchestration session to validate
  the environment before dispatching tasks.

### claude_run_task

Input: `ClaudeRunTaskRequest`
- `workspace_path: str` (REQUIRED — must be inside `CLAUDE_MCP_ALLOWED_ROOTS`)
- `prompt: str` (REQUIRED)
- `options: ClaudeExecOptions` (nested object, default: defaults)
- `capture_changes: bool` (default: True)
- `change_scope: "workspace" | "git_only"` (default: "workspace")
- `model: str | None` (default: None; aliases: sonnet/fable/opus/haiku)
- `fallback_model: str | None` (default: None)
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
- `timeout_s: int` (default: 600, range: 1-3600 — note: sync wrapper is hard-capped at 600s in practice; use `claude_start_task` for >600s)
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
- Sync mode is bounded by FastMCP's 600s wrapper cap. For tasks that may
  exceed 600s, use `claude_start_task` (async, up to 3600s) instead.
  For timeout routing, call the `claude_timeout_help` prompt first.
- Cost depends on `model` + prompt size; check `total_cost_usd` in the
  response. Typical: haiku ~$0.02, sonnet ~$0.50, opus ~$1.50.
- If the call returns a malformed response (parse_error), the subprocess
  likely returned non-JSON (truncated, mid-timeout, or auth failure). Do
  NOT retry sync — switch to `claude_start_task` (async) which buffers
  output safely. Use `claude_list_runs` to recover the run_id of the
  in-flight subprocess.
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
- Use for any task that may exceed 600s (architecture, migration, large
  multi-file refactors, long-running data agents).
- Automatically enforces `CLAUDE_MCP_MAX_CONCURRENT_RUNS` limit (default: 10).
  Calls beyond the limit return `MAX_CONCURRENT_RUNS_EXCEEDED`.
- Active runs survive across orchestrator/MCP-client restarts and can be
  recovered via `claude_list_runs` (use the run_id to poll or cancel).

### claude_poll_task

Input: `ClaudePollTaskRequest`
- `run_id: str`
- `drain: bool` (default: False. If True, blocks until status is no longer "running" — use for fire-and-wait. Bounded by your MCP client's request timeout, not the async task's timeout_s.)
- `wait_seconds: float` (default: 0.5; recommended schedule when drain=false: start at 1s, double up to 10s if still running)

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
- For tight control loops with explicit backoff, prefer drain=false.
- For fire-and-wait semantics, use drain=true (but watch your MCP client's request timeout — if you expect a long wait, poll in a loop with explicit wait_seconds instead).
- Once completed, the final workspace changes are automatically calculated and returned inside `result.changes`.

### claude_cancel_task

Input: `ClaudeCancelTaskRequest`
- `run_id: str`
- `force: bool` (default: False. Two escalation levels:
  - force=false (default): SIGTERM (graceful). Subprocess has ~5s to clean up
    before the OS escalates. Try this first.
  - force=true: SIGKILL (immediate). Use only if the subprocess doesn't
    respond to SIGTERM within ~5s.)

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

Notes:
- Use this for **recovery** after orchestrator restart: active async runs
  (from `claude_start_task`) survive across MCP client restarts and can be
  polled via `claude_poll_task` or stopped via `claude_cancel_task` using
  the run_id from this listing.
- Active runs are listed first (newest first), followed by completed runs
  (bounded by `Settings.max_runs`).

### claude_init_persistence

Input: `ClaudeInitPersistenceRequest`
- `force: bool` (default: False) — re-create files even if they exist
- `seed_templates: bool | None` (default: None) — override the per-server default for seeding templates

Output: `ClaudeInitPersistenceResponse`
- `base_dir: str`
- `created: list[str]`
- `already_existed: list[str]`
- `seed_version: str`

Notes:
- Initializes the file-based persistence layer folder at `~/.open-cli-router/claude-code/` (or `CLAUDE_MCP_PERSISTENCE_BASE_DIR/claude-code/`).
- Idempotent: does not overwrite existing files unless `force=true` is supplied.
- Call this ONCE at startup before `claude_load_persistence_context` will produce useful results.

### claude_read_persistence

Input: `ClaudeReadPersistenceRequest`
- `file: "agents" | "projects" | "memory"`
- `offset: int` (default: 0) — start reading from line N (0-indexed)
- `limit: int | None` (default: None) — max lines to return (None = no limit)

Output: `ClaudeReadPersistenceResponse`
- `file: str`
- `content: str`
- `size_bytes: int`
- `truncated: bool` — true when content was truncated by `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES`
- `modified_at: datetime | None`

Notes:
- Large files are automatically capped at `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES`
  (default 1 MiB). The `truncated` flag indicates if content was clipped.

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
- Do not store secrets, credentials, or full file dumps — keep entries
  small and high-signal.

### claude_update_persistence

Input: `ClaudeUpdatePersistenceRequest`
- `file: "agents" | "projects" | "memory"`
- `section_anchor: str` — heading text without the `## ` prefix
  (matching is case-insensitive and strips leading `#` and whitespace)
- `new_content: str`
- `mode: "replace" | "append"` (default: "replace") — replace the entire section vs append inside it
- `confirm: bool` (default: False) — required `True` to update `AGENTS.md`
  in safe mode (parity with `agy-mcp-server`).

Output: `ClaudeUpdatePersistenceResponse`
- `file: str`
- `section_anchor: str`
- `matched: bool` — true when the section_anchor was found and edited;
  false when the anchor was not found and no edit happened (so callers
  can detect typos before silent-append)
- `new_size_bytes: int`

Notes:
- Replacing/appending to `AGENTS.md` in safe mode raises `CONFIRM_REQUIRED` unless `confirm` is set to `True`.

### claude_load_persistence_context

Input: `ClaudeLoadPersistenceContextRequest`
- `include: list["agents" | "projects" | "memory"]` (default: all three)
- `max_chars_per_file: int` (default: 20000)

Truncation strategy (Phase 2, C4):
- Default is **asymmetric**: 20% head + 80% tail (configurable via
  `CLAUDE_MCP_PERSISTENCE_TRUNCATION_HEAD_RATIO`). This favors recency
  over ancient history.
- The marker between head and tail includes the number of chars
  omitted: `[truncated N chars]`.

Output: `ClaudeLoadPersistenceContextResponse`
- `agents_excerpt: str | None`
- `projects_excerpt: str | None`
- `memory_excerpt: str | None`
- `truncated_flags: dict[str, bool]`
- `total_chars: int`
- `base_dir: str`
- `initialized: bool`

Notes:
- Call this at the start of each session to hydrate orchestrator memory
  before dispatching tasks to `claude_run_task` / `claude_start_task`.
- Returns `initialized=false` if `claude_init_persistence` has not been
  called yet (no excerpts will be present).

Persistence settings (env vars, all `CLAUDE_MCP_PERSISTENCE_*`):
- `CLAUDE_MCP_PERSISTENCE_ENABLED` (default `true`)
- `CLAUDE_MCP_PERSISTENCE_LOCATION` (default `global`) — `"global"` or
  `"workspace"`. When `"workspace"`, files live in
  `<cwd_parent>/.open-cli-router/claude-code/` instead of
  `~/.open-cli-router/claude-code/`.
- `CLAUDE_MCP_PERSISTENCE_BASE_DIR` (default `~/.open-cli-router`) —
  accepts the special token `$cwd_parent` (parent of the server's CWD)
  for custom paths, e.g. `$cwd_parent/.my-persistence`.
- `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES` (default `524288` / 512 KiB;
  Phase 5 alignment with agy)
- `CLAUDE_MCP_PERSISTENCE_BACKUP_ON_WRITE` (default `false`)
- `CLAUDE_MCP_PERSISTENCE_BACKUP_KEEP` (default `10`) — number of
  `.bak` files to retain per source file (Phase 2, C3).
- `CLAUDE_MCP_PERSISTENCE_SEED_TEMPLATES` (default `true`)
- `CLAUDE_MCP_PERSISTENCE_TRUNCATION_HEAD_RATIO` (default `0.2`) —
  fraction of `max_chars_per_file` preserved at the head (Phase 2, C4).

---

## Prompts

The MCP server exposes the following 5 system prompts for coordinating workflows:

- `claude_sync_orchestration`: Instructs orchestrator agents on how to use `claude_run_task` to execute a single synchronous workspace command, inspect results, and handle changes (including parse_error recovery via `claude_list_runs`).
- `claude_async_orchestration`: Guides agents through the lifecycle of asynchronous background commands (start, poll with explicit 1s→10s backoff, cancel with SIGTERM→SIGKILL escalation, complete). Includes post-restart recovery via `claude_list_runs`.
- `claude_model_selection_guidance`: Informs the client agent how model parameters are passed; lists all 4 aliases (sonnet/fable/opus/haiku) with tier + cost + multi-file-safety annotations. Source of truth: `MODEL_REGISTRY` (not CLI docs).
- `claude_security_and_workspace_rules`: Enforces path boundaries, `--bare` execution, and restricts parameters in safe vs permissive mode.
- `claude_persistence_protocol`: Details the lifecycle of loading persistence context before a task and appending session learning/summary after a task.
- `claude_timeout_help`: Decision matrix + code snippet for `(task_class, files_to_edit, model_alias) → (timeout_s, must_use_async)`. Matrix reflects the requested model profile (not always sonnet).
- `claude_quickstart`: First-call cheat-sheet for new orchestrators (workspace_path discipline, tool catalog, common gotchas + troubleshoot).
- `claude_troubleshoot`: Pattern-matches an error string and returns a canonical fix recipe (NOT_ALLOWED, NOT_LOGGED_IN, MODEL_NOT_ALLOWED, CLAUDE_NOT_FOUND, etc.).

---

## Model Selection

Model selection is controlled per request by specifying the `model` parameter inside `claude_run_task` or `claude_start_task` (which passes `--model <model>` to the CLI process).

**Recommended aliases** (defined by `Settings.claude_model_aliases`,
defaults: `{sonnet, fable, opus, haiku}`):

- `sonnet`: Standard workhorse, multi-file safe, ~$0.50/run
- `fable`:  Mid-tier, multi-file safe, ~$0.30/run
- `opus`:   Flagship — only model with full reasoning depth; required for
           architecture / migration, ~$1.50/run
- `haiku`:  Cheapest, ~$0.02/run. **Avoid for >5-file edits**
           (multi_file_safe=False triggers a `compute_timeout` warning).

You can also specify full names like `claude-sonnet-4-6`. Aliases are
resolved to full CLI strings via `Settings.claude_model_aliases`
(env-overridable: `CLAUDE_MCP_CLAUDE_MODEL_ALIASES__SONNET` etc).

If `CLAUDE_MCP_ALLOWED_MODELS` is configured (default:
`{"sonnet", "fable", "opus", "haiku"}`), any requested model must reside
in that set, otherwise a `MODEL_NOT_ALLOWED` error is raised. If
`CLAUDE_MCP_ALLOWED_MODELS` is an empty set, validation is skipped.

## Model Registry & Timeout Policy

This section formalizes the model registry and task-class-aware timeout policy.

## Model Registry

The MCP server exposes 4 Claude model aliases via `MODEL_REGISTRY`
(`claude_code_mcp.models`):

| Alias    | CLI string (default, override via env) | Tier     | Cost     | multi_file_safe |
|----------|---------------------------------------|----------|----------|-----------------|
| `sonnet` | `claude-sonnet-5-...`                  | standard | ~$0.50   | true            |
| `fable`  | `claude-fable-5-...`                   | mid_tier | ~$0.30   | true            |
| `opus`   | `claude-opus-4-8-...`                  | flagship | ~$1.50   | true            |
| `haiku`  | `claude-haiku-4-5-...`                 | cheap    | ~$0.02   | false           |

Default `Settings.allowed_models = {sonnet, fable, opus, haiku}` —
previously `{sonnet, opus}` only. Fable and Haiku are now first-class.

## Task Class Taxonomy

`TaskClass` enum (10 values) classifies work by complexity:

- `trivial_edit`, `smoke_test`, `review` — sub-3-minute, sync only
- `single_feature`, `docs_update`, `test_suite` — sub-6-minute, sync only
- `multi_file_refactor` — variable; sync if ≤600s, async otherwise
- `architecture`, `migration` — always async, 1500-1800s typical
- `long_running` — always async, 3600s ceiling

## Timeout Policy Helper

`compute_timeout(task_class, model_profile, files_to_edit=1,
max_budget_usd=None) -> TimeoutRecommendation` is exported as a
public helper. Returns `(timeout_s, must_use_async, warning)`.

Constants:
- `SYNC_CEILING_S = 600` (FastMCP wrapper hard cap)
- `ASYNC_CEILING_S = 3600` (Pydantic validator upper bound)

Auto-bumps `timeout_s` based on `files_to_edit`:
- ≥5 files → ≥600s
- ≥20 files → ≥900s
- ≥50 files → ≥1800s

## Decision Matrix (files_to_edit=5, default)

| Task class                | timeout_s | must_use_async |
|---------------------------|-----------|----------------|
| trivial_edit              | 180       | false          |
| smoke_test                | 120       | false          |
| single_feature            | 300       | false          |
| docs_update               | 240       | false          |
| test_suite                | 360       | false          |
| review                    | 180       | false          |
| multi_file_refactor       | 600       | false          |
| architecture              | 1800      | true           |
| migration                 | 1500      | true           |
| long_running              | 3600      | true           |

## MCP Prompt: claude_timeout_help

`prompt_timeout_help(task_class, files_to_edit, model_alias)` returns
a structured guide with:
- Recommended (timeout_s, must_use_async) for the requested triple
- Pre-formatted python snippet (claude_run_task vs claude_start_task)
- Full decision matrix **scoped to the requested model profile** (not always sonnet)
- SYNC_CEILING_S / ASYNC_CEILING_S reference
- Sync/async routing rules
- Model registry listing (sonnet/fable/opus/haiku)

When `model_alias='haiku'` and `files_to_edit > 5`, the response emits
a `multi_file_safe=False` warning recommending opus or sonnet.

## Settings additions (this sprint)

- `claude_model_alias_default: str = "sonnet"`
- `claude_model_aliases: dict[str, str]` (4 entries, env-overridable)
- `timeout_policy_enabled: bool = False` (feature flag, OFF default)
- `timeout_policy_default_max_s: int = 3600`
- `timeout_policy_floor_s: int = 60`
- `allowed_models` default updated to `{sonnet, fable, opus, haiku}`

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
