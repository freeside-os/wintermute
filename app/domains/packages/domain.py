"""Packages domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import PackagesWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="packages",
        description="Freeside user-space and system declarative package manifests and recipes",
        workflows={w.value for w in PackagesWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
