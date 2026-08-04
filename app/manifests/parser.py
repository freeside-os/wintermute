"""Kubernetes-style manifest parser for Wintermute workflow and event handler specifications."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
import yaml

from app.types import ExecutionSpec
from app.registry_domains import DomainRegistry


class ManifestKind(StrEnum):
    WORKFLOW = "Workflow"
    EVENT_HANDLER = "EventHandler"


@dataclass
class EventHandlerSpec:
    name: str
    source_type: str
    source_module: str
    source_config: dict[str, Any]
    handlers: list[dict[str, Any]]


class ManifestParser:
    """Parses apiVersion/kind/metadata/spec YAML manifests into ExecutionSpec or EventHandlerSpec."""

    @classmethod
    def parse_file(cls, path: Path) -> ExecutionSpec | EventHandlerSpec | list[ExecutionSpec]:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid YAML manifest at {path}")

        kind = data.get("kind")
        metadata = data.get("metadata", {})
        spec = data.get("spec", {})

        if kind == ManifestKind.WORKFLOW:
            domain = spec.get("domain", "packages")
            steps = spec.get("steps", [])
            execution_env = spec.get("execution_env", "host")

            if steps:
                specs = []
                for step in steps:
                    step_domain = step.get("domain", domain)
                    step_workflow = step.get("workflow", "check")
                    DomainRegistry.validate(step_domain, step_workflow)
                    specs.append(ExecutionSpec(
                        domain=step_domain,
                        workflow=step_workflow,
                        target=step.get("target"),
                        params=step.get("params", {}),
                        execution_env=execution_env
                    ))
                return specs if len(specs) > 1 else specs[0]
            else:
                workflow = spec.get("workflow", "check")
                DomainRegistry.validate(domain, workflow)
                return ExecutionSpec(
                    domain=domain,
                    workflow=workflow,
                    target=spec.get("target"),
                    params=spec.get("params", {}),
                    execution_env=execution_env
                )

        elif kind == ManifestKind.EVENT_HANDLER:
            source = spec.get("source", {})
            return EventHandlerSpec(
                name=metadata.get("name", "unnamed-handler"),
                source_type=source.get("type", "custom"),
                source_module=source.get("module", ""),
                source_config=source.get("config", {}),
                handlers=spec.get("handlers", [])
            )
        else:
            raise ValueError(f"Unsupported manifest kind '{kind}' in {path}")
