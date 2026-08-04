"""Simstim domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import SimstimWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="simstim",
        description="Declarative developer sandboxes and workspace snapshot subsystem",
        workflows={w.value for w in SimstimWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
