"""Dependency injection container wiring domain, application, and outbound adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter, TurnRunner
from app.adapters.outbound.just_synthesizer import (
    CommandRunner,
    DumpProvider,
    JustToolSynthesizer,
)
from app.adapters.outbound.pricing_catalog import FeedFetcher, PricingCatalogAdapter
from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
from app.adapters.outbound.worktree_manager import WorktreeManagerAdapter
from app.application.cost_service import CostController
from app.application.knowledge_service import KnowledgeService
from app.application.pipeline import WorkUnitPipelineEngine
from app.application.single_agent_service import SingleAgentService
from app.bootstrap.config import WintermuteConfig, load_config


@dataclass(slots=True)
class AppContainer:
    """Initialized runtime container for Wintermute services and adapters."""

    config: WintermuteConfig
    workspace_root: Path
    storage: SQLiteStorageAdapter
    pricing_catalog: PricingCatalogAdapter
    cost_controller: CostController
    knowledge_service: KnowledgeService
    worktree_manager: WorktreeManagerAdapter
    just_tools: JustToolSynthesizer
    harness: AntigravityHarnessAdapter
    pipeline_engine: WorkUnitPipelineEngine
    single_agent_service: SingleAgentService
    _initialized: bool = False

    async def initialize(self, *, sync_pricing: bool = True) -> None:
        if self._initialized:
            return
        await self.storage.initialize()
        await self.cost_controller.sync_daily_limit_from_storage()
        if sync_pricing:
            await self.pricing_catalog.sync_catalog(force=False)
        self._initialized = True


def build_container(
    config: WintermuteConfig | None = None,
    *,
    workspace_root: Path | None = None,
    config_path: Path | None = None,
    turn_runner: TurnRunner | None = None,
    command_runner: CommandRunner | None = None,
    dump_provider: DumpProvider | None = None,
    feed_fetcher: FeedFetcher | None = None,
) -> AppContainer:
    """Build and wire an AppContainer from config and optional test doubles."""
    resolved_config = config or load_config(
        workspace_root=workspace_root,
        default_toml_path=config_path,
    )
    storage = SQLiteStorageAdapter(
        db_path=resolved_config.workspace.db_path,
        embedding_dimensions=resolved_config.memory.embedding_dimensions,
    )
    pricing_catalog = PricingCatalogAdapter(
        config=resolved_config,
        storage=storage,
        feed_fetcher=feed_fetcher,
    )
    cost_controller = CostController(
        config=resolved_config,
        storage=storage,
        pricing_catalog=pricing_catalog,
    )
    knowledge_service = KnowledgeService(
        config=resolved_config,
        storage=storage,
    )
    worktree_manager = WorktreeManagerAdapter(config=resolved_config)
    just_tools = JustToolSynthesizer(
        config=resolved_config,
        worktree=worktree_manager,
        dump_provider=dump_provider,
        command_runner=command_runner,
    )
    harness = AntigravityHarnessAdapter(
        config=resolved_config,
        turn_runner=turn_runner,
    )
    pipeline_engine = WorkUnitPipelineEngine(
        config=resolved_config,
        storage=storage,
        harness=harness,
        worktree=worktree_manager,
        just_tools=just_tools,
        cost_controller=cost_controller,
        knowledge=knowledge_service,
    )
    single_agent_service = SingleAgentService(
        config=resolved_config,
        storage=storage,
        cost_controller=cost_controller,
        harness=harness,
        just_tools=just_tools,
        pipeline_engine=pipeline_engine,
    )
    return AppContainer(
        config=resolved_config,
        workspace_root=resolved_config.workspace.workspace_root,
        storage=storage,
        pricing_catalog=pricing_catalog,
        cost_controller=cost_controller,
        knowledge_service=knowledge_service,
        worktree_manager=worktree_manager,
        just_tools=just_tools,
        harness=harness,
        pipeline_engine=pipeline_engine,
        single_agent_service=single_agent_service,
    )

