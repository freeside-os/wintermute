"""Scoped `just` AST tool synthesizer with worktree materialization and build locks."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.application.ports import JustToolProviderPort, WorktreePort
from app.bootstrap.config import AgentRoleConfig, WintermuteConfig
from app.domain.models import AgentRole, JustParamSpec, JustRecipeSpec

DumpProvider = Callable[[Path], dict[str, Any]]
CommandRunner = Callable[
    [list[str], Path, dict[str, str]], Awaitable[tuple[int, str, str]]
]

_READ_ONLY_TARGETS = frozenset(
    {
        "info",
        "resolve",
        "list",
        "logs",
        "audit",
        "verify",
        "verify-all",
        "status",
        "diff",
        "checksum",
    }
)

_MODULE_AFFECTED_REPOS: dict[str, tuple[str, ...]] = {
    "pkg": ("packages", "fstools"),
    "straylight": ("straylight",),
    "fstools": ("fstools",),
    "installer": ("installer",),
    "wintermute": ("wintermute",),
    "bootstrap": ("bootstrap",),
    "docs": ("docs",),
    "live": ("installer", "packages"),
    "sandbox": ("fstools", "packages"),
    "git": (),
    "sys": (),
    "": (),
}

_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "pkg_name": ("target", "name"),
    "target": ("pkg_name", "name"),
    "name": ("pkg_name", "target"),
}


def _infer_affected_repos(module: str, recipe_name: str) -> tuple[str, ...]:
    if module in _MODULE_AFFECTED_REPOS:
        return _MODULE_AFFECTED_REPOS[module]
    if "::" in recipe_name:
        prefix = recipe_name.split("::", 1)[0]
        if prefix in _MODULE_AFFECTED_REPOS:
            return _MODULE_AFFECTED_REPOS[prefix]
    return ()


def _is_read_only_recipe(target: str) -> bool:
    return target in _READ_ONLY_TARGETS


def _parse_recipe_dict(
    full_name: str,
    module: str,
    target: str,
    rdata: dict[str, Any],
) -> JustRecipeSpec:
    params: list[JustParamSpec] = []
    for p in rdata.get("parameters", []):
        if not isinstance(p, dict):
            continue
        raw_default = p.get("default")
        has_default = raw_default is not None
        default_val: str | None = None
        if isinstance(raw_default, str):
            default_val = raw_default
        elif isinstance(raw_default, list):
            # just --dump json represents string defaults as ["string", "val"] or similar
            default_val = str(raw_default[-1]) if raw_default else ""
        elif has_default:
            default_val = str(raw_default)
        params.append(
            JustParamSpec(
                name=str(p["name"]),
                default=default_val,
                has_default=has_default,
                kind=str(p.get("kind", "singular")),
            )
        )
    return JustRecipeSpec(
        name=full_name,
        module=module,
        target=target,
        doc=str(rdata.get("doc") or f"Execute `just {full_name}`"),
        parameters=tuple(params),
        affected_repos=_infer_affected_repos(module, full_name),
        read_only=_is_read_only_recipe(target),
    )


def parse_just_dump_json(payload: dict[str, Any]) -> dict[str, JustRecipeSpec]:
    """Parse `just --dump --dump-format json` output into JustRecipeSpec records."""
    specs: dict[str, JustRecipeSpec] = {}

    def _walk_module(mod_node: dict[str, Any], mod_prefix: str) -> None:
        for r_key, r_data in mod_node.get("recipes", {}).items():
            if not isinstance(r_data, dict):
                continue
            if r_data.get("private"):
                continue
            target = str(r_data.get("name") or r_key)
            if "::" in target and not mod_prefix:
                module, short_target = target.split("::", 1)
                full_name = target
            elif mod_prefix:
                module = mod_prefix
                short_target = target
                full_name = f"{mod_prefix}::{target}"
            else:
                module = ""
                short_target = target
                full_name = target
            specs[full_name] = _parse_recipe_dict(full_name, module, short_target, r_data)

        for sub_name, sub_node in mod_node.get("modules", {}).items():
            if isinstance(sub_node, dict):
                next_prefix = f"{mod_prefix}::{sub_name}" if mod_prefix else sub_name
                _walk_module(sub_node, next_prefix)

    _walk_module(payload, "")
    return specs


_MOD_DECL_RE = re.compile(r"^mod\??\s+([A-Za-z0-9_-]+)\s+['\"]([^'\"]+)['\"]")
_RECIPE_HEADER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)(?:\s+(.*?))?:(?:\s|$)")


def _parse_justfile_params(raw_params: str) -> tuple[JustParamSpec, ...]:
    if not raw_params.strip():
        return ()
    specs: list[JustParamSpec] = []
    # Split respecting quoted defaults
    tokens = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+', raw_params)
    for tok in tokens:
        kind = "singular"
        if tok.startswith("*"):
            kind = "star"
            tok = tok[1:]
        elif tok.startswith("+"):
            kind = "plus"
            tok = tok[1:]
        if "=" in tok:
            pname, pdef = tok.split("=", 1)
            pdef = pdef.strip().strip('"').strip("'")
            specs.append(
                JustParamSpec(name=pname, default=pdef, has_default=True, kind=kind)
            )
        else:
            specs.append(JustParamSpec(name=tok, has_default=False, kind=kind))
    return tuple(specs)


def _fallback_parse_justfile(just_path: Path, module: str = "") -> dict[str, JustRecipeSpec]:
    """Parse a justfile statically when `just` CLI is not installed in PATH."""
    if not just_path.is_file():
        return {}
    specs: dict[str, JustRecipeSpec] = {}
    last_comment = ""
    for line in just_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            last_comment = ""
            continue
        if stripped.startswith("#"):
            last_comment = stripped.lstrip("#").strip()
            continue
        if line.startswith((" ", "\t")):
            continue
        if stripped.startswith(("set ", "export ", "import ", "alias ", "mod ", "mod? ")):
            mod_match = _MOD_DECL_RE.match(stripped)
            if mod_match and not module:
                sub_mod, rel_file = mod_match.group(1), mod_match.group(2)
                specs.update(
                    _fallback_parse_justfile(just_path.parent / rel_file, module=sub_mod)
                )
            last_comment = ""
            continue
        if ":=" in stripped:
            last_comment = ""
            continue
        m = _RECIPE_HEADER_RE.match(stripped)
        if m:
            target = m.group(1)
            raw_params = m.group(2) or ""
            if target.startswith("_"):
                last_comment = ""
                continue
            full_name = f"{module}::{target}" if module else target
            specs[full_name] = JustRecipeSpec(
                name=full_name,
                module=module,
                target=target,
                doc=last_comment or f"Execute `just {full_name}`",
                parameters=_parse_justfile_params(raw_params),
                affected_repos=_infer_affected_repos(module, full_name),
                read_only=_is_read_only_recipe(target),
            )
            last_comment = ""
    return specs


async def _default_command_runner(
    cmd: list[str], cwd: Path, env: dict[str, str]
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


@dataclass(slots=True)
class JustToolSynthesizer(JustToolProviderPort):
    config: WintermuteConfig
    worktree: WorktreePort | None = None
    dump_provider: DumpProvider | None = None
    command_runner: CommandRunner | None = None
    dispatch_callback: Callable[..., Awaitable[str]] | None = None
    _recipes: dict[str, JustRecipeSpec] = field(default_factory=dict)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for lock_name, limit in self.config.concurrency.build_locks.items():
            self._semaphores[lock_name] = asyncio.Semaphore(max(1, int(limit)))

    def _load_dump_payload(self) -> dict[str, Any] | None:
        ws_root = self.config.workspace.workspace_root
        if self.dump_provider is not None:
            return self.dump_provider(ws_root)
        just_bin = shutil.which("just")
        if not just_bin:
            return None
        import subprocess

        proc = subprocess.run(
            [just_bin, "--dump", "--dump-format", "json"],
            cwd=str(ws_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)

    def discover_recipes(self, reload: bool = False) -> dict[str, JustRecipeSpec]:
        if self._recipes and not reload:
            return dict(self._recipes)

        payload = self._load_dump_payload()
        if payload is not None:
            self._recipes = parse_just_dump_json(payload)
        else:
            self._recipes = _fallback_parse_justfile(
                self.config.workspace.workspace_root / "justfile"
            )
        return dict(self._recipes)

    def _tool_fn_name(self, recipe_name: str) -> str:
        sanitized = recipe_name.replace("::", "_").replace("-", "_")
        return f"just_{sanitized}"

    def _make_tool_callable(
        self, spec: JustRecipeSpec, task_id: str | None
    ) -> Callable[..., Awaitable[str]]:
        synth = self

        async def _invoke(*args: Any, **kwargs: Any) -> str:
            values: list[str] = []
            last_non_default_idx = -1
            declared_param_names = {
                p.name.replace("-", "_") for p in spec.parameters
            } | {p.name for p in spec.parameters}
            for idx, param in enumerate(spec.parameters):
                py_name = param.name.replace("-", "_")
                if idx < len(args):
                    val = args[idx]
                elif py_name in kwargs:
                    val = kwargs[py_name]
                elif param.name in kwargs:
                    val = kwargs[param.name]
                else:
                    val = param.default
                    for alias in _PARAM_ALIASES.get(py_name, ()):
                        if alias in kwargs and alias not in declared_param_names:
                            val = kwargs[alias]
                            break

                sval = "" if val is None else str(val)
                values.append(sval)
                default_str = "" if param.default is None else str(param.default)
                if not param.has_default or (val is not None and (sval != "" or sval != default_str)):
                    last_non_default_idx = idx

            ordered_args = values[: last_non_default_idx + 1] if last_non_default_idx >= 0 else []
            ok, output = await synth._execute_spec(
                spec, args=ordered_args, task_id=task_id
            )
            prefix = "[OK]" if ok else "[FAILED]"
            return f"{prefix} just {spec.name}\n{output}".strip()

        sig_params = [
            inspect.Parameter(
                name=p.name.replace("-", "_"),
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=(p.default if p.has_default else inspect.Parameter.empty),
                annotation=str,
            )
            for p in spec.parameters
        ]
        _invoke.__name__ = self._tool_fn_name(spec.name)
        _invoke.__qualname__ = _invoke.__name__
        _invoke.__doc__ = f"{spec.doc}\n\nJust recipe: `{spec.name}`"
        _invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters=sig_params, return_annotation=str
        )
        return _invoke

    async def _execute_spec(
        self,
        spec: JustRecipeSpec,
        *,
        args: list[str],
        task_id: str | None,
    ) -> tuple[bool, str]:
        cwd = self.config.workspace.workspace_root
        env = os.environ.copy()

        if task_id and self.worktree is not None:
            if spec.affected_repos:
                wt_info = await self.worktree.ensure_materialized(
                    task_id, spec.affected_repos
                )
            else:
                wt_info = await self.worktree.ensure_worktree(task_id)
            cwd = Path(wt_info.root_path)
            env["STRAYLIGHT_PACKAGES_ROOT"] = str(cwd / "packages")
            env["STRAYLIGHT_BUILDER_ROOT"] = wt_info.build_root
            env["STRAYLIGHT_BUILDER_OUTPUT_ROOT"] = str(
                Path(wt_info.build_root) / "packages"
            )

        just_bin = shutil.which("just") or "just"
        cmd = [just_bin, spec.name, *args]
        runner = self.command_runner or _default_command_runner

        sem = self._semaphores.get(spec.name)
        if sem is not None:
            async with sem:
                rc, out, err = await runner(cmd, cwd, env)
        else:
            rc, out, err = await runner(cmd, cwd, env)

        combined = "\n".join(part for part in (out.strip(), err.strip()) if part)
        return rc == 0, combined

    def _matches_domain(self, spec: JustRecipeSpec, domain: str | None) -> bool:
        if spec.module == "git":
            return True
        if not domain:
            return True
        if domain == "packages":
            return spec.module == "pkg"
        if domain == "installer":
            return spec.module in {"installer", "live"}
        return spec.module == domain

    def _allow_recipe_for_role(
        self,
        spec: JustRecipeSpec,
        role_cfg: AgentRoleConfig | None,
        domain: str | None,
    ) -> bool:
        tool_name = self._tool_fn_name(spec.name)
        if role_cfg is None:
            return self._matches_domain(spec, domain)
        if role_cfg.is_tool_denied(tool_name):
            return False
        if role_cfg.read_only and not spec.read_only:
            return False
        if not role_cfg.allowed_tools:
            return self._matches_domain(spec, domain)
        for pat in role_cfg.allowed_tools:
            if pat == "@domain":
                if self._matches_domain(spec, domain):
                    return True
            elif pat == "@read_only":
                if spec.read_only:
                    return True
            elif fnmatch.fnmatchcase(tool_name, pat):
                return True
        return False

    def get_tools_for(
        self,
        role: AgentRole,
        domain: str | None = None,
        task_id: str | None = None,
        dispatch_callback: Callable[..., Awaitable[str]] | None = None,
    ) -> list[Callable[..., Any]]:
        specs = self.discover_recipes()
        role_cfg = self.config.agents.get(role.value)
        tools: list[Callable[..., Any]] = [
            self._make_tool_callable(spec, task_id)
            for spec in specs.values()
            if self._allow_recipe_for_role(spec, role_cfg, domain)
        ]

        orchestration_tools = (
            ("reload_just_tools", self._build_reload_tool),
            ("reload_wintermute_config", self._build_reload_config_tool),
            (
                "dispatch_work_unit",
                lambda: self._build_dispatch_tool(dispatch_callback=dispatch_callback),
            ),
        )
        for tool_name, builder in orchestration_tools:
            if role_cfg is not None and role_cfg.allowed_tools and role_cfg.is_tool_allowed(tool_name):
                tools.append(builder())
        return tools

    def _build_reload_tool(self) -> Callable[..., str]:
        synth = self

        def reload_just_tools() -> str:
            """Re-parse justfile recipes and reload synthesized `just_*` tools."""
            specs = synth.discover_recipes(reload=True)
            return f"Reloaded {len(specs)} just recipes."

        return reload_just_tools

    def _build_reload_config_tool(self) -> Callable[..., str]:
        synth = self

        def reload_wintermute_config() -> str:
            """Reload wintermute.toml + .wintermute/config.toml and refresh runtime build locks."""
            cfg = synth.config.reload()
            synth._semaphores = {
                lock_name: asyncio.Semaphore(max(1, int(limit)))
                for lock_name, limit in cfg.concurrency.build_locks.items()
            }
            return (
                f"Reloaded config: {len(cfg.pipeline.templates)} pipeline(s), "
                f"{len(cfg.agents)} agent role(s), {len(cfg.models)} model(s)."
            )

        return reload_wintermute_config

    def _build_dispatch_tool(
        self, dispatch_callback: Callable[..., Awaitable[str]] | None = None
    ) -> Callable[..., Awaitable[str]]:
        synth = self

        async def dispatch_work_unit(
            domain: str,
            target: str = "",
            pipeline_template: str = "",
            complexity: str = "simple",
            skills: str = "",
            prompt: str = "",
            fast_path: bool = False,
        ) -> str:
            """Dispatch a typed WorkUnit into the stage-gate pipeline engine."""
            cb = dispatch_callback or synth.dispatch_callback
            if cb is not None:
                return await cb(
                    domain=domain,
                    target=target,
                    pipeline_template=pipeline_template,
                    complexity=complexity,
                    skills=[s.strip() for s in skills.split(",") if s.strip()],
                    prompt=prompt,
                    fast_path=bool(fast_path),
                )
            return (
                f"Queued work unit: domain={domain}, target={target}, "
                f"pipeline_template={pipeline_template or 'auto'}, complexity={complexity}, "
                f"skills={skills or 'auto'}, fast_path={bool(fast_path)}."
            )

        return dispatch_work_unit

    async def run_validator(
        self,
        validator: str,
        *,
        task_id: str,
        domain: str,
        target: str | None = None,
    ) -> tuple[bool, str]:
        specs = self.discover_recipes()
        effective_validator = validator
        if validator == "pkg::verify" and not target and "pkg::verify-all" in specs:
            effective_validator = "pkg::verify-all"
        spec = specs.get(effective_validator)
        if spec is None:
            return False, f"Validator recipe '{validator}' not found in justfiles."

        args: list[str] = []
        if target and spec.parameters:
            first_param = spec.parameters[0]
            if not first_param.has_default or first_param.name in {
                "pkg_name",
                "target",
                "group_name",
            }:
                args.append(target)

        return await self._execute_spec(spec, args=args, task_id=task_id)

