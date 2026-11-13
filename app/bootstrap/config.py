"""Hierarchical TOML configuration loader and serializer for Wintermute."""

from __future__ import annotations

import fnmatch
import os
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain.cost import BudgetPolicy
from app.domain.models import PipelineTemplate, StageKind, StageSpec


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            items = ", ".join(_format_inline_table(item) for item in value)
            return f"[{items}]"
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return _format_inline_table(value)
    return '""'


def _format_inline_table(table: dict[str, Any]) -> str:
    parts = [f"{_format_toml_key(k)} = {_format_toml_value(v)}" for k, v in table.items()]
    return "{ " + ", ".join(parts) + " }"


def _format_toml_key(key: str) -> str:
    if key.replace("_", "").replace("-", "").isalnum():
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_toml(data: dict[str, Any]) -> str:
    """Serialize a nested Python dictionary into readable TOML."""
    lines: list[str] = []

    def _emit_table(table: dict[str, Any], prefix: list[str]) -> None:
        scalars: list[tuple[str, Any]] = []
        subtables: list[tuple[str, dict[str, Any]]] = []
        for k, v in table.items():
            if v is None:
                continue
            if isinstance(v, dict):
                subtables.append((k, v))
            else:
                scalars.append((k, v))

        if prefix and scalars:
            lines.append("[" + ".".join(_format_toml_key(p) for p in prefix) + "]")
        for k, v in scalars:
            lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
        if prefix and scalars:
            lines.append("")

        for k, sub in subtables:
            _emit_table(sub, [*prefix, k])

    _emit_table(data, [])
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    alias: str
    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    vertex: bool = False
    project: str | None = None
    location: str | None = None
    model_path: str | None = None
    lightweight: bool = False

    @property
    def is_local(self) -> bool:
        if self.provider == "litert":
            return True
        if self.provider == "openai_compat" and self.base_url:
            lowered = self.base_url.lower()
            return any(h in lowered for h in ("localhost", "127.0.0.1", "0.0.0.0", ".local", ".ts.net"))
        return False


def _parse_yaml_scalar(raw: str) -> Any:
    val = raw.strip()
    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
        return val[1:-1]
    lowered = val.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if val.isdigit():
        return int(val)
    return val


def _parse_yaml_inline_list(raw: str) -> list[Any]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_parse_yaml_scalar(item) for item in inner.split(",") if item.strip()]


def parse_markdown_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML frontmatter (`--- ... ---`) from a markdown file."""
    cleaned = raw_text.lstrip("\ufeff")
    lines = cleaned.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, cleaned.strip()

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break

    if end_idx is None:
        return {}, cleaned.strip()

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key is not None:
            meta[current_list_key].append(_parse_yaml_scalar(stripped[2:]))
            continue
        if ":" in line:
            key, raw_val = line.split(":", 1)
            key = key.strip()
            val = raw_val.strip()
            if val == "":
                meta[key] = []
                current_list_key = key
            elif val.startswith("[") and val.endswith("]"):
                meta[key] = _parse_yaml_inline_list(val)
                current_list_key = None
            else:
                meta[key] = _parse_yaml_scalar(val)
                current_list_key = None

    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


@dataclass(frozen=True, slots=True)
class AgentRoleConfig:
    role: str
    model: str
    description: str = ""
    agent_file: str | None = None
    max_model_calls: int = 30
    max_tool_calls: int = 60
    max_total_tokens: int = 300_000
    read_only: bool = False
    thinking_level: str | None = None
    compaction_token_threshold: int | None = None
    skills: tuple[str, ...] = ()
    allowed_subagents: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    instructions: str | None = None

    def is_tool_denied(self, tool_name: str) -> bool:
        """Return True when tool_name matches any denied_tools glob pattern."""
        return any(
            fnmatch.fnmatchcase(tool_name, pat)
            for pat in self.denied_tools
            if not pat.startswith("@")
        )

    def is_tool_allowed(self, tool_name: str, *, include_macro_match: bool = False) -> bool:
        """Evaluate allowed_tools / denied_tools glob rules for a tool name."""
        if self.is_tool_denied(tool_name):
            return False
        if not self.allowed_tools:
            return True
        if (
            include_macro_match
            and tool_name.startswith("just_")
            and any(pat in {"@domain", "@read_only"} for pat in self.allowed_tools)
        ):
            return True
        return any(
            fnmatch.fnmatchcase(tool_name, pat)
            for pat in self.allowed_tools
            if not pat.startswith("@")
        )


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    workspace_root: Path
    worktrees_dir: Path
    sessions_dir: Path
    db_path: Path
    skills_dirs: tuple[Path, ...]
    sub_repos: tuple[str, ...]
    agents_dirs: tuple[Path, ...] = ()
    prefer_btrfs_cow: bool = True


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    enable_fast_path: bool
    default_template: str
    domain_defaults: dict[str, str]
    domain_test_targets: dict[str, tuple[str, ...]]
    templates: dict[str, PipelineTemplate]


@dataclass(frozen=True, slots=True)
class ConcurrencyConfig:
    max_concurrent_tasks: int
    max_concurrent_subagents_per_task: int
    build_locks: dict[str, int]


@dataclass(frozen=True, slots=True)
class CostConfig:
    policy: BudgetPolicy
    pricing_feeds: tuple[str, ...]
    pricing_cache_ttl_hours: int


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool
    embedding_model: str
    embedding_dimensions: int
    auto_prime_limit: int
    consolidate_threshold: int


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str = ""
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str
    port: int


@dataclass(slots=True)
class WintermuteConfig:
    workspace: WorkspaceConfig
    pipeline: PipelineConfig
    concurrency: ConcurrencyConfig
    cost: CostConfig
    models: dict[str, ModelProfile]
    agents: dict[str, AgentRoleConfig]
    memory: MemoryConfig
    server: ServerConfig
    skills: dict[str, SkillDefinition] = field(default_factory=dict)
    raw_data: dict[str, Any] = field(default_factory=dict)
    default_toml_path: Path = Path("wintermute/wintermute.toml")
    override_toml_path: Path = Path(".wintermute/config.toml")

    def resolve_skill(self, name: str) -> SkillDefinition | None:
        return self.skills.get(name)

    def resolve_skills_for_stage(
        self,
        *,
        role: str | None = None,
        explicit_skills: Sequence[str] = (),
    ) -> list[SkillDefinition]:
        """Resolve skills for a turn: explicit WorkUnit skills, else the role's declared skills."""
        resolved: list[SkillDefinition] = []
        seen: set[str] = set()

        def _add(skill_name: str) -> None:
            if skill_name in seen:
                return
            skill = self.skills.get(skill_name)
            if skill is not None:
                resolved.append(skill)
                seen.add(skill_name)

        for skill_name in explicit_skills:
            _add(skill_name)

        if not explicit_skills and role:
            role_cfg = self.agents.get(role)
            if role_cfg:
                for skill_name in role_cfg.skills:
                    _add(skill_name)

        return resolved

    def resolve_template_for_domain(self, domain: str | None, override: str | None = None) -> PipelineTemplate:
        name = override or (self.pipeline.domain_defaults.get(domain or "") if domain else None) or self.pipeline.default_template
        if name not in self.pipeline.templates:
            name = self.pipeline.default_template
        return self.pipeline.templates[name]

    def _find_agent_files(self, role: str, agent_file: str | None = None) -> list[Path]:
        """Return matching agent definition files ordered from lowest to highest priority."""
        role_cfg = self.agents.get(role)
        filename = agent_file or (role_cfg.agent_file if role_cfg else None) or f"{role}.md"
        return _collect_agent_files(
            filename=filename,
            agents_dirs=self.workspace.agents_dirs,
            workspace_root=self.workspace.workspace_root,
        )

    def resolve_agent_instructions(self, role: str) -> str | None:
        """Resolve per-role markdown body (without frontmatter) from agents_dirs or explicit agent_file."""
        files = self._find_agent_files(role)
        for path in reversed(files):
            _, body = parse_markdown_frontmatter(path.read_text(encoding="utf-8"))
            if body:
                return body
        return None

    def reload(self) -> WintermuteConfig:
        """Reload configuration from wintermute.toml and .wintermute/config.toml in place."""
        reloaded = load_config(
            workspace_root=self.workspace.workspace_root,
            default_toml_path=self.default_toml_path,
            override_toml_path=self.override_toml_path,
        )
        self.workspace = reloaded.workspace
        self.pipeline = reloaded.pipeline
        self.concurrency = reloaded.concurrency
        self.cost = reloaded.cost
        self.models = reloaded.models
        self.agents = reloaded.agents
        self.memory = reloaded.memory
        self.server = reloaded.server
        self.skills = reloaded.skills
        self.raw_data = reloaded.raw_data
        return self

    def save_override_raw(self, raw_toml_text: str) -> None:
        parsed = tomllib.loads(raw_toml_text)
        self.override_toml_path.parent.mkdir(parents=True, exist_ok=True)
        self.override_toml_path.write_text(raw_toml_text, encoding="utf-8")
        self.reload()
        self.raw_data = _deep_merge(self.raw_data, parsed)

    def save_override_dict(self, updates: dict[str, Any]) -> None:
        existing: dict[str, Any] = {}
        if self.override_toml_path.is_file():
            with open(self.override_toml_path, "rb") as f:
                existing = tomllib.load(f)
        merged = _deep_merge(existing, updates)
        self.save_override_raw(dump_toml(merged))


def _detect_workspace_root(workspace_root: str | Path | None = None) -> Path:
    if workspace_root:
        return Path(workspace_root).resolve()
    env_root = os.environ.get("FREESIDE_WORKSPACE_ROOT") or os.environ.get("WINTERMUTE_WORKSPACE_ROOT")
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if cwd.name == "wintermute" and (cwd.parent / "justfile").exists():
        return cwd.parent
    return cwd


def _resolve_rel(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p).resolve()


def _collect_agent_files(
    *,
    filename: str,
    agents_dirs: tuple[Path, ...],
    workspace_root: Path,
) -> list[Path]:
    candidate_path = Path(filename)
    if candidate_path.is_absolute():
        return [candidate_path] if candidate_path.is_file() else []

    found: list[Path] = []
    seen: set[Path] = set()
    for agents_dir in agents_dirs:
        p = (agents_dir / candidate_path).resolve()
        if p.is_file() and p not in seen:
            found.append(p)
            seen.add(p)
    if not found:
        ws_candidate = (workspace_root / candidate_path).resolve()
        if ws_candidate.is_file():
            found.append(ws_candidate)
    return found


def _resolve_bundled_dirs(
    root: Path,
    raw_dirs: Sequence[str],
    bundled_rel: str,
    bundled_dir: Path,
) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for raw_dir in raw_dirs:
        candidate = _resolve_rel(root, raw_dir)
        if not candidate.exists() and raw_dir == bundled_rel and bundled_dir.is_dir():
            candidate = bundled_dir.resolve()
        resolved.append(candidate)
    return tuple(resolved)


def _load_skills(skills_dirs: tuple[Path, ...]) -> dict[str, SkillDefinition]:
    skills: dict[str, SkillDefinition] = {}
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            fm, body = parse_markdown_frontmatter(skill_md.read_text(encoding="utf-8"))
            name = str(fm.get("name") or skill_md.parent.name)
            skills[name] = SkillDefinition(
                name=name,
                description=str(fm.get("description", "")),
                instructions=body,
                path=skill_md,
            )
    return skills


def _load_agent_frontmatter(
    *,
    role_name: str,
    agent_file: str | None,
    agents_dirs: tuple[Path, ...],
    workspace_root: Path,
) -> tuple[dict[str, Any], str | None]:
    filename = agent_file or f"{role_name}.md"
    files = _collect_agent_files(
        filename=filename,
        agents_dirs=agents_dirs,
        workspace_root=workspace_root,
    )
    meta: dict[str, Any] = {}
    resolved_body: str | None = None
    for path in files:
        fm, body = parse_markdown_frontmatter(path.read_text(encoding="utf-8"))
        meta.update(fm)
        if body:
            resolved_body = body
    return meta, resolved_body


def _parse_stage(item: dict[str, Any]) -> StageSpec:
    return StageSpec(
        name=str(item["name"]),
        kind=StageKind(item.get("kind", "agent")),
        role=item.get("role"),
        validators=tuple(item.get("validators", ())),
        require_file_edits=bool(item.get("require_file_edits", False)),
        pass_git_diff=bool(item.get("pass_git_diff", False)),
        skip_next_gate_if_unmodified=bool(item.get("skip_next_gate_if_unmodified", False)),
        run_only_on_failure=bool(item.get("run_only_on_failure", False)),
        attach_build_logs=bool(item.get("attach_build_logs", False)),
        read_only=bool(item.get("read_only", False)),
        on_fail_goto=item.get("on_fail_goto"),
        on_complete_goto=item.get("on_complete_goto"),
        capture_knowledge=bool(item.get("capture_knowledge", False)),
        instruction_hint=str(item.get("instruction_hint", "")),
    )


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
    ):
        return value[1:-1]
    return value


def load_dotenv_files(workspace_root: Path, *, override: bool = False) -> list[Path]:
    """Load environment variables from workspace `.env` files if present."""
    candidates = (
        workspace_root / ".env",
        workspace_root / ".wintermute" / ".env",
        workspace_root / "wintermute" / ".env",
    )
    loaded: list[Path] = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]

    for env_path in candidates:
        if not env_path.is_file():
            continue
        if load_dotenv is not None:
            load_dotenv(dotenv_path=env_path, override=override)
        else:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key and (override or key not in os.environ):
                    os.environ[key] = _strip_matching_quotes(val.strip())
        loaded.append(env_path)
    return loaded


def load_config(
    workspace_root: str | Path | None = None,
    default_toml_path: str | Path | None = None,
    override_toml_path: str | Path | None = None,
) -> WintermuteConfig:
    """Load hierarchical TOML configuration from wintermute.toml + .wintermute/config.toml."""
    root = _detect_workspace_root(workspace_root)
    load_dotenv_files(root)
    default_path = Path(default_toml_path) if default_toml_path else (root / "wintermute" / "wintermute.toml")
    if not default_path.exists():
        # Fallback relative to this file when invoked from installed package
        pkg_default = Path(__file__).resolve().parents[2] / "wintermute.toml"
        if pkg_default.exists():
            default_path = pkg_default

    override_path = Path(override_toml_path) if override_toml_path else (root / ".wintermute" / "config.toml")

    data: dict[str, Any] = {}
    if default_path.is_file():
        with open(default_path, "rb") as f:
            data = tomllib.load(f)
    if override_path.is_file():
        with open(override_path, "rb") as f:
            data = _deep_merge(data, tomllib.load(f))

    ws_raw = data.get("workspace", {})
    resolved_agents_dirs = _resolve_bundled_dirs(
        root,
        ws_raw.get("agents_dirs", ["wintermute/agents", ".wintermute/agents"]),
        "wintermute/agents",
        default_path.parent / "agents",
    )
    resolved_skills_dirs = _resolve_bundled_dirs(
        root,
        ws_raw.get("skills_dirs", ["wintermute/skills", ".wintermute/skills"]),
        "wintermute/skills",
        default_path.parent / "skills",
    )

    workspace = WorkspaceConfig(
        workspace_root=root,
        worktrees_dir=_resolve_rel(root, ws_raw.get("worktrees_dir", ".wintermute/worktrees")),
        sessions_dir=_resolve_rel(root, ws_raw.get("sessions_dir", ".wintermute/sessions")),
        db_path=_resolve_rel(root, ws_raw.get("db_path", ".wintermute/wintermute.db")),
        agents_dirs=resolved_agents_dirs,
        skills_dirs=resolved_skills_dirs,
        sub_repos=tuple(ws_raw.get("sub_repos", ["bootstrap", "packages", "straylight", "docs", "wintermute", "installer", "fstools"])),
        prefer_btrfs_cow=bool(ws_raw.get("prefer_btrfs_cow", True)),
    )

    pipe_raw = data.get("pipeline", {})
    templates: dict[str, PipelineTemplate] = {}
    for tpl_name, tpl_dict in pipe_raw.get("templates", {}).items():
        templates[tpl_name] = PipelineTemplate(
            name=tpl_name,
            description=str(tpl_dict.get("description", "")),
            max_retries_per_gate=int(tpl_dict.get("max_retries_per_gate", 3)),
            stages=tuple(_parse_stage(s) for s in tpl_dict.get("stages", [])),
        )
    pipeline = PipelineConfig(
        enable_fast_path=bool(pipe_raw.get("enable_fast_path", False)),
        default_template=str(pipe_raw.get("default_template", "standard_change")),
        domain_defaults=dict(pipe_raw.get("domain_defaults", {})),
        domain_test_targets={k: tuple(v) for k, v in pipe_raw.get("domain_test_targets", {}).items()},
        templates=templates,
    )

    conc_raw = data.get("concurrency", {})
    concurrency = ConcurrencyConfig(
        max_concurrent_tasks=int(os.environ.get("WINTERMUTE_MAX_CONCURRENT_TASKS", conc_raw.get("max_concurrent_tasks", 2))),
        max_concurrent_subagents_per_task=int(conc_raw.get("max_concurrent_subagents_per_task", 3)),
        build_locks={str(k): int(v) for k, v in conc_raw.get("build_locks", {}).items()},
    )

    cost_raw = data.get("cost", {})
    cost = CostConfig(
        policy=BudgetPolicy(
            max_task_usd=float(os.environ.get("WINTERMUTE_MAX_TASK_USD", cost_raw.get("max_task_usd", 5.0))),
            max_daily_usd=float(os.environ.get("WINTERMUTE_MAX_DAILY_USD", cost_raw.get("max_daily_usd", 25.0))),
            pause_on_budget_exceeded=bool(cost_raw.get("pause_on_budget_exceeded", True)),
        ),
        pricing_feeds=tuple(cost_raw.get("pricing_feeds", [])),
        pricing_cache_ttl_hours=int(cost_raw.get("pricing_cache_ttl_hours", 24)),
    )

    models: dict[str, ModelProfile] = {}
    for alias, m_dict in data.get("models", {}).items():
        models[alias] = ModelProfile(
            alias=alias,
            provider=str(m_dict.get("provider", "gemini")),
            model=str(m_dict.get("model", "gemini-3.8-flash")),
            base_url=m_dict.get("base_url"),
            api_key_env=m_dict.get("api_key_env"),
            vertex=bool(m_dict.get("vertex", False)),
            project=m_dict.get("project"),
            location=m_dict.get("location"),
            model_path=m_dict.get("model_path"),
            lightweight=bool(m_dict.get("lightweight", False)),
        )

    agents: dict[str, AgentRoleConfig] = {}
    for role_name, a_dict in data.get("agents", {}).items():
        agent_file = a_dict.get("agent_file")
        fm, instructions_body = _load_agent_frontmatter(
            role_name=role_name,
            agent_file=agent_file,
            agents_dirs=workspace.agents_dirs,
            workspace_root=root,
        )
        raw_thinking = (
            a_dict.get("thinking_level")
            if a_dict.get("thinking_level") is not None
            else fm.get("thinking_level")
        )
        raw_compaction = (
            a_dict.get("compaction_token_threshold")
            if a_dict.get("compaction_token_threshold") is not None
            else fm.get("compaction_token_threshold")
        )
        agents[role_name] = AgentRoleConfig(
            role=role_name,
            model=str(a_dict.get("model", "flash")),
            description=str(fm.get("description", a_dict.get("description", ""))),
            agent_file=agent_file,
            max_model_calls=int(a_dict.get("max_model_calls", 30)),
            max_tool_calls=int(a_dict.get("max_tool_calls", 60)),
            max_total_tokens=int(a_dict.get("max_total_tokens", 300_000)),
            read_only=bool(fm.get("read_only", a_dict.get("read_only", False))),
            thinking_level=str(raw_thinking).strip() if raw_thinking is not None else None,
            compaction_token_threshold=(
                int(raw_compaction) if raw_compaction is not None else None
            ),
            skills=tuple(
                str(x) for x in fm.get("skills", a_dict.get("skills", ()))
            ),
            allowed_subagents=tuple(
                str(x) for x in fm.get("allowed_subagents", a_dict.get("allowed_subagents", ()))
            ),
            allowed_tools=tuple(
                str(x) for x in fm.get("allowed_tools", a_dict.get("allowed_tools", ()))
            ),
            denied_tools=tuple(
                str(x) for x in fm.get("denied_tools", a_dict.get("denied_tools", ()))
            ),
            instructions=instructions_body,
        )

    mem_raw = data.get("memory", {})
    memory = MemoryConfig(
        enabled=bool(mem_raw.get("enabled", True)),
        embedding_model=str(mem_raw.get("embedding_model", "gemini-embedding-001")),
        embedding_dimensions=int(mem_raw.get("embedding_dimensions", 768)),
        auto_prime_limit=int(mem_raw.get("auto_prime_limit", 4)),
        consolidate_threshold=int(mem_raw.get("consolidate_threshold", 5)),
    )

    srv_raw = data.get("server", {})
    server = ServerConfig(
        host=str(os.environ.get("WINTERMUTE_HOST", srv_raw.get("host", "127.0.0.1"))),
        port=int(os.environ.get("WINTERMUTE_PORT", srv_raw.get("port", 8420))),
    )

    skills = _load_skills(workspace.skills_dirs)

    return WintermuteConfig(
        workspace=workspace,
        pipeline=pipeline,
        concurrency=concurrency,
        cost=cost,
        models=models,
        agents=agents,
        memory=memory,
        server=server,
        skills=skills,
        raw_data=data,
        default_toml_path=default_path,
        override_toml_path=override_path,
    )

