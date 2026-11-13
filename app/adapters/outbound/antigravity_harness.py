"""Google Antigravity SDK harness adapter with policies, budgets, and stop gates."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.application.ports import (
    AgentHarnessPort,
    BudgetScope,
    EventCallback,
    InteractionCallback,
    PreWriteCallback,
    StageTurnResult,
    StopGateCallback,
)
from app.bootstrap.config import AgentRoleConfig, ModelProfile, WintermuteConfig
from app.domain.cost import TokenUsage
from app.domain.models import AgentRole, Task, WorkUnit

TurnRunner = Callable[[dict[str, Any]], Awaitable[StageTurnResult]]

_BLOCKED_RAW_COMMANDS = ("cargo ", "pytest", "straylight", "fstools")
_CONVERSATION_ID_RE = re.compile(r"^[a-zA-Z0-9-]+$")


def should_block_raw_command(args: dict[str, Any]) -> bool:
    """Policy predicate blocking raw cargo/pytest/straylight/fstools shell calls."""
    cmd = str(args.get("CommandLine") or args.get("command") or "").strip().lower()
    if not cmd:
        return False
    for token in _BLOCKED_RAW_COMMANDS:
        if cmd.startswith(token) or f" {token}" in cmd or f"/{token.strip()}" in cmd:
            return True
    return False


_PATH_ARG_KEYS = (
    "path",
    "file_path",
    "TargetFile",
    "AbsolutePath",
    "DirectoryPath",
    "directory",
)
_QUERY_ARG_KEYS = ("query", "Query", "pattern", "Pattern")
_SCOPE_ARG_KEYS = ("SearchPath", "SearchDirectory", "path", "directory")
_CMD_ARG_KEYS = ("CommandLine", "command")

_FILE_TOOLS = frozenset({"view_file", "edit_file", "create_file", "write_to_file"})
_LIST_TOOLS = frozenset({"list_directory", "list_dir"})
_SEARCH_TOOLS = frozenset({"find_file", "find_by_name", "search_directory", "grep_search"})
_CMD_TOOLS = frozenset({"run_command"})


def _compact_text(value: Any, max_len: int = 88) -> str:
    """Collapse internal whitespace and truncate cleanly at word boundaries."""
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= max_len:
        return collapsed
    clipped = collapsed[: max_len - 1].rstrip()
    if " " in clipped and len(clipped) > max_len // 2:
        clipped = clipped.rsplit(" ", 1)[0]
    return f"{clipped}…"


def _shorten_path(raw: Any, worktree_path: str | None) -> str:
    """Strip the active worktree prefix from paths when present."""
    text = str(raw).strip()
    if not text or not worktree_path:
        return text
    wt = worktree_path.rstrip("/")
    if text == wt:
        return "."
    prefix = f"{wt}/"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _first_present(args: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        val = args.get(key)
        if val not in (None, ""):
            return val
    return None


def format_tool_args(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    worktree_path: str | None = None,
    max_len: int = 92,
) -> str:
    """Format tool parameters into a concise single-line preview for CLI telemetry."""
    if not args or not isinstance(args, dict):
        return ""

    if tool_name in _CMD_TOOLS:
        cmd = _first_present(args, _CMD_ARG_KEYS)
        if cmd is not None:
            return _compact_text(cmd, max_len=max_len)

    if tool_name in _FILE_TOOLS:
        raw_path = _first_present(args, _PATH_ARG_KEYS)
        if raw_path is not None:
            short = _shorten_path(raw_path, worktree_path)
            start = args.get("StartLine") or args.get("start_line")
            end = args.get("EndLine") or args.get("end_line")
            if start is not None and end is not None:
                return _compact_text(f"{short}:{start}-{end}", max_len=max_len)
            if start is not None:
                return _compact_text(f"{short}:{start}+", max_len=max_len)
            return _compact_text(short, max_len=max_len)

    if tool_name in _LIST_TOOLS:
        raw_path = _first_present(args, _PATH_ARG_KEYS)
        if raw_path is not None:
            return _compact_text(_shorten_path(raw_path, worktree_path) or ".", max_len=max_len)

    if tool_name in _SEARCH_TOOLS:
        query = _first_present(args, _QUERY_ARG_KEYS)
        scope = _first_present(args, _SCOPE_ARG_KEYS)
        if query is not None:
            q_part = f'"{_compact_text(query, max_len=44)}"'
            if scope is not None:
                short_scope = _shorten_path(scope, worktree_path) or "."
                return _compact_text(f"{q_part} in {short_scope}", max_len=max_len)
            return _compact_text(q_part, max_len=max_len)

    parts: list[str] = []
    for key, val in args.items():
        if val in (None, "") or key in {"SafeToAutoRun", "WaitMsBeforeAsync"}:
            continue
        if isinstance(val, str):
            rendered = _shorten_path(val, worktree_path) if "/" in val else val
            parts.append(f"{key}={_compact_text(rendered, max_len=38)}")
        elif isinstance(val, (int, float, bool)):
            parts.append(f"{key}={val}")
        elif isinstance(val, list):
            parts.append(f"{key}=[{len(val)} items]")
    return _compact_text(", ".join(parts), max_len=max_len)


def extract_intent_summary(raw_text: str, max_len: int = 140) -> str:
    """Extract a crisp one-line reasoning/intention headline without verbose dumps."""
    text = raw_text.strip()
    if not text:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip leading markdown heading markers and wrapping bold stars.
        line = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            line = line[2:-2].strip()
        line = line.replace("**", "")
        if not line:
            continue
        sentence_end = re.search(r"[.!?](?:\s|$)", line)
        if sentence_end and sentence_end.start() >= 18:
            line = line[: sentence_end.end()].strip()
        return _compact_text(line, max_len=max_len)
    return ""


async def stream_chat_response(
    response: Any,
    *,
    event_callback: EventCallback | None,
    role: str,
    worktree_path: str | None = None,
) -> str:
    """Drain ChatResponse.chunks, collect assistant text, and emit concise telemetry."""
    from google.antigravity import types  # type: ignore[import-not-found]

    all_text_parts: list[str] = []
    thought_buf: list[str] = []
    step_text_buf: list[str] = []
    current_step_idx: int | None = None
    last_intent: str | None = None
    pending_calls: dict[str, tuple[str, str]] = {}

    def _reset_step(next_step: int) -> None:
        nonlocal current_step_idx
        if current_step_idx is not None and next_step != current_step_idx:
            thought_buf.clear()
            step_text_buf.clear()
        current_step_idx = next_step

    def _emit_pre_tool_intent() -> None:
        nonlocal last_intent
        if event_callback is None:
            thought_buf.clear()
            step_text_buf.clear()
            return
        plan_text = "".join(step_text_buf).strip()
        thought_text = "".join(thought_buf).strip()
        thought_buf.clear()
        step_text_buf.clear()

        if plan_text:
            kind = "plan"
            summary = extract_intent_summary(plan_text)
        elif thought_text:
            kind = "thought"
            summary = extract_intent_summary(thought_text)
        else:
            return

        if summary and summary != last_intent:
            last_intent = summary
            event_callback(
                "AGENT_INTENT",
                {"role": role, "kind": kind, "summary": summary},
            )

    async for chunk in response.chunks:
        if isinstance(chunk, types.Thought):
            _reset_step(chunk.step_index)
            thought_buf.append(chunk.text)
        elif isinstance(chunk, types.Text):
            all_text_parts.append(chunk.text)
            _reset_step(chunk.step_index)
            step_text_buf.append(chunk.text)
        elif isinstance(chunk, types.ToolCall):
            _emit_pre_tool_intent()
            tool_name = str(getattr(chunk, "name", "unknown"))
            raw_args = getattr(chunk, "args", None)
            args_summary = format_tool_args(
                tool_name,
                raw_args if isinstance(raw_args, dict) else None,
                worktree_path=worktree_path,
            )
            call_id = getattr(chunk, "id", None)
            if call_id:
                pending_calls[str(call_id)] = (tool_name, args_summary)
            if event_callback is not None:
                event_callback(
                    "TOOL_CALL_STARTED",
                    {"tool": tool_name, "args": args_summary, "role": role},
                )
        elif isinstance(chunk, types.ToolResult):
            call_id = getattr(chunk, "id", None)
            tool_name, args_summary = (
                pending_calls.pop(str(call_id), (str(getattr(chunk, "name", "unknown")), ""))
                if call_id
                else (str(getattr(chunk, "name", "unknown")), "")
            )
            err = getattr(chunk, "error", None)
            if err and event_callback is not None:
                event_callback(
                    "TOOL_CALL_FAILED",
                    {
                        "tool": tool_name,
                        "args": args_summary,
                        "error": _compact_text(err, max_len=140),
                        "role": role,
                    },
                )

    return "".join(all_text_parts)


@dataclass(slots=True)
class AntigravityHarnessAdapter(AgentHarnessPort):
    config: WintermuteConfig
    turn_runner: TurnRunner | None = None
    _cancelled_tasks: set[str] = field(default_factory=set)

    def build_sdk_agent_config(
        self,
        *,
        role: AgentRole,
        tools: Sequence[Callable[..., Any]],
        worktree_path: str,
        read_only: bool = False,
        stop_gate: StopGateCallback | None = None,
        budget_scope: BudgetScope = BudgetScope.LIFETIME,
        model_override: str | None = None,
        thinking_level_override: str | None = None,
        conversation_id: str | None = None,
        pre_write_callback: PreWriteCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> Any:
        role_cfg: AgentRoleConfig = self.config.agents[role.value]
        effective_model_alias = model_override or role_cfg.model
        model_profile: ModelProfile = self.config.models[effective_model_alias]
        effective_thinking_level = (
            thinking_level_override
            or role_cfg.thinking_level
        )
        if role_cfg.skills:
            skills_paths = sorted(
                str(skill.path.parent)
                for skill_name in role_cfg.skills
                if (skill := self.config.resolve_skill(skill_name)) is not None
                and skill.path is not None
                and skill.path.parent.exists()
            )
        else:
            skills_paths = sorted(
                str(p) for p in self.config.workspace.skills_dirs if p.exists()
            )
        system_instructions = self.config.resolve_agent_instructions(role.value)
        effective_read_only = read_only or role_cfg.read_only
        filtered_tools = sorted(
            (
                t
                for t in tools
                if role_cfg.is_tool_allowed(
                    getattr(t, "__name__", str(t)), include_macro_match=True
                )
            ),
            key=lambda t: getattr(t, "__name__", str(t)),
        )

        try:
            from google.antigravity import (  # type: ignore[import-not-found]
                LiteRTAgentConfig,
                LocalAgentConfig,
                LocalOpenAIAgentConfig,
                types,
            )
            from google.antigravity.hooks import hooks, policy  # type: ignore[import-not-found]
        except ImportError:
            # Structured descriptor when google-antigravity is absent in unit tests
            return {
                "provider": model_profile.provider,
                "model": model_profile.model,
                "thinking_level": effective_thinking_level,
                "compaction_token_threshold": role_cfg.compaction_token_threshold,
                "role": role.value,
                "system_instructions": system_instructions,
                "worktree_path": worktree_path,
                "read_only": effective_read_only,
                "skills_paths": skills_paths,
                "budget_scope": budget_scope.value,
                "conversation_id": conversation_id,
                "allowed_subagents": list(role_cfg.allowed_subagents),
                "allowed_tools": list(role_cfg.allowed_tools),
                "denied_tools": list(role_cfg.denied_tools),
                "tools": [getattr(t, "__name__", str(t)) for t in filtered_tools],
            }

        budget_kwargs: dict[str, Any] = {
            "max_model_calls": role_cfg.max_model_calls,
            "max_tool_calls": role_cfg.max_tool_calls,
            "max_total_tokens": role_cfg.max_total_tokens,
        }
        if hasattr(types, "BudgetScope"):
            budget_kwargs["scope"] = getattr(types.BudgetScope, budget_scope.value)
        budget_cfg = types.BudgetConfig(**budget_kwargs)

        subagent_configs: list[Any] = []
        can_use_subagents = (
            bool(role_cfg.allowed_subagents)
            and not effective_read_only
            and not model_profile.lightweight
            and model_profile.provider != "litert"
            and role_cfg.is_tool_allowed("invoke_subagent")
        )
        if can_use_subagents:
            for sub_name in sorted(role_cfg.allowed_subagents):
                sub_role = self.config.agents.get(sub_name)
                if sub_role is None:
                    continue
                sub_model_alias = model_override or sub_role.model
                sub_model = self.config.models[sub_model_alias].model
                subagent_configs.append(
                    types.SubagentConfig(
                        name=sub_name,
                        description=sub_role.description or f"Freeside {sub_name} agent.",
                        system_instructions=self.config.resolve_agent_instructions(sub_name),
                        model=sub_model,
                    )
                )

        researcher_gated_run_cmd = (
            role == AgentRole.RESEARCHER and role_cfg.is_tool_allowed("run_command")
        )
        enabled_builtin_tools: list[Any] = []
        write_exec_builtins = {
            types.BuiltinTools.EDIT_FILE,
            types.BuiltinTools.CREATE_FILE,
            types.BuiltinTools.RUN_COMMAND,
        }
        for bt in types.BuiltinTools:
            if bt.value == "start_subagent":
                continue
            if bt == types.BuiltinTools.FINISH:
                enabled_builtin_tools.append(bt)
            elif bt == types.BuiltinTools.INVOKE_SUBAGENT:
                if subagent_configs:
                    enabled_builtin_tools.append(bt)
            elif bt == types.BuiltinTools.RUN_COMMAND and researcher_gated_run_cmd:
                enabled_builtin_tools.append(bt)
            elif bt in write_exec_builtins:
                if not effective_read_only and role_cfg.is_tool_allowed(bt.value):
                    enabled_builtin_tools.append(bt)
            elif role_cfg.is_tool_allowed(bt.value):
                enabled_builtin_tools.append(bt)

        # SDK PolicyMatcher matches exact tool names or "*" (no fnmatch globs).
        # Build deny-first exact allowlist followed by deny_all().
        policies: list[Any] = []
        if not researcher_gated_run_cmd:
            policies.append(
                policy.deny(
                    "run_command",
                    when=should_block_raw_command,
                    reason="Raw cargo/pytest/straylight/fstools shell commands are blocked; use synthesized just_* tools.",
                    name="block_raw_build_commands",
                )
            )
        denied_names: set[str] = set()
        if effective_read_only:
            ro_tools = (
                ("edit_file", "create_file")
                if researcher_gated_run_cmd
                else ("edit_file", "create_file", "run_command")
            )
            for ro_denied in ro_tools:
                policies.append(
                    policy.deny(
                        ro_denied,
                        reason=f"Role '{role.value}' is read-only in this stage.",
                    )
                )
                denied_names.add(ro_denied)

        for pat in role_cfg.denied_tools:
            if (
                not pat.startswith("@")
                and not any(ch in pat for ch in "*?[")
                and pat not in denied_names
            ):
                policies.append(policy.deny(pat, reason=f"Denied for role '{role.value}'."))
                denied_names.add(pat)

        for t in tools:
            tname = getattr(t, "__name__", str(t))
            if role_cfg.is_tool_denied(tname) and tname not in denied_names:
                policies.append(policy.deny(tname, reason=f"Denied for role '{role.value}'."))
                denied_names.add(tname)

        async def _approve_researcher_run_command(
            call: types.ToolCall, _reason: str | None = None
        ) -> bool:
            if interaction_callback is None:
                return False
            cmd_val = _first_present(call.args or {}, _CMD_ARG_KEYS)
            cmd_preview = str(cmd_val) if cmd_val is not None else str(call.args or "")
            answers = await interaction_callback(
                [
                    {
                        "question": (
                            f"Researcher wants to run command:\n  {cmd_preview}\n"
                            "Approve execution?"
                        ),
                        "options": [
                            {"id": "yes", "text": "Yes"},
                            {"id": "no", "text": "No"},
                        ],
                    }
                ]
            )
            if not answers:
                return False
            return str(answers[0]).strip().lower() in {
                "y",
                "yes",
                "approve",
                "approved",
                "true",
                "1",
            }

        allowed_names: set[str] = set()
        for t in filtered_tools:
            tname = getattr(t, "__name__", str(t))
            if tname not in denied_names and tname not in allowed_names:
                policies.append(policy.allow(tname))
                allowed_names.add(tname)
        for bt in enabled_builtin_tools:
            bname = bt.value
            if bname not in denied_names and bname not in allowed_names:
                if researcher_gated_run_cmd and bt == types.BuiltinTools.RUN_COMMAND:
                    policies.append(
                        policy.ask_user(
                            bname,
                            handler=_approve_researcher_run_command,
                            name="researcher_approve_run_command",
                            reason="Researcher command execution always requires user approval.",
                        )
                    )
                else:
                    policies.append(policy.allow(bname))
                allowed_names.add(bname)
        policies.append(
            policy.deny(
                "*",
                reason=f"Tool is not in the allowlist for role '{role.value}'.",
                name="deny_all",
            )
        )

        agent_hooks: list[Any] = []
        if stop_gate is not None:

            @hooks.stop
            async def _on_stop(data: Any) -> Any:
                resp_text = getattr(data, "response_text", "")
                cont_count = int(getattr(data, "continuation_count", 0))
                allow_stop, reason = await stop_gate(resp_text, cont_count)
                if allow_stop:
                    return types.StopHookResult(decision=types.StopDecision.ALLOW_STOP)
                return types.StopHookResult(
                    decision=types.StopDecision.CONTINUE,
                    reason=reason or "Stage gate requirements are not yet satisfied.",
                )

            agent_hooks.append(_on_stop)

        if pre_write_callback is not None:

            @hooks.pre_tool_call_decide
            async def _materialize_before_write(data: Any) -> Any:
                tname = getattr(data, "name", "")
                args = getattr(data, "args", None)
                if tname in {"edit_file", "create_file"} and isinstance(args, dict):
                    raw_path = (
                        args.get("path")
                        or args.get("file_path")
                        or args.get("TargetFile")
                        or args.get("target_file")
                        or ""
                    )
                    if raw_path:
                        await pre_write_callback(str(raw_path))
                return types.HookResult(allow=True)

            agent_hooks.append(_materialize_before_write)

        if interaction_callback is not None:

            @hooks.on_interaction
            async def _on_interaction(spec: Any) -> Any:
                questions_payload = [
                    {
                        "question": getattr(q, "question", ""),
                        "options": [
                            {"id": getattr(o, "id", ""), "text": getattr(o, "text", "")}
                            for o in (getattr(q, "options", None) or [])
                        ],
                        "is_multi_select": bool(getattr(q, "is_multi_select", False)),
                    }
                    for q in (getattr(spec, "questions", None) or [])
                ]
                answers = await interaction_callback(questions_payload)
                responses = [
                    types.QuestionResponse(freeform_response=ans)
                    for ans in answers
                ]
                return types.QuestionHookResult(responses=responses, cancelled=False)

            agent_hooks.append(_on_interaction)

        enabled_builtin_tools.sort(key=lambda bt: bt.value)

        if model_profile.lightweight:
            agent_behavior = types.AgentBehavior.MINIMAL
        elif (
            interaction_callback is not None
            and types.BuiltinTools.ASK_QUESTION in enabled_builtin_tools
        ):
            agent_behavior = types.AgentBehavior.INTERACTIVE
        else:
            agent_behavior = types.AgentBehavior.AUTONOMOUS

        cap_kwargs: dict[str, Any] = {
            "enabled_tools": enabled_builtin_tools,
            "run_command_config": types.RunCommandConfig(enable_sandbox=True),
            "agent_behavior": agent_behavior,
            "enable_subagents": bool(subagent_configs),
        }
        if subagent_configs:
            cap_kwargs["allowed_subagents"] = [s.name for s in subagent_configs]
        capabilities = types.CapabilitiesConfig(**cap_kwargs)

        common_kwargs: dict[str, Any] = {
            "tools": filtered_tools,
            "workspaces": [worktree_path],
            "skills_paths": skills_paths,
            "policies": policies,
            "hooks": agent_hooks,
            "budget_config": budget_cfg,
            "capabilities": capabilities,
        }
        if system_instructions is not None:
            common_kwargs["system_instructions"] = system_instructions
        if (
            role_cfg.compaction_token_threshold
            and role_cfg.compaction_token_threshold > 0
            and hasattr(types, "CompactionConfig")
        ):
            common_kwargs["compaction_config"] = types.CompactionConfig(
                token_threshold=role_cfg.compaction_token_threshold
            )
        if (
            conversation_id
            and len(conversation_id) >= 32
            and _CONVERSATION_ID_RE.match(conversation_id)
        ):
            self.config.workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
            common_kwargs["conversation_id"] = conversation_id
            common_kwargs["session_continuation_mode"] = (
                types.SessionContinuationMode.CREATE_OR_RESUME
            )
            common_kwargs["save_dir"] = str(self.config.workspace.sessions_dir)

        if model_profile.provider == "litert":
            model_path = os.path.expanduser(model_profile.model_path or "")
            return LiteRTAgentConfig(model_path=model_path, **common_kwargs)

        if subagent_configs:
            common_kwargs["subagents"] = subagent_configs

        if model_profile.provider == "openai_compat":
            cfg = LocalOpenAIAgentConfig(
                model=model_profile.model,
                base_url=model_profile.base_url or "http://localhost:8000/v1",
                **common_kwargs,
            )
            return cfg.lightweight() if model_profile.lightweight else cfg

        local_kwargs: dict[str, Any] = {
            "model": model_profile.model,
            **common_kwargs,
        }
        if model_profile.provider == "vertex" or model_profile.vertex:
            local_kwargs["vertex"] = True
            if model_profile.project:
                local_kwargs["project"] = model_profile.project
            if model_profile.location:
                local_kwargs["location"] = model_profile.location
        key_env = model_profile.api_key_env or (
            None if local_kwargs.get("vertex") else "GEMINI_API_KEY"
        )
        if key_env and os.environ.get(key_env):
            local_kwargs["api_key"] = os.environ[key_env]
        cfg = LocalAgentConfig(**local_kwargs)
        if (
            effective_thinking_level
            and hasattr(types, "ThinkingLevel")
            and hasattr(types, "GeminiModelOptions")
        ):
            try:
                thinking_enum = types.ThinkingLevel(
                    effective_thinking_level.strip().lower()
                )
            except ValueError:
                thinking_enum = None
            if thinking_enum is not None:
                for target in getattr(cfg, "models", None) or ():
                    if (
                        hasattr(types, "ModelType")
                        and types.ModelType.TEXT in getattr(target, "types", ())
                        and getattr(target, "endpoint", None) is not None
                        and hasattr(target.endpoint, "options")
                    ):
                        if target.endpoint.options is None:
                            target.endpoint.options = types.GeminiModelOptions(
                                thinking_level=thinking_enum
                            )
                        else:
                            target.endpoint.options.thinking_level = thinking_enum
        return cfg

    async def run_stage_turn(
        self,
        *,
        task: Task,
        work_unit: WorkUnit | None,
        role: AgentRole,
        prompt: str,
        tools: Sequence[Callable[..., Any]],
        worktree_path: str,
        read_only: bool = False,
        stop_gate: StopGateCallback | None = None,
        budget_scope: BudgetScope = BudgetScope.LIFETIME,
        model_override: str | None = None,
        thinking_level_override: str | None = None,
        conversation_id: str | None = None,
        pre_write_callback: PreWriteCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> StageTurnResult:
        if task.id in self._cancelled_tasks:
            return StageTurnResult(
                response_text="Task cancelled.",
                usage=TokenUsage(),
                stop_reason="CANCELLED",
            )

        role_cfg = self.config.agents[role.value]
        model_profile = self.config.models[model_override or role_cfg.model]
        sdk_cfg = self.build_sdk_agent_config(
            role=role,
            tools=tools,
            worktree_path=worktree_path,
            read_only=read_only,
            stop_gate=stop_gate,
            budget_scope=budget_scope,
            model_override=model_override,
            thinking_level_override=thinking_level_override,
            conversation_id=conversation_id,
            pre_write_callback=pre_write_callback,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )

        if self.turn_runner is not None:
            result = await self.turn_runner(
                {
                    "task": task,
                    "work_unit": work_unit,
                    "role": role,
                    "prompt": prompt,
                    "tools": tools,
                    "worktree_path": worktree_path,
                    "read_only": read_only,
                    "stop_gate": stop_gate,
                    "budget_scope": budget_scope,
                    "model_override": model_override,
                    "thinking_level_override": thinking_level_override,
                    "conversation_id": conversation_id,
                    "sdk_config": sdk_cfg,
                }
            )
            if not result.conversation_id:
                result.conversation_id = conversation_id
            return result

        for target in getattr(sdk_cfg, "models", None) or ():
            endpoint = getattr(target, "endpoint", None)
            if endpoint is not None and hasattr(endpoint, "validate_endpoint"):
                endpoint.validate_endpoint()

        from google.antigravity import Agent  # type: ignore[import-not-found]

        async with Agent(config=sdk_cfg) as agent:
            response = await agent.chat(prompt)
            text = await stream_chat_response(
                response,
                event_callback=event_callback,
                role=role.value,
                worktree_path=worktree_path,
            )
            usage_meta = getattr(agent.conversation, "total_usage", None)
            usage = TokenUsage(
                prompt_tokens=int(getattr(usage_meta, "prompt_token_count", 0) or 0),
                cached_tokens=int(
                    getattr(usage_meta, "cached_content_token_count", 0) or 0
                ),
                candidate_tokens=int(
                    getattr(usage_meta, "candidates_token_count", 0) or 0
                ),
                thoughts_tokens=int(
                    getattr(usage_meta, "thoughts_token_count", 0) or 0
                ),
                total_tokens=int(getattr(usage_meta, "total_token_count", 0) or 0),
            )
            stop_reason = str(getattr(response, "stop_reason", "UNSPECIFIED"))
            resolved_conv_id = (
                getattr(agent, "conversation_id", None)
                or conversation_id
            )
            return StageTurnResult(
                response_text=text,
                usage=usage,
                stop_reason=stop_reason,
                model_alias=model_profile.alias,
                model_id=model_profile.model,
                conversation_id=resolved_conv_id,
            )

    async def cancel_task(self, task_id: str) -> None:
        self._cancelled_tasks.add(task_id)

