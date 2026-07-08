from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Set

from pydantic import BaseModel, BeforeValidator, Field, StrictBool


class TaskClass(str, Enum):
    """Classificação das tarefas submetidas para fins de alocação de tempo e orçamentos."""
    TRIVIAL_EDIT = "trivial_edit"               # Alterações em 1 arquivo, tempo de execução < 90s.
    SMOKE_TEST = "smoke_test"                    # Validações estáticas de código, pytest sem escrita.
    SINGLE_FEATURE = "single_feature"            # Implementação de funcionalidade simples em 1-3 arquivos.
    DOCS_UPDATE = "docs_update"                  # Edição de arquivos de documentação (.md, .rst).
    TEST_SUITE = "test_suite"                    # Geração de suite de testes unitários ou de integração.
    REVIEW = "review"                            # Revisão estática de conformidade e linting.
    MULTI_FILE_REFACTOR = "multi_file_refactor"  # Refatorações estruturais abrangendo 5 a 50 arquivos.
    ARCHITECTURE = "architecture"                # Decisões de design de sistemas e modelagem.
    MIGRATION = "migration"                      # Migrações massivas de dependências ou pacotes.
    LONG_RUNNING_DATA_AGENT = "long_running"     # Agentes de processamento contínuo em background.


class ModelTier(str, Enum):
    """Classificação tarifária dos modelos Claude para tomada de decisão financeira."""
    CHEAP = "cheap"             # Claude Haiku 4.5 — Baixo custo e velocidade.
    STANDARD = "standard"       # Claude Sonnet 5 — Modelo padrão equilibrado.
    MID_TIER = "mid_tier"       # Claude Fable 5 — Modelo intermediário.
    FLAGSHIP = "flagship"       # Claude Opus 4.8 — Modelo de alto raciocínio analítico.


class ModelProfile(BaseModel):
    """Perfil detalhado contendo capacidades, custos e limites de um modelo Claude específico."""
    cli_string: str = Field(..., description="String identificadora enviada para o executável claude.")
    alias: str = Field(..., description="Apelido curto e legível (ex: sonnet, haiku).")
    tier: ModelTier = Field(..., description="Categoria de preço associada ao modelo.")
    display_name: str = Field(..., description="Nome de exibição em logs e relatórios.")
    best_for: List[TaskClass] = Field(..., description="Classes de tarefas recomendadas para o modelo.")
    typical_latency_min: float = Field(..., description="Latência média observada por execução típica.")
    typical_cost_per_run_usd: float = Field(..., description="Custo médio estimado por run.")
    multi_file_safe: bool = Field(..., description="Indica se é recomendado para escrita de múltiplos arquivos.")
    max_context_k_tokens: int = Field(200, description="Tamanho de janela de contexto em milhares de tokens.")


# Registro estático contendo os perfis dos 4 modelos homologados
MODEL_REGISTRY: Dict[str, ModelProfile] = {
    "sonnet": ModelProfile(
        cli_string="claude-sonnet-5-2026",
        alias="sonnet",
        tier=ModelTier.STANDARD,
        display_name="Claude Sonnet 5",
        best_for=[
            TaskClass.SINGLE_FEATURE,
            TaskClass.DOCS_UPDATE,
            TaskClass.MULTI_FILE_REFACTOR,
            TaskClass.TEST_SUITE
        ],
        typical_latency_min=8.0,
        typical_cost_per_run_usd=0.50,
        multi_file_safe=True,
        max_context_k_tokens=400,
    ),
    "fable": ModelProfile(
        cli_string="claude-fable-5-2026",
        alias="fable",
        tier=ModelTier.MID_TIER,
        display_name="Claude Fable 5",
        best_for=[
            TaskClass.MULTI_FILE_REFACTOR,
            TaskClass.MIGRATION,
            TaskClass.LONG_RUNNING_DATA_AGENT
        ],
        typical_latency_min=12.0,
        typical_cost_per_run_usd=0.30,
        multi_file_safe=True,
        max_context_k_tokens=400,
    ),
    "opus": ModelProfile(
        cli_string="claude-opus-4-8-2026",
        alias="opus",
        tier=ModelTier.FLAGSHIP,
        display_name="Claude Opus 4.8",
        best_for=[
            TaskClass.ARCHITECTURE,
            TaskClass.MIGRATION,
            TaskClass.LONG_RUNNING_DATA_AGENT
        ],
        typical_latency_min=25.0,
        typical_cost_per_run_usd=1.50,
        multi_file_safe=True,
        max_context_k_tokens=1000,
    ),
    "haiku": ModelProfile(
        cli_string="claude-haiku-4-5-2026",
        alias="haiku",
        tier=ModelTier.CHEAP,
        display_name="Claude Haiku 4.5",
        best_for=[
            TaskClass.TRIVIAL_EDIT,
            TaskClass.SMOKE_TEST,
            TaskClass.REVIEW
        ],
        typical_latency_min=1.5,
        typical_cost_per_run_usd=0.02,
        multi_file_safe=False,
        max_context_k_tokens=200,
    ),
}


def resolve_model_alias(alias: str) -> str:
    """Resolve um apelido curto de modelo (ex: 'haiku') para sua correspondente cli_string."""
    try:
        profile = MODEL_REGISTRY[alias.lower()]
        return profile.cli_string
    except KeyError as e:
        raise KeyError(
            f"O apelido de modelo '{alias}' não é suportado pelo servidor MCP. "
            f"Modelos válidos: {list(MODEL_REGISTRY.keys())}"
        ) from e


def is_model_allowed(alias: str, allowed_models: Set[str]) -> bool:
    """Verifica se o apelido fornecido está presente na lista de permitidos do servidor."""
    if not allowed_models:
        return alias.lower() in MODEL_REGISTRY
    return alias.lower() in {m.lower() for m in allowed_models}



def _coerce_empty_str_to_dict(v: Any) -> Any:
    if v == "" or v is None:
        return {}
    return v


class ClaudeExecOptions(BaseModel):
    sandbox: StrictBool = True
    dangerously_skip_permissions: StrictBool = False
    timeout_s: int = Field(default=600, ge=1, le=3600)
    env: dict[str, str] | None = None
    extra_args: list[str] = Field(default_factory=list)


class ClaudeHealthRequest(BaseModel):
    expected_version: str | None = None


class ClaudeHealthResponse(BaseModel):
    claude_path: str
    claude_version: str
    ok: bool
    notes: list[str] = Field(default_factory=list)
    auth_status: str | None = None


class ClaudeRunTaskRequest(BaseModel):
    workspace_path: str
    prompt: str
    options: ClaudeExecOptions = Field(default_factory=ClaudeExecOptions)
    capture_changes: bool = True
    change_scope: Literal["workspace", "git_only"] = "workspace"
    model: str | None = None
    fallback_model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    permission_mode: Literal[
        "default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"
    ] | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    system_prompt_append: str | None = None
    resume_session_id: str | None = None


class ClaudeStartTaskRequest(ClaudeRunTaskRequest):
    pass


class ClaudePollTaskRequest(BaseModel):
    run_id: str
    drain: bool = False
    wait_seconds: float = 0.5


class ClaudeCancelTaskRequest(BaseModel):
    run_id: str
    force: bool = False


class ClaudeListRunsRequest(BaseModel):
    limit: int = 50


ClaudeHealthRequestIn = Annotated[
    ClaudeHealthRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeRunTaskRequestIn = Annotated[
    ClaudeRunTaskRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeStartTaskRequestIn = Annotated[
    ClaudeStartTaskRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudePollTaskRequestIn = Annotated[
    ClaudePollTaskRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeCancelTaskRequestIn = Annotated[
    ClaudeCancelTaskRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeListRunsRequestIn = Annotated[
    ClaudeListRunsRequest, BeforeValidator(_coerce_empty_str_to_dict)
]


class ClaudeRunResult(BaseModel):
    run_id: str
    workspace_path: str
    final_text: str
    result: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool = False
    started_at: datetime
    finished_at: datetime | None
    session_id: str | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    model_usage: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class WorkspaceChanges(BaseModel):
    method: Literal["git", "snapshot", "none"]
    changed_files: list[str] = Field(default_factory=list)
    diff: str | None = None


class ClaudeRunTaskResponse(BaseModel):
    run_id: str
    status: Literal["done", "error", "timeout", "cancelled"]
    exit_code: int | None
    final_text: str
    result: dict[str, Any]
    changes: WorkspaceChanges | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    model_usage: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class ClaudeStartTaskResponse(BaseModel):
    run_id: str
    started_at: datetime
    session_id: str


class ClaudePollTaskResponse(BaseModel):
    run_id: str
    status: Literal["running", "done", "error", "timeout", "cancelled"]
    new_messages: list[dict[str, Any]] = Field(default_factory=list)
    result: ClaudeRunResult | None = None
    stdout_len: int = 0
    stderr_len: int = 0
    elapsed_seconds: float = 0.0
    notes: list[str] = Field(default_factory=list)


class ClaudeCancelTaskResponse(BaseModel):
    canceled: bool
    status: Literal["cancelled", "not_found", "already_done"]


class ClaudeRunSummary(BaseModel):
    run_id: str
    workspace_path: str
    status: Literal["running", "done", "error", "timeout", "cancelled"]
    started_at: datetime


class ClaudeListRunsResponse(BaseModel):
    runs: list[ClaudeRunSummary] = Field(default_factory=list)


# ------------------------------------------------------------------
# Persistence tool models
# ------------------------------------------------------------------

PersistenceFileName = Literal["agents", "projects", "memory"]


class ClaudeInitPersistenceRequest(BaseModel):
    force: bool = False
    seed_templates: bool | None = None


class ClaudeInitPersistenceResponse(BaseModel):
    base_dir: str
    created: list[str] = Field(default_factory=list)
    already_existed: list[str] = Field(default_factory=list)
    seed_version: str


class ClaudeReadPersistenceRequest(BaseModel):
    file: PersistenceFileName
    offset: int = 0
    limit: int | None = None


class ClaudeReadPersistenceResponse(BaseModel):
    file: str
    content: str
    size_bytes: int
    truncated: bool
    modified_at: datetime | None


class ClaudeAppendPersistenceRequest(BaseModel):
    file: PersistenceFileName
    content: str
    section_header: str | None = None
    confirm: bool = False  # required true for AGENTS.md in safe mode


class ClaudeAppendPersistenceResponse(BaseModel):
    file: str
    appended_bytes: int
    new_size_bytes: int
    timestamp: datetime


class ClaudeUpdatePersistenceRequest(BaseModel):
    file: PersistenceFileName
    section_anchor: str
    new_content: str
    mode: Literal["replace", "append"] = "replace"
    confirm: bool = False


class ClaudeUpdatePersistenceResponse(BaseModel):
    file: str
    section_anchor: str
    matched: bool
    new_size_bytes: int


class ClaudeLoadPersistenceContextRequest(BaseModel):
    include: list[PersistenceFileName] = Field(
        default_factory=lambda: ["agents", "projects", "memory"]  # type: ignore[arg-type]
    )
    max_chars_per_file: int = 20_000


class ClaudeLoadPersistenceContextResponse(BaseModel):
    agents_excerpt: str | None = None
    projects_excerpt: str | None = None
    memory_excerpt: str | None = None
    truncated_flags: dict[str, bool] = Field(default_factory=dict)
    total_chars: int = 0
    base_dir: str = ""
    initialized: bool = False


ClaudeInitPersistenceRequestIn = Annotated[
    ClaudeInitPersistenceRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeReadPersistenceRequestIn = Annotated[
    ClaudeReadPersistenceRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeAppendPersistenceRequestIn = Annotated[
    ClaudeAppendPersistenceRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeUpdatePersistenceRequestIn = Annotated[
    ClaudeUpdatePersistenceRequest, BeforeValidator(_coerce_empty_str_to_dict)
]
ClaudeLoadPersistenceContextRequestIn = Annotated[
    ClaudeLoadPersistenceContextRequest, BeforeValidator(_coerce_empty_str_to_dict)
]


class ClaudeSelfTestRequest(BaseModel):
    include: list[str] | None = None
    only_show_tolerant: bool = False


ClaudeSelfTestRequestIn = Annotated[
    ClaudeSelfTestRequest, BeforeValidator(_coerce_empty_str_to_dict)
]


class ClaudeToolSchemaReport(BaseModel):
    name: str
    top_level_required: list[str] = Field(default_factory=list)
    top_level_properties: list[str] = Field(default_factory=list)
    accepts_empty_args: bool
    requires_req_wrapper: bool


class ClaudeSelfTestResponse(BaseModel):
    total_tools: int
    tolerant_count: int
    requires_req_count: int
    tools: list[ClaudeToolSchemaReport] = Field(default_factory=list)
    server_info: dict[str, Any] = Field(default_factory=dict)
    summary: str

