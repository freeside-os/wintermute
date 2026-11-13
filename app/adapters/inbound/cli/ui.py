"""Rich UI renderers for Wintermute CLI & REPL."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.bootstrap.config import WintermuteConfig
from app.domain.cost import ModelPricing, TokenUsage
from app.domain.models import KnowledgeEntry, SessionSummary, Task, TaskMessage, WorkUnit


def render_pipeline_stepper(
    template_name: str,
    stage_names: Sequence[str],
    completed_stages: Sequence[str] = (),
    current_stage: str | None = None,
    failed_stage: str | None = None,
) -> Panel:
    """Render a horizontal pipeline stage progression panel."""
    completed_set = set(completed_stages)
    parts: list[str] = []
    for stage in stage_names:
        if stage == failed_stage:
            parts.append(f"[red]✖ {stage}[/red]")
        elif stage == current_stage:
            parts.append(f"[cyan bold]▶ {stage}[/cyan bold]")
        elif stage in completed_set:
            parts.append(f"[green]✔ {stage}[/green]")
        else:
            parts.append(f"[dim]○ {stage}[/dim]")
    body = " ──▶ ".join(parts) if parts else "[dim](no stages)[/dim]"
    return Panel(
        body,
        title=f"Pipeline: {template_name}",
        border_style="blue",
        box=box.ROUNDED,
    )


def _format_budget_line(
    label: str,
    *,
    spent: float,
    limit: float,
    extra_note: str = "",
) -> str:
    left = max(0.0, limit - spent)
    left_style = "red" if left <= 0 else "green"
    note = f", {extra_note}" if extra_note else ""
    return (
        f"{label}: [bold]${spent:.4f}[/bold] / ${limit:.2f} USD "
        f"([{left_style}]${left:.4f} left[/{left_style}]{note})"
    )


def render_cost_panel(
    task: Task | None,
    *,
    total_usd: float,
    usage: TokenUsage,
    per_role_usd: dict[str, float],
    max_task_usd: float,
    daily_usd_spent: float = 0.0,
    max_daily_usd: float | None = None,
    monthly_usd_spent: float = 0.0,
    month_label: str | None = None,
) -> Panel:
    """Render session, daily, and monthly spend vs configured budget limits and remaining balance."""
    month_suffix = f" ({month_label})" if month_label else ""
    if task is None:
        lines = [
            "[dim]No active session yet.[/dim]",
            _format_budget_line("Session Budget", spent=0.0, limit=max_task_usd),
        ]
        if max_daily_usd is not None:
            lines.append(
                _format_budget_line("Daily Budget", spent=daily_usd_spent, limit=max_daily_usd)
            )
        lines.append(f"Monthly Spend{month_suffix}: [bold]${monthly_usd_spent:.4f}[/bold] USD")
        return Panel(
            "\n".join(lines),
            title="Cost & Budget",
            border_style="yellow",
            box=box.ROUNDED,
        )
    base_limit = task.max_usd if task.max_usd is not None else max_task_usd
    effective_limit = base_limit + task.extra_usd_granted
    topup_note = f"+${task.extra_usd_granted:.2f} top-up" if task.extra_usd_granted > 0 else ""
    lines = [
        f"Session: [bold]{task.id}[/bold] ({task.status.value})",
        _format_budget_line(
            "Session Budget",
            spent=total_usd,
            limit=effective_limit,
            extra_note=topup_note,
        ),
    ]
    if max_daily_usd is not None:
        lines.append(
            _format_budget_line("Daily Budget", spent=daily_usd_spent, limit=max_daily_usd)
        )
    lines.append(f"Monthly Spend{month_suffix}: [bold]${monthly_usd_spent:.4f}[/bold] USD")
    lines.append(
        f"Tokens: total={usage.total_tokens:,} "
        f"(in={usage.prompt_tokens:,}, cached={usage.cached_tokens:,}, "
        f"out={usage.candidate_tokens:,}, think={usage.thoughts_tokens:,})"
    )
    if per_role_usd:
        breakdown = ", ".join(
            f"{role}: ${cost:.4f}" for role, cost in sorted(per_role_usd.items())
        )
        lines.append(f"By Role: {breakdown}")
    return Panel(
        "\n".join(lines),
        title="Cost & Budget",
        border_style="yellow",
        box=box.ROUNDED,
    )


def render_status_panel(
    task: Task | None,
    work_units: Sequence[WorkUnit],
    config: WintermuteConfig,
) -> Panel:
    """Render current task, configuration flags, and work-unit states."""
    lines = [
        f"Workspace: {config.workspace.workspace_root}",
        "Fast-Path: opt-in (--fast-path, /fast-path, or `fast_path:` prefix; Tech Lead default)",
        f"Btrfs CoW: {'enabled' if config.workspace.prefer_btrfs_cow else 'disabled'}",
    ]
    if task is None:
        lines.append("Active Session: [dim]none[/dim]")
    else:
        lines.extend(
            [
                f"Active Session: [bold]{task.id}[/bold] — {task.title}",
                f"Status: [bold]{task.status.value}[/bold] | Domain: {task.domain or 'auto'} | Pipeline: {task.pipeline_template or 'auto'}",
                f"Conversation ID: {task.conversation_id or '-'}",
            ]
        )
        if task.worktree_path:
            lines.append(f"Worktree: {task.worktree_path}")
        if work_units:
            lines.append("Work Units:")
            for wu in work_units:
                stage_info = wu.current_stage_name or "pending"
                lines.append(
                    f"  - {wu.id}: [{wu.status}] {wu.domain}/{wu.target or 'general'} (stage={stage_info})"
                )
    return Panel("\n".join(lines), title="Wintermute Status", border_style="cyan", box=box.ROUNDED)


def render_pipelines_table(config: WintermuteConfig) -> Table:
    """Render configured pipeline templates and domain defaults."""
    table = Table(title="Configured Pipeline Templates", box=box.ROUNDED, show_lines=True)
    table.add_column("Template", style="cyan bold", no_wrap=True)
    table.add_column("Stages", style="white")
    table.add_column("Default Domains", style="green")
    table.add_column("Description", style="dim")

    domain_map: dict[str, list[str]] = {}
    for dom, tpl_name in config.pipeline.domain_defaults.items():
        domain_map.setdefault(tpl_name, []).append(dom)

    for name, tpl in sorted(config.pipeline.templates.items()):
        stage_str = " -> ".join(s.name for s in tpl.stages)
        mappings = ", ".join(domain_map.get(name, ())) or "-"
        table.add_row(name, stage_str, mappings, tpl.description)
    return table


def render_models_table(
    config: WintermuteConfig, pricing_map: dict[str, ModelPricing]
) -> Table:
    """Render configured model profiles, assigned roles, and USD pricing per 1M tokens."""
    table = Table(title="Configured Models & Pricing", box=box.ROUNDED)
    table.add_column("Alias", style="cyan bold", no_wrap=True)
    table.add_column("Provider / Model", style="white")
    table.add_column("Roles", style="magenta")
    table.add_column("In / 1M", justify="right", style="green")
    table.add_column("Cached / 1M", justify="right", style="green")
    table.add_column("Out / 1M", justify="right", style="yellow")
    table.add_column("Source", style="dim")

    role_map: dict[str, list[str]] = {}
    for role_name, role_cfg in config.agents.items():
        role_map.setdefault(role_cfg.model, []).append(role_name)

    for alias, profile in sorted(config.models.items()):
        pricing = pricing_map.get(profile.model)
        roles = ", ".join(sorted(role_map.get(alias, ()))) or "-"
        if pricing:
            in_cost = f"${pricing.input_cost_per_1m:.4f}"
            cache_cost = f"${pricing.cached_input_cost_per_1m:.4f}"
            out_cost = f"${pricing.output_cost_per_1m:.4f}"
            src = pricing.source
        else:
            in_cost = cache_cost = out_cost = "-"
            src = "unresolved"
        table.add_row(
            alias,
            f"{profile.provider}:{profile.model}",
            roles,
            in_cost,
            cache_cost,
            out_cost,
            src,
        )
    return table


def render_tools_table(
    tools: Sequence[Callable[..., Any]], role: str, domain: str | None
) -> Table:
    """Render synthesized tools available to a given role/domain."""
    title = f"Tools for role={role}" + (f", domain={domain}" if domain else "")
    table = Table(title=title, box=box.ROUNDED)
    table.add_column("Tool Name", style="cyan bold", no_wrap=True)
    table.add_column("Description", style="white")
    for fn in tools:
        name = getattr(fn, "__name__", str(fn))
        doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
        summary = doc[0] if doc else "-"
        table.add_row(name, summary)
    return table


def render_memory_table(entries: Sequence[KnowledgeEntry]) -> Table:
    """Render knowledge base entries."""
    table = Table(title="Wintermute Knowledge Base", box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Domain / Target", style="magenta")
    table.add_column("Type / Outcome", style="green")
    table.add_column("Issue", style="yellow")
    table.add_column("Resolution", style="white")
    for entry in entries:
        table.add_row(
            entry.id,
            f"{entry.domain}/{entry.target}",
            f"{entry.capture_type} ({entry.outcome})",
            entry.issue[:140],
            entry.resolution[:180],
        )
    return table


def render_history_panel(entries: Sequence[str], *, limit: int = 20) -> Panel:
    """Render numbered prompt history for interactive recall/retry."""
    if not entries:
        return Panel(
            "[dim]No sent messages yet. Press ↑ at the prompt to recall once sent.[/dim]",
            title="Sent Message History",
            border_style="blue",
            box=box.ROUNDED,
        )
    start = max(0, len(entries) - limit)
    text = Text()
    for idx in range(start, len(entries)):
        msg = " ".join(entries[idx].splitlines())
        if len(msg) > 96:
            msg = f"{msg[:95]}…"
        text.append(f"  {idx + 1:>3}. ", style="cyan bold")
        text.append(f"{msg}\n", style="white")
    return Panel(
        text,
        title=f"Sent Message History ({len(entries)} total — ↑/↓ or /retry [n])",
        border_style="blue",
        box=box.ROUNDED,
    )


def render_sessions_table(
    sessions: Sequence[SessionSummary], *, active_task_id: str | None = None
) -> Table:
    """Render stored cross-process sessions ordered by most recent activity."""
    table = Table(title="Stored Wintermute Sessions", box=box.ROUNDED)
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("Session ID", style="cyan bold", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Status", style="magenta", no_wrap=True)
    table.add_column("Domain", style="green", no_wrap=True)
    table.add_column("Msgs", justify="right")
    table.add_column("WUs", justify="right")
    table.add_column("Cost", justify="right", style="yellow")
    table.add_column("Updated (UTC)", style="dim", no_wrap=True)

    for idx, summary in enumerate(sessions, start=1):
        task = summary.task
        marker = f"*{idx}" if task.id == active_task_id else str(idx)
        title = " ".join(task.title.split())
        if len(title) > 54:
            title = f"{title[:53]}…"
        updated = task.updated_at.replace("T", " ")[:19]
        table.add_row(
            marker,
            task.id,
            title,
            task.status.value,
            task.domain or "auto",
            str(summary.message_count),
            str(summary.work_unit_count),
            f"${summary.total_usd:.4f}",
            updated,
        )
    return table


def render_session_resume_panel(
    task: Task,
    work_units: Sequence[WorkUnit],
    messages: Sequence[TaskMessage],
    *,
    total_usd: float,
    usage: TokenUsage | None = None,
    max_task_usd: float | None = None,
    per_role_usd: Mapping[str, float] | None = None,
    work_unit_costs: Mapping[str, tuple[float, int]] | None = None,
    orchestration_cost: tuple[float, int] | None = None,
    max_messages: int = 8,
    max_work_units: int | None = 5,
) -> Panel:
    """Render a compact recap of session state, costs, WorkUnits, and transcript."""
    text = Text()
    text.append("  Session: ", style="cyan bold")
    text.append(f"{task.id}", style="bold white")
    text.append(f" ({task.status.value}) — {task.title}\n", style="white")
    text.append("  Updated: ", style="cyan bold")
    text.append(
        f"{task.updated_at.replace('T', ' ')[:19]} UTC | "
        f"Domain: {task.domain or 'auto'}\n",
        style="dim",
    )
    text.append("  Session Cost: ", style="cyan bold")
    text.append(f"${total_usd:.4f} USD", style="bold white")
    if max_task_usd is not None:
        base_limit = task.max_usd if task.max_usd is not None else max_task_usd
        effective_limit = base_limit + task.extra_usd_granted
        left = max(0.0, effective_limit - total_usd)
        topup_note = f", +${task.extra_usd_granted:.2f} top-up" if task.extra_usd_granted > 0 else ""
        text.append(f" / ${effective_limit:.2f} limit (", style="dim")
        text.append(f"${left:.4f} left", style="red" if left <= 0 else "green")
        text.append(f"{topup_note})", style="dim")
    if usage is not None and usage.total_tokens > 0:
        text.append(
            f" | {usage.total_tokens:,} tokens "
            f"(in={usage.prompt_tokens:,}, out={usage.candidate_tokens:,}, cached={usage.cached_tokens:,})",
            style="dim",
        )
    text.append("\n")
    if per_role_usd:
        role_summary = ", ".join(
            f"{role}: ${cost:.4f}" for role, cost in sorted(per_role_usd.items())
        )
        text.append("  By Role: ", style="cyan bold")
        text.append(f"{role_summary}\n", style="dim")
    if task.worktree_path:
        text.append("  Worktree:", style="cyan bold")
        text.append(f" {task.worktree_path}\n", style="dim")

    if work_units:
        shown_wus = work_units if max_work_units is None else work_units[-max_work_units:]
        hidden_wus = len(work_units) - len(shown_wus)
        wu_header = (
            f"  WorkUnits (last {len(shown_wus)} of {len(work_units)}):\n"
            if hidden_wus > 0
            else f"  WorkUnits ({len(work_units)}):\n"
        )
        text.append(wu_header, style="cyan bold")
        for wu in shown_wus:
            stage_info = wu.current_stage_name or "pending"
            wu_usd, wu_tokens = (work_unit_costs or {}).get(wu.id, (0.0, 0))
            token_suffix = f", {wu_tokens:,} tokens" if wu_tokens > 0 else ""
            text.append(
                f"    • {wu.id}: [{wu.status}] {wu.domain}/{wu.target or 'general'} "
                f"(stage={stage_info}) — ${wu_usd:.4f}{token_suffix}\n",
                style="white",
            )
        if orchestration_cost is not None:
            orch_usd, orch_tokens = orchestration_cost
            if orch_usd > 0 or orch_tokens > 0:
                orch_tok_suffix = f", {orch_tokens:,} tokens" if orch_tokens > 0 else ""
                text.append(
                    f"    • orchestration / direct: ${orch_usd:.4f}{orch_tok_suffix}\n",
                    style="dim",
                )

    if messages:
        recent = messages[-max_messages:]
        hidden = len(messages) - len(recent)
        header = (
            f"  Transcript (last {len(recent)} of {len(messages)}):\n"
            if hidden > 0
            else f"  Transcript ({len(messages)} message(s)):\n"
        )
        text.append(header, style="cyan bold")
        for msg in recent:
            label = "You" if msg.sender == "user" else "Wintermute"
            style = "yellow bold" if msg.sender == "user" else "green bold"
            snippet = " ".join(msg.content.split())
            if len(snippet) > 140:
                snippet = f"{snippet[:139]}…"
            text.append(f"    {label}: ", style=style)
            text.append(f"{snippet}\n", style="white")
    elif task.prompt:
        snippet = " ".join(task.prompt.split())
        if len(snippet) > 140:
            snippet = f"{snippet[:139]}…"
        text.append("  Initial Ask: ", style="cyan bold")
        text.append(f"{snippet}\n", style="white")

    return Panel(text, title="Resumed Session", border_style="cyan", box=box.ROUNDED)


def render_help_panel() -> Panel:
    """Render interactive REPL slash command reference."""
    text = Text()
    commands = [
        ("/status [id]", "Show active or specified session, workspace, and work unit status"),
        ("/sessions [n|<id>|delete <id>]", "List, inspect, rename, resume, or delete sessions (* = active)"),
        ("/resume <id>", "Switch/resume stored session by SessionID, prefix, #index, or latest"),
        ("/rename <title>", "Rename the active session title"),
        ("/pipeline", "Render the active or default pipeline stage stepper"),
        ("/fast-path [on|off]", "Opt into fast-path execution (skip initial Coordinator turn)"),
        ("/new", "Start a new session & fresh Coordinator conversation"),
        ("/history [n]", "Show previously sent messages (also ↑/↓ arrows at prompt)"),
        ("/retry [n]", "Re-run the last sent message or history item #n"),
        ("/wu-resume [id] [hint]", "Resume incomplete WorkUnit from its paused/failed stage"),
        ("/wu-retry [id] [hint]", "Retry a WorkUnit from stage 0 (clears stage/gate progress)"),
        ("/cost [id]", "Show session, daily, & monthly USD spend, limits, and tokens"),
        ("/budget +<amt> | daily [+]<amt>", "Grant session USD budget or set/add daily USD limit"),
        ("/diff", "Show git status & diff stats in the workspace/worktree"),
        ("/tools [role] [domain]", "List synthesized tools for a role/domain"),
        ("/memory [query]", "Search or list knowledge base entries"),
        ("/help", "Show this help message"),
        ("/exit | /quit", "Exit interactive chat"),
    ]
    for cmd, desc in commands:
        text.append(f"  {cmd:<32}", style="cyan bold")
        text.append(f"{desc}\n", style="white")
    return Panel(text, title="Interactive REPL Commands", border_style="green", box=box.ROUNDED)


def render_chat_welcome_panel(
    config: WintermuteConfig, *, fast_path: bool = False
) -> Panel:
    """Render concise session, routing & command quick-guide when opening chat."""
    mode_label = (
        "fast_path active (skips initial Coordinator turn)"
        if fast_path
        else "Coordinator default (converses, routes WorkUnits, or escalates to Tech Lead)"
    )
    text = Text()
    text.append("  Session:  ", style="cyan bold")
    text.append(
        "All messages share one Session & are stored across restarts (`chat -c`, `/sessions`, `/resume`, `/new`).\n",
        style="white",
    )
    text.append("  Routing:  ", style="cyan bold")
    text.append(f"{mode_label}.\n", style="white")
    text.append("  FastPath: ", style="cyan bold")
    text.append("Prefix ", style="white")
    text.append("fast_path: <ask>", style="yellow")
    text.append(", pass ", style="white")
    text.append("--fast-path", style="yellow")
    text.append(", or toggle ", style="white")
    text.append("/fast-path on", style="cyan bold")
    text.append(" to skip Coordinator.\n", style="white")
    text.append("  Options:  ", style="cyan bold")
    text.append(
        "--continue (-c)  --resume <id|latest>  --domain <repo>  --pipeline <tpl>  --no-worktree  --fast-path\n",
        style="dim",
    )
    text.append("  History:  ", style="cyan bold")
    text.append("↑/↓ arrows recall previous messages  (/history, /retry [n])\n", style="white")
    text.append("  Commands: ", style="cyan bold")
    text.append(
        "/help  /sessions  /resume  /rename  /new  /status  /fast-path  /wu-resume  /cost  /diff  /exit",
        style="white",
    )
    return Panel(text, title="Wintermute Chat", border_style="cyan", box=box.ROUNDED)

