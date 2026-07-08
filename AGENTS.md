# AGENTS.md

> Cross-reference guide for orchestrator agents (Femto, Claude Code,
> agy) using `claude-code-cli-mcp` as a delegate tool.

## Model selection

The 4 supported model aliases are `sonnet`, `fable`, `opus`, `haiku`.
Pick by task class:

| Task type                       | Recommended alias | Reason                                  |
|---------------------------------|-------------------|-----------------------------------------|
| Trivial edits, smoke tests      | `haiku`           | Cheapest, fastest, 6-10× cheaper than sonnet |
| Single-feature, docs, tests     | `sonnet`          | Default workhorse                       |
| Multi-file refactor (≤50 files) | `fable` or `sonnet` | Fable is cheaper for medium work      |
| Architecture, migration         | `opus`            | Flagship, only model with full reasoning depth |
| Long-running data agents        | `sonnet` or `fable` | Cost × latency sweet spot              |

Use `multi_file_safe=false` for Haiku on >5 file edits — emit a
warning to the user.

## Timeout policy

For every task, compute `timeout_s` from the task class. **Do not
hard-code 600s** — that's the sync ceiling, not the default for
every task.

Recommended pattern:

```python
from claude_code_mcp.models import MODEL_REGISTRY, TaskClass
from claude_code_mcp.timeout_policy import compute_timeout

profile = MODEL_REGISTRY[chosen_alias]
rec = compute_timeout(task_class, profile, files_to_edit=N)

if rec.must_use_async:
    run_id = claude_start_task(req={"prompt": "...", "timeout_s": rec.timeout_s, ...})
    # Poll with claude_poll_task until done
else:
    result = claude_run_task(req={"prompt": "...", "timeout_s": rec.timeout_s, ...})
```

Or call the MCP prompt:

```python
guide = prompt_timeout_help(task_class="...", files_to_edit=N, model_alias="...")
```

## Sync vs async decision

- `claude_run_task` (sync, max 600s) — small tasks only
- `claude_start_task` + `claude_poll_task` (async, up to 3600s) — anything that may exceed 600s

When `must_use_async=true`, the orchestrator MUST use `start_task`.
A `run_task` call with `timeout_s > 600` will time out at the wrapper
layer (FastMCP hard cap) before the actual work completes.

## Workspace discipline

`workspace_path` is auto-filled from MCP context. Do NOT pass it
explicitly unless working cross-repo. See AGENTS.md in the parent
CLI-router project for cross-repo rules.

## Recovery from a hung task

If `claude_run_task` returns a `parse_error` or hangs past
`timeout_s`, the subprocess may still be running. Recovery:

1. `claude_list_runs(req={limit: 5})` to find the run_id
2. `claude_poll_task(req={run_id, drain: true, wait_seconds: 30})` to drain output
3. `claude_cancel_task(req={run_id})` if it must be stopped

For long-running tasks, ALWAYS use `claude_start_task` (async) to
avoid the parse_error-on-timeout failure mode.
