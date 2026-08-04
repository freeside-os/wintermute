"""Freesidectl domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import FreesidectlWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="freesidectl",
        description="Unified system control command-line utility",
        workflows={w.value for w in FreesidectlWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
