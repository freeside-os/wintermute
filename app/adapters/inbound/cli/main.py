"""Wintermute Click CLI & Interactive REPL."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel

from app.adapters.inbound.cli.ui import (
    render_chat_welcome_panel,
    render_cost_panel,
    render_help_panel,
    render_history_panel,
    render_memory_table,
    render_models_table,
    render_pipeline_stepper,
    render_pipelines_table,
    render_session_resume_panel,
    render_sessions_table,
    render_status_panel,
    render_tools_table,
)
from app.application.ports import EventCallback, InteractionCallback
from app.application.single_agent_service import SingleAgentRunResult
from app.bootstrap.container import AppContainer, build_container
from app.domain.cost import TokenUsage
from app.domain.models import AgentRole, Task, TaskStatus, WorkUnit, utcnow_iso


def _console() -> Console:
    # Instantiate per render so Click CliRunner stdout capture works cleanly.
    return Console(file=sys.stdout, width=120)


class ChatHistory:
    """Persists sent chat prompts and wires readline Up/Down recall."""

    def __init__(self, path: Path, max_entries: int = 500) -> None:
        self.path = path
        self.max_entries = max_entries
        self.entries: list[str] = []
        self._readline: Any = None

    def load(self) -> None:
        loaded: list[str] = []
        try:
            if self.path.is_file():
                for raw in self.path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        val = json.loads(raw)
                    except json.JSONDecodeError:
                        val = raw
                    if isinstance(val, str) and val.strip():
                        loaded.append(val.strip())
        except OSError:
            pass
        self.entries = loaded[-self.max_entries :]

        try:
            import readline

            self._readline = readline
            readline.clear_history()
            if hasattr(readline, "set_auto_history"):
                readline.set_auto_history(False)
            readline.set_history_length(self.max_entries)
            for item in self.entries:
                readline.add_history(item)
        except ImportError:
            self._readline = None

    def record(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if self.entries and self.entries[-1] == cleaned:
            return
        self.entries.append(cleaned)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]
        if self._readline is not None:
            self._readline.add_history(cleaned)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = "\n".join(json.dumps(item) for item in self.entries) + "\n"
            self.path.write_text(payload, encoding="utf-8")
        except OSError:
            pass

    def get(self, index: int | None = None) -> str | None:
        if not self.entries:
            return None
        if index is None:
            return self.entries[-1]
        if 1 <= index <= len(self.entries):
            return self.entries[index - 1]
        return None


async def _ensure_container(ctx: click.Context) -> AppContainer:
    obj = ctx.obj
    if isinstance(obj, AppContainer):
        await obj.initialize()
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("container"), AppContainer):
        container: AppContainer = obj["container"]
        await container.initialize()
        return container
    workspace_opt = obj.get("workspace") if isinstance(obj, dict) else None
    config_opt = obj.get("config") if isinstance(obj, dict) else None
    container = build_container(
        workspace_root=Path(workspace_opt) if workspace_opt else None,
        config_path=Path(config_opt) if config_opt else None,
    )
    await container.initialize()
    return container


def _render_run_result(
    console: Console, container: AppContainer, result: SingleAgentRunResult
) -> None:
    tpl = container.config.resolve_template_for_domain(
        result.route.domain, override=result.route.pipeline_template
    )
    stage_names = [s.name for s in tpl.stages]
    failed_stage: str | None = None
    if not result.completed and result.work_units:
        last_wu = result.work_units[-1]
        if last_wu.status in {"ESCALATED", "FAILED"}:
            failed_stage = last_wu.current_stage_name or None
    completed_stages = [
        s for s in result.stage_history if s in stage_names and s != failed_stage
    ]
    ran_pipeline_stages = bool(completed_stages or failed_stage)
    if ran_pipeline_stages:
        console.print(
            render_pipeline_stepper(
                tpl.name,
                stage_names,
                completed_stages=completed_stages,
                failed_stage=failed_stage,
            )
        )
    border = "green" if result.completed else ("yellow" if result.task.status == TaskStatus.PAUSED_BUDGET else "red")
    console.print(
        Panel(
            result.summary or "(no summary)",
            title=f"Session {result.task.id} [{result.task.status.value}]",
            border_style=border,
        )
    )


async def _render_task_cost(
    console: Console, container: AppContainer, task: Task | None
) -> None:
    policy = container.config.cost.policy
    daily_usd_spent = await container.storage.get_daily_usd_spent()
    month_label = utcnow_iso()[:7]
    monthly_usd_spent = await container.storage.get_monthly_usd_spent(month_label)
    if task is None:
        console.print(
            render_cost_panel(
                None,
                total_usd=0.0,
                usage=TokenUsage(),
                per_role_usd={},
                max_task_usd=policy.max_task_usd,
                daily_usd_spent=daily_usd_spent,
                max_daily_usd=policy.max_daily_usd,
                monthly_usd_spent=monthly_usd_spent,
                month_label=month_label,
            )
        )
        return
    total_usd, usage, per_role = await container.single_agent_service.get_task_cost_breakdown(
        task.id
    )
    refreshed = await container.storage.get_task(task.id) or task
    console.print(
        render_cost_panel(
            refreshed,
            total_usd=total_usd,
            usage=usage,
            per_role_usd=per_role,
            max_task_usd=policy.max_task_usd,
            daily_usd_spent=daily_usd_spent,
            max_daily_usd=policy.max_daily_usd,
            monthly_usd_spent=monthly_usd_spent,
            month_label=month_label,
        )
    )


def _run_git_diff(target_dir: Path, sub_repos: Sequence[str] = ()) -> str:
    if not target_dir.exists():
        return f"Directory does not exist: {target_dir}"
    candidates: list[tuple[str, Path]] = []
    if (target_dir / ".git").exists():
        candidates.append(("root", target_dir))
    for repo in sub_repos:
        repo_dir = target_dir / repo
        if repo_dir.exists() and (repo_dir / ".git").exists():
            candidates.append((repo, repo_dir))
    if not candidates:
        candidates.append((target_dir.name, target_dir))

    chunks: list[str] = []
    for label, repo_dir in candidates:
        status_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--short"],
            capture_output=True,
            text=True,
            check=False,
        )
        diff_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--stat"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = "\n".join(
            p.strip() for p in (status_proc.stdout, diff_proc.stdout) if p.strip()
        )
        if out:
            chunks.append(f"=== {label} ===\n{out}")
    return "\n\n".join(chunks) or "Working tree clean (no diff)."


def _make_cli_event_callback(console: Console) -> EventCallback:
    def _on_event(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "route":
            dom = payload.get("domain") or ",".join(payload.get("domains", []))
            console.print(
                f"[dim]→ Route: {payload.get('mode')} | domain={dom} | "
                f"pipeline={payload.get('pipeline_template')}[/dim]"
            )
        elif event_type == "STAGE_STARTED":
            role_part = f"role={payload['role']}, " if payload.get("role") else ""
            console.print(
                f"  ↳ Stage [cyan]{payload.get('stage')}[/cyan] ({role_part}{payload.get('kind')})"
            )
        elif event_type == "AGENT_INTENT":
            summary = payload.get("summary")
            if summary:
                console.print(f"    [dim]💭 {summary}[/dim]")
        elif event_type == "TOOL_CALL_STARTED":
            tool_name = payload.get("tool")
            args = payload.get("args")
            args_part = f"({args})" if args else "()"
            console.print(f"    • [dim]{tool_name}[/dim]{args_part}")
        elif event_type == "TOOL_CALL_FAILED":
            tool_name = payload.get("tool")
            args = payload.get("args")
            err = payload.get("error") or "failed"
            args_part = f"({args})" if args else "()"
            console.print(f"    [red]✗ {tool_name}{args_part}:[/red] {err}")
        elif event_type == "TOOL_CALL_COMPLETED":
            err = payload.get("error")
            args = payload.get("args")
            args_part = f"({args})" if args else ""
            suffix = f" [red](error: {err})[/red]" if err else ""
            console.print(f"    • Tool [dim]{payload.get('tool')}{args_part}[/dim]{suffix}")
        elif event_type == "GATE_PASSED":
            vals = ", ".join(payload.get("validators", [])) or payload.get("stage")
            console.print(f"    [green]✓ Gate passed:[/green] {vals}")
        elif event_type == "GATE_FAILED":
            vals = ", ".join(payload.get("validators", [])) or payload.get("stage")
            console.print(
                f"    [red]✗ Gate failed:[/red] {vals} (attempt {payload.get('attempt')})"
            )
            err_summary = payload.get("error_summary")
            if err_summary:
                console.print(f"      [dim]{err_summary}[/dim]")
        elif event_type == "STAGE_STOP_GATE_REJECTED":
            console.print(f"    [yellow]⚠ Stop gate:[/yellow] {payload.get('content')}")
        elif event_type == "WORK_UNIT_COMMITTED":
            console.print(
                f"  [green]✓ Committed:[/green] {payload.get('commit_sha') or 'no-op'}"
            )
        elif event_type == "WORK_UNIT_ESCALATED":
            console.print(f"  [yellow]↑ Escalated:[/yellow] {payload.get('reason')}")
        elif event_type == "TASK_WAITING_FOR_INPUT":
            console.print(f"  [cyan]? Waiting for input:[/cyan] {payload.get('content')}")
        elif event_type == "TASK_FAILED":
            console.print("  [red]✗ Session failed[/red]")

    return _on_event


def _make_cli_interaction_callback(console: Console) -> InteractionCallback:
    async def _ask_user(questions: list[dict[str, Any]]) -> list[str]:
        answers: list[str] = []
        for q in questions:
            q_text = str(q.get("question") or "Agent requests input:")
            options = list(q.get("options") or [])
            console.print(Panel(q_text, title="Agent Question", border_style="cyan"))
            for idx, opt in enumerate(options, start=1):
                label = opt.get("text") or opt.get("id") or ""
                console.print(f"  {idx}. {label}")
            raw_ans = str(
                click.prompt("Answer", default="", show_default=False)
            ).strip()
            if raw_ans.isdigit() and options:
                choice_idx = int(raw_ans) - 1
                if 0 <= choice_idx < len(options):
                    raw_ans = str(options[choice_idx].get("text") or options[choice_idx].get("id") or raw_ans)
            answers.append(raw_ans)
        return answers

    return _ask_user


async def _maybe_interactive_budget_top_up(
    *,
    console: Console,
    container: AppContainer,
    result: SingleAgentRunResult,
    domain: str | None,
    target: str | None,
    pipeline: str | None,
    skills: tuple[str, ...],
    model_override: str | None,
    worktree: bool | None,
    event_cb: EventCallback,
    interaction_cb: InteractionCallback,
    complexity: str | None = None,
    fast_path: bool = False,
) -> SingleAgentRunResult:
    if result.task.status != TaskStatus.PAUSED_BUDGET or not sys.stdin.isatty():
        return result
    if not click.confirm(
        f"Session {result.task.id} paused on USD budget limit. Grant forward-looking top-up and resume?",
        default=True,
    ):
        return result
    extra_usd = float(click.prompt("Additional USD budget", type=float, default=5.0))
    if extra_usd <= 0:
        return result
    await container.single_agent_service.grant_task_budget(result.task.id, extra_usd)
    console.print(f"[cyan]Granted +${extra_usd:.2f} USD. Resuming session {result.task.id}...[/cyan]")
    return await container.single_agent_service.execute_prompt(
        result.task.prompt,
        task_id=result.task.id,
        domain=domain or result.task.domain,
        target=target or result.task.target,
        pipeline_template=pipeline or result.task.pipeline_template,
        complexity=complexity,
        skills=skills,
        fast_path=fast_path,
        model_override=model_override,
        use_worktree=worktree,
        event_callback=event_cb,
        interaction_callback=interaction_cb,
    )


@click.group()
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Workspace root directory override.",
)
@click.option(
    "--config",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    help="Path to wintermute.toml override.",
)
@click.pass_context
def cli(ctx: click.Context, workspace: str | None, config: str | None) -> None:
    """Wintermute Autonomous Freeside OS Developer CLI."""
    if ctx.obj is None:
        ctx.obj = {"workspace": workspace, "config": config}


async def _load_and_render_session(
    console: Console,
    container: AppContainer,
    selector: str,
    *,
    message_limit: int = 8,
    max_work_units: int | None = 5,
) -> Task:
    task, work_units, messages, _ = (
        await container.single_agent_service.get_session_overview(selector, message_limit=200)
    )
    (
        total_usd,
        usage,
        per_role,
        per_wu_costs,
        orch_cost,
    ) = await container.single_agent_service.get_task_detailed_cost_breakdown(task.id)
    console.print(
        render_session_resume_panel(
            task,
            work_units,
            messages,
            total_usd=total_usd,
            usage=usage,
            max_task_usd=container.config.cost.policy.max_task_usd,
            per_role_usd=per_role,
            work_unit_costs=per_wu_costs,
            orchestration_cost=orch_cost,
            max_messages=message_limit,
            max_work_units=max_work_units,
        )
    )
    return task


async def _print_sessions_list(
    console: Console,
    container: AppContainer,
    *,
    status: TaskStatus | None = None,
    limit: int = 20,
    active_task_id: str | None = None,
) -> None:
    sessions = await container.single_agent_service.list_sessions(status=status, limit=limit)
    if not sessions:
        console.print("[dim]No stored sessions found.[/dim]")
        return
    console.print(render_sessions_table(sessions, active_task_id=active_task_id))


@cli.command("chat")
@click.argument("prompt", required=False)
@click.option("--pipeline", default=None, help="Explicit pipeline template override.")
@click.option("--domain", default=None, help="Explicit domain override.")
@click.option("--target", default=None, help="Explicit target (package or component).")
@click.option("--skill", "skills", multiple=True, help="Explicit skill name(s) to activate.")
@click.option(
    "--resume",
    "resume_task_id",
    default=None,
    help="Resume a stored session by ID, prefix, #index, or 'latest'.",
)
@click.option(
    "-c",
    "--continue",
    "continue_latest",
    is_flag=True,
    default=False,
    help="Continue the most recently updated stored session.",
)
@click.option(
    "--worktree/--no-worktree",
    default=None,
    help="Enable or disable worktree/Btrfs CoW isolation.",
)
@click.option("--model", "model_override", default=None, help="Temporary model alias override.")
@click.option(
    "--fast-path",
    "fast_path",
    is_flag=True,
    default=False,
    help="Opt in to fast-path execution (skip initial Tech Lead turn).",
)
@click.pass_context
def chat_cmd(
    ctx: click.Context,
    prompt: str | None,
    pipeline: str | None,
    domain: str | None,
    target: str | None,
    skills: tuple[str, ...],
    resume_task_id: str | None,
    continue_latest: bool,
    worktree: bool | None,
    model_override: str | None,
    fast_path: bool,
) -> None:
    """Start an interactive Wintermute REPL session."""
    if continue_latest and not resume_task_id:
        resume_task_id = "latest"

    with asyncio.Runner() as runner:
        container = runner.run(_ensure_container(ctx))
        console = _console()
        active_task: Task | None = None
        last_result: SingleAgentRunResult | None = None
        fast_path_mode = fast_path

        if resume_task_id:
            try:
                active_task = runner.run(
                    _load_and_render_session(console, container, resume_task_id)
                )
            except KeyError as exc:
                raise click.ClickException(str(exc)) from exc

        event_cb = _make_cli_event_callback(console)
        interaction_cb = _make_cli_interaction_callback(console)
        db_dir = container.config.workspace.db_path.parent
        history = ChatHistory(db_dir / "chat_history.jsonl")
        history.load()

        async def _execute(text: str) -> None:
            nonlocal active_task, last_result
            history.record(text)
            result = await container.single_agent_service.execute_prompt(
                text,
                task_id=active_task.id if active_task else None,
                domain=domain,
                target=target,
                pipeline_template=pipeline,
                skills=skills,
                fast_path=fast_path_mode,
                model_override=model_override,
                use_worktree=worktree,
                event_callback=event_cb,
                interaction_callback=interaction_cb,
            )
            result = await _maybe_interactive_budget_top_up(
                console=console,
                container=container,
                result=result,
                domain=domain,
                target=target,
                pipeline=pipeline,
                skills=skills,
                model_override=model_override,
                worktree=worktree,
                event_cb=event_cb,
                interaction_cb=interaction_cb,
                fast_path=fast_path_mode,
            )
            active_task = result.task
            last_result = result
            _render_run_result(console, container, result)
            await _render_task_cost(console, container, active_task)

        async def _run_wu(cmd: str, args: list[str]) -> None:
            nonlocal active_task, last_result
            from_start = cmd == "/wu-retry"
            wu_id: str | None = None
            hint_args = list(args)
            if hint_args:
                cand = await container.storage.get_work_unit(hint_args[0])
                if cand is not None or hint_args[0].startswith("wu_"):
                    wu_id = hint_args.pop(0)
            if wu_id is None:
                if active_task is None:
                    console.print(
                        f"[yellow]No active session. Pass an explicit WorkUnit ID: {cmd} <wu_id>[/yellow]"
                    )
                    return
                units = await container.storage.list_work_units(active_task.id)
                if not units:
                    console.print("[yellow]No WorkUnits in current session yet.[/yellow]")
                    return
                if not from_start:
                    incomplete = [u for u in units if u.status != "COMPLETED"]
                    wu_id = (incomplete[-1] if incomplete else units[-1]).id
                else:
                    wu_id = units[-1].id
            hint_text = " ".join(hint_args).strip() or None
            action_word = "Retrying from stage 0" if from_start else "Resuming"
            console.print(f"[cyan]{action_word} WorkUnit {wu_id}...[/cyan]")
            try:
                result = await container.single_agent_service.resume_work_unit(
                    wu_id,
                    from_start=from_start,
                    prompt=hint_text,
                    model_override=model_override,
                    use_worktree=worktree,
                    event_callback=event_cb,
                    interaction_callback=interaction_cb,
                )
                result = await _maybe_interactive_budget_top_up(
                    console=console,
                    container=container,
                    result=result,
                    domain=domain,
                    target=target,
                    pipeline=pipeline,
                    skills=skills,
                    model_override=model_override,
                    worktree=worktree,
                    event_cb=event_cb,
                    interaction_cb=interaction_cb,
                    fast_path=fast_path_mode,
                )
                active_task = result.task
                last_result = result
                _render_run_result(console, container, result)
                await _render_task_cost(console, container, active_task)
            except KeyError as exc:
                console.print(f"[red]{exc}[/red]")
            except Exception as exc:
                console.print(Panel(str(exc), title="Execution Error", border_style="red"))

        async def _run_budget(args: list[str]) -> None:
            nonlocal active_task
            if not args:
                await _render_task_cost(console, container, active_task)
                console.print("[dim]Usage: /budget +<amount> | /budget daily [+]<amount>[/dim]")
                return

            scope = "session"
            value_idx = 0
            first = args[0].lower()
            if first in {"daily", "--daily"}:
                scope = "daily"
                value_idx = 1
            elif first in {"session", "--session", "task", "--task"}:
                scope = "session"
                value_idx = 1

            if value_idx >= len(args):
                console.print(
                    "[red]Usage: /budget +<amount> | /budget daily [+]<amount>[/red]"
                )
                return

            raw_val = args[value_idx].strip()
            additive = raw_val.startswith("+")
            try:
                amount = float(raw_val.lstrip("+="))
                if amount <= 0:
                    raise ValueError("Budget amount must be positive.")
            except ValueError as exc:
                console.print(f"[red]Invalid budget amount: {exc}[/red]")
                return

            if scope == "daily":
                was_paused = (
                    active_task is not None
                    and active_task.status == TaskStatus.PAUSED_BUDGET
                )
                new_limit, updated_task = (
                    await container.single_agent_service.update_daily_budget(
                        amount,
                        additive=additive,
                        task_id=active_task.id if active_task else None,
                    )
                )
                if updated_task is not None:
                    active_task = updated_task
                daily_spent = await container.storage.get_daily_usd_spent()
                daily_left = max(0.0, new_limit - daily_spent)
                action = (
                    f"Increased daily limit by +${amount:.2f}"
                    if additive
                    else "Updated daily limit"
                )
                console.print(
                    f"[green]{action} to ${new_limit:.2f} USD "
                    f"(spent=${daily_spent:.4f}, ${daily_left:.4f} left).[/green]"
                )
                if was_paused and active_task is not None:
                    console.print("[cyan]Resuming paused session...[/cyan]")
                    await _execute(active_task.prompt)
                return

            if active_task is None:
                console.print(
                    "[yellow]No active session to grant session budget to (use `/budget daily <amount>` for daily limit).[/yellow]"
                )
                return
            was_paused = active_task.status == TaskStatus.PAUSED_BUDGET
            active_task = await container.single_agent_service.grant_task_budget(
                active_task.id, amount
            )
            console.print(
                f"[green]Granted +${amount:.2f} USD to session {active_task.id} "
                f"(extra granted total: ${active_task.extra_usd_granted:.2f}).[/green]"
            )
            if was_paused:
                console.print("[cyan]Resuming paused session...[/cyan]")
                await _execute(active_task.prompt)

        if prompt:
            try:
                runner.run(_execute(prompt))
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as exc:
                console.print(Panel(str(exc), title="Execution Error", border_style="red"))
        else:
            console.print(
                render_chat_welcome_panel(container.config, fast_path=fast_path_mode)
            )

        while True:
            try:
                line = input("wintermute> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                console.print()
                continue

            stripped = line.strip()
            if not stripped:
                continue

            try:
                if not stripped.startswith("/"):
                    try:
                        runner.run(_execute(stripped))
                    except Exception as exc:
                        console.print(Panel(str(exc), title="Execution Error", border_style="red"))
                    continue

                parts = shlex.split(stripped)
                cmd = parts[0].lower()
                args = parts[1:]

                if cmd in {"/exit", "/quit"}:
                    console.print("[dim]Goodbye.[/dim]")
                    break

                if cmd == "/help":
                    console.print(render_help_panel())
                    continue

                if cmd == "/history":
                    limit = 20
                    if args:
                        try:
                            limit = max(1, int(args[0]))
                        except ValueError:
                            console.print("[red]Usage: /history [limit][/red]")
                            continue
                    console.print(render_history_panel(history.entries, limit=limit))
                    continue

                if cmd == "/retry":
                    idx: int | None = None
                    if args:
                        try:
                            idx = int(args[0])
                        except ValueError:
                            console.print("[red]Usage: /retry [history_index][/red]")
                            continue
                    target_prompt = history.get(idx)
                    if target_prompt is None:
                        console.print("[yellow]No matching message in history.[/yellow]")
                        continue
                    console.print(f"[dim]↻ Retrying: {target_prompt}[/dim]")
                    try:
                        runner.run(_execute(target_prompt))
                    except Exception as exc:
                        console.print(Panel(str(exc), title="Execution Error", border_style="red"))
                    continue

                if cmd == "/fast-path":
                    arg = args[0].lower() if args else ""
                    if arg in {"on", "true", "1"}:
                        fast_path_mode = True
                    elif arg in {"off", "false", "0"}:
                        fast_path_mode = False
                    elif not arg:
                        fast_path_mode = not fast_path_mode
                    else:
                        console.print("[red]Usage: /fast-path [on|off][/red]")
                        continue
                    state = (
                        "ON (direct pipeline execution, skips Coordinator)"
                        if fast_path_mode
                        else "OFF (Coordinator orchestrates each turn)"
                    )
                    console.print(f"[cyan]Fast-path mode: {state}[/cyan]")
                    continue

                if cmd in {"/wu-resume", "/wu-retry"}:
                    runner.run(_run_wu(cmd, args))
                    continue

                if cmd == "/new":
                    active_task = None
                    last_result = None
                    console.print("[cyan]Started new session (next message creates a new session).[/cyan]")
                    continue

                if cmd == "/sessions":
                    sub = args[0].lower() if args else ""
                    if not args or sub == "list" or (args[0].isdigit() and len(args) == 1):
                        limit = 20
                        limit_arg = args[1] if sub == "list" and len(args) > 1 else (args[0] if args and sub != "list" else None)
                        if limit_arg is not None:
                            try:
                                limit = max(1, int(limit_arg))
                            except ValueError:
                                console.print("[red]Usage: /sessions [limit][/red]")
                                continue
                        runner.run(
                            _print_sessions_list(
                                console,
                                container,
                                limit=limit,
                                active_task_id=active_task.id if active_task else None,
                            )
                        )
                        continue

                    if sub == "delete":
                        rest = [a for a in args[1:] if a != "--keep-worktree"]
                        keep_wt = "--keep-worktree" in args[1:]
                        if not rest:
                            console.print("[red]Usage: /sessions delete <SessionID> [--keep-worktree][/red]")
                            continue
                        try:
                            deleted = runner.run(
                                container.single_agent_service.delete_session(
                                    rest[0], cleanup_worktree=not keep_wt
                                )
                            )
                            if active_task is not None and active_task.id == deleted.id:
                                active_task = None
                                last_result = None
                            console.print(
                                f"[green]Deleted session {deleted.id} ({deleted.title}).[/green]"
                            )
                        except KeyError as exc:
                            console.print(f"[red]{exc}[/red]")
                        continue

                    if sub == "resume":
                        if len(args) < 2:
                            console.print("[red]Usage: /sessions resume <SessionID>[/red]")
                            continue
                        try:
                            active_task = runner.run(
                                _load_and_render_session(console, container, args[1])
                            )
                            last_result = None
                        except KeyError as exc:
                            console.print(f"[red]{exc}[/red]")
                        continue

                    if sub == "rename":
                        if len(args) < 3:
                            console.print("[red]Usage: /sessions rename <SessionID> <new title>[/red]")
                            continue
                        try:
                            renamed = runner.run(
                                container.single_agent_service.rename_session(
                                    args[1], " ".join(args[2:])
                                )
                            )
                            if active_task is not None and active_task.id == renamed.id:
                                active_task = renamed
                            console.print(
                                f"[green]Renamed session {renamed.id} to: {renamed.title}[/green]"
                            )
                        except (KeyError, ValueError) as exc:
                            console.print(f"[red]{exc}[/red]")
                        continue

                    if sub == "show":
                        if len(args) < 2:
                            console.print("[red]Usage: /sessions show <SessionID>[/red]")
                            continue
                        selector = args[1]
                    else:
                        selector = args[0]

                    try:
                        runner.run(
                            _load_and_render_session(
                                console,
                                container,
                                selector,
                                message_limit=20,
                                max_work_units=None,
                            )
                        )
                    except KeyError as exc:
                        console.print(f"[red]{exc}[/red]")
                    continue

                if cmd == "/resume":
                    if not args:
                        console.print(
                            "[yellow]Usage: /resume <session_id|prefix|#index|latest>[/yellow]"
                        )
                        runner.run(
                            _print_sessions_list(
                                console,
                                container,
                                limit=10,
                                active_task_id=active_task.id if active_task else None,
                            )
                        )
                        continue
                    try:
                        active_task = runner.run(
                            _load_and_render_session(console, container, args[0])
                        )
                        last_result = None
                    except KeyError as exc:
                        console.print(f"[red]{exc}[/red]")
                    continue

                if cmd == "/rename":
                    if active_task is None:
                        console.print("[yellow]No active session to rename.[/yellow]")
                        continue
                    new_title = " ".join(args).strip()
                    if not new_title:
                        console.print("[red]Usage: /rename <new title>[/red]")
                        continue
                    active_task = runner.run(
                        container.single_agent_service.rename_session(
                            active_task.id, new_title
                        )
                    )
                    console.print(
                        f"[green]Renamed session {active_task.id} to: {active_task.title}[/green]"
                    )
                    continue

                if cmd == "/status":
                    target_task = active_task
                    if args:
                        try:
                            target_task = runner.run(
                                container.single_agent_service.resolve_session(args[0])
                            )
                        except KeyError as exc:
                            console.print(f"[red]{exc}[/red]")
                            continue
                    elif active_task is not None:
                        target_task = (
                            runner.run(container.storage.get_task(active_task.id))
                            or active_task
                        )
                        active_task = target_task
                    work_units: list[WorkUnit] = []
                    if target_task is not None:
                        work_units = runner.run(
                            container.storage.list_work_units(target_task.id)
                        )
                    console.print(render_status_panel(target_task, work_units, container.config))
                    continue

                if cmd == "/pipeline":
                    tpl_name = (
                        (active_task.pipeline_template if active_task else None)
                        or pipeline
                        or container.config.pipeline.default_template
                    )
                    dom_name = (active_task.domain if active_task else None) or domain
                    tpl = container.config.resolve_template_for_domain(dom_name, override=tpl_name)
                    stage_names = [s.name for s in tpl.stages]
                    completed = last_result.stage_history if last_result else ()
                    console.print(
                        render_pipeline_stepper(
                            tpl.name, stage_names, completed_stages=completed
                        )
                    )
                    continue

                if cmd == "/cost":
                    target_task = active_task
                    if args:
                        try:
                            target_task = runner.run(
                                container.single_agent_service.resolve_session(args[0])
                            )
                        except KeyError as exc:
                            console.print(f"[red]{exc}[/red]")
                            continue
                    runner.run(_render_task_cost(console, container, target_task))
                    continue

                if cmd == "/budget":
                    runner.run(_run_budget(args))
                    continue

                if cmd == "/diff":
                    if active_task is not None:
                        diff_text = runner.run(
                            container.worktree_manager.get_git_diff(active_task.id)
                        )
                        if not diff_text:
                            wt_dir = (
                                Path(active_task.worktree_path)
                                if active_task.worktree_path
                                else container.workspace_root
                            )
                            diff_text = _run_git_diff(wt_dir, container.config.workspace.sub_repos)
                        title = f"Diff ({active_task.worktree_path or container.workspace_root})"
                    else:
                        diff_text = _run_git_diff(
                            container.workspace_root, container.config.workspace.sub_repos
                        )
                        title = f"Diff ({container.workspace_root})"
                    console.print(Panel(diff_text, title=title, border_style="blue"))
                    continue

                if cmd == "/tools":
                    role_str = args[0] if len(args) >= 1 else AgentRole.SENIOR_CODER.value
                    dom_str = args[1] if len(args) >= 2 else domain
                    try:
                        role_enum = AgentRole(role_str)
                    except ValueError:
                        console.print(f"[red]Unknown role: {role_str}[/red]")
                        continue
                    tools = container.just_tools.get_tools_for(role_enum, domain=dom_str)
                    console.print(render_tools_table(tools, role_enum.value, dom_str))
                    continue

                if cmd == "/memory":
                    query = " ".join(args).strip()
                    if query:
                        entries = runner.run(
                            container.knowledge_service.search(query, limit=10)
                        )
                    else:
                        entries = runner.run(container.storage.list_knowledge(limit=10))
                    console.print(render_memory_table(entries))
                    continue

                console.print(f"[yellow]Unknown command '{cmd}'. Type /help for commands.[/yellow]")
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")


@cli.command("exec")
@click.argument("prompt", required=False, default="")
@click.option("--pipeline", default=None, help="Explicit pipeline template override.")
@click.option("--domain", default=None, help="Explicit domain override.")
@click.option("--target", default=None, help="Explicit target (package or component).")
@click.option("--skill", "skills", multiple=True, help="Explicit skill name(s) to activate.")
@click.option(
    "--complexity",
    type=click.Choice(["simple", "complex"]),
    default=None,
    help="Session complexity override.",
)
@click.option("--resume", "resume_task_id", default=None, help="Resume an existing Session ID.")
@click.option("--work-unit", "work_unit_id", default=None, help="Resume or retry a specific WorkUnit ID.")
@click.option(
    "--from-start",
    "from_start",
    is_flag=True,
    default=False,
    help="When used with --work-unit, reset stage index and retry from stage 0.",
)
@click.option(
    "--budget-top-up",
    type=float,
    default=None,
    help="Grant additional forward-looking USD budget before resuming.",
)
@click.option(
    "--worktree/--no-worktree",
    default=None,
    help="Enable or disable worktree/Btrfs CoW isolation.",
)
@click.option("--model", "model_override", default=None, help="Temporary model alias override.")
@click.option(
    "--fast-path",
    "fast_path",
    is_flag=True,
    default=False,
    help="Opt in to fast-path execution (skip initial Tech Lead turn).",
)
@click.pass_context
def exec_cmd(
    ctx: click.Context,
    prompt: str,
    pipeline: str | None,
    domain: str | None,
    target: str | None,
    skills: tuple[str, ...],
    complexity: str | None,
    resume_task_id: str | None,
    work_unit_id: str | None,
    from_start: bool,
    budget_top_up: float | None,
    worktree: bool | None,
    model_override: str | None,
    fast_path: bool,
) -> None:
    """Execute a single prompt non-interactively (or resume a session/WorkUnit) and print the result."""
    if not prompt.strip() and not resume_task_id and not work_unit_id:
        raise click.UsageError(
            "Missing argument 'PROMPT' (required unless '--resume' or '--work-unit' is given)."
        )

    async def _run() -> SingleAgentRunResult:
        container = await _ensure_container(ctx)
        console = _console()
        event_cb = _make_cli_event_callback(console)
        interaction_cb = _make_cli_interaction_callback(console)

        if work_unit_id:
            wu = await container.storage.get_work_unit(work_unit_id)
            if wu is None:
                raise click.ClickException(f"WorkUnit '{work_unit_id}' not found.")
            if budget_top_up is not None:
                if budget_top_up <= 0:
                    raise click.BadParameter("must be positive", param_hint="--budget-top-up")
                await container.single_agent_service.grant_task_budget(wu.task_id, budget_top_up)
                console.print(
                    f"[green]Granted +${budget_top_up:.2f} USD to session {wu.task_id}.[/green]"
                )
            result = await container.single_agent_service.resume_work_unit(
                work_unit_id,
                from_start=from_start,
                prompt=prompt.strip() or None,
                model_override=model_override,
                use_worktree=worktree,
                event_callback=event_cb,
                interaction_callback=interaction_cb if sys.stdin.isatty() else None,
            )
            _render_run_result(console, container, result)
            await _render_task_cost(console, container, result.task)
            return result

        effective_prompt = prompt
        resolved_task_id: str | None = None
        if resume_task_id:
            try:
                existing = await container.single_agent_service.resolve_session(
                    resume_task_id
                )
            except KeyError as exc:
                raise click.ClickException(str(exc)) from exc
            resolved_task_id = existing.id
            if not effective_prompt.strip():
                effective_prompt = existing.prompt
            if budget_top_up is not None:
                if budget_top_up <= 0:
                    raise click.BadParameter("must be positive", param_hint="--budget-top-up")
                await container.single_agent_service.grant_task_budget(
                    resolved_task_id, budget_top_up
                )
                console.print(
                    f"[green]Granted +${budget_top_up:.2f} USD to session {resolved_task_id}.[/green]"
                )

        result = await container.single_agent_service.execute_prompt(
            effective_prompt,
            task_id=resolved_task_id,
            domain=domain,
            target=target,
            pipeline_template=pipeline,
            complexity=complexity,
            skills=skills,
            fast_path=fast_path,
            model_override=model_override,
            use_worktree=worktree,
            event_callback=event_cb,
            interaction_callback=interaction_cb if sys.stdin.isatty() else None,
        )
        result = await _maybe_interactive_budget_top_up(
            console=console,
            container=container,
            result=result,
            domain=domain,
            target=target,
            pipeline=pipeline,
            skills=skills,
            model_override=model_override,
            worktree=worktree,
            event_cb=event_cb,
            interaction_cb=interaction_cb,
            complexity=complexity,
            fast_path=fast_path,
        )
        _render_run_result(console, container, result)
        await _render_task_cost(console, container, result.task)
        return result

    try:
        result = asyncio.run(_run())
    except click.ClickException:
        raise
    except Exception as exc:
        _console().print(Panel(str(exc), title="Wintermute Error", border_style="red"))
        raise SystemExit(1) from exc
    if not result.completed and result.task.status != TaskStatus.PAUSED_BUDGET:
        raise SystemExit(1)


@cli.command("tools")
@click.option(
    "--role",
    default=AgentRole.SENIOR_CODER.value,
    help="Agent role to inspect tools for.",
)
@click.option("--domain", default=None, help="Optional domain scope.")
@click.pass_context
def tools_cmd(ctx: click.Context, role: str, domain: str | None) -> None:
    """List synthesized `just_*` and orchestration tools available to an agent role."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        try:
            role_enum = AgentRole(role)
        except ValueError as exc:
            raise click.ClickException(
                f"Unknown role '{role}'. Choices: {', '.join(r.value for r in AgentRole)}"
            ) from exc
        tools = container.just_tools.get_tools_for(role_enum, domain=domain)
        _console().print(render_tools_table(tools, role_enum.value, domain))

    asyncio.run(_run())


@cli.command("pipelines")
@click.pass_context
def pipelines_cmd(ctx: click.Context) -> None:
    """Inspect configured pipeline templates and domain/skill defaults."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        _console().print(render_pipelines_table(container.config))

    asyncio.run(_run())


@cli.command("models")
@click.pass_context
def models_cmd(ctx: click.Context) -> None:
    """Inspect configured models, role assignments, and cached pricing."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        pricing_map: dict[str, Any] = {}
        for profile in container.config.models.values():
            pricing_map[profile.model] = await container.pricing_catalog.resolve_pricing(profile)
        _console().print(render_models_table(container.config, pricing_map))

    asyncio.run(_run())


@cli.group("memory")
def memory_group() -> None:
    """Inspect and manage the Wintermute hybrid knowledge base."""


@memory_group.command("search")
@click.argument("query")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--target", default=None, help="Filter by target.")
@click.option("--limit", default=10, show_default=True, help="Maximum entries to return.")
@click.pass_context
def memory_search_cmd(
    ctx: click.Context, query: str, domain: str | None, target: str | None, limit: int
) -> None:
    """Search knowledge entries using hybrid FTS5 + vector similarity."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        entries = await container.knowledge_service.search(
            query, domain=domain, target=target, limit=limit
        )
        _console().print(render_memory_table(entries))

    asyncio.run(_run())


@memory_group.command("list")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--target", default=None, help="Filter by target.")
@click.option("--limit", default=20, show_default=True, help="Maximum entries to return.")
@click.pass_context
def memory_list_cmd(
    ctx: click.Context, domain: str | None, target: str | None, limit: int
) -> None:
    """List recent knowledge base entries."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        entries = await container.storage.list_knowledge(
            domain=domain, target=target, limit=limit
        )
        _console().print(render_memory_table(entries))

    asyncio.run(_run())


@memory_group.command("add")
@click.argument("issue")
@click.argument("resolution", required=False, default="")
@click.option("--domain", default="packages", show_default=True, help="Knowledge domain.")
@click.option("--target", default="general", show_default=True, help="Target package/component.")
@click.option("--outcome", default="success", show_default=True, help="Outcome label.")
@click.pass_context
def memory_add_cmd(
    ctx: click.Context,
    issue: str,
    resolution: str,
    domain: str,
    target: str,
    outcome: str,
) -> None:
    """Add an explicit lesson/note to the knowledge base."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        saved = await container.knowledge_service.record_note(
            domain=domain,
            target=target,
            issue=issue,
            resolution=resolution or issue,
            outcome=outcome,
            capture_type="explicit",
        )
        _console().print(
            f"[green]Saved knowledge entry {saved.id} ({saved.domain}/{saved.target}).[/green]"
        )

    asyncio.run(_run())


@memory_group.command("consolidate")
@click.option("--domain", default="packages", show_default=True, help="Target domain.")
@click.option("--target", default="general", show_default=True, help="Target package/component.")
@click.pass_context
def memory_consolidate_cmd(ctx: click.Context, domain: str, target: str) -> None:
    """Consolidate multiple entries for a domain/target into a single summary."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        consolidated = await container.knowledge_service.consolidate_target(domain, target)
        console = _console()
        if consolidated is None:
            console.print(f"[yellow]No entries found to consolidate for {domain}/{target}.[/yellow]")
            return
        console.print(
            f"[green]Consolidated {domain}/{target} into entry {consolidated.id}.[/green]"
        )
        console.print(render_memory_table([consolidated]))

    asyncio.run(_run())


@cli.group("sessions", invoke_without_command=True)
@click.pass_context
def sessions_group(ctx: click.Context) -> None:
    """List, inspect, resume, rename, or delete stored Wintermute sessions."""
    if ctx.invoked_subcommand is not None:
        return

    async def _run() -> None:
        container = await _ensure_container(ctx)
        await _print_sessions_list(_console(), container, limit=20)

    asyncio.run(_run())


@sessions_group.command("list")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice([s.value for s in TaskStatus], case_sensitive=False),
    default=None,
    help="Filter sessions by status.",
)
@click.option("--limit", default=20, show_default=True, help="Maximum sessions to list.")
@click.pass_context
def sessions_list_cmd(
    ctx: click.Context, status_filter: str | None, limit: int
) -> None:
    """List stored sessions ordered by most recent update."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        status_enum = TaskStatus(status_filter.upper()) if status_filter else None
        await _print_sessions_list(
            _console(), container, status=status_enum, limit=max(1, limit)
        )

    asyncio.run(_run())


@sessions_group.command("show")
@click.argument("session_id", default="latest")
@click.option("--limit", default=20, show_default=True, help="Maximum recent transcript messages to display.")
@click.pass_context
def sessions_show_cmd(ctx: click.Context, session_id: str, limit: int) -> None:
    """Inspect a stored session, its WorkUnits, cost, and recent transcript."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        try:
            await _load_and_render_session(
                _console(),
                container,
                session_id,
                message_limit=max(1, limit),
                max_work_units=None,
            )
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc

    asyncio.run(_run())


@sessions_group.command("resume")
@click.argument("session_id", default="latest")
@click.option(
    "--worktree/--no-worktree",
    default=None,
    help="Enable or disable worktree/Btrfs CoW isolation.",
)
@click.option("--model", "model_override", default=None, help="Temporary model alias override.")
@click.option(
    "--fast-path",
    "fast_path",
    is_flag=True,
    default=False,
    help="Opt in to fast-path execution (skip initial Tech Lead turn).",
)
@click.pass_context
def sessions_resume_cmd(
    ctx: click.Context,
    session_id: str,
    worktree: bool | None,
    model_override: str | None,
    fast_path: bool,
) -> None:
    """Open interactive `chat` resumed onto a stored session."""
    ctx.invoke(
        chat_cmd,
        resume_task_id=session_id,
        continue_latest=False,
        worktree=worktree,
        model_override=model_override,
        fast_path=fast_path,
    )


@sessions_group.command("rename")
@click.argument("session_id")
@click.argument("title", nargs=-1, required=True)
@click.pass_context
def sessions_rename_cmd(
    ctx: click.Context, session_id: str, title: tuple[str, ...]
) -> None:
    """Rename a stored session's title."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        try:
            task = await container.single_agent_service.rename_session(
                session_id, " ".join(title)
            )
        except (KeyError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        _console().print(
            f"[green]Renamed session {task.id} to: {task.title}[/green]"
        )

    asyncio.run(_run())


@sessions_group.command("delete")
@click.argument("session_id")
@click.option(
    "--keep-worktree",
    is_flag=True,
    default=False,
    help="Keep any associated git/Btrfs worktree on disk.",
)
@click.pass_context
def sessions_delete_cmd(
    ctx: click.Context, session_id: str, keep_worktree: bool
) -> None:
    """Delete a stored session and its persisted SQLite/SDK state."""

    async def _run() -> None:
        container = await _ensure_container(ctx)
        try:
            deleted = await container.single_agent_service.delete_session(
                session_id, cleanup_worktree=not keep_worktree
            )
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
        _console().print(
            f"[green]Deleted session {deleted.id} ({deleted.title}).[/green]"
        )

    asyncio.run(_run())


