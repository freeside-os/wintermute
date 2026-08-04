"""Straylight domain plugin for Wintermute."""

from app.registry_domains import DomainRegistry, DomainSpec
from app.types import StraylightWorkflow, KnownCoreWorkflow

def register_domain(registry: type[DomainRegistry]) -> None:
    spec = DomainSpec(
        name="straylight",
        description="Straylight package manager CLI client and straylightd system daemon in Rust",
        workflows={w.value for w in StraylightWorkflow} | {w.value for w in KnownCoreWorkflow}
    )
    registry.register(spec)
