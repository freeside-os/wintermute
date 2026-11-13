"""Cost accounting, automatic pricing lookup, and budget enforcement service."""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.application.ports import BudgetScope, PricingCatalogPort, StoragePort
from app.bootstrap.config import ModelProfile, WintermuteConfig
from app.domain.cost import BudgetVerdict, CostLedgerEntry, TokenUsage
from app.domain.models import AgentRole, Task, TaskEvent, TaskStatus, new_id, utcnow_iso


@dataclass(slots=True)
class CostController:
    config: WintermuteConfig
    storage: StoragePort
    pricing_catalog: PricingCatalogPort

    async def check_budget(self, task: Task) -> BudgetVerdict:
        policy = self.config.cost.policy
        task_limit = (
            task.max_usd if task.max_usd is not None else policy.max_task_usd
        ) + task.extra_usd_granted
        daily_limit = policy.max_daily_usd

        task_spent = await self.storage.get_task_usd_spent(task.id)
        daily_spent = await self.storage.get_daily_usd_spent()

        if task_spent >= task_limit:
            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"Task budget exceeded: ${task_spent:.4f} spent >= "
                    f"${task_limit:.4f} limit."
                ),
                task_usd_spent=task_spent,
                task_usd_limit=task_limit,
                daily_usd_spent=daily_spent,
                daily_usd_limit=daily_limit,
            )

        if daily_spent >= daily_limit:
            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"Daily budget exceeded: ${daily_spent:.4f} spent >= "
                    f"${daily_limit:.4f} limit."
                ),
                task_usd_spent=task_spent,
                task_usd_limit=task_limit,
                daily_usd_spent=daily_spent,
                daily_usd_limit=daily_limit,
            )

        return BudgetVerdict(
            allowed=True,
            task_usd_spent=task_spent,
            task_usd_limit=task_limit,
            daily_usd_spent=daily_spent,
            daily_usd_limit=daily_limit,
        )

    async def record_turn_usage(
        self,
        *,
        task: Task,
        role: AgentRole,
        model_profile: ModelProfile,
        usage: TokenUsage,
        subagent_run_id: str | None = None,
    ) -> tuple[CostLedgerEntry, BudgetVerdict]:
        pricing = await self.pricing_catalog.resolve_pricing(model_profile)
        usd_cost = pricing.calculate_usd(usage)

        entry = CostLedgerEntry(
            id=new_id("cost"),
            task_id=task.id,
            subagent_run_id=subagent_run_id,
            role=role.value,
            model_id=model_profile.model,
            usage=usage,
            usd_cost=usd_cost,
        )
        await self.storage.record_cost_entry(entry)

        verdict = await self.check_budget(task)
        if not verdict.allowed and self.config.cost.policy.pause_on_budget_exceeded:
            task.status = TaskStatus.PAUSED_BUDGET
            task.updated_at = utcnow_iso()
            await self.storage.upsert_task(task)
            await self.storage.append_event(
                TaskEvent(
                    id=new_id("evt"),
                    task_id=task.id,
                    source="cost_controller",
                    event_type="PAUSED_BUDGET",
                    content=verdict.reason,
                    metadata={
                        "task_usd_spent": verdict.task_usd_spent,
                        "task_usd_limit": verdict.task_usd_limit,
                        "daily_usd_spent": verdict.daily_usd_spent,
                        "daily_usd_limit": verdict.daily_usd_limit,
                    },
                )
            )
        return entry, verdict

    async def grant_budget(
        self,
        task_id: str,
        additional_usd: float,
        *,
        scope: BudgetScope = BudgetScope.FORWARD_LOOKING,
    ) -> Task:
        if additional_usd <= 0:
            raise ValueError("additional_usd must be positive")
        task = await self.storage.get_task(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        task.extra_usd_granted += additional_usd
        if task.status == TaskStatus.PAUSED_BUDGET:
            task.status = TaskStatus.RUNNING
        task.updated_at = utcnow_iso()
        await self.storage.upsert_task(task)

        await self.storage.append_event(
            TaskEvent(
                id=new_id("evt"),
                task_id=task.id,
                source="cost_controller",
                event_type="BUDGET_GRANTED",
                content=(
                    f"Granted +${additional_usd:.2f} USD ({scope.value}); "
                    f"extra granted total=${task.extra_usd_granted:.2f}."
                ),
                metadata={
                    "additional_usd": additional_usd,
                    "scope": scope.value,
                    "extra_usd_granted": task.extra_usd_granted,
                },
            )
        )
        return task

    def _apply_daily_limit(self, new_limit: float) -> float:
        updated_policy = replace(self.config.cost.policy, max_daily_usd=new_limit)
        self.config.cost = replace(self.config.cost, policy=updated_policy)
        return updated_policy.max_daily_usd

    async def sync_daily_limit_from_storage(self) -> float:
        """Load any persisted daily USD limit override from storage into runtime config."""
        raw = await self.storage.get_setting("max_daily_usd")
        if raw is not None:
            try:
                parsed = float(raw)
                if parsed > 0:
                    return self._apply_daily_limit(parsed)
            except ValueError:
                pass
        return self.config.cost.policy.max_daily_usd

    async def set_daily_limit(self, amount: float, *, additive: bool = False) -> float:
        """Set or increase the daily USD ceiling and persist it across restarts."""
        if amount <= 0:
            raise ValueError("Daily budget amount must be positive.")
        current = self.config.cost.policy.max_daily_usd
        new_limit = (current + amount) if additive else amount
        self._apply_daily_limit(new_limit)
        await self.storage.set_setting("max_daily_usd", f"{new_limit:.6f}")
        return new_limit

