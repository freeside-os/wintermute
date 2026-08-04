"""Domain registry and autodiscovery for Wintermute."""

from dataclasses import dataclass, field
import importlib
from importlib.metadata import entry_points
import pkgutil
from typing import Any, Callable

from app.types import KnownCoreWorkflow


@dataclass
class DomainSpec:
    """Specification for a registered domain."""
    name: str
    description: str
    workflows: set[str] = field(default_factory=set)
    provider_factory: Callable[[], Any] | None = None

    def supports_workflow(self, workflow_name: str) -> bool:
        """Checks if a workflow is supported by this domain.
        
        If self.workflows is defined, checks workflow_name in self.workflows.
        Otherwise, defaults to checking if workflow_name is in KnownCoreWorkflow.
        """
        if self.workflows:
            return workflow_name in self.workflows
        return workflow_name in KnownCoreWorkflow or any(workflow_name == w.value for w in KnownCoreWorkflow)


class DomainRegistry:
    """Central registry for discovering and validating domains."""
    _domains: dict[str, DomainSpec] = {}

    @classmethod
    def register(cls, spec: DomainSpec) -> None:
        cls._domains[spec.name] = spec

    @classmethod
    def get_domain(cls, name: str) -> DomainSpec | None:
        return cls._domains.get(name)

    @classmethod
    def list_domains(cls) -> list[DomainSpec]:
        return list(cls._domains.values())

    @classmethod
    def autodiscover(cls) -> None:
        """Autodiscovers internal domains in app.domains and external entry_points."""
        try:
            import app.domains as domains_pkg
            for _, module_name, is_pkg in pkgutil.iter_modules(domains_pkg.__path__):
                if is_pkg:
                    try:
                        mod = importlib.import_module(f"app.domains.{module_name}.domain")
                        if hasattr(mod, "register_domain"):
                            mod.register_domain(cls)
                    except ModuleNotFoundError:
                        pass
        except ModuleNotFoundError:
            pass

        try:
            for ep in entry_points(group="wintermute.domains"):
                plugin_init = ep.load()
                plugin_init(cls)
        except Exception:
            pass

    @classmethod
    def validate(cls, domain: str, workflow: str) -> None:
        if domain not in cls._domains:
            valid = list(cls._domains.keys())
            raise ValueError(f"Unknown domain '{domain}'. Registered domains: {valid}")
        
        domain_spec = cls._domains[domain]
        if not domain_spec.supports_workflow(workflow):
            valid_wf = list(domain_spec.workflows) if domain_spec.workflows else [w.value for w in KnownCoreWorkflow]
            raise ValueError(
                f"Domain '{domain}' does not support workflow '{workflow}'. "
                f"Supported workflows: {valid_wf}"
            )
