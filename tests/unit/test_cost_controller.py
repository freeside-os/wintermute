"""Unit tests for CostController USD calculations, budget caps, and forward-looking grants."""

from pathlib import Path

import pytest

from app.adapters.outbound.pricing_catalog import PricingCatalogAdapter
from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
from app.application.cost_service import CostController
from app.application.ports import BudgetScope
from app.bootstrap.config import load_config
from app.domain.cost import CostLedgerEntry, ModelPricing, TokenUsage
from app.domain.models import AgentRole, Task, TaskStatus


@pytest.mark.asyncio
async def test_cost_controller_tracks_usage_pauses_on_budget_and_grants(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()

    await storage.upsert_cached_pricing(
        ModelPricing(
            model_id="gemini-4-argon",
            input_cost_per_1m=2.0,
            cached_input_cost_per_1m=0.5,
            output_cost_per_1m=10.0,
            thinking_cost_per_1m=10.0,
            source="test",
        )
    )
    pricing = PricingCatalogAdapter(config=cfg, storage=storage)
    controller = CostController(
        config=cfg, storage=storage, pricing_catalog=pricing
    )

    task = Task(
        id="t_budget",
        title="Budget test",
        prompt="Run expensive step",
        status=TaskStatus.RUNNING,
        max_usd=1.0,
    )
    await storage.upsert_task(task)

    # 1M uncached input ($2.00) -> exceeds $1.00 task cap
    usage = TokenUsage(
        prompt_tokens=1_000_000,
        cached_tokens=0,
        candidate_tokens=0,
        thoughts_tokens=0,
        total_tokens=1_000_000,
    )
    entry, verdict = await controller.record_turn_usage(
        task=task,
        role=AgentRole.TECH_LEAD,
        model_profile=cfg.models["argon"],
        usage=usage,
    )
    assert entry.usd_cost == 2.0
    assert verdict.allowed is False
    assert task.status == TaskStatus.PAUSED_BUDGET

    updated = await controller.grant_budget(
        task.id, 3.0, scope=BudgetScope.FORWARD_LOOKING
    )
    assert updated.status == TaskStatus.RUNNING
    assert updated.extra_usd_granted == 3.0

    post_grant_verdict = await controller.check_budget(updated)
    assert post_grant_verdict.allowed is True
    assert post_grant_verdict.task_usd_limit == 4.0


@pytest.mark.asyncio
async def test_monthly_spend_persists_when_sessions_deleted(tmp_path: Path):
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm_monthly.db")
    await storage.initialize()

    t1 = Task(id="task_m1", title="one", prompt="one")
    t2 = Task(id="task_m2", title="two", prompt="two")
    await storage.upsert_task(t1)
    await storage.upsert_task(t2)

    await storage.record_cost_entry(
        CostLedgerEntry(
            id="cost_m1",
            task_id=t1.id,
            subagent_run_id=None,
            role="tech_lead",
            model_id="gemini-4-argon",
            usage=TokenUsage(prompt_tokens=100, candidate_tokens=50, total_tokens=150),
            usd_cost=0.25,
            timestamp="2026-11-05T10:00:00+00:00",
        )
    )
    await storage.record_cost_entry(
        CostLedgerEntry(
            id="cost_m2",
            task_id=t2.id,
            subagent_run_id=None,
            role="senior_coder",
            model_id="gemini-4-hydrogen",
            usage=TokenUsage(prompt_tokens=200, candidate_tokens=100, total_tokens=300),
            usd_cost=0.75,
            timestamp="2026-11-12T12:00:00+00:00",
        )
    )
    await storage.record_cost_entry(
        CostLedgerEntry(
            id="cost_prev",
            task_id=t2.id,
            subagent_run_id=None,
            role="senior_coder",
            model_id="gemini-4-hydrogen",
            usage=TokenUsage(prompt_tokens=50, candidate_tokens=25, total_tokens=75),
            usd_cost=0.10,
            timestamp="2026-10-28T12:00:00+00:00",
        )
    )

    assert await storage.get_monthly_usd_spent("2026-11") == pytest.approx(1.00)
    assert await storage.get_monthly_usd_spent("2026-10") == pytest.approx(0.10)
    nov_usage = await storage.get_monthly_token_usage("2026-11")
    assert nov_usage.total_tokens == 450

    # Delete both sessions; monthly_spend must still retain full totals
    assert await storage.delete_task(t1.id) is True
    assert await storage.delete_task(t2.id) is True

    # Re-initialize DB (simulating restart) and verify totals are preserved
    await storage.initialize()
    assert await storage.get_monthly_usd_spent("2026-11") == pytest.approx(1.00)
    assert await storage.get_monthly_usd_spent("2026-10") == pytest.approx(0.10)
    nov_usage_after = await storage.get_monthly_token_usage("2026-11")
    assert nov_usage_after.total_tokens == 450
    history = await storage.list_monthly_spend()
    assert [m for m, _, _ in history] == ["2026-11", "2026-10"]


