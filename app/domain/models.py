"""Core domain entities and value objects for Wintermute."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str = "") -> str:
    short = uuid4().hex[:12]
    return f"{prefix}_{short}" if prefix else short


def new_conversation_id() -> str:
    """Return a 36-char hyphenated UUID4 accepted by the Antigravity SDK."""
    return str(uuid4())


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSED = "PAUSED"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }

    @property
    def holds_execution_slot(self) -> bool:
        return self == TaskStatus.RUNNING


class StageKind(StrEnum):
    AGENT = "agent"
    GATE = "gate"
    COMMIT = "commit"


class AgentRole(StrEnum):
    TECH_LEAD = "tech_lead"
    SENIOR_CODER = "senior_coder"
    JUNIOR_CODER = "junior_coder"
    TROUBLESHOOTER = "troubleshooter"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    DOC_WRITER = "doc_writer"


@dataclass(frozen=True, slots=True)
class StageSpec:
    name: str
    kind: StageKind
    role: str | None = None
    validators: tuple[str, ...] = ()
    require_file_edits: bool = False
    pass_git_diff: bool = False
    skip_next_gate_if_unmodified: bool = False
    run_only_on_failure: bool = False
    attach_build_logs: bool = False
    read_only: bool = False
    on_fail_goto: str | None = None
    on_complete_goto: str | None = None
    capture_knowledge: bool = False
    instruction_hint: str = ""


@dataclass(frozen=True, slots=True)
class PipelineTemplate:
    name: str
    description: str
    max_retries_per_gate: int
    stages: tuple[StageSpec, ...]

    def stage_index(self, stage_name: str) -> int:
        for idx, stage in enumerate(self.stages):
            if stage.name == stage_name:
                return idx
        raise KeyError(f"Stage '{stage_name}' not found in pipeline template '{self.name}'")


@dataclass(slots=True)
class WorkUnit:
    id: str
    task_id: str
    title: str
    domain: str
    target: str | None
    pipeline_template: str = ""
    complexity: str = "simple"
    skills: list[str] = field(default_factory=list)
    prompt: str = ""
    current_stage_idx: int = 0
    current_stage_name: str = ""
    gate_attempts: dict[str, int] = field(default_factory=dict)
    last_file_modified_at: float = 0.0
    last_gate_passed_at: float = 0.0
    commit_sha: str | None = None
    status: str = "PENDING"
    summary: str = ""


@dataclass(slots=True)
class Task:
    id: str
    title: str
    prompt: str
    status: TaskStatus = TaskStatus.QUEUED
    mode: str = "multi"
    domain: str | None = None
    target: str | None = None
    pipeline_template: str | None = None
    conversation_id: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    touched_repos: list[str] = field(default_factory=list)
    max_usd: float | None = None
    extra_usd_granted: float = 0.0
    pending_question: str | None = None
    review_summary: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)


@dataclass(slots=True)
class SubagentRun:
    id: str
    task_id: str
    work_unit_id: str | None
    role: AgentRole
    model_alias: str
    model_id: str
    status: str = "RUNNING"
    prompt: str = ""
    summary: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    candidate_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0
    usd_cost: float = 0.0
    started_at: str = field(default_factory=utcnow_iso)
    completed_at: str | None = None


@dataclass(slots=True)
class TaskEvent:
    id: str
    task_id: str
    source: str
    event_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utcnow_iso)


@dataclass(slots=True)
class TaskMessage:
    id: str
    task_id: str
    sender: str
    content: str
    created_at: str = field(default_factory=utcnow_iso)


@dataclass(slots=True)
class SessionSummary:
    task: Task
    message_count: int = 0
    work_unit_count: int = 0
    total_usd: float = 0.0
    has_sdk_session: bool = False


@dataclass(slots=True)
class KnowledgeEntry:
    id: str
    domain: str
    target: str
    issue: str
    resolution: str
    outcome: str = "success"
    capture_type: str = "automatic"
    embedding: list[float] | None = None
    created_at: str = field(default_factory=utcnow_iso)


@dataclass(slots=True)
class WorktreeInfo:
    task_id: str
    root_path: str
    build_root: str
    branch_name: str
    is_btrfs_cow: bool = False
    materialized_sub_repos: set[str] = field(default_factory=set)
    symlinked_sub_repos: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class JustParamSpec:
    name: str
    default: str | None = None
    has_default: bool = False
    kind: str = "singular"


@dataclass(frozen=True, slots=True)
class JustRecipeSpec:
    name: str
    module: str
    target: str
    doc: str
    parameters: tuple[JustParamSpec, ...] = ()
    affected_repos: tuple[str, ...] = ()
    read_only: bool = False

