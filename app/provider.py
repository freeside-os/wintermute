"""DomainProvider protocol definition for Wintermute component domains."""

from typing import Any, Protocol, runtime_checkable

from app.registry import SkillInstruction


@runtime_checkable
class DomainProvider(Protocol):
    """Protocol for domain provider implementations."""
    name: str

    def get_tools(self) -> list[Any]:
        """Optional override to return custom domain tools."""
        ...

    def get_skills(self) -> list[SkillInstruction]:
        """Optional override to return custom domain skills/instructions."""
        ...

    def get_diagnostic_parser(self) -> Any | None:
        """Optional override to return a log/diagnostic parser."""
        ...

    def get_verification_runner(self) -> Any | None:
        """Optional override to return a verification test runner."""
        ...


class DefaultDomainProvider:
    """Default domain provider fallback when no custom provider class is supplied."""
    def __init__(self, name: str):
        self.name = name

    def get_tools(self) -> list[Any]:
        from app.registry import RegistryLibrary
        return RegistryLibrary.resolve_tools(f"{self.name}:*")

    def get_skills(self) -> list[SkillInstruction]:
        from app.registry import RegistryLibrary
        return RegistryLibrary.resolve_skills(f"{self.name}:*")

    def get_diagnostic_parser(self) -> Any | None:
        return None

    def get_verification_runner(self) -> Any | None:
        return None
