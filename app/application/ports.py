"""Outbound I/O protocol ports and shared application boundary types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.domain.cost import CostLedgerEntry, ModelPricing, TokenUsage
from app.domain.models import (
    AgentRole,
    JustRecipeSpec,
    KnowledgeEntry,
    SubagentRun,
    Task,
    TaskEvent,
    TaskMessage,
    TaskStatus,
    WorktreeInfo,
    WorkUnit,
)

if TYPE_CHECKING:
    from app.bootstrap.config import ModelProfile


class BudgetScope(StrEnum):
    LIFETIME = "LIFETIME"
    FORWARD_LOOKING = "FORWARD_LOOKING"


@dataclass(slots=True)
class StageTurnResult:
    response_text: str
    usage: TokenUsage
    stop_reason: str = "UNSPECIFIED"
    files_modified: bool = False
    model_alias: str = ""
    model_id: str = ""
    conversation_id: str | None = None


StopGateCallback = Callable[[str, int], Awaitable[tuple[bool, str]]]
EventCallback = Callable[[str, dict[str, Any]], None]
InteractionCallback = Callable[[list[dict[str, Any]]], Awaitable[list[str]]]
PreWriteCallback = Callable[[str], Awaitable[None]]


@runtime_checkable
class AgentHarnessPort(Protocol):
    async def run_stage_turn(
        self,
        *,
        task: Task,
        work_unit: WorkUnit | None,
        role: AgentRole,
        prompt: str,
        tools: Sequence[Callable[..., Any]],
        worktree_path: str,
        read_only: bool = False,
        stop_gate: StopGateCallback | None = None,
        budget_scope: BudgetScope = BudgetScope.LIFETIME,
        model_override: str | None = None,
        conversation_id: str | None = None,
        pre_write_callback: PreWriteCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> StageTurnResult: ...

    async def cancel_task(self, task_id: str) -> None: ...


@runtime_checkable
class StoragePort(Protocol):
    async def initialize(self) -> None: ...

    async def upsert_task(self, task: Task) -> None: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def list_tasks(
        self, status: TaskStatus | None = None, *, limit: int | None = None
    ) -> list[Task]: ...

    async def delete_task(self, task_id: str) -> bool: ...

    async def append_task_message(self, message: TaskMessage) -> None: ...

    async def list_task_messages(
        self, task_id: str, limit: int = 200
    ) -> list[TaskMessage]: ...

    async def get_session_stats(
        self, task_ids: Sequence[str]
    ) -> dict[str, tuple[int, int, float]]: ...

    async def upsert_work_unit(self, work_unit: WorkUnit) -> None: ...

    async def get_work_unit(self, work_unit_id: str) -> WorkUnit | None: ...

    async def list_work_units(self, task_id: str) -> list[WorkUnit]: ...

    async def upsert_subagent_run(self, run: SubagentRun) -> None: ...

    async def list_subagent_runs(self, task_id: str) -> list[SubagentRun]: ...

    async def append_event(self, event: TaskEvent) -> None: ...

    async def list_events(self, task_id: str, limit: int = 200) -> list[TaskEvent]: ...

    async def record_cost_entry(self, entry: CostLedgerEntry) -> None: ...

    async def get_task_usd_spent(self, task_id: str) -> float: ...

    async def get_daily_usd_spent(self, day_prefix: str | None = None) -> float: ...

    async def get_task_token_usage(self, task_id: str) -> TokenUsage: ...

    async def get_daily_token_usage(self, day_prefix: str | None = None) -> TokenUsage: ...

    async def get_cached_pricing(
        self, model_id: str, *, max_age_hours: int = 24
    ) -> ModelPricing | None: ...

    async def upsert_cached_pricing(self, pricing: ModelPricing) -> None: ...

    async def upsert_knowledge(self, entry: KnowledgeEntry) -> None: ...

    async def search_knowledge(
        self,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        domain: str | None = None,
        target: str | None = None,
        limit: int = 5,
    ) -> list[KnowledgeEntry]: ...

    async def list_knowledge(
        self, *, domain: str | None = None, target: str | None = None, limit: int = 50
    ) -> list[KnowledgeEntry]: ...

    async def replace_target_knowledge(
        self, domain: str, target: str, consolidated: KnowledgeEntry
    ) -> None: ...

    async def get_setting(self, key: str) -> str | None: ...

    async def set_setting(self, key: str, value: str) -> None: ...


@runtime_checkable
class PricingCatalogPort(Protocol):
    async def sync_catalog(self, force: bool = False) -> dict[str, ModelPricing]: ...

    async def resolve_pricing(self, profile: ModelProfile) -> ModelPricing: ...


@runtime_checkable
class WorktreePort(Protocol):
    async def ensure_worktree(
        self, task_id: str, *, use_worktree: bool = True
    ) -> WorktreeInfo: ...

    async def ensure_materialized(
        self, task_id: str, sub_repos: Sequence[str]
    ) -> WorktreeInfo: ...

    async def get_latest_mtime(
        self, task_id: str, domain: str | None = None
    ) -> float: ...

    async def get_git_diff(
        self, task_id: str, sub_repos: Sequence[str] | None = None
    ) -> str: ...

    async def commit_changes(
        self, task_id: str, message: str, sub_repos: Sequence[str] | None = None
    ) -> str | None: ...

    async def merge_locally(self, task_id: str) -> list[str]: ...

    async def cleanup_worktree(self, task_id: str) -> None: ...


@runtime_checkable
class JustToolProviderPort(Protocol):
    def discover_recipes(self, reload: bool = False) -> dict[str, JustRecipeSpec]: ...

    def get_tools_for(
        self,
        role: AgentRole,
        domain: str | None = None,
        task_id: str | None = None,
        dispatch_callback: Callable[..., Awaitable[str]] | None = None,
    ) -> list[Callable[..., Any]]: ...

    async def run_validator(
        self,
        validator: str,
        *,
        task_id: str,
        domain: str,
        target: str | None = None,
    ) -> tuple[bool, str]: ...

