"""Pluggable Stage-Gate WorkUnit Pipeline Engine."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.application.cost_service import CostController
from app.application.knowledge_service import KnowledgeService
from app.application.ports import (
    AgentHarnessPort,
    EventCallback,
    InteractionCallback,
    JustToolProviderPort,
    StoragePort,
    WorktreePort,
)
from app.bootstrap.config import WintermuteConfig
from app.domain.models import (
    AgentRole,
    PipelineTemplate,
    StageKind,
    StageSpec,
    SubagentRun,
    Task,
    TaskEvent,
    TaskStatus,
    WorkUnit,
    new_id,
    utcnow_iso,
)


@dataclass(slots=True)
class PipelineRunResult:
    completed: bool
    work_unit: WorkUnit
    escalated_to_tech_lead: bool = False
    escalation_reason: str = ""
    stage_history: list[str] = field(default_factory=list)


_GATE_ERR_RE = re.compile(
    r"\b(?:error|fail(?:ed|ure)?|missing|invalid|denied|exception|traceback|not found|exit)\b",
    re.IGNORECASE,
)


def _summarize_gate_failure(gate_report: str, max_chars: int = 200) -> str:
    """Extract up to 2 concise error lines from a failed gate report."""
    lines = [
        " ".join(ln.split())
        for ln in gate_report.splitlines()
        if ln.strip() and not ln.lstrip().startswith("$ just ")
    ]
    if not lines:
        return ""
    matched = [ln for ln in lines if _GATE_ERR_RE.search(ln)]
    picked = (matched or lines)[-2:]
    joined = " | ".join(picked)
    if len(joined) <= max_chars:
        return joined
    return f"{joined[: max_chars - 1].rstrip()}…"


@dataclass(slots=True)
class WorkUnitPipelineEngine:
    config: WintermuteConfig
    storage: StoragePort
    harness: AgentHarnessPort
    worktree: WorktreePort
    just_tools: JustToolProviderPort
    cost_controller: CostController
    knowledge: KnowledgeService | None = None

    def resolve_template(self, work_unit: WorkUnit) -> PipelineTemplate:
        return self.config.resolve_template_for_domain(
            work_unit.domain, override=work_unit.pipeline_template or None
        )

    def resolve_role(self, stage: StageSpec, work_unit: WorkUnit) -> AgentRole:
        raw_role = stage.role or "coder"
        if raw_role == "coder":
            if (
                work_unit.domain == "docs"
                and AgentRole.DOC_WRITER.value in self.config.agents
            ):
                return AgentRole.DOC_WRITER
            return (
                AgentRole.SENIOR_CODER
                if work_unit.complexity == "complex"
                else AgentRole.JUNIOR_CODER
            )
        return AgentRole(raw_role)

    def resolve_validators(self, stage: StageSpec, work_unit: WorkUnit) -> list[str]:
        resolved: list[str] = []
        for v in stage.validators:
            if v == "@domain_test":
                targets = self.config.pipeline.domain_test_targets.get(
                    work_unit.domain, ()
                )
                resolved.extend(targets)
            else:
                resolved.append(v)
        return resolved

    async def _emit(
        self,
        task_id: str,
        event_type: str,
        content: str,
        metadata: dict | None = None,
        *,
        event_callback: EventCallback | None = None,
    ) -> None:
        payload = metadata or {}
        await self.storage.append_event(
            TaskEvent(
                id=new_id("evt"),
                task_id=task_id,
                source="pipeline_engine",
                event_type=event_type,
                content=content,
                metadata=payload,
            )
        )
        if event_callback is not None:
            event_callback(event_type, {"content": content, **payload})

    async def _build_stage_prompt(
        self,
        *,
        task: Task,
        work_unit: WorkUnit,
        stage: StageSpec,
        last_failure_output: str,
    ) -> str:
        sections: list[str] = [
            f"Task: {task.title}",
            f"Work Unit: {work_unit.title} (domain={work_unit.domain}, target={work_unit.target or 'N/A'})",
            f"Pipeline Stage: {stage.name}",
        ]
        if stage.instruction_hint:
            sections.append(f"Stage Instructions: {stage.instruction_hint}")
        if work_unit.skills:
            active_skills = self.config.resolve_skills_for_stage(
                explicit_skills=work_unit.skills,
            )
            rendered = "\n\n".join(
                f"### Skill: {s.name}\n{s.instructions}"
                for s in active_skills
                if s.instructions
            )
            if rendered:
                sections.append(f"Active Skill Instructions:\n{rendered}")
        base_prompt = work_unit.prompt or task.prompt
        if base_prompt:
            sections.append(f"Objective:\n{base_prompt}")

        if self.knowledge is not None and not stage.read_only:
            primed = await self.knowledge.prime_context(
                domain=work_unit.domain,
                target=work_unit.target,
                query=f"{work_unit.target or ''} {last_failure_output or base_prompt}",
            )
            if primed:
                sections.append(primed)

        if stage.pass_git_diff:
            sub_repos = (
                [work_unit.domain]
                if work_unit.domain in self.config.workspace.sub_repos
                else None
            )
            diff_text = await self.worktree.get_git_diff(task.id, sub_repos=sub_repos)
            sections.append(
                f"Git Diff to Review:\n```diff\n{diff_text or '(no diff)'}\n```"
            )

        if last_failure_output and (stage.attach_build_logs or stage.kind == StageKind.AGENT):
            sections.append(f"Previous Validation/Gate Failure Output:\n{last_failure_output}")

        if stage.attach_build_logs and work_unit.domain == "packages" and work_unit.target:
            _, logs_output = await self.just_tools.run_validator(
                "pkg::logs",
                task_id=task.id,
                domain=work_unit.domain,
                target=work_unit.target,
            )
            cleaned_logs = logs_output.strip()
            if cleaned_logs and "not found in justfiles" not in cleaned_logs:
                sections.append(
                    f"Extracted Package Build Logs ({work_unit.target}):\n{cleaned_logs}"
                )

        return "\n\n".join(sections)

    async def execute_work_unit(
        self,
        task: Task,
        work_unit: WorkUnit,
        *,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> PipelineRunResult:
        try:
            return await self._execute_work_unit_impl(
                task,
                work_unit,
                model_override=model_override,
                use_worktree=use_worktree,
                event_callback=event_callback,
                interaction_callback=interaction_callback,
            )
        except Exception as exc:
            work_unit.status = "FAILED"
            work_unit.summary = str(exc)[:1000]
            await self.storage.upsert_work_unit(work_unit)
            await self._emit(
                task.id,
                "WORK_UNIT_FAILED",
                f"[{work_unit.id}] Failed: {exc}",
                {"work_unit_id": work_unit.id, "error": str(exc)},
                event_callback=event_callback,
            )
            raise

    async def _execute_work_unit_impl(
        self,
        task: Task,
        work_unit: WorkUnit,
        *,
        model_override: str | None = None,
        use_worktree: bool | None = None,
        event_callback: EventCallback | None = None,
        interaction_callback: InteractionCallback | None = None,
    ) -> PipelineRunResult:
        template = self.resolve_template(work_unit)
        work_unit.pipeline_template = template.name
        work_unit.status = "RUNNING"
        wt_kwargs: dict[str, Any] = {}
        if use_worktree is False:
            wt_kwargs["use_worktree"] = False
        wt_info = await self.worktree.ensure_worktree(task.id, **wt_kwargs)
        if not task.worktree_path:
            task.worktree_path = wt_info.root_path
            task.branch_name = wt_info.branch_name
            await self.storage.upsert_task(task)

        async def _pre_write_materialize(raw_path: str) -> None:
            wt_abs = Path(os.path.abspath(wt_info.root_path))
            ws_abs = Path(os.path.abspath(self.config.workspace.workspace_root))
            raw_p = Path(raw_path)
            cand = Path(os.path.abspath(raw_p if raw_p.is_absolute() else wt_abs / raw_p))
            matched_repo: str | None = None
            for base in (wt_abs, ws_abs):
                try:
                    rel = cand.relative_to(base)
                    if rel.parts and rel.parts[0] in self.config.workspace.sub_repos:
                        matched_repo = rel.parts[0]
                        break
                except ValueError:
                    continue
            if matched_repo:
                await self.worktree.ensure_materialized(task.id, [matched_repo])
            elif work_unit.domain in self.config.workspace.sub_repos:
                await self.worktree.ensure_materialized(task.id, [work_unit.domain])

        stage_history: list[str] = []
        last_failure_output = ""
        idx = max(0, min(work_unit.current_stage_idx, len(template.stages) - 1))
        forced_jump_idx: int | None = None
        max_steps = max(25, len(template.stages) * (template.max_retries_per_gate + 3))
        steps = 0

        while idx < len(template.stages):
            steps += 1
            if steps > max_steps:
                return await self._escalate(
                    task,
                    work_unit,
                    stage_history,
                    f"Pipeline exceeded maximum stage transitions ({max_steps}).",
                    event_callback=event_callback,
                )

            stage = template.stages[idx]
            if stage.run_only_on_failure and forced_jump_idx != idx:
                idx += 1
                continue
            forced_jump_idx = None

            work_unit.current_stage_idx = idx
            work_unit.current_stage_name = stage.name
            await self.storage.upsert_work_unit(work_unit)
            stage_history.append(stage.name)

            await self._emit(
                task.id,
                "STAGE_STARTED",
                f"[{work_unit.id}] Entering stage '{stage.name}' ({stage.kind.value})",
                {
                    "work_unit_id": work_unit.id,
                    "stage": stage.name,
                    "kind": stage.kind.value,
                    "role": stage.role or "",
                    "pipeline": template.name,
                },
                event_callback=event_callback,
            )

            if stage.kind == StageKind.AGENT:
                verdict = await self.cost_controller.check_budget(task)
                if not verdict.allowed:
                    if self.config.cost.policy.pause_on_budget_exceeded:
                        task.status = TaskStatus.PAUSED_BUDGET
                        task.updated_at = utcnow_iso()
                        await self.storage.upsert_task(task)
                    return PipelineRunResult(
                        completed=False,
                        work_unit=work_unit,
                        escalated_to_tech_lead=False,
                        escalation_reason=verdict.reason,
                        stage_history=stage_history,
                    )

                role = self.resolve_role(stage, work_unit)
                role_cfg = self.config.agents[role.value]
                model_profile = self.config.models[model_override or role_cfg.model]
                effective_read_only = stage.read_only or role_cfg.read_only

                tools = list(
                    self.just_tools.get_tools_for(
                        role, domain=work_unit.domain, task_id=task.id
                    )
                )
                if self.knowledge is not None:
                    memory_tools = self.knowledge.build_memory_tools(
                        default_domain=work_unit.domain,
                        default_target=work_unit.target or work_unit.title,
                        read_only=effective_read_only,
                    )
                    tools.extend(
                        tool
                        for tool in memory_tools
                        if role_cfg.is_tool_allowed(
                            getattr(tool, "__name__", str(tool))
                        )
                    )

                prompt = await self._build_stage_prompt(
                    task=task,
                    work_unit=work_unit,
                    stage=stage,
                    last_failure_output=last_failure_output,
                )

                before_mtime = await self.worktree.get_latest_mtime(
                    task.id, domain=work_unit.domain
                )

                async def _stop_gate(
                    response_text: str,
                    continuation_count: int,
                    *,
                    req_edits: bool = stage.require_file_edits,
                    stage_name: str = stage.name,
                    baseline_mtime: float = before_mtime,
                ) -> tuple[bool, str]:
                    if not req_edits:
                        return True, ""
                    current_mtime = await self.worktree.get_latest_mtime(
                        task.id, domain=work_unit.domain
                    )
                    if current_mtime > baseline_mtime:
                        return True, ""
                    reason = (
                        f"Stage '{stage_name}' requires creating or editing files "
                        "in the worktree before stopping."
                    )
                    await self._emit(
                        task.id,
                        "STAGE_STOP_GATE_REJECTED",
                        reason,
                        {"work_unit_id": work_unit.id, "stage": stage_name},
                        event_callback=event_callback,
                    )
                    return False, reason

                run_id = new_id("run")
                turn = await self.harness.run_stage_turn(
                    task=task,
                    work_unit=work_unit,
                    role=role,
                    prompt=prompt,
                    tools=tools,
                    worktree_path=wt_info.root_path,
                    read_only=effective_read_only,
                    stop_gate=_stop_gate if stage.require_file_edits else None,
                    model_override=model_override,
                    conversation_id=task.conversation_id,
                    pre_write_callback=(
                        None if effective_read_only else _pre_write_materialize
                    ),
                    interaction_callback=interaction_callback,
                    event_callback=event_callback,
                )
                if turn.conversation_id and turn.conversation_id != task.conversation_id:
                    task.conversation_id = turn.conversation_id
                    await self.storage.upsert_task(task)

                after_mtime = await self.worktree.get_latest_mtime(
                    task.id, domain=work_unit.domain
                )
                files_modified = turn.files_modified or (after_mtime > before_mtime)
                if files_modified:
                    work_unit.last_file_modified_at = max(
                        work_unit.last_file_modified_at, after_mtime or time.time()
                    )
                work_unit.summary = turn.response_text[:1000]

                entry, post_verdict = await self.cost_controller.record_turn_usage(
                    task=task,
                    role=role,
                    model_profile=model_profile,
                    usage=turn.usage,
                    subagent_run_id=run_id,
                )
                await self.storage.upsert_subagent_run(
                    SubagentRun(
                        id=run_id,
                        task_id=task.id,
                        work_unit_id=work_unit.id,
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

                if not post_verdict.allowed:
                    return PipelineRunResult(
                        completed=False,
                        work_unit=work_unit,
                        escalated_to_tech_lead=False,
                        escalation_reason=post_verdict.reason,
                        stage_history=stage_history,
                    )

                if stage.require_file_edits and not files_modified:
                    attempts = work_unit.gate_attempts.get(stage.name, 0) + 1
                    work_unit.gate_attempts[stage.name] = attempts
                    await self.storage.upsert_work_unit(work_unit)
                    if attempts >= template.max_retries_per_gate:
                        return await self._escalate(
                            task,
                            work_unit,
                            stage_history,
                            f"Stage '{stage.name}' required file edits but none were made after {attempts} attempt(s).",
                            event_callback=event_callback,
                        )
                    last_failure_output = (
                        f"Stage '{stage.name}' completed without modifying any files."
                    )
                    continue

                if stage.skip_next_gate_if_unmodified and not files_modified:
                    await self._emit(
                        task.id,
                        "STAGE_SKIPPED",
                        f"[{work_unit.id}] Reviewer made 0 edits; skipping subsequent gate.",
                        {"work_unit_id": work_unit.id, "stage": stage.name},
                        event_callback=event_callback,
                    )
                    if (
                        idx + 1 < len(template.stages)
                        and template.stages[idx + 1].kind == StageKind.GATE
                    ):
                        idx += 2
                        continue

                if stage.on_complete_goto:
                    target_idx = template.stage_index(stage.on_complete_goto)
                    forced_jump_idx = target_idx
                    idx = target_idx
                    continue

                idx += 1
                continue

            if stage.kind == StageKind.GATE:
                validators = self.resolve_validators(stage, work_unit)
                all_passed = True
                combined_outputs: list[str] = []
                for validator in validators:
                    ok, output = await self.just_tools.run_validator(
                        validator,
                        task_id=task.id,
                        domain=work_unit.domain,
                        target=work_unit.target,
                    )
                    combined_outputs.append(f"$ just {validator}\n{output}".strip())
                    if not ok:
                        all_passed = False
                        break

                gate_report = "\n\n".join(combined_outputs)
                if all_passed:
                    work_unit.last_gate_passed_at = time.time()
                    last_failure_output = ""
                    await self.storage.upsert_work_unit(work_unit)
                    await self._emit(
                        task.id,
                        "GATE_PASSED",
                        f"[{work_unit.id}] Gate '{stage.name}' passed ({', '.join(validators)})",
                        {
                            "work_unit_id": work_unit.id,
                            "stage": stage.name,
                            "validators": list(validators),
                        },
                        event_callback=event_callback,
                    )
                    idx += 1
                    continue

                attempts = work_unit.gate_attempts.get(stage.name, 0) + 1
                work_unit.gate_attempts[stage.name] = attempts
                last_failure_output = gate_report
                await self.storage.upsert_work_unit(work_unit)
                await self._emit(
                    task.id,
                    "GATE_FAILED",
                    f"[{work_unit.id}] Gate '{stage.name}' failed (attempt {attempts}/{template.max_retries_per_gate})",
                    {
                        "work_unit_id": work_unit.id,
                        "stage": stage.name,
                        "attempt": attempts,
                        "validators": list(validators),
                        "error_summary": _summarize_gate_failure(gate_report),
                    },
                    event_callback=event_callback,
                )

                if attempts >= template.max_retries_per_gate:
                    return await self._escalate(
                        task,
                        work_unit,
                        stage_history,
                        f"Gate '{stage.name}' failed {attempts} time(s):\n{gate_report}",
                        event_callback=event_callback,
                    )

                if stage.on_fail_goto:
                    target_idx = template.stage_index(stage.on_fail_goto)
                    forced_jump_idx = target_idx
                    idx = target_idx
                continue

            if stage.kind == StageKind.COMMIT:
                if work_unit.last_file_modified_at > work_unit.last_gate_passed_at:
                    return await self._escalate(
                        task,
                        work_unit,
                        stage_history,
                        (
                            f"Refusing to commit '{work_unit.id}': files were modified after "
                            "the last passing validation gate."
                        ),
                        event_callback=event_callback,
                    )
                sub_repos = (
                    [work_unit.domain]
                    if work_unit.domain in self.config.workspace.sub_repos
                    else None
                )
                commit_msg = f"wintermute({work_unit.domain}): {work_unit.title}"
                commit_sha = await self.worktree.commit_changes(
                    task.id, commit_msg, sub_repos=sub_repos
                )
                work_unit.commit_sha = commit_sha
                work_unit.status = "COMPLETED"
                await self.storage.upsert_work_unit(work_unit)
                await self._emit(
                    task.id,
                    "WORK_UNIT_COMMITTED",
                    f"[{work_unit.id}] Committed changes ({commit_sha or 'no-op'})",
                    {"work_unit_id": work_unit.id, "commit_sha": commit_sha},
                    event_callback=event_callback,
                )

                if stage.capture_knowledge and self.knowledge is not None and (
                    last_failure_output or work_unit.summary
                ):
                    await self.knowledge.record_note(
                        domain=work_unit.domain,
                        target=work_unit.target or work_unit.title,
                        issue=last_failure_output[:400] or f"Completed {work_unit.title}",
                        resolution=work_unit.summary[:600] or commit_msg,
                        outcome="success",
                        capture_type="automatic",
                    )

                idx += 1
                continue

        work_unit.status = "COMPLETED"
        await self.storage.upsert_work_unit(work_unit)
        return PipelineRunResult(
            completed=True,
            work_unit=work_unit,
            escalated_to_tech_lead=False,
            stage_history=stage_history,
        )

    async def _escalate(
        self,
        task: Task,
        work_unit: WorkUnit,
        stage_history: list[str],
        reason: str,
        *,
        event_callback: EventCallback | None = None,
    ) -> PipelineRunResult:
        work_unit.status = "ESCALATED"
        await self.storage.upsert_work_unit(work_unit)
        await self._emit(
            task.id,
            "WORK_UNIT_ESCALATED",
            f"[{work_unit.id}] Escalated to tech_lead: {reason}",
            {"work_unit_id": work_unit.id, "reason": reason},
            event_callback=event_callback,
        )
        return PipelineRunResult(
            completed=False,
            work_unit=work_unit,
            escalated_to_tech_lead=True,
            escalation_reason=reason,
            stage_history=stage_history,
        )

