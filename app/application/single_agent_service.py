"""Single-agent request routing, execution, and budget resumption service."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.application.cost_service import CostController
from app.application.pipeline import PipelineRunResult, WorkUnitPipelineEngine
from app.application.ports import (
    AgentHarnessPort,
    BudgetScope,
    EventCallback,
    InteractionCallback,
    StoragePort,
)
from app.bootstrap.config import WintermuteConfig
from app.domain.cost import TokenUsage
from app.domain.models import (
    AgentRole,
    SessionSummary,
    SubagentRun,
    Task,
    TaskEvent,
    TaskMessage,
    TaskStatus,
    WorkUnit,
    new_conversation_id,
    new_id,
    utcnow_iso,
)

_KNOWN_DOMAINS: tuple[str, ...] = (
    "packages",
    "straylight",
    "installer",
    "fstools",
    "wintermute",
    "docs",
)

_PACKAGE_HINT_RE = re.compile(
    r"\b(?:package|pkg|upgrade|bump|fix|build|cve(?:\s+in)?)\s+([a-z0-9][a-z0-9._+-]{1,40})\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "in",
        "for",
        "on",
        "to",
        "of",
        "and",
        "or",
        "with",
        "from",
        "all",
        "this",
        "that",
        "build",
        "package",
        "packages",
        "group",
        "groups",
        "list",
        "tree",
        "manifest",
        "manifests",
        "updates",
        "version",
        "versions",
        "issue",
        "error",
        "bug",
        "code",
        "repo",
        "repos",
    }
)


@dataclass(slots=True)
class RequestRoute:
    """Routing decision for a user prompt."""

    is_fast_path: bool
    domain: str
    target: str | None
    pipeline_template: str
    complexity: str
    skills: list[str] = field(default_factory=list)
    sub_domains: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SingleAgentRunResult:
    """Aggregated outcome of executing a prompt in single-agent mode."""

    task: Task
    work_units: list[WorkUnit]
    route: RequestRoute
    completed: bool
    summary: str
    total_usd: float
    total_tokens: int
    stage_history: list[str] = field(default_factory=list)


_READ_ONLY_QUERY_PREFIXES = (
    "check what ",
    "check which ",
    "check if ",
    "check whether ",
    "what ",
    "which ",
    "list ",
    "show ",
    "inspect ",
    "explain ",
    "how ",
    "summarize ",
)
_MUTATION_VERBS_RE = re.compile(
    r"\b(?:fix|patch|edit|create|add|remove|delete|write|refactor|implement|bump|upgrade|update|build|cve)\b"
)


_FAST_PATH_PREFIX_RE = re.compile(r"^\s*fast[-_]path\s*:\s*", re.IGNORECASE)


def _strip_fast_path_prefix(prompt: str) -> tuple[str, bool]:
    """Extract opt-in `fast_path:` prompt prefix if present."""
    match = _FAST_PATH_PREFIX_RE.match(prompt)
    if not match:
        return prompt.strip(), False
    stripped = prompt[match.end() :].strip()
    return stripped or prompt.strip(), True


def _is_read_only_query(lowered: str) -> bool:
    """Detect clearly read-only inspection queries so they skip file-edit stop gates."""
    stripped = lowered.strip()
    if not any(stripped.startswith(prefix) for prefix in _READ_ONLY_QUERY_PREFIXES):
        return False
    return not bool(_MUTATION_VERBS_RE.search(stripped))


def _infer_skills_from_prompt(lowered: str) -> list[str]:
    inferred: list[str] = []
    if any(tok in lowered for tok in ("cve", "advisory", "vulnerability", "security")):
        inferred.append("cve-remediation")
    if any(tok in lowered for tok in ("upgrade", "bump", "outdated", "latest version")):
        inferred.append("packages-upgrade")
    if any(tok in lowered for tok in ("housekeeping", "dead code", "cleanup", "tidy")):
        inferred.append("housekeeping")
    if "audit" in lowered and "cve" not in lowered:
        inferred.append("packages-audit")
    return inferred


def _detect_domains(lowered: str) -> list[str]:
    found: list[str] = []
    for dom in _KNOWN_DOMAINS:
        if re.search(rf"\b{re.escape(dom)}\b", lowered):
            found.append(dom)
    if "packages" not in found and any(
        tok in lowered
        for tok in ("package.manifest", "package.justfile", "pkg::", "cve", "upgrade ")
    ):
        found.append("packages")
    if "docs" not in found and any(
        tok in lowered for tok in ("readme", "documentation", "guide", "docs/", "flip")
    ):
        found.append("docs")
    return found


def _infer_target(prompt: str, domain: str) -> str | None:
    if domain not in {"packages", "straylight", "installer", "fstools", "wintermute", "docs"}:
        return None
    match = _PACKAGE_HINT_RE.search(prompt)
    if match:
        candidate = match.group(1).strip(".,:;\"'`")
        if candidate.lower() not in _STOPWORDS and candidate.lower() not in _KNOWN_DOMAINS:
            return candidate
    return None


def _format_execution_error(exc: Exception) -> str:
    msg = str(exc).strip() or type(exc).__name__
    lowered = msg.lower()
    if any(
        tok in lowered
        for tok in (
            "gemini_api_key",
            "api key",
            "api_key_invalid",
            "unauthenticated",
            "permission_denied",
            "401",
            "403",
        )
    ):
        return (
            f"{msg}\n\n"
            "Hint: Set a valid GEMINI_API_KEY in `.env` (see `.env-example`) or export it in your shell, "
            "or pass `--model local_coder` to use a local model."
        )
    return f"{type(exc).__name__}: {msg}"


class SingleAgentService:
    """Routes prompts via Fast-Path or Swarm-Path and drives stage-gate execution."""

    def __init__(
        self,
        *,
        config: WintermuteConfig,
        storage: StoragePort,
        cost_controller: CostController,
        harness: AgentHarnessPort,
        just_tools: Any,
        pipeline_engine: WorkUnitPipelineEngine,
    ) -> None:
        self._config = config
        self._storage = storage
        self._cost_controller = cost_controller
        self._harness = harness
        self._just_tools = just_tools
        self._pipeline_engine = pipeline_engine

    def classify_request(
        self,
        prompt: str,
        *,
        domain: str | None = None,
        target: str | None = None,
        pipeline_template: str | None = None,
        complexity: str | None = None,
        skills: Sequence[str] | None = None,
        fast_path: bool = False,
    ) -> RequestRoute:
        """Infer domain, skills, pipeline template, complexity, and opt-in Fast-Path vs Tech Lead."""
        clean_prompt, has_fast_path_prefix = _strip_fast_path_prefix(prompt)
        lowered = clean_prompt.lower()
        resolved_skills: list[str] = [s.strip() for s in (skills or ()) if s.strip()]
        if not resolved_skills:
            resolved_skills = _infer_skills_from_prompt(lowered)

        detected_domains = _detect_domains(lowered)
        if domain and domain not in detected_domains:
            detected_domains.insert(0, domain)

        has_pkg_skill = any(
            s.startswith("packages-") or s == "cve-remediation"
            for s in resolved_skills
        )
        inferred_domain = "packages" if has_pkg_skill else None
        resolved_domain = (
            domain
            or inferred_domain
            or (detected_domains[0] if detected_domains else "wintermute")
        )
        sub_domains = detected_domains or [resolved_domain]

        inferred_template: str | None = pipeline_template
        if not inferred_template and (
            (("audit" in lowered or "packages-audit" in resolved_skills) and "cve" not in lowered)
            or _is_read_only_query(lowered)
        ):
            inferred_template = "investigation_audit"
        resolved_tpl = self._config.resolve_template_for_domain(
            resolved_domain, override=inferred_template
        )

        if complexity in {"simple", "complex"}:
            resolved_complexity = complexity
        elif len(sub_domains) > 1 or any(
            tok in lowered for tok in ("refactor", "architect", "rewrite", "multi-repo", "swarm")
        ):
            resolved_complexity = "complex"
        else:
            resolved_complexity = "simple"

        resolved_target = target or _infer_target(clean_prompt, resolved_domain)
        is_fast_path = bool(fast_path or has_fast_path_prefix)

        return RequestRoute(
            is_fast_path=is_fast_path,
            domain=resolved_domain,
            target=resolved_target,
            pipeline_template=resolved_tpl.name,
            complexity=resolved_complexity,
            skills=resolved_skills,
            sub_domains=sub_domains,
        )

    async def execute_prompt(
        self,
        prompt: str,
        *,
        task_id: str | None = None,
        domain: str | None = None,
        target: str | None = None,
        pipeline_template: str | None = None,
        complexity: str | None = None,
        skills: Sequence[str] | None = None,
        fast_path: bool = False,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> SingleAgentRunResult:
        """Execute a new turn within a session Task or resume an existing task."""
        if model_override and model_override not in self._config.models:
            raise KeyError(
                f"Unknown model alias '{model_override}'. Available: {', '.join(self._config.models)}"
            )

        effective_prompt, _ = _strip_fast_path_prefix(prompt)
        route = self.classify_request(
            prompt,
            domain=domain,
            target=target,
            pipeline_template=pipeline_template,
            complexity=complexity,
            skills=skills,
            fast_path=fast_path,
        )

        is_follow_up = False
        if task_id:
            task = await self._storage.get_task(task_id)
            if task is None:
                raise KeyError(f"Task '{task_id}' not found")
            is_follow_up = True
            if effective_prompt and effective_prompt != task.prompt:
                task.prompt = f"{task.prompt}\n\nFollow-up: {effective_prompt}"
            if (
                not task.conversation_id
                or len(task.conversation_id) < 32
                or "_" in task.conversation_id
            ):
                task.conversation_id = new_conversation_id()
            task.status = TaskStatus.RUNNING
            task.error_message = None
            task.updated_at = utcnow_iso()
            await self._storage.upsert_task(task)
        else:
            task = Task(
                id=new_id("task"),
                title=effective_prompt.splitlines()[0][:100] or "Wintermute Session",
                prompt=effective_prompt,
                status=TaskStatus.RUNNING,
                mode="single",
                domain=route.domain,
                target=route.target,
                pipeline_template=route.pipeline_template,
                conversation_id=new_conversation_id(),
                touched_repos=(
                    [route.domain] if route.domain in self._config.workspace.sub_repos else []
                ),
            )
            await self._storage.upsert_task(task)

        if effective_prompt.strip():
            await self._storage.append_task_message(
                TaskMessage(
                    id=new_id("msg"),
                    task_id=task.id,
                    sender="user",
                    content=effective_prompt,
                )
            )

        wrapped_interaction = self._wrap_interaction(
            task,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )

        try:
            if route.is_fast_path:
                work_units, stage_history, completed, summary = await self._execute_fast_path(
                    task,
                    route,
                    event_callback,
                    turn_prompt=effective_prompt,
                    model_override=model_override,
                    use_worktree=use_worktree,
                    interaction_callback=wrapped_interaction,
                )
            else:
                work_units, stage_history, completed, summary = await self._execute_swarm_path(
                    task,
                    route,
                    event_callback,
                    turn_prompt=effective_prompt,
                    is_follow_up=is_follow_up,
                    model_override=model_override,
                    use_worktree=use_worktree,
                    interaction_callback=wrapped_interaction,
                )
        except Exception as exc:
            return await self._handle_execution_failure(
                task, route, exc, event_callback=event_callback
            )

        return await self._build_run_result(
            task,
            route=route,
            work_units=work_units,
            stage_history=stage_history,
            completed=completed,
            summary=summary,
        )

    def _wrap_interaction(
        self,
        task: Task,
        *,
        interaction_callback: InteractionCallback | None,
        event_callback: EventCallback | None,
    ) -> InteractionCallback | None:
        if interaction_callback is None:
            return None

        async def _wrapped(questions: list[dict[str, Any]]) -> list[str]:
            summary_q = "; ".join(
                str(q.get("question", "")).strip() for q in questions if q.get("question")
            )[:500]
            task.status = TaskStatus.WAITING_FOR_INPUT
            task.pending_question = summary_q or None
            task.updated_at = utcnow_iso()
            await self._storage.upsert_task(task)
            await self._storage.append_event(
                TaskEvent(
                    id=new_id("evt"),
                    task_id=task.id,
                    source="single_agent_service",
                    event_type="TASK_WAITING_FOR_INPUT",
                    content=summary_q or "Agent requested user input.",
                    metadata={"questions": questions},
                )
            )
            if event_callback is not None:
                event_callback(
                    "TASK_WAITING_FOR_INPUT",
                    {"content": summary_q or "Agent requested user input."},
                )
            try:
                return await interaction_callback(questions)
            finally:
                task.status = TaskStatus.RUNNING
                task.pending_question = None
                task.updated_at = utcnow_iso()
                await self._storage.upsert_task(task)

        return _wrapped

    async def _handle_execution_failure(
        self,
        task: Task,
        route: RequestRoute,
        exc: Exception,
        *,
        event_callback: EventCallback | None,
    ) -> SingleAgentRunResult:
        formatted_error = _format_execution_error(exc)
        task.status = TaskStatus.FAILED
        task.error_message = formatted_error
        task.updated_at = utcnow_iso()
        await self._storage.upsert_task(task)
        await self._storage.append_event(
            TaskEvent(
                id=new_id("evt"),
                task_id=task.id,
                source="single_agent_service",
                event_type="TASK_FAILED",
                content=formatted_error,
                metadata={"error_type": type(exc).__name__},
            )
        )
        if event_callback is not None:
            event_callback("TASK_FAILED", {"content": formatted_error})
        work_units = await self._storage.list_work_units(task.id)
        return await self._build_run_result(
            task,
            route=route,
            work_units=work_units,
            stage_history=[],
            completed=False,
            summary=formatted_error,
        )

    async def _build_run_result(
        self,
        task: Task,
        *,
        route: RequestRoute,
        work_units: list[WorkUnit],
        stage_history: list[str],
        completed: bool,
        summary: str,
        record_message: bool = True,
    ) -> SingleAgentRunResult:
        if record_message and summary.strip():
            await self._storage.append_task_message(
                TaskMessage(
                    id=new_id("msg"),
                    task_id=task.id,
                    sender="assistant",
                    content=summary,
                )
            )
        total_usd = await self._storage.get_task_usd_spent(task.id)
        usage = await self._storage.get_task_token_usage(task.id)
        return SingleAgentRunResult(
            task=task,
            work_units=work_units,
            route=route,
            completed=completed,
            summary=summary,
            total_usd=total_usd,
            total_tokens=usage.total_tokens,
            stage_history=stage_history,
        )

    async def resume_work_unit(
        self,
        work_unit_id: str,
        *,
        from_start: bool = False,
        prompt: str | None = None,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> SingleAgentRunResult:
        """Resume an incomplete WorkUnit from its current stage or retry it from stage 0."""
        if model_override and model_override not in self._config.models:
            raise KeyError(
                f"Unknown model alias '{model_override}'. Available: {', '.join(self._config.models)}"
            )

        work_unit = await self._storage.get_work_unit(work_unit_id)
        if work_unit is None:
            raise KeyError(f"WorkUnit '{work_unit_id}' not found")
        task = await self._storage.get_task(work_unit.task_id)
        if task is None:
            raise KeyError(f"Parent Task '{work_unit.task_id}' for WorkUnit '{work_unit_id}' not found")

        reset_all = from_start or work_unit.status == "COMPLETED"
        if reset_all:
            work_unit.current_stage_idx = 0
            work_unit.current_stage_name = ""
            work_unit.last_file_modified_at = 0.0
            work_unit.last_gate_passed_at = 0.0
            work_unit.commit_sha = None
        work_unit.gate_attempts = {}
        work_unit.status = "PENDING"
        work_unit.summary = ""
        if prompt and prompt.strip() and prompt.strip() not in work_unit.prompt:
            work_unit.prompt = f"{work_unit.prompt}\n\nRetry guidance: {prompt.strip()}"
        await self._storage.upsert_work_unit(work_unit)

        if (
            not task.conversation_id
            or len(task.conversation_id) < 32
            or "_" in task.conversation_id
        ):
            task.conversation_id = new_conversation_id()
        task.status = TaskStatus.RUNNING
        task.error_message = None
        task.updated_at = utcnow_iso()
        await self._storage.upsert_task(task)

        route = RequestRoute(
            is_fast_path=True,
            domain=work_unit.domain,
            target=work_unit.target,
            pipeline_template=work_unit.pipeline_template,
            complexity=work_unit.complexity,
            skills=list(work_unit.skills),
            sub_domains=[work_unit.domain],
        )
        if event_callback is not None:
            event_callback(
                "route",
                {
                    "mode": "work_unit_retry" if reset_all else "work_unit_resume",
                    "domain": work_unit.domain,
                    "pipeline_template": work_unit.pipeline_template,
                    "skills": work_unit.skills,
                },
            )

        wrapped_interaction = self._wrap_interaction(
            task,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )
        try:
            work_units, stage_history, completed, summary = await self._run_work_unit_with_recovery(
                task,
                work_unit,
                model_override=model_override,
                use_worktree=use_worktree,
                event_callback=event_callback,
                interaction_callback=wrapped_interaction,
            )
        except Exception as exc:
            return await self._handle_execution_failure(
                task, route, exc, event_callback=event_callback
            )

        return await self._build_run_result(
            task,
            route=route,
            work_units=work_units,
            stage_history=stage_history,
            completed=completed,
            summary=summary,
        )

    async def _execute_fast_path(
        self,
        task: Task,
        route: RequestRoute,
        event_callback: EventCallback | None,
        *,
        turn_prompt: str | None = None,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> tuple[list[WorkUnit], list[str], bool, str]:
        if event_callback:
            event_callback(
                "route",
                {
                    "mode": "fast_path",
                    "domain": route.domain,
                    "pipeline_template": route.pipeline_template,
                    "skills": route.skills,
                },
            )

        existing = await self._storage.list_work_units(task.id)
        work_unit = next(
            (wu for wu in existing if wu.status not in {"COMPLETED"}),
            None,
        )
        if work_unit is None:
            effective = turn_prompt or task.prompt
            work_unit = WorkUnit(
                id=new_id("wu"),
                task_id=task.id,
                title=effective.splitlines()[0][:100] or task.title,
                domain=route.domain,
                target=route.target,
                pipeline_template=route.pipeline_template,
                complexity=route.complexity,
                skills=list(route.skills),
                prompt=effective,
            )
            await self._storage.upsert_work_unit(work_unit)

        return await self._run_work_unit_with_recovery(
            task,
            work_unit,
            model_override=model_override,
            use_worktree=use_worktree,
            event_callback=event_callback,
            interaction_callback=interaction_callback,
        )

    async def _run_work_unit_with_recovery(
        self,
        task: Task,
        work_unit: WorkUnit,
        *,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> tuple[list[WorkUnit], list[str], bool, str]:
        outcome = await self._pipeline_engine.execute_work_unit(
            task,
            work_unit,
            model_override=model_override,
            use_worktree=use_worktree,
            event_callback=event_callback,
            interaction_callback=interaction_callback,
        )
        stage_history = list(outcome.stage_history)
        work_units = [work_unit]
        final_outcomes = [outcome]

        summary = work_unit.summary or outcome.escalation_reason or "Completed."
        if outcome.escalated_to_tech_lead:
            lead_summary, recovery_units = await self._run_tech_lead_escalation(
                task,
                work_unit,
                outcome.escalation_reason,
                model_override=model_override,
                use_worktree=use_worktree,
                interaction_callback=interaction_callback,
                event_callback=event_callback,
            )
            stage_history.append("tech_lead_escalation")
            if recovery_units:
                work_units.extend(recovery_units)
                final_outcomes, rec_summary = await self._execute_recovery_units(
                    task,
                    recovery_units,
                    stage_history=stage_history,
                    model_override=model_override,
                    use_worktree=use_worktree,
                    event_callback=event_callback,
                    interaction_callback=interaction_callback,
                )
                summary = rec_summary or lead_summary or outcome.escalation_reason
            else:
                summary = lead_summary or outcome.escalation_reason

        all_completed = bool(final_outcomes) and all(o.completed for o in final_outcomes)
        await self._finalize_task_status(task, final_outcomes, summary)
        return work_units, stage_history, all_completed, summary

    async def _execute_recovery_units(
        self,
        task: Task,
        recovery_units: Sequence[WorkUnit],
        *,
        stage_history: list[str],
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> tuple[list[PipelineRunResult], str]:
        """Run replacement WorkUnits dispatched by Tech Lead recovery (single recovery pass)."""
        outcomes: list[PipelineRunResult] = []
        summaries: list[str] = []
        for wu in recovery_units:
            outcome = await self._pipeline_engine.execute_work_unit(
                task,
                wu,
                model_override=model_override,
                use_worktree=use_worktree,
                event_callback=event_callback,
                interaction_callback=interaction_callback,
            )
            outcomes.append(outcome)
            stage_history.extend(f"{wu.domain}:{s}" for s in outcome.stage_history)
            label = f"{wu.domain}/{wu.target}" if wu.target else wu.domain
            if outcome.completed:
                summaries.append(f"[{label}] {wu.summary or 'Completed'}")
            elif task.status == TaskStatus.PAUSED_BUDGET:
                summaries.append(f"[{label}] Paused on budget: {outcome.escalation_reason}")
                break
            else:
                summaries.append(f"[{label}] Escalated: {outcome.escalation_reason}")
        return outcomes, "\n".join(summaries)

    def _make_dispatch_collector(
        self,
        task: Task,
        queued_units: list[WorkUnit],
        *,
        default_complexity: str = "simple",
        default_skills: Sequence[str] = (),
        fast_path_ids: set[str] | None = None,
    ) -> Any:
        async def _on_dispatch(
            *,
            domain: str,
            target: str = "",
            pipeline_template: str = "",
            complexity: str = "simple",
            skills: list[str] | None = None,
            prompt: str = "",
            fast_path: bool = False,
        ) -> str:
            wu = WorkUnit(
                id=new_id("wu"),
                task_id=task.id,
                title=f"{domain}: {target}" if target else f"{domain}: {task.title[:60]}",
                domain=domain,
                target=target or None,
                pipeline_template=pipeline_template
                or self._config.resolve_template_for_domain(domain).name,
                complexity=complexity or default_complexity,
                skills=list(skills or default_skills),
                prompt=prompt or task.prompt,
            )
            await self._storage.upsert_work_unit(wu)
            queued_units.append(wu)
            if fast_path and fast_path_ids is not None:
                fast_path_ids.add(wu.id)
            return f"Dispatched {wu.id} ({domain}/{target or 'general'})."

        return _on_dispatch

    async def _execute_swarm_path(
        self,
        task: Task,
        route: RequestRoute,
        event_callback: EventCallback | None,
        *,
        turn_prompt: str | None = None,
        is_follow_up: bool = False,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> tuple[list[WorkUnit], list[str], bool, str]:
        if event_callback:
            event_callback(
                "route",
                {
                    "mode": "tech_lead",
                    "domains": route.sub_domains,
                    "pipeline_template": route.pipeline_template,
                },
            )

        existing = await self._storage.list_work_units(task.id)
        queued_units: list[WorkUnit] = [
            wu for wu in existing if wu.status not in {"COMPLETED", "FAILED", "ESCALATED"}
        ]
        fast_path_ids: set[str] = set()
        lead_text = ""

        # On a fresh turn (no pending paused WorkUnits waiting to resume), let Tech Lead
        # converse with the user or dispatch WorkUnits / fast-path execution.
        if not queued_units or is_follow_up:
            dispatched_this_turn: list[WorkUnit] = []
            on_dispatch = self._make_dispatch_collector(
                task,
                dispatched_this_turn,
                default_complexity=route.complexity,
                default_skills=route.skills,
                fast_path_ids=fast_path_ids,
            )
            lead_text = await self._run_tech_lead_decomposition(
                task,
                route,
                turn_prompt=turn_prompt,
                is_follow_up=is_follow_up,
                dispatch_callback=on_dispatch,
                model_override=model_override,
                use_worktree=use_worktree,
                interaction_callback=interaction_callback,
                event_callback=event_callback,
            )
            if task.status == TaskStatus.PAUSED_BUDGET:
                await self._finalize_task_status(task, [], lead_text)
                return existing, ["tech_lead_turn"], False, lead_text
            if not dispatched_this_turn and not queued_units:
                # Conversational / planning turn with Tech Lead: no WorkUnit execution requested.
                summary = lead_text.strip() or "Tech Lead turn completed."
                await self._finalize_task_status(task, [], summary)
                return existing, ["tech_lead_turn"], True, summary
            queued_units.extend(dispatched_this_turn)

        outcomes: list[PipelineRunResult] = []
        stage_history: list[str] = ["tech_lead_turn"]
        summaries: list[str] = []

        for wu in queued_units:
            if wu.id in fast_path_ids and event_callback is not None:
                event_callback(
                    "route",
                    {
                        "mode": "fast_path",
                        "domain": wu.domain,
                        "pipeline_template": wu.pipeline_template,
                        "skills": wu.skills,
                    },
                )
            outcome = await self._pipeline_engine.execute_work_unit(
                task,
                wu,
                model_override=model_override,
                use_worktree=use_worktree,
                event_callback=event_callback,
                interaction_callback=interaction_callback,
            )
            stage_history.extend(f"{wu.domain}:{s}" for s in outcome.stage_history)
            if outcome.completed:
                outcomes.append(outcome)
                summaries.append(f"[{wu.domain}] {wu.summary or 'Completed'}")
            elif task.status == TaskStatus.PAUSED_BUDGET:
                outcomes.append(outcome)
                summaries.append(f"[{wu.domain}] Paused on budget: {outcome.escalation_reason}")
                break
            elif outcome.escalated_to_tech_lead:
                lead_note, recovery_units = await self._run_tech_lead_escalation(
                    task,
                    wu,
                    outcome.escalation_reason,
                    model_override=model_override,
                    use_worktree=use_worktree,
                    interaction_callback=interaction_callback,
                    event_callback=event_callback,
                )
                stage_history.append("tech_lead_escalation")
                if recovery_units:
                    rec_outcomes, rec_summary = await self._execute_recovery_units(
                        task,
                        recovery_units,
                        stage_history=stage_history,
                        model_override=model_override,
                        use_worktree=use_worktree,
                        event_callback=event_callback,
                        interaction_callback=interaction_callback,
                    )
                    outcomes.extend(rec_outcomes)
                    summaries.append(rec_summary or lead_note)
                else:
                    outcomes.append(outcome)
                    summaries.append(f"[{wu.domain}] Escalated: {lead_note or outcome.escalation_reason}")
            else:
                outcomes.append(outcome)

        all_completed = bool(outcomes) and all(o.completed for o in outcomes)
        combined_summary = "\n".join(summaries) if summaries else (lead_text or "Session turn finished.")
        await self._finalize_task_status(task, outcomes, combined_summary)
        return await self._storage.list_work_units(task.id), stage_history, all_completed, combined_summary

    async def _invoke_tech_lead_turn(
        self,
        task: Task,
        *,
        stage_name: str,
        domain: str,
        prompt: str,
        work_unit: WorkUnit | None = None,
        dispatch_callback: Any = None,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> str:
        """Shared Tech Lead execution helper for planning decomposition and escalation recovery."""
        role = AgentRole.TECH_LEAD
        role_cfg = self._config.agents[role.value]
        model_profile = self._config.models[model_override or role_cfg.model]
        wt_kwargs: dict[str, Any] = {}
        if use_worktree is False:
            wt_kwargs["use_worktree"] = False
        wt_info = await self._pipeline_engine.worktree.ensure_worktree(task.id, **wt_kwargs)
        if not task.worktree_path:
            task.worktree_path = wt_info.root_path
            task.branch_name = wt_info.branch_name
            await self._storage.upsert_task(task)
        if event_callback is not None:
            event_callback(
                "STAGE_STARTED",
                {"stage": stage_name, "kind": "agent", "role": role.value},
            )
        tools = self._just_tools.get_tools_for(
            role,
            domain=domain,
            task_id=task.id,
            dispatch_callback=dispatch_callback,
        )
        run_id = new_id("run")
        turn = await self._harness.run_stage_turn(
            task=task,
            work_unit=work_unit,
            role=role,
            prompt=prompt,
            tools=tools,
            worktree_path=wt_info.root_path,
            read_only=role_cfg.read_only,
            model_override=model_override,
            conversation_id=task.conversation_id,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )
        if turn.conversation_id and turn.conversation_id != task.conversation_id:
            task.conversation_id = turn.conversation_id
            await self._storage.upsert_task(task)
        entry, _ = await self._cost_controller.record_turn_usage(
            task=task,
            role=role,
            model_profile=model_profile,
            usage=turn.usage,
            subagent_run_id=run_id,
        )
        await self._storage.upsert_subagent_run(
            SubagentRun(
                id=run_id,
                task_id=task.id,
                work_unit_id=work_unit.id if work_unit else None,
                role=role,
                model_alias=turn.model_alias or model_profile.alias,
                model_id=turn.model_id or model_profile.model,
                status="COMPLETED",
                prompt=prompt[:500],
                summary=turn.response_text[:500],
                prompt_tokens=turn.usage.prompt_tokens,
                cached_tokens=turn.usage.cached_tokens,
                candidate_tokens=turn.usage.candidate_tokens,
                thoughts_tokens=turn.usage.thoughts_tokens,
                total_tokens=turn.usage.total_tokens,
                usd_cost=entry.usd_cost,
                completed_at=utcnow_iso(),
            )
        )
        return turn.response_text

    async def _run_tech_lead_decomposition(
        self,
        task: Task,
        route: RequestRoute,
        *,
        turn_prompt: str | None = None,
        is_follow_up: bool = False,
        dispatch_callback: Any = None,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> str:
        verdict = await self._cost_controller.check_budget(task)
        if not verdict.allowed:
            if self._config.cost.policy.pause_on_budget_exceeded:
                task.status = TaskStatus.PAUSED_BUDGET
                await self._storage.upsert_task(task)
            return verdict.reason

        msg = (turn_prompt or task.prompt).strip()
        if is_follow_up:
            prompt = (
                f"User follow-up in this session:\n{msg}\n\n"
                f"Respond directly if this is discussion/clarification, or call "
                f"`dispatch_work_unit(..., fast_path=True|False)` when ready to execute work."
            )
        else:
            prompt = (
                f"Session task ask (detected domains: {', '.join(route.sub_domains)}, "
                f"suggested pipeline: {route.pipeline_template}):\n{msg}\n\n"
                f"You lead this session Task:\n"
                f"- If this is a question, discussion, or needs planning/clarification, inspect with "
                f"read-only tools and reply directly without calling `dispatch_work_unit`.\n"
                f"- If it is ready for single-unit execution, call `dispatch_work_unit(..., fast_path=True)`.\n"
                f"- If it spans multiple components/packages/repos, decompose it into scoped `WorkUnit`s "
                f"(for `packages` edits, each `target` must be a single package under `packages/<pkg>`)."
            )
        return await self._invoke_tech_lead_turn(
            task,
            stage_name="tech_lead",
            domain=route.domain,
            prompt=prompt,
            dispatch_callback=dispatch_callback,
            model_override=model_override,
            use_worktree=use_worktree,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )

    async def _run_tech_lead_escalation(
        self,
        task: Task,
        work_unit: WorkUnit,
        reason: str,
        *,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        interaction_callback: InteractionCallback | None = None,
        event_callback: EventCallback | None = None,
    ) -> tuple[str, list[WorkUnit]]:
        verdict = await self._cost_controller.check_budget(task)
        if not verdict.allowed:
            return reason, []

        recovery_units: list[WorkUnit] = []
        on_dispatch = self._make_dispatch_collector(
            task,
            recovery_units,
            default_complexity=work_unit.complexity,
            default_skills=work_unit.skills,
        )
        prompt = (
            f"WorkUnit '{work_unit.title}' ({work_unit.domain}, target={work_unit.target or 'none'}) "
            f"escalated from stage '{work_unit.current_stage_name}'.\n"
            f"Reason: {reason}\n\n"
            f"Decide the next step:\n"
            f"1. If this task can recover by splitting into smaller/scoped WorkUnits "
            f"(e.g., an `investigation_audit` WorkUnit first, or individual single-package WorkUnits "
            f"under `packages/<pkg>`), inspect repository state and call `dispatch_work_unit` now — "
            f"those WorkUnits will be executed automatically.\n"
            f"2. If user clarification or manual intervention is genuinely required, do not dispatch "
            f"and instead provide a clear diagnosis and concrete recovery plan."
        )
        text = await self._invoke_tech_lead_turn(
            task,
            stage_name="tech_lead_escalation",
            domain=work_unit.domain,
            prompt=prompt,
            work_unit=work_unit,
            dispatch_callback=on_dispatch,
            model_override=model_override,
            use_worktree=use_worktree,
            interaction_callback=interaction_callback,
            event_callback=event_callback,
        )
        return text, recovery_units

    async def _finalize_task_status(
        self,
        task: Task,
        outcomes: Sequence[PipelineRunResult],
        summary: str,
    ) -> None:
        if task.status == TaskStatus.PAUSED_BUDGET:
            task.error_message = summary
        elif not outcomes or all(o.completed for o in outcomes):
            task.status = TaskStatus.COMPLETED
            task.review_summary = summary[:1000]
            task.error_message = None
        else:
            task.status = TaskStatus.FAILED
            task.error_message = summary[:1000]
        task.updated_at = utcnow_iso()
        await self._storage.upsert_task(task)
        await self._storage.append_event(
            TaskEvent(
                id=new_id("evt"),
                task_id=task.id,
                source="single_agent_service",
                event_type=f"TASK_{task.status.value}",
                content=summary[:500],
                metadata={"status": task.status.value},
            )
        )

    async def _unblock_budget_paused_work_units(self, task_id: str) -> None:
        work_units = await self._storage.list_work_units(task_id)
        for wu in work_units:
            if wu.status in {"BLOCKED_BUDGET", "PAUSED_BUDGET"}:
                wu.status = "PENDING"
                await self._storage.upsert_work_unit(wu)

    async def grant_task_budget(
        self, task_id: str, extra_usd: float
    ) -> Task:
        """Grant forward-looking USD budget to a task and unpause blocked work units."""
        task = await self._cost_controller.grant_budget(
            task_id=task_id,
            additional_usd=extra_usd,
            scope=BudgetScope.FORWARD_LOOKING,
        )
        await self._unblock_budget_paused_work_units(task_id)
        return task

    async def update_daily_budget(
        self,
        amount: float,
        *,
        additive: bool = False,
        task_id: str | None = None,
    ) -> tuple[float, Task | None]:
        """Set or increase the daily USD ceiling and unpause a budget-paused task if present."""
        new_limit = await self._cost_controller.set_daily_limit(amount, additive=additive)
        task: Task | None = None
        if task_id:
            task = await self._storage.get_task(task_id)
            if task is not None and task.status == TaskStatus.PAUSED_BUDGET:
                task.status = TaskStatus.RUNNING
                task.updated_at = utcnow_iso()
                await self._storage.upsert_task(task)
                await self._unblock_budget_paused_work_units(task.id)
        return new_limit, task

    async def get_task_cost_breakdown(self, task_id: str) -> tuple[float, TokenUsage, dict[str, float]]:
        """Return (total_usd, token_usage, per_role_usd) for a task."""
        total_usd = await self._storage.get_task_usd_spent(task_id)
        usage = await self._storage.get_task_token_usage(task_id)
        runs = await self._storage.list_subagent_runs(task_id)
        per_role: dict[str, float] = {}
        for run in runs:
            per_role[run.role.value] = per_role.get(run.role.value, 0.0) + run.usd_cost
        return total_usd, usage, per_role

    def has_sdk_session(self, conversation_id: str | None) -> bool:
        """Return True when the Antigravity SDK session DB exists on disk."""
        if not conversation_id:
            return False
        return (self._config.workspace.sessions_dir / f"{conversation_id}.db").is_file()

    async def resolve_session(self, selector: str) -> Task:
        """Resolve a session Task by exact ID, unique prefix, `#N` / 1-based index, or `latest`."""
        cleaned = selector.strip()
        if not cleaned:
            raise KeyError("Session selector cannot be empty.")

        lowered = cleaned.lower()
        tasks = await self._storage.list_tasks()
        if not tasks:
            raise KeyError("No stored sessions found.")

        if lowered in {"latest", "last"}:
            return tasks[0]

        index_token = cleaned[1:] if cleaned.startswith("#") else cleaned
        if index_token.isdigit():
            idx = int(index_token)
            if 1 <= idx <= len(tasks):
                return tasks[idx - 1]
            raise KeyError(f"Session index #{idx} out of range (1..{len(tasks)}).")

        exact = await self._storage.get_task(cleaned)
        if exact is not None:
            return exact

        matches = [
            t
            for t in tasks
            if t.id.startswith(cleaned) or t.id.removeprefix("task_").startswith(cleaned)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(m.id for m in matches[:5])
            raise KeyError(f"Ambiguous session prefix '{cleaned}'. Matches: {ids}")
        raise KeyError(f"Session '{cleaned}' not found.")

    async def list_sessions(
        self, *, status: TaskStatus | None = None, limit: int = 20
    ) -> list[SessionSummary]:
        """List stored sessions ordered most-recently-updated first with aggregate counts."""
        tasks = await self._storage.list_tasks(status=status, limit=limit)
        stats = await self._storage.get_session_stats([t.id for t in tasks])
        summaries: list[SessionSummary] = []
        for task in tasks:
            msg_count, wu_count, total_usd = stats.get(task.id, (0, 0, 0.0))
            summaries.append(
                SessionSummary(
                    task=task,
                    message_count=msg_count,
                    work_unit_count=wu_count,
                    total_usd=total_usd,
                    has_sdk_session=self.has_sdk_session(task.conversation_id),
                )
            )
        return summaries

    async def get_session_overview(
        self, selector: str | None = None, *, message_limit: int = 200
    ) -> tuple[Task, list[WorkUnit], list[TaskMessage], float]:
        """Return (task, work_units, messages, total_usd) for a resolved session."""
        task = await self.resolve_session(selector or "latest")
        wus = await self._storage.list_work_units(task.id)
        msgs = await self._storage.list_task_messages(task.id, limit=message_limit)
        total_usd = await self._storage.get_task_usd_spent(task.id)
        return task, wus, msgs, total_usd

    async def rename_session(self, selector: str, title: str) -> Task:
        """Update a stored session's title and updated_at timestamp."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Session title cannot be empty.")
        task = await self.resolve_session(selector)
        task.title = clean_title[:120]
        task.updated_at = utcnow_iso()
        await self._storage.upsert_task(task)
        return task

    async def delete_session(
        self, selector: str, *, cleanup_worktree: bool = True
    ) -> Task:
        """Delete a stored session, its SQLite records, SDK session files, and optional worktree."""
        task = await self.resolve_session(selector)
        if task.conversation_id:
            sess_dir = self._config.workspace.sessions_dir
            for ext in ("db", "db-wal", "db-shm"):
                cand = sess_dir / f"{task.conversation_id}.{ext}"
                if cand.exists():
                    cand.unlink(missing_ok=True)
        if cleanup_worktree and task.worktree_path:
            try:
                await self._pipeline_engine.worktree.cleanup_worktree(task.id)
            except Exception:
                pass
        await self._storage.delete_task(task.id)
        return task

