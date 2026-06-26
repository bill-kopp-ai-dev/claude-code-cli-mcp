from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, StrictBool


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
        default_factory=lambda: ["agents", "projects", "memory"]
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

