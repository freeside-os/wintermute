"""Namespaced Tool and Skill Library Registry for Wintermute."""

import fnmatch
from pathlib import Path
from typing import Any, Callable
from dataclasses import dataclass


@dataclass
class SkillInstruction:
    namespace: str
    name: str
    content: str
    path: Path | None = None


class RegistryLibrary:
    """Central registry mapping 'namespace:name' strings to tools and skills."""
    _tools: dict[str, Any] = {}
    _skills: dict[str, SkillInstruction] = {}

    @classmethod
    def register_tool(cls, namespace: str, name: str, tool_func: Any) -> None:
        key = f"{namespace}:{name}"
        cls._tools[key] = tool_func

    @classmethod
    def register_skill(cls, namespace: str, name: str, content: str, path: Path | None = None) -> None:
        key = f"{namespace}:{name}"
        cls._skills[key] = SkillInstruction(namespace=namespace, name=name, content=content, path=path)

    @classmethod
    def resolve_tools(cls, filter_patterns: list[str] | str | None) -> list[Any]:
        if not filter_patterns:
            return list(cls._tools.values())
        
        patterns = [filter_patterns] if isinstance(filter_patterns, str) else filter_patterns
        resolved = []
        for key, tool in cls._tools.items():
            for pattern in patterns:
                if fnmatch.fnmatch(key, pattern):
                    resolved.append(tool)
                    break
        return resolved

    @classmethod
    def resolve_skills(cls, filter_patterns: list[str] | str | None) -> list[SkillInstruction]:
        if not filter_patterns:
            return list(cls._skills.values())
        
        patterns = [filter_patterns] if isinstance(filter_patterns, str) else filter_patterns
        resolved = []
        for key, skill in cls._skills.items():
            for pattern in patterns:
                if fnmatch.fnmatch(key, pattern):
                    resolved.append(skill)
                    break
        return resolved
