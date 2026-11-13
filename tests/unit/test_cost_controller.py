"""Unit tests for CostController USD calculations, budget caps, and forward-looking grants."""

from pathlib import Path

import pytest

from app.adapters.outbound.pricing_catalog import PricingCatalogAdapter
from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
from app.application.cost_service import CostController
from app.application.ports import BudgetScope
from app.bootstrap.config import load_config
from app.domain.cost import ModelPricing, TokenUsage
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

