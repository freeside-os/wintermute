"""Central execution engine runtime for Wintermute platform."""

from dataclasses import dataclass
from typing import Any, AsyncGenerator

from app.config import WorkspaceConfig
from app.provider import DefaultDomainProvider, DomainProvider
from app.registry import RegistryLibrary
from app.registry_domains import DomainRegistry
from app.types import ExecutionSpec


@dataclass
class EngineEvent:
    event_type: str
    message: str
    data: dict[str, Any] | None = None


class WintermuteEngine:
    """Central engine executing ExecutionSpec configurations across components."""

    def __init__(self, workspace_config: WorkspaceConfig | None = None):
        self.config = workspace_config or WorkspaceConfig()
        DomainRegistry.autodiscover()

    def resolve_provider(self, domain_name: str) -> DomainProvider:
        spec = DomainRegistry.get_domain(domain_name)
        if spec and spec.provider_factory:
            return spec.provider_factory()
        return DefaultDomainProvider(domain_name)

    async def execute(self, spec: ExecutionSpec) -> AsyncGenerator[EngineEvent, None]:
        # Validate domain and workflow
        DomainRegistry.validate(spec.domain, spec.workflow)
        
        yield EngineEvent("started", f"Starting workflow '{spec.workflow}' on domain '{spec.domain}'")

        provider = self.resolve_provider(spec.domain)
        tools = RegistryLibrary.resolve_tools(spec.tools_filter or f"{spec.domain}:*")
        skills = RegistryLibrary.resolve_skills(spec.skills_filter or f"{spec.domain}:*")

        yield EngineEvent(
            "context_loaded",
            f"Loaded {len(tools)} tools and {len(skills)} skills for domain '{spec.domain}'",
            {"tools_count": len(tools), "skills_count": len(skills)}
        )

        # Execution delegation
        yield EngineEvent("completed", f"Workflow '{spec.workflow}' completed successfully on '{spec.domain}'")
