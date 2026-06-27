# Claude Code CLI MCP Server

Version: 0.1.0

A local STDIO MCP server that exposes tools and reusable prompts for running the Anthropic Claude Code CLI (`claude`) inside a controlled workspace.

## Links

- Model Context Protocol (MCP): https://modelcontextprotocol.io/
- FastMCP: https://github.com/PrefectHQ/fastmcp
- FastMCP docs: https://gofastmcp.com/
- uv: https://github.com/astral-sh/uv
- uv docs: https://docs.astral.sh/uv/
- Pydantic: https://github.com/pydantic/pydantic
- Pydantic Settings: https://github.com/pydantic/pydantic-settings
- Claude Code CLI: https://claude.ai/

## Why This Project Exists

This project is a multi-provider fork of `agy-mcp-server` (the sister project wrapping the Google Antigravity CLI). Both projects share the same underlying architecture to provide a thin, secure CLI shim that exposes terminal-based AI tools as Model Context Protocol (MCP) servers.

Using the Claude Code CLI directly inside an editor or orchestrator workspace poses security and configuration challenges. This MCP server encapsulates the `claude` process, enforcing directory access controls, standardizing outputs, and providing a file-based persistent memory layer so that system prompts and session history survive across project reloads.

Now you can leverage your Claude Pro/Team subscription or `ANTHROPIC_API_KEY` seamlessly inside any MCP-enabled IDE:
- Cursor
- Windsurf
- Trae

## Features

**Tools**

- `claude_health`: Checks that the `claude` binary is installed, authenticated, and returns its version and authentication status.
- `claude_run_task`: Runs a synchronous (blocking) task inside the target workspace directory. Accepts `model` and `fallback_model` to select the Claude model per-run.
- `claude_start_task`: Starts an asynchronous background task inside the target workspace. Accepts the same `model` and `fallback_model` parameters.
- `claude_poll_task`: Polls the status, stdout, stderr, and output changes of an active background task.
- `claude_cancel_task`: Cancels a running background task (supporting an optional force signal).
- `claude_list_runs`: Lists recent tasks managed by the server and their statuses.
- `claude_init_persistence`: Initializes the persistence layer folder and creates template markdown files.
- `claude_read_persistence`: Reads the contents of a persistent memory file (`AGENTS.md`, `PROJECTS.md`, or `MEMORY.md`).
- `claude_append_persistence`: Appends high-signal updates (e.g., session memories) to a persistence file.
- `claude_update_persistence`: Replaces or appends to a specific section of a persistence file by heading anchor.
- `claude_load_persistence_context`: Loads the persistent files as truncated excerpts to inject into session context.

**Prompts**

- `claude_sync_orchestration`: Guidance playbook for executing synchronous tasks safely via `claude_run_task`.
- `claude_async_orchestration`: Playbook for orchestrating background tasks via `claude_start_task` and `claude_poll_task`.
- `claude_model_selection_guidance`: Outlines model selection constraints, allowed models, and aliases.
- `claude_security_and_workspace_rules`: Summarizes sandbox limits and rules for safe and permissive environments.
- `claude_persistence_protocol`: Guides the orchestrator on maintaining the persistent memory files.

## Persistent Memory

This MCP server features a file-based persistence layer stored by default under `~/.open-cli-router/claude-code/`. The directory contains three editable markdown files:

1. `AGENTS.md`: The system prompt or agent persona instructions.
2. `PROJECTS.md`: Summaries and structures of active projects.
3. `MEMORY.md`: Chronological log of high-signal session takeaways and permanent learning.

When persistence is enabled, the server automatically loads these files and prepends their formatted excerpts to the prompt sent to the `claude` CLI, ensuring cross-session state persistence.

### Storage location: global vs workspace

The persistence directory can live in **two places**, controlled by
`CLAUDE_MCP_PERSISTENCE_LOCATION`:

| Mode | Location | Use case |
|------|----------|----------|
| `global` (default) | `~/.open-cli-router/claude-code/` | User-level, persists across projects, survives `cd` |
| `workspace` | `<cwd_parent>/.open-cli-router/claude-code/` | Project-level, can be committed (use `.gitignore`!), portable with the repo |

`<cwd_parent>` is the parent of the server's CWD — for a typical setup,
the server's CWD is the server project directory (e.g.
`/home/user/CLI-router-project/claude-code-cli-mcp`), so the workspace
mode resolves to `/home/user/CLI-router-project/.open-cli-router/claude-code/`.

**Escape hatch:** `CLAUDE_MCP_PERSISTENCE_BASE_DIR="$cwd_parent/.my-persistence"` lets
you pick any custom subdirectory under the workspace root.

> ⚠️ When using `workspace` mode, add `.open-cli-router/` to `.gitignore`
> to avoid accidentally committing agent memory to source control.

### Two-level configuration

Persistence is controlled by **both** server-level environment variables and runtime MCP tool calls:

**Server level** (set in the MCP client `env` block — see [MCP Client Configuration](#mcp-client-configuration)):

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_MCP_PERSISTENCE_ENABLED` | Master switch for the persistence feature | `true` |
| `CLAUDE_MCP_PERSISTENCE_LOCATION` | `"global"` (in `~`) or `"workspace"` (in `<cwd_parent>/.open-cli-router/`) | `global` |
| `CLAUDE_MCP_PERSISTENCE_BASE_DIR` | Base directory; the namespace `claude-code` is appended automatically. Supports `$cwd_parent` token for custom workspace paths. | `~/.open-cli-router` |
| `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES` | Maximum size per file before writes are rejected | `524288` (512 KiB; aligned with agy in Phase 5) |
| `CLAUDE_MCP_PERSISTENCE_BACKUP_ON_WRITE` | Create `.bak` before each modification | `false` |
| `CLAUDE_MCP_PERSISTENCE_BACKUP_KEEP` | Number of `.bak` files to retain per source file (rotation) | `10` |
| `CLAUDE_MCP_PERSISTENCE_SEED_TEMPLATES` | Seed default markdown content when initializing | `true` |
| `CLAUDE_MCP_PERSISTENCE_TRUNCATION_HEAD_RATIO` | Fraction of `max_chars_per_file` preserved at head (rest is tail). Lower = more recency. | `0.2` (20% head / 80% tail) |

**Runtime level** (called by the orchestrator via MCP tools):

1. **Initialize once** — call `claude_init_persistence` to create the directory and seed the three files.
2. **Load context** — call `claude_load_persistence_context` at the start of each session to inject excerpts into the next prompt.
3. **Append session notes** — after meaningful work, call `claude_append_persistence` on `MEMORY.md`.
4. **Update structured sections** — when the user changes `AGENTS.md` or `PROJECTS.md`, call `claude_update_persistence` to persist. **Note:** updating `AGENTS.md` in safe mode requires `confirm=true`.

Without step 1, persistence is **enabled but uninitialized** — the server will not inject any context until the directory exists.

### How to initialize the persistence directory

The persistence directory is created lazily — it does **not** exist by default. There are two equivalent ways to create it:

**Option A — via MCP (recommended, normal flow):**

Once both the MCP client configuration and the server are running, ask the orchestrator agent to call:

```text
Please call claude_init_persistence to create the persistence directory.
```

The tool seeds `AGENTS.md`, `PROJECTS.md`, `MEMORY.md`, and a `.initialized` marker under `~/.open-cli-router/claude-code/`.

**Option B — directly via Python (one-shot, useful for verification or first-time setup):**

From the project root (`claude-code-cli-mcp/`):

```bash
uv run python -c "from claude_code_mcp.persistence import PersistenceStore; from pathlib import Path; PersistenceStore(base_dir=Path.home()/'.open-cli-router', max_file_bytes=524288, backup_on_write=False, seed_templates=True).init()"
```

This call is idempotent — running it twice does not destroy existing data unless you pass `force=True`.

The seed templates are written in **English** so they can be edited by any language-aware agent later.

## Quickstart

Prerequisites:
- Python 3.11+
- `uv` installed
- `claude` CLI installed (via `curl -fsSL https://claude.ai/install.sh | bash`) and authenticated (via a Claude Pro/Team subscription stored at `~/.claude.json` or the `ANTHROPIC_API_KEY` environment variable).

Sync dependencies:

```bash
uv sync
```

Launch the server using STDIO transport:

```bash
uv run python -m fastmcp.cli run src/claude_code_mcp/server.py --transport stdio
```

## MCP Client Configuration

The server is launched by an MCP client (Trae, Cursor, Windsurf, etc.) over STDIO. The recommended setup uses `uvx` to install the package from a local source path on demand — no global Python install required.

### Trae / Cursor / Windsurf (`uvx` from local source)

Add to your MCP client configuration (`~/.trae/mcp.json`, `.cursor/mcp.json`, `.windsurf/mcp.json`, or the IDE's MCP settings panel):

```json
{
  "mcpServers": {
    "claude-code-cli-mcp": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--from",
        "/path/to/claude-code-cli-mcp",
        "fastmcp",
        "run",
        "src/claude_code_mcp/server.py"
      ],
      "cwd": "/path/to/claude-code-cli-mcp",
      "env": {
        "CLAUDE_MCP_MODE": "safe",
        "CLAUDE_MCP_ALLOWED_ROOTS": "[\"/path/to/your/projects\"]",
        "CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE": "true",
        "CLAUDE_MCP_ALLOWED_MODELS": "[\"sonnet\", \"opus\"]",
        "START_MCP_TIMEOUT_MS": "30000",
        "RUN_MCP_TIMEOUT_MS": "600000"
      }
    }
  }
}
```

> **Tip:** `--refresh` forces `uvx` to re-resolve the local source on every start. Drop it once you stop iterating on the server.
>
> **Tip:** `START_MCP_TIMEOUT_MS` and `RUN_MCP_TIMEOUT_MS` are **client-side** timeouts consumed by the Trae IDE (not by this server).

### Relevant environment variables

The most relevant variables for the `env` block are listed below. See [Configuration](#configuration) for the full reference.

| Variable | Purpose |
|----------|---------|
| `CLAUDE_MCP_MODE` | `safe` (default) or `permissive` |
| `CLAUDE_MCP_ALLOWED_ROOTS` | JSON list of workspace roots the server is allowed to access |
| `CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE` | `true` enforces sandboxing under safe mode |
| `CLAUDE_MCP_ALLOWED_MODELS` | JSON list of permitted model aliases / full names (e.g. `["sonnet", "opus"]`) |
| `CLAUDE_MCP_PERSISTENCE_ENABLED` | Enables the persistent memory layer (`true` by default) |
| `CLAUDE_MCP_PERSISTENCE_BASE_DIR` | Base directory for the persistence layer (`~/.open-cli-router`) |

> **Persistence is a two-level configuration.** The env vars above only **enable** the feature and pick the base directory. To actually create the files and start injecting context, the orchestrator must call `claude_init_persistence` once at startup. See [Persistent Memory](#persistent-memory) for the full lifecycle.

### Step-by-step Trae setup (Portuguese)

For a guided walkthrough in Portuguese, see [USO_TRAE.md](USO_TRAE.md).

## Running Tests

To install dev dependencies and execute the test suite:

```bash
uv sync --extra dev
uv run pytest
```

## Using This Server in Trae

For step-by-step instructions in Portuguese on setting up and invoking this server inside the Trae IDE, see [USO_TRAE.md](USO_TRAE.md).

## Configuration

The server is configured using environment variables prefixed with `CLAUDE_MCP_` via Pydantic Settings.

| Variable | Description | Default |
|----------|-------------|---------|
| `CLAUDE_MCP_MODE` | Server access mode (`safe` or `permissive`). | `"safe"` |
| `CLAUDE_MCP_ALLOWED_ROOTS` | JSON list of allowed workspace roots. | Current working directory |
| `CLAUDE_MCP_CLAUDE_PATH` | Path or command to invoke the `claude` CLI. | `"claude"` |
| `CLAUDE_MCP_CLAUDE_PATH_FALLBACKS` | Fallback absolute paths to search for the binary. | `["/usr/local/bin/claude", "/opt/homebrew/bin/claude", "~/.local/bin/claude"]` |
| `CLAUDE_MCP_FORCE_BARE` | Pass `--bare` to disable local workspace profiles and hooks. | `true` |
| `CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE` | Enforce sandbox execution in safe mode. | `true` |
| `CLAUDE_MCP_DEFAULT_PERMISSION_MODE` | Permission mode flag (`default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`). | `"acceptEdits"` |
| `CLAUDE_MCP_DEFAULT_TIMEOUT_S` | Maximum allowed execution time for tasks in seconds. | `600` |
| `CLAUDE_MCP_POLL_DEFAULT_WAIT_SECONDS` | Default wait time between polling iterations. | `0.5` |
| `CLAUDE_MCP_MAX_CONCURRENT_RUNS` | Maximum concurrent background executions allowed. | `10` |
| `CLAUDE_MCP_MAX_RUNS` | History size of completed tasks in the run store. | `50` |
| `CLAUDE_MCP_MAX_STDOUT_BYTES` | Maximum stdout bytes captured from the child process. | `1000000` (1 MiB) |
| `CLAUDE_MCP_MAX_STDERR_BYTES` | Maximum stderr bytes captured from the child process. | `200000` |
| `CLAUDE_MCP_ALLOWED_MODELS` | JSON set of permitted model names or aliases. | `["sonnet", "opus"]` |
| `CLAUDE_MCP_ALLOW_ENV_KEYS` | JSON list of allowed env keys to pass in permissive mode. | `[]` |
| `CLAUDE_MCP_ALLOW_EXTRA_ARGS` | JSON list of allowed CLI arguments in permissive mode. | `[]` |
| `CLAUDE_MCP_PERSISTENCE_ENABLED` | Enables the persistent markdown memory layer. | `true` |
| `CLAUDE_MCP_PERSISTENCE_BASE_DIR` | Base directory for the persistence layer. | `~/.open-cli-router` |
| `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES` | Maximum file size for persistence files before rejecting writes. | `1048576` (1 MiB) |
| `CLAUDE_MCP_PERSISTENCE_BACKUP_ON_WRITE` | Create a `.bak` backup copy of files before modification. | `false` |
| `CLAUDE_MCP_PERSISTENCE_SEED_TEMPLATES` | Seed default markdown files if missing on init. | `true` |
| `CLAUDE_MCP_LOGFIRE_TOKEN` | Optional Logfire token for application telemetry. | `None` |

## Model Selection

The MCP server exposes two optional parameters on `claude_run_task` and `claude_start_task` for selecting the model on a per-request basis:

- `model`: The primary model used for the run. Accepts a short alias (`"sonnet"`, `"opus"`, `"haiku"`) or a full model name (e.g., `"claude-sonnet-4-6"`).
- `fallback_model`: An optional automatic fallback model used when the primary model is overloaded. **Print-mode only** in the CLI — since both `claude_run_task` (sync) and `claude_start_task` (async) invoke the CLI via `-p`, this flag is safe to use in both modes.

Examples:

```json
{
  "workspace_path": "/abs/path/to/project",
  "prompt": "Refactor the auth module",
  "model": "sonnet",
  "fallback_model": "haiku"
}
```

```json
{
  "workspace_path": "/abs/path/to/project",
  "prompt": "Plan a migration to event sourcing",
  "model": "claude-sonnet-4-6"
}
```

Both fields are validated against the `CLAUDE_MCP_ALLOWED_MODELS` allowlist. When set, a `model` outside the allowlist returns `MODEL_NOT_ALLOWED`; the same check applies to `fallback_model`. An empty allowlist disables validation and permits any model requested.

For deeper guidance on choosing models and aliases, see the `claude_model_selection_guidance` prompt.

## Security

### Safe Mode (Default)
In safe mode (`CLAUDE_MCP_MODE=safe`):
- Sandbox execution is enforced (if `CLAUDE_MCP_FORCE_SANDBOX_IN_SAFE_MODE` is enabled).
- Custom environment overrides are blocked.
- Custom extra command-line arguments are blocked.
- Bypassing permissions (`bypassPermissions` or `dontAsk` permission modes) is rejected.

### Permissive Mode
In permissive mode (`CLAUDE_MCP_MODE=permissive`):
- Environment variable overrides are permitted only if they appear in the `CLAUDE_MCP_ALLOW_ENV_KEYS` list.
- Extra arguments are allowed only if they appear in `CLAUDE_MCP_ALLOW_EXTRA_ARGS`.
- Skipping permissions (`bypassPermissions` or `--dangerously-skip-permissions`) requires `"--dangerously-skip-permissions"` to be explicitly listed in `CLAUDE_MCP_ALLOW_EXTRA_ARGS`.

## Troubleshooting

### `CLAUDE_NOT_FOUND`
The server cannot locate the `claude` executable. Make sure it is installed (e.g. via the official installer) and added to your `PATH`. Alternatively, set `CLAUDE_MCP_CLAUDE_PATH` to the absolute path of the binary.

### `NOT_ALLOWED: workspace_path is outside allowed roots`
Your workspace path is outside of the configured roots. Include the target directory in the `CLAUDE_MCP_ALLOWED_ROOTS` JSON array, or launch the server from within the target folder.

### `PERSISTENCE_FILE_TOO_LARGE`
A persistence file has reached the maximum allowed bytes (default 1 MiB). Clean up or truncate obsolete entries in the file (`~/.open-cli-router/claude-code/MEMORY.md` or `PROJECTS.md`) to allow new writes.

### `CONFIRM_REQUIRED`
Modifying `AGENTS.md` (the system prompt) in safe mode requires setting the `confirm` parameter to `true` to ensure the override is intentional.

### `~/.open-cli-router/claude-code/` does not exist
The persistence directory is created lazily. The server will not create it on its own — you must initialize it once via `claude_init_persistence` (or the Python one-liner under [Persistent Memory → How to initialize](#how-to-initialize-the-persistence-directory)). Without this, the server is **enabled but uninitialized** and no context is injected into prompts.