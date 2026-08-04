"""Installer domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import InstallerWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="installer",
        description="Freeside TUI installer ratatui application",
        workflows={w.value for w in InstallerWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
