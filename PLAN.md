# Plano de Criação — `claude-code-cli-mcp` (v4 final)

> Servidor MCP thin-CLI-shim que envolve o `claude` CLI da Anthropic, expondo-o como toolset para o Trae IDE.
>
> **v4** consolida a arquitetura multi-provider validada em `antigravity-cli-mcp` v0.0.1:
> - Padronização de nomes via `PROVIDER_PREFIX` (fork-safe)
> - Camada de persistência file-based em `~/.open-cli-router/claude-code/`
> - 5 tools de persistência + 1 prompt de protocolo
> - Injeção automática de contexto persistente em `claude_run_task` / `claude_start_task`
>
> v3 incorporava: pesquisa na doc oficial do Claude Code + 6 achados do review do `agy-agent` + respostas próprias para revisão complementar interrompida.

## 0. Motivação — arquitetura multi-provider

A família `open-cli-router` (atualmente `antigravity-cli-mcp`, em breve
`claude-code-cli-mcp`, depois `codex-cli-mcp`) compartilha a mesma
arquitetura base. Cada fork difere apenas em:

1. **Binário alvo** (`agy` vs `claude` vs `codex`) e suas flags
2. **Provider prefix** (`PROVIDER_PREFIX = "agy"` vs `"claude"` vs `"codex"`)
3. **Namespace de persistência** (derivado de `PROVIDER_PREFIX`)

O resto é copy-paste com `sed`:

```
agy_mcp_server/        -> claude_code_mcp/
agy_                   -> claude_
AGY_MCP_               -> CLAUDE_MCP_
agy_path               -> claude_path
~/.open-cli-router/agy -> ~/.open-cli-router/claude-code
PROVIDER_PREFIX="agy"  -> PROVIDER_PREFIX="claude"
```

Esse fork-safe é garantido pelo módulo `provider.py` (single source of truth):

```python
PROVIDER_PREFIX: str = "claude"   # era "agy" no outro fork

def tool_name(suffix: str) -> str:
    return f"{PROVIDER_PREFIX}_{suffix}"   # "claude_health", "claude_run_task", ...

def prompt_name(suffix: str) -> str:
    return tool_name(suffix)               # "claude_persistence_protocol", ...
```

Todas as tools, prompts e referências visíveis ao usuário passam por
`tool_name(...)` / `prompt_name(...)`. Renomear `PROVIDER_PREFIX` renomeia
toda a API automaticamente.

---

## 1. Metadados

- **Nome**: `claude-code-cli-mcp`
- **Diretório**: `/home/bill/Codes/CLI-router-project/claude-code-cli-mcp/`
- **Versão inicial**: `0.1.0`
- **Python mínimo**: 3.11
- **Dependências**: `fastmcp>=3.0.0`, `pydantic>=2.5`, `pydantic-settings>=2.1`, `loguru>=0.7`, `logfire>=3.0`
- **Engine externo**: `claude` CLI (instalado via `curl -fsSL https://claude.ai/install.sh | bash`)
- **Provider**: Claude.ai subscription (auth via `~/.claude.json`)
- **Inspiração**: `antigravity-cli-mcp` v0.0.1 (fork estrutural, com adaptações de flags)

## 2. Goals & Non-Goals

### Goals

- Expor `claude` em print mode (`-p`) como tools MCP com forma inspirada em `agy_*` (mas com prefixo `claude_` derivado de `PROVIDER_PREFIX`).
- Async: `--output-format stream-json --verbose --include-partial-messages`.
- Sync: `--output-format json` (single result blob).
- Sessões com `--session-id` (fresh UUID) + `--resume` (opcional).
- 5 permission modes mapeados aos modos do MCP server.
- **Sempre** passar `--bare` ao child `claude` (bloqueia skills/hooks/MCP/CLAUDE.md do workspace).
- Hardening: `allowed_roots`, sandbox, env allowlist, extra_args allowlist.
- Capturar campos do `result` event: `session_id`, `total_cost_usd`, `duration_ms`, `num_turns`, `modelUsage`.
- Observabilidade via Logfire.
- **Camada de persistência** herdada de `agy-mcp-server` (versão 0.0.1), adaptada para namespace `claude-code/`.
- Documentação para Trae IDE.

### Non-Goals

- Não usar `claude-agent-sdk`.
- Não expor hooks/subagentes/agent teams/plugins/skills/slash commands como tools.
- Não executar `claude mcp` subcommand.
- Não fazer `claude auth login` interativo.
- Não expor TUI.
- Não replicar checkpointing/rewind.
- Não tocar em `~/.claude.json` (credenciais).
- Não implementar Dream/Consolidator de memória na v1.
- Não versionar `MEMORY.md` com git na v1 (opt-in v2).

## 3. Arquitetura

### 3.1 Componentes

```
Trae IDE
   | JSON-RPC stdio
   v
claude-code-cli-mcp (FastMCP)
   |- provider.py        (PROVIDER_PREFIX = "claude", tool_name, prompt_name)
   |- settings.py, models.py
   |- run_store.py, rolling_buffer.py, changes.py
   |- claude_args.py     (request -> CLI flags)
   |- claude_stream.py   (NDJSON parser)
   |- claude_runner.py   (Popen + args + stream + cancel)
   |- persistence/       (multi-provider file-based memory)
   |   |- __init__.py
   |   |- paths.py       (resolve ~/.open-cli-router/claude-code/)
   |   |- store.py       (PersistenceStore: init/read/append/update/load_context)
   |   |- locks.py       (threading.Lock global)
   |   |- templates.py   (seeds AGENTS.md, PROJECTS.md, MEMORY.md)
   |- server.py          (@mcp.tool, @mcp.prompt)
   | subprocess.Popen (args=[...], shell=False)
   v
claude CLI (com --bare, --output-format json|stream-json)
```

### 3.2 Mapeamento de modos

| Tool MCP | Flags essenciais do `claude` |
|---|---|
| `claude_health` | `--version` + `auth status --text` (sync, sem spawn de agent) |
| `claude_run_task` (sync) | `-p "..." --output-format json --bare --no-session-persistence` |
| `claude_start_task` (async) | `-p "..." --output-format stream-json --verbose --include-partial-messages --bare --session-id <uuid>` |
| `claude_poll_task` | (sem spawn; lê buffer in-memory) |
| `claude_cancel_task` | `os.killpg(SIGTERM)` + fallback `SIGKILL` 5s |
| `claude_list_runs` | (sem spawn; serializa RunStore) |

### 3.3 Tools de persistência (novas em v4)

| Tool MCP | Função |
|---|---|
| `claude_init_persistence` | Cria `~/.open-cli-router/claude-code/` com `AGENTS.md`, `PROJECTS.md`, `MEMORY.md` (idempotente). |
| `claude_read_persistence` | Lê um dos 3 arquivos (com `offset`/`limit` em bytes). |
| `claude_append_persistence` | Anexa conteúdo (uso típico: pós-sessão em `MEMORY.md`). |
| `claude_update_persistence` | Substitui seção por heading anchor (`mode="replace"` ou `"append"`). |
| `claude_load_persistence_context` | Retorna excerpts (head+tail) dos 3 arquivos para a sessão atual. |

### 3.4 Layout de persistência em runtime

```
~/.open-cli-router/                     ← PERSISTENCE_BASE_DIR (compartilhado)
├── agy/                                ← fork 1 (já existente)
├── claude-code/                        ← fork 2 (este plano)
│   ├── AGENTS.md                       ← system-prompt editável
│   ├── PROJECTS.md                     ← resumos de projetos
│   ├── MEMORY.md                       ← memória permanente
│   ├── .initialized                    ← marker JSON (criado por claude_init_persistence)
│   └── .backups/                       ← backups automáticos (opt-in)
└── codex/                              ← fork 3 (futuro)
```

O namespace `claude-code/` é **derivado automaticamente** de
`PROVIDER_PREFIX = "claude"` em `paths.py:get_persistence_dir()`. Nenhuma
string `claude-code` é hard-coded fora de `provider.py`.

### 3.5 Integração automática de contexto

Quando `persistence_enabled=true` E `~/.open-cli-router/claude-code/.initialized`
existe, `claude_run_task` e `claude_start_task` chamam internamente
`_persistence_store.load_context()` e prependem os excerpts como tags XML
no prompt antes de passar ao `claude`:

```xml
<persistent-agents-context>
[excerpt de AGENTS.md]
</persistent-agents-context>

<persistent-projects-context>
[excerpt de PROJECTS.md]
</persistent-projects-context>

<persistent-memory-context>
[excerpt de MEMORY.md]
</persistent-memory-context>

<user prompt>
[prompt original da tool]
</user prompt>
```

Falha ao carregar contexto é **não-fatal**: warning em `notes`, execução
prossegue sem contexto.

## 4. Estrutura de arquivos

```
claude-code-cli-mcp/
├── .env.example
├── .gitignore
├── CONTRATO_TOOLS.md
├── PLAN.md
├── README.md
├── USO_TRAE.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── claude_code_mcp/
│       ├── __init__.py
│       ├── provider.py            ← PROVIDER_PREFIX = "claude"
│       ├── server.py
│       ├── settings.py
│       ├── models.py
│       ├── run_store.py
│       ├── rolling_buffer.py
│       ├── changes.py
│       ├── claude_args.py
│       ├── claude_stream.py
│       ├── claude_runner.py
│       ├── logfire_setup.py
│       └── persistence/           ← fork do agy-mcp (mesma estrutura)
│           ├── __init__.py
│           ├── paths.py
│           ├── store.py
│           ├── locks.py
│           └── templates.py
└── tests/
    ├── test_integration_contract.py
    ├── test_security.py
    ├── test_claude_runner.py
    ├── test_provider.py           ← garante PROVIDER_PREFIX renomeia tools/prompts
    └── test_persistence.py        ← paths, atomic write, locks, integração
```

## 5. Contrato de tools

> Todas as tools usam `name=tool_name("<suffix>")` em `@mcp.tool`. Os nomes
> concretos (`claude_health`, `claude_run_task`, ...) são derivados em runtime
> de `PROVIDER_PREFIX = "claude"`. Esta seção documenta os **sufixos** e seus
> schemas; os nomes finais são `claude_<suffix>`.

### 5.1 Tools de execução

#### ClaudeRunTaskRequest
- `prompt: str`
- `workspace_path: str` (validado contra `allowed_roots`)
- `model: str | None`
- `max_turns: int | None`
- `max_budget_usd: float | None`
- `effort: Literal["low","medium","high","max"] | None`
- `permission_mode: Literal["default","acceptEdits","plan","dontAsk","bypassPermissions"]` (default `"acceptEdits"`)
- `allowed_tools: list[str] | None`
- `disallowed_tools: list[str] | None`
- `system_prompt_append: str | None`
- `capture_changes: bool` (default `True`)
- `change_scope: Literal["workspace","git_only"]` (default `"workspace"`)
- `timeout_seconds: int` (default 600)
- `extra_args: list[str] | None`
- `env: dict[str, str] | None`

#### ClaudeRunTaskResponse (sync — single result blob)
- `run_id: str`
- `status: Literal["done","error","timeout","cancelled"]`
- `exit_code: int`
- `final_text: str`
- `result: dict`
- `changes: dict | None`
- `session_id: str | None`
- `total_cost_usd: float | None`
- `duration_ms: int | None`
- `num_turns: int | None`
- `model_usage: dict | None`

#### ClaudeStartTaskRequest
- Mesmos campos de `ClaudeRunTaskRequest`
- Acrescenta: `resume_session_id: str | None` (vira `--resume <id>`)

#### ClaudePollTaskRequest
- `run_id: str`
- `wait_seconds: float` (default 0.5)
- `drain: bool` (default False — se True, bloqueia até done)

#### ClaudePollTaskResponse (async — NDJSON delta)
- `run_id: str`
- `status: Literal["running","done","error","timeout","cancelled"]`
- `new_messages: list[dict]`
- `result: dict | None`
- `stdout_len: int`
- `elapsed_seconds: float`

#### ClaudeCancelTaskRequest
- `run_id: str`
- `force: bool` (default False)

#### ClaudeListRunsRequest
- `limit: int` (default 50)

### 5.2 Tools de persistência

#### ClaudeInitPersistenceRequest
```python
class ClaudeInitPersistenceRequest(BaseModel):
    force: bool = False                  # sobrescreve arquivos existentes
    seed_templates: bool | None = None   # None = settings.persistence_seed_templates
```
**Response:** `base_dir`, `created: list[str]`, `already_existed: list[str]`, `seed_version: str`

#### ClaudeReadPersistenceRequest
```python
class ClaudeReadPersistenceRequest(BaseModel):
    file: Literal["agents", "projects", "memory"]
    offset: int = 0
    limit: int | None = None
```
**Response:** `file`, `content`, `size_bytes`, `truncated`, `modified_at`

#### ClaudeAppendPersistenceRequest
```python
class ClaudeAppendPersistenceRequest(BaseModel):
    file: Literal["agents", "projects", "memory"]
    content: str
    section_header: str | None = None   # insere `## <header>` se ausente
```
**Response:** `file`, `appended_bytes`, `new_size_bytes`, `timestamp`

#### ClaudeUpdatePersistenceRequest
```python
class ClaudeUpdatePersistenceRequest(BaseModel):
    file: Literal["agents", "projects", "memory"]
    section_anchor: str                 # `## <anchor>` (sem prefixo)
    new_content: str
    mode: Literal["replace", "append"] = "replace"
```
**Response:** `file`, `section_anchor`, `matched: bool`, `new_size_bytes`

#### ClaudeLoadPersistenceContextRequest
```python
class ClaudeLoadPersistenceContextRequest(BaseModel):
    include: list[Literal["agents","projects","memory"]] = ["agents","projects","memory"]
    max_chars_per_file: int = 20_000
```
**Response:** `agents_excerpt`, `projects_excerpt`, `memory_excerpt`, `truncated_flags`, `total_chars`, `base_dir`, `initialized`

## 6. Settings (env prefix `CLAUDE_MCP_`)

### 6.1 Settings herdados de `agy-mcp`

```python
claude_path: str = "claude"
claude_path_fallbacks: list[str] = ["/usr/local/bin/claude","/opt/homebrew/bin/claude","~/.local/bin/claude"]
mode: Literal["safe","permissive"] = "safe"
default_permission_mode: str = "acceptEdits"
force_bare: bool = True
force_sandbox_in_safe_mode: bool = True
allowed_roots: list[Path]
run_timeout_seconds: int = 600
poll_default_wait_seconds: float = 0.5
max_concurrent_runs: int = 10
max_runs: int = 50
max_stdout_bytes: int = 1_000_000
max_stderr_bytes: int = 200_000
allowed_models: set[str] = {"sonnet","opus"}
allow_env_keys: set[str]
allow_extra_args: set[str]
logfire_token: str | None
```

### 6.2 Settings de persistência (novos em v4)

```python
# ---- Persistence ----
persistence_enabled: bool = True
persistence_base_dir: Path = Path("~/.open-cli-router")
persistence_max_file_bytes: int = 524_288       # 512 KiB
persistence_backup_on_write: bool = False
persistence_seed_templates: bool = True
```

Variáveis de ambiente correspondentes:
- `CLAUDE_MCP_PERSISTENCE_ENABLED`
- `CLAUDE_MCP_PERSISTENCE_BASE_DIR`
- `CLAUDE_MCP_PERSISTENCE_MAX_FILE_BYTES`
- `CLAUDE_MCP_PERSISTENCE_BACKUP_ON_WRITE`
- `CLAUDE_MCP_PERSISTENCE_SEED_TEMPLATES`

**Comportamento quando `persistence_enabled=false`:** tools de persistência
continuam registradas no schema MCP (facilita debugging e mantém schema
estável para o Trae), mas retornam `PERSISTENCE_DISABLED`. `claude_run_task`
e `claude_start_task` simplesmente não prependem contexto.

## 7. Env vars passadas ao subprocess `claude`

**Sempre** (independente de mode):
- `CLAUDE_CODE_HIDE_ACCOUNT_INFO=1`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` (não carrega CLAUDE.md do workspace)
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (não cria auto-memory)
- `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING=1`

**safe mode (+)**:
- `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
- `CLAUDE_CODE_DISABLE_CRON=1`
- `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`

**permissive mode (+)**:
- `CLAUDE_CODE_ENABLE_TELEMETRY=1`

## 8. Mapeamento permission mode -> safe mode

| Mode | safe | permissive | Flag extra necessária |
|---|---|---|---|
| `default` | ok | ok | — |
| `acceptEdits` | ok | ok | — |
| `plan` | ok | ok | — |
| `dontAsk` | só com allowlist | ok | — |
| `bypassPermissions` | bloqueado | só com `--dangerously-skip-permissions` em `allow_extra_args` | `--dangerously-skip-permissions` |
| `auto` | bloqueado (requer Max/Team/Enterprise/API) | idem | sempre bloqueado |

## 9. Phase 1 — fork-and-rename: edições concretas

| Arquivo origem | Edições |
|---|---|
| `agy_mcp_server/provider.py` | `PROVIDER_PREFIX = "claude"` (única mudança). Helpers `tool_name` / `prompt_name` ficam idênticos. |
| `agy_mcp_server/settings.py` | rename `AGY_MCP_` -> `CLAUDE_MCP_`; **dropar** `fix_antigravity_mcp_config` e `antigravity_mcp_config_path`; renomear `agy_path` -> `claude_path`; **adicionar** `claude_path_fallbacks`, `force_bare=True`, `max_concurrent_runs=10`, `default_permission_mode`, `allowed_models`; **adicionar** bloco Persistence §6.2. |
| `agy_mcp_server/hardening.py` | **dropar** `ensure_valid_mcp_config_json` e `_atomic_write_json` (Antigravity-specific; `~/.claude.json` é imutável). |
| `agy_mcp_server/run_store.py` | rename; **adicionar** enforcement de `max_concurrent_runs` em `start_run`. |
| `agy_mcp_server/rolling_buffer.py` | rename; sem mudança de lógica. |
| `agy_mcp_server/changes.py` | manter (git detection é universal). |
| `agy_mcp_server/persistence/` | **fork integral** (paths.py, store.py, locks.py, templates.py). Namespace `claude-code/` é derivado de `PROVIDER_PREFIX` — nenhuma mudança de código necessária além do `PROVIDER_PREFIX`. |
| `agy_mcp_server/server.py` | imports de `tool_name`, `prompt_name`, `PROVIDER_PREFIX`; tools e prompts usam `name=tool_name(...)` / `name=prompt_name(...)`; 5 tools de persistência + 1 prompt novos; integração de `load_context` em `claude_run_task` / `claude_start_task`. |
| `agy_mcp_server/models.py` | adicionar 5 Request/Response de persistência (ou fork do bloco correspondente). |

## 10. Subprocess invocation

- `subprocess.Popen(args=[...], shell=False)`, `start_new_session=True`.
- Prompt passado como **um único argv**: `args = ["-p", request.prompt]`.
- Sem escaping manual; sem `shell=True`; sem heredoc.

## 11. `--bare` semantics

Da doc oficial:
- **Desliga**: hooks, skills, plugins, MCP servers, auto memory, CLAUDE.md discovery.
- **Mantém**: built-in tools (`Bash`, `Read`, `Edit`, `Glob`, `Grep`, `WebSearch`).
- **Seta env**: `CLAUDE_CODE_SIMPLE=1`.
- **Não** oferecer override (segurança).

## 12. Sessões

- `claude_start_task`: gera UUID v4 fresh por run; passa via `--session-id`.
- `resume_session_id` em `ClaudeStartTaskRequest` vira `--resume <id>`.
- `RunStore.start_run` rejeita UUIDs duplicados.

## 13. Camada de persistência — detalhes

### 13.1 Atomicidade e locking

- `persistence/locks.py` exporta `persistence_lock()` — `threading.Lock`
  global serializando **todas** as escritas (init, append, update) entre
  tools MCP concorrentes.
- `persistence/store.py:_atomic_write(path, content)` grava em
  `<file>.tmp.<uuid>` e usa `os.replace()` para tornar a escrita
  atômica no Linux. Em caso de exceção, o arquivo original permanece
  intacto.

### 13.2 Segurança

- **Path validation:** `resolve_file_path` chama `Path.resolve()` e
  verifica `is_relative_to(<base>/<provider>)`. Symlinks intermediários
  são recusados.
- **File size cap:** `persistence_max_file_bytes` aplicado em leitura E
  escrita. Escrita que excederia o limite falha com
  `PERSISTENCE_FILE_TOO_LARGE` sem truncar.
- **Literal whitelist:** as tools aceitam apenas `Literal["agents",
  "projects", "memory"]`. `file="../../etc/passwd"` é rejeitado pelo
  validador Pydantic antes de chegar ao filesystem.
- **Mode-aware:** em `mode="safe"`, `claude_update_persistence` em
  `AGENTS.md` exige `confirm=true` (campo novo opcional). Em
  `mode="permissive"`, segue livre.

### 13.3 Templates seed (adaptados do agy-mcp)

`AGENTS.md` template (substituir `{provider}` por `claude`):

```markdown
# AGENTS — Claude Code CLI

> System prompt editável para o agente orquestrador (Trae IDE) que
> consome o MCP `claude-code-cli-mcp`. Edite livremente.

## Identidade

Você é um agente orquestrador que usa o Claude Code CLI (`claude`)
como backend de raciocínio via este MCP.

## Diretrizes

1. Antes de cada tarefa, chame `claude_load_persistence_context`.
2. Após cada sessão, chame
   `claude_append_persistence(file="memory", ...)` com resumo curto.
3. Nunca exponha o conteúdo de `~/.open-cli-router/claude-code/` em logs.
```

`PROJECTS.md` e `MEMORY.md` seguem o mesmo padrão do `agy-mcp`, com
`{provider}` resolvido para `claude`.

### 13.4 Fork-safety

`tests/test_provider.py::test_forking_provider_prefix_renames_all_tools`
valida que mudar `PROVIDER_PREFIX` propaga para tools, prompts e
namespace de persistência. O `agy-mcp-server` já cobre esse cenário;
o `claude-code-cli-mcp` deve ter teste equivalente.

## 14. Phases de implementação (7.5 dias)

| Phase | Descrição | Dias |
|---|---|---|
| 0 | Scaffolding (`pyproject.toml`, `uv sync`, esqueleto) | 0.5 |
| 1 | Fork-and-rename de `agy_mcp_server` (inclui `provider.py`) | 0.5 |
| 2 | `claude_args.py` (request -> flags) | 0.5 |
| 3 | `claude_stream.py` (NDJSON parser com `result` event) | 1.0 |
| 4 | `claude_runner.py` (Popen + args + stream + cancel) | 1.0 |
| 5 | 6 tools de execução no `server.py` | 1.0 |
| 6 | 4 prompts MCP de execução | 0.5 |
| **7** | **Camada de persistência: fork `persistence/` + 5 tools + 1 prompt + integração em `claude_run_task`/`claude_start_task`** | **1.0** |
| 8 | Testes (unit + contract + security + persistence + provider) | 1.0 |
| 9 | Docs (README, USO_TRAE, CONTRATO_TOOLS, .env.example) | 0.5 |
| 10 | Validação ponta a ponta | 0.5 |
| **Total** | | **8.0** |

Phase 7 é **nova** em relação ao v3 e adiciona ~1 dia ao cronograma
(consistente com o que o `agy-mcp` consumiu para a mesma feature).

## 15. Testes críticos

### 15.1 Testes de execução (mantidos do v3)

1. **Env leakage**: parent tem `ANTHROPIC_API_KEY=secret`; child env NÃO contém.
2. **Session-ID collision**: dois `start_run` simultâneos com mesmo UUID -> segundo rejeitado.
3. **Sandbox bypass**: `mode=permissive`, `permission_mode=bypassPermissions` sem `allow_extra_args` -> `ValueError`.
4. **`--bare` regression**: workspace com `.claude/settings.json` malicioso -> child não carrega.
5. **NDJSON hardening**: linha parcial, JSON malformado, `result` sem `modelUsage`, 1.5MB de eventos.

### 15.2 Testes de persistência (novos em v4 — herdados do agy-mcp)

1. `test_persistence_disabled_raises` — `persistence_enabled=false` → tools retornam `PERSISTENCE_DISABLED`.
2. `test_init_persistence_creates_files` — cria os 3 arquivos + `.initialized` com templates.
3. `test_init_persistence_idempotent` — 2ª chamada sem `force` retorna `already_existed`.
4. `test_path_traversal_blocked` — `file="../../etc/passwd"` rejeitado pelo Literal.
5. `test_symlink_escape_blocked` — symlink fora de `base_dir` é recusado.
6. `test_atomic_write_no_partial_file` — exceção simulada em `os.replace` → target intacto.
7. `test_concurrent_writes_serialized` — 10 threads × 100 appends → 1000 entradas sem perda.
8. `test_file_size_cap_enforced` — escrita que excederia cap falha com `FILE_TOO_LARGE`.
9. `test_append_section_header_inserts_when_missing`.
10. `test_update_section_replaces_existing` / `test_update_section_miss_returns_matched_false`.
11. `test_load_context_returns_truncation_flags`.
12. `test_load_context_uninitialized_returns_false`.
13. `test_run_task_loads_context_automatically` — mock `_persistence_store.load_context`; verifica prepended XML.
14. `test_run_task_continues_if_context_load_fails` — exceção simulada → run continua com warning em `notes`.
15. `test_provider_namespace_uses_prefix` — `PROVIDER_PREFIX="claude"` → `~/.open-cli-router/claude-code/`.

### 15.3 Testes de provider (novos em v4)

1. `test_provider_prefix_constant_value` — `PROVIDER_PREFIX == "claude"`.
2. `test_tool_name_builds_correctly` — `tool_name("health") == "claude_health"`.
3. `test_prompt_name_matches_tool_name` — `prompt_name("persistence_protocol") == "claude_persistence_protocol"`.
4. `test_tool_name_rejects_invalid_suffix` — empty / non-snake-case → `ValueError`.
5. `test_forking_provider_prefix_renames_all_tools` — `monkeypatch` muda prefixo → tools mudam.
6. `test_provider_prefix_remaps_namespace` — mudança de prefixo remapeia `~/.open-cli-router/<prefix>/`.

**Total esperado:** ~25 testes novos, somando ~50-60 testes totais.

## 16. Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Workspace injeta MCP/skills/hooks | `--bare` sempre; `force_bare=True` em settings |
| Workspace injeta CLAUDE.md / auto-memory | `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` |
| `claude` requer login interativo | `claude_health` retorna erro claro; documentar `claude auth login` |
| Flags do `claude` quebram com versões novas | `claude_path` discovery + version check em `claude_health`; smoke test detecta drift |
| Permission mode `auto` exige plano Max+ | Não expor no schema; rejeitar se vier |
| Sessões persistem em `~/.claude/projects/<hash>/` | `--no-session-persistence` em sync; documentar cleanup em async |
| Sandbox quebra rede | Documentar; oferecer `mode=permissive` |
| 10+ runs concorrentes lotam o sistema | `max_concurrent_runs=10` em `RunStore.start_run` |
| NDJSON parsing lento com output grande | `ijson` streaming + `RollingTextBuffer` com cap |
| 1MB+ de stdout trava o parser | `max_stdout_bytes=1MB` enforced; trunca com warning |
| `~/.open-cli-router/` em FS read-only | Falha clara em `claude_init_persistence` com `BASE_DIR_NOT_WRITABLE` |
| Crescimento descontrolado de `MEMORY.md` | `persistence_max_file_bytes` (512 KiB); v2: Dream/Consolidator |
| Concorrência entre múltiplos MCPs (agy + claude) | Cada MCP escreve só no seu subdiretório — sem colisão |
| Mudança de schema MCP causa re-descoberta no Trae | Fallback de coerção `BeforeValidator(_coerce_empty_str_to_dict)` nos novos `*In` types (lição do `agy_quota`) |
| Backup `.bak` infinito em `persistence_backup_on_write=true` | v2: rotação automática; v1: documentar |

## 17. Decisões fechadas

| # | Decisão | Recomendação |
|---|---|---|
| 1 | Nome | `claude-code-cli-mcp` (alinhado com diretório) |
| 2 | `ANTHROPIC_API_KEY` em safe | Permitir se listado em `allow_env_keys` |
| 3 | Streaming | sync=json, async=stream-json; sem `stream: bool` |
| 4 | Session list/resume v1 | Só `resume_session_id` em StartTaskRequest; list em v1.1 |
| 5 | `CLAUDE_CODE_ENABLE_TELEMETRY` | off em safe; on em permissive |
| 6 | Padrão de naming | `tool_name(...)` / `prompt_name(...)` em todo `@mcp.tool`/`@mcp.prompt` |
| 7 | Namespace de persistência | derivado de `PROVIDER_PREFIX` (`claude-code/`) |
| 8 | Persistência habilitada por padrão | sim (`persistence_enabled=true`) |
| 9 | Auto-init no bootstrap | **não** — exige `claude_init_persistence` explícito |
| 10 | Injeção automática de contexto em `claude_run_task` | sim, mas não-fatal se falhar |
| 11 | Tools de persistência quando `persistence_enabled=false` | registradas, retornam `PERSISTENCE_DISABLED` |
| 12 | `AGENTS.md` em mode=safe exige `confirm=true` para update | sim |

## 18. Itens cobertos no plano

1. `claude` binary discovery: `shutil.which` + fallbacks.
2. `claude auth status` parsing: regex para `--text`.
3. `max_concurrent_runs` enforcement.
4. `--bare` regression test.
5. `~/.claude.json` imutabilidade documentada.
6. Padronização `PROVIDER_PREFIX` / `tool_name` / `prompt_name`.
7. Camada de persistência multi-provider.
8. Integração automática de contexto em tools de execução.

## 19. Open decisions (precisam do Bill)

1. `max_concurrent_runs`: 10 ok? Ou 5 / 20?
2. `max_stdout_bytes`: 1MB ok?
3. Logfire token: obrigatório ou optional?
4. Cleanup de `~/.claude/projects/` ao cancelar?
5. `persistence_max_file_bytes`: 512 KiB ok? Ou 1 MiB?
6. `claude_persistence_protocol` prompt: deve ser **default-on** (listado
   automaticamente pelo Trae) ou **opt-in** (apenas se o orquestrador
   listar prompts)?

## 20. Próximos passos (após aprovação)

1. `mkdir -p /home/bill/Codes/CLI-router-project/claude-code-cli-mcp/src/claude_code_mcp/persistence` ✅
2. Phase 0: scaffolding (`pyproject.toml`, `uv init`, deps)
3. Phase 1: fork-and-rename de `agy_mcp_server` (com `provider.py` apontando para `"claude"`)
4. Phase 2-4: módulos novos
5. Phase 5-6: tools e prompts de execução
6. Phase 7: persistência (fork do pacote + integração em `claude_run_task`)

---

## 21. Refatoração 2026-06 (changelog)

Esta refatoração endereçou **1 bug crítico** e propagou para o `claude-code-cli-mcp` todas as melhorias implementadas no `agy-mcp-server`. Executada em 7 fases, todas entregues.

### Phase 1 — Bug crítico (A1): template AGENTS.md apontava para path errado

- **Sintoma:** `AGENTS.md` seed continha `~/.open-cli-router/{provider}/` que era renderizado com `PROVIDER_PREFIX="claude"`, gerando `~/.open-cli-router/claude/` — diretório que **não existe** (o real é `claude-code/`).
- **Risco:** instruía o agente a "nunca expor" um path incorreto, deixando o path real (`claude-code/`) sem proteção.
- **Correção:** template usa agora `{namespace}` resolvido por `PERSISTENCE_NAMESPACE="claude-code"`.
- 2 testes novos em `test_persistence.py`.

### Phase 2 — Robustez do store (C1-C5)

Idêntica à do agy:
- **C1** Normalização de `section_header` (regex `^[#\s]+`) + dedup case-insensitive.
- **C2** Match case-insensitive em `_replace_section`.
- **C3** Backup rotation (`backup_keep: int = 10`).
- **C4** Truncamento assimétrico (`head_ratio: float = 0.2`, default 80% tail).
- **C5** Fix `read()` truncated flag.
- 17 testes novos.

### Phase 3 — Refatoração de integração

- Novo módulo [persistence/context.py](file:///home/bill/Codes/CLI-router-project/claude-code-cli-mcp/src/claude_code_mcp/persistence/context.py) com `build_prompt_with_context()` (Protocol-based).
- `_build_prompt_with_context` no `server.py` agora é wrapper fino (compatibilidade).
- 12 testes novos em `test_persistence_context.py`.

### Phase 4 — Feature nova: `persistence_location`

- Setting novo `persistence_location: Literal["global", "workspace"] = "global"`.
- Settings da Fase 3 do agy integrados: `backup_keep=10`, `truncation_head_ratio=0.2`.
- Método novo `Settings.resolve_persistence_base_dir()` com 3 modos: `$cwd_parent` token > workspace > global.
- Bootstrap do `server.py` chama `_settings.resolve_persistence_base_dir()` e propaga settings da Fase 2.
- `prompt_persistence_protocol` agora inclui nota sobre `persistence_location` + warning sobre `.gitignore`.
- 19 testes novos em `test_persistence_location.py`.

### Phase 5 — Padronização de settings com agy (B2)

- `persistence_max_file_bytes` mudou de `1_048_576` (1 MiB) para `524_288` (512 KiB) — alinhado com agy.
- `.env.example` documenta a mudança com comentário.
- 1 teste novo: `test_default_max_file_bytes_is_512kib`.

### Phase 6 — Testes de integração (gap §4 do diagnóstico)

- 14 testes novos em `test_persistence_integration.py` cobrindo:
  - `claude_persistence_protocol` registrado em `mcp.list_prompts()`.
  - Helper `build_prompt_with_context` testado diretamente (cenário `claude_run_task` é complexo de mockar; ver skip em `test_run_task_prepends_persistent_context`).
  - Helper tolerante a `RuntimeError`/`OSError`.
  - Helper pula contexto se `persistence_enabled=False` ou `is_initialized=False`.
  - `load_context().initialized` reflete o marker.
  - Symlink escape bloqueado por `resolve_file_path`.
  - `load_context` respeita `max_chars_per_file`.

### Phase 7 — Documentação

- `CONTRATO_TOOLS.md`: documentação de `confirm`, truncation assimétrica, novos env vars, mudança em `MAX_FILE_BYTES`.
- `README.md`: nova seção "Storage location: global vs workspace" com tabela, warning sobre `.gitignore`, atualização dos defaults (incluindo `MAX_FILE_BYTES` → 512 KiB).
- `USO_TRAE.md`: nova subseção 4.1 com exemplo de config Trae para workspace mode + nota sobre escape hatch.
- `PLAN.md`: este §21 (changelog).
- `.env.example`: documenta todas as variáveis novas com exemplos comentados.
- **Migração automática:** **não incluída**. Usuário que muda de `global` para `workspace` deve mover manualmente os arquivos:
  ```bash
  mv ~/.open-cli-router/claude-code <cwd_parent>/.open-cli-router/claude-code
  ```
- **⚠️ Migração manual recomendada para o bug A1:** usuários com `AGENTS.md` já inicializado devem rodar `claude_init_persistence(req={"force": true})` para regenerar o template com o path correto, OU editar manualmente a linha 3 do `AGENTS.md`.

### Métricas finais

| Métrica | Antes | Depois |
|---|---|---|
| Testes em `test_persistence*.py` | 26 | 73 |
| Total de testes | 72 | 137 (136 + 1 skip) |
| Bugs em runtime | 1 (A1: path errado) | 0 |
| Divergências com agy pós-refatoração | 5 | 0 (parity total) |
| Features de persistência | location=global | +location=workspace, +`$cwd_parent` escape hatch |

**Compatibilidade:** zero impacto em setups existentes — todos os settings novos têm defaults que reproduzem o comportamento anterior. Apenas o default de `MAX_FILE_BYTES` mudou (1 MiB → 512 KiB) — usuários que tinham valor customizado em `.env` continuam com ele.

### Pendente (deferido para v2)

- D1 Dream cycle / Consolidator
- D2 Versionamento Git automático
- D3 `claude_search_persistence`
- D4 Export/import tool
- D5 Métricas em `claude_status`
- D6 `render_session_entry` helper
- C6 Lock per-file
- C7 Cross-MCP awareness
- Migration tool automática `global` ↔ `workspace`
- Reparo automático de `AGENTS.md` para usuários existentes (forçar re-seed)
- Abstração de `persistence.py` em pacote compartilhado entre agy e claude
7. Phase 8-10: testes, docs, validação