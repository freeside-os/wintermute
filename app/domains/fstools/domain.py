"""Fstools domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import FstoolsWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="fstools",
        description="Python packaging and repository indexing utilities",
        workflows={w.value for w in FstoolsWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
