"""End-to-end integration tests for M1 SingleAgentService and Click CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from app.adapters.inbound.cli.main import cli
from app.application.ports import StageTurnResult
from app.bootstrap.config import WintermuteConfig, load_config
from app.bootstrap.container import AppContainer, build_container
from app.domain.cost import TokenUsage
from app.domain.models import AgentRole, TaskStatus


def _fake_dump_provider(_ws_root: Path) -> dict[str, Any]:
    return {
        "recipes": {
            "status": {"name": "status", "doc": "Show status", "parameters": []},
        },
        "modules": {
            "pkg": {
                "recipes": {
                    "verify": {
                        "name": "verify",
                        "doc": "Verify package manifests",
                        "parameters": [{"name": "pkg_name", "default": None}],
                    },
                    "resolve": {
                        "name": "resolve",
                        "doc": "Resolve dependencies",
                        "parameters": [{"name": "pkg_name", "default": None}],
                    },
                    "build": {
                        "name": "build",
                        "doc": "Build package",
                        "parameters": [{"name": "pkg_name", "default": None}],
                    },
                },
                "modules": {},
            },
            "straylight": {
                "recipes": {
                    "test": {"name": "test", "doc": "Run cargo tests", "parameters": []}
                },
                "modules": {},
            },
            "fstools": {
                "recipes": {
                    "test": {"name": "test", "doc": "Run pytest suite", "parameters": []}
                },
                "modules": {},
            },
            "wintermute": {
                "recipes": {
                    "test": {"name": "test", "doc": "Run wintermute tests", "parameters": []}
                },
                "modules": {},
            },
            "git": {
                "recipes": {
                    "status": {"name": "status", "doc": "Git status", "parameters": []}
                },
                "modules": {},
            },
        },
    }


async def _fake_feed(_url: str) -> dict[str, Any]:
    return {
        "google": {
            "models": {
                "gemini-4-argon": {
                    "id": "gemini-4-argon",
                    "cost": {"input": 1.25, "output": 10.0, "cache_read": 0.3125},
                },
                "gemini-4-hydrogen": {
                    "id": "gemini-4-hydrogen",
                    "cost": {"input": 0.60, "output": 4.0, "cache_read": 0.15},
                },
                "gemini-3.8-flash": {
                    "id": "gemini-3.8-flash",
                    "cost": {"input": 0.15, "output": 0.60, "cache_read": 0.0375},
                },
            }
        }
    }


def _make_isolated_config(tmp_path: Path) -> WintermuteConfig:
    repo_root = Path(__file__).resolve().parents[3]
    default_toml = repo_root / "wintermute" / "wintermute.toml"
    (tmp_path / ".wintermute").mkdir(parents=True, exist_ok=True)
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    # Keep tests fast and off external hosts
    object.__setattr__(cfg.workspace, "prefer_btrfs_cow", False)
    return cfg


def _make_test_container(
    tmp_path: Path,
    *,
    token_usage: TokenUsage | None = None,
    max_task_usd: float | None = None,
) -> tuple[AppContainer, list[dict[str, Any]]]:
    cfg = _make_isolated_config(tmp_path)
    if max_task_usd is not None:
        object.__setattr__(cfg.cost.policy, "max_task_usd", max_task_usd)

    recorded_turns: list[dict[str, Any]] = []
    usage = token_usage or TokenUsage(
        prompt_tokens=1000,
        cached_tokens=200,
        candidate_tokens=400,
        thoughts_tokens=100,
        total_tokens=1700,
    )

    async def _turn_runner(ctx: dict[str, Any]) -> StageTurnResult:
        role: AgentRole = ctx["role"]
        recorded_turns.append(
            {
                "role": role,
                "prompt": ctx["prompt"],
                "work_unit": ctx["work_unit"],
            }
        )
        # Reviewer leaves repo unmodified so skip_next_gate_if_unmodified triggers cleanly.
        if role == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM",
                usage=usage,
                files_modified=False,
            )
        # TECH_LEAD during decomposition can invoke dispatch_work_unit if available.
        if role == AgentRole.TECH_LEAD:
            for tool in ctx["tools"]:
                if getattr(tool, "__name__", "") == "dispatch_work_unit":
                    await tool(domain="straylight", target="straylightd", complexity="complex")
                    await tool(domain="fstools", target="pkg", complexity="simple")
                    break
            return StageTurnResult(
                response_text="Decomposed into work units.",
                usage=usage,
                files_modified=False,
            )
        return StageTurnResult(
            response_text=f"Completed stage as {role.value}.",
            usage=usage,
            files_modified=True,
        )

    async def _cmd_runner(
        cmd: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[int, str, str]:
        return 0, f"OK: {' '.join(cmd)}", ""

    container = build_container(
        config=cfg,
        turn_runner=_turn_runner,
        command_runner=_cmd_runner,
        dump_provider=_fake_dump_provider,
        feed_fetcher=_fake_feed,
    )
    return container, recorded_turns


@pytest.mark.asyncio
async def test_single_agent_service_fast_path_and_skill_routing(tmp_path: Path) -> None:
    container, turns = _make_test_container(tmp_path)
    await container.initialize()

    # Without fast_path opt-in, even a single-domain CVE task starts with Tech Lead
    default_route = container.single_agent_service.classify_request(
        "Patch CVE-2025-1234 vulnerability in zlib"
    )
    assert default_route.is_fast_path is False

    # Opt-in via `fast_path:` prompt prefix runs fast_path directly
    result = await container.single_agent_service.execute_prompt(
        "fast_path: Patch CVE-2025-1234 vulnerability in zlib"
    )

    assert result.route.is_fast_path is True
    assert result.route.domain == "packages"
    assert "cve-remediation" in result.route.skills
    assert result.route.pipeline_template == "package_lifecycle"
    assert result.completed is True
    assert result.task.status == TaskStatus.COMPLETED
    assert result.total_usd > 0.0
    assert result.total_tokens > 0
    assert len(result.work_units) == 1
    assert any(t["role"] == AgentRole.JUNIOR_CODER for t in turns)


@pytest.mark.asyncio
async def test_single_agent_service_swarm_path_multi_domain(tmp_path: Path) -> None:
    container, turns = _make_test_container(tmp_path)
    await container.initialize()

    result = await container.single_agent_service.execute_prompt(
        "Coordinate straylight daemon change and fstools CLI update"
    )

    assert result.route.is_fast_path is False
    assert "straylight" in result.route.sub_domains
    assert "fstools" in result.route.sub_domains
    assert result.completed is True
    assert len(result.work_units) == 2
    assert turns[0]["role"] == AgentRole.TECH_LEAD


@pytest.mark.asyncio
async def test_single_agent_service_budget_pause_and_top_up_resume(
    tmp_path: Path,
) -> None:
    # Make each turn cost > $0.10 while budget ceiling is $0.05
    expensive_usage = TokenUsage(
        prompt_tokens=100_000,
        cached_tokens=0,
        candidate_tokens=50_000,
        thoughts_tokens=0,
        total_tokens=150_000,
    )
    container, _ = _make_test_container(
        tmp_path,
        token_usage=expensive_usage,
        max_task_usd=0.05,
    )
    await container.initialize()

    paused_result = await container.single_agent_service.execute_prompt(
        "Update straylight state sync"
    )
    assert paused_result.completed is False
    assert paused_result.task.status == TaskStatus.PAUSED_BUDGET

    # Grant enough budget to finish remaining pipeline stages
    top_up_task = await container.single_agent_service.grant_task_budget(
        paused_result.task.id, 5.00
    )
    assert top_up_task.status == TaskStatus.RUNNING
    assert top_up_task.extra_usd_granted == pytest.approx(5.00)

    resumed_result = await container.single_agent_service.execute_prompt(
        paused_result.task.prompt,
        task_id=paused_result.task.id,
        domain="straylight",
    )
    assert resumed_result.completed is True
    assert resumed_result.task.status == TaskStatus.COMPLETED


def test_cli_inspection_commands(tmp_path: Path) -> None:
    container, _ = _make_test_container(tmp_path)
    runner = CliRunner()

    res_pipelines = runner.invoke(cli, ["pipelines"], obj=container)
    assert res_pipelines.exit_code == 0, res_pipelines.output
    assert "package_lifecycle" in res_pipelines.output
    assert "tdd_coding" in res_pipelines.output

    res_models = runner.invoke(cli, ["models"], obj=container)
    assert res_models.exit_code == 0, res_models.output
    assert "argon" in res_models.output
    assert "gemini-4-argon" in res_models.output

    res_tools = runner.invoke(
        cli, ["tools", "--role", "senior_coder", "--domain", "packages"], obj=container
    )
    assert res_tools.exit_code == 0, res_tools.output
    assert "just_pkg_verify" in res_tools.output

    res_add = runner.invoke(
        cli,
        [
            "memory",
            "add",
            "musl link error for zlib",
            "Set LDFLAGS=-static",
            "--domain",
            "packages",
            "--target",
            "zlib",
        ],
        obj=container,
    )
    assert res_add.exit_code == 0, res_add.output
    assert "Saved knowledge entry" in res_add.output

    res_list = runner.invoke(cli, ["memory", "list", "--domain", "packages"], obj=container)
    assert res_list.exit_code == 0, res_list.output
    assert "zlib" in res_list.output

    res_search = runner.invoke(
        cli, ["memory", "search", "musl link error"], obj=container
    )
    assert res_search.exit_code == 0, res_search.output
    assert "musl" in res_search.output

    res_consolidate = runner.invoke(
        cli,
        ["memory", "consolidate", "--domain", "packages", "--target", "zlib"],
        obj=container,
    )
    assert res_consolidate.exit_code == 0, res_consolidate.output
    assert "Consolidated packages/zlib" in res_consolidate.output


def test_cli_exec_and_chat_repl_slash_commands(tmp_path: Path) -> None:
    container, _ = _make_test_container(tmp_path)
    runner = CliRunner()

    res_exec = runner.invoke(
        cli,
        ["exec", "Fix zlib build", "--domain", "packages", "--target", "zlib"],
        obj=container,
    )
    assert res_exec.exit_code == 0, res_exec.output
    assert "COMPLETED" in res_exec.output
    assert "Cost & Budget" in res_exec.output

    repl_inputs = "\n".join(
        [
            "Update fstools CLI help text",
            "/status",
            "/pipeline",
            "/cost",
            "/budget +2.00",
            "/diff",
            "/tools senior_coder fstools",
            "/memory",
            "/help",
            "/exit",
        ]
    ) + "\n"
    res_chat = runner.invoke(cli, ["chat"], input=repl_inputs, obj=container)
    assert res_chat.exit_code == 0, res_chat.output
    assert "Wintermute Status" in res_chat.output
    assert "Pipeline:" in res_chat.output
    assert "Cost & Budget" in res_chat.output
    assert "Granted +$2.00 USD" in res_chat.output
    assert "Tools for role=senior_coder" in res_chat.output
    assert "Wintermute Knowledge Base" in res_chat.output
    assert "Interactive REPL Commands" in res_chat.output
    assert "Goodbye." in res_chat.output


@pytest.mark.asyncio
async def test_no_worktree_direct_mode_events_and_conversation_id(tmp_path: Path) -> None:
    container, _ = _make_test_container(tmp_path)
    await container.initialize()

    events: list[tuple[str, dict[str, Any]]] = []
    result = await container.single_agent_service.execute_prompt(
        "Update README guide in docs",
        domain="docs",
        use_worktree=False,
        event_callback=lambda etype, data: events.append((etype, data)),
    )

    assert result.completed is True
    assert result.task.conversation_id is not None
    assert len(result.task.conversation_id) >= 32
    assert "_" not in result.task.conversation_id
    assert result.task.worktree_path == str(tmp_path.resolve())
    assert not (tmp_path / ".wintermute" / "worktrees" / result.task.id).exists()
    assert any(etype == "STAGE_STARTED" for etype, _ in events)
    assert any(etype == "WORK_UNIT_COMMITTED" for etype, _ in events)


def test_cli_exec_resume_with_budget_top_up_and_repl_diff(tmp_path: Path) -> None:
    import asyncio
    import subprocess

    # Initialize a sub-repo git directory with a dirty diff to verify REPL /diff
    sub_repo = tmp_path / "fstools"
    sub_repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(sub_repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(sub_repo), "config", "user.email", "test@freeside.os"], check=True)
    subprocess.run(["git", "-C", str(sub_repo), "config", "user.name", "Test"], check=True)
    pkg_py = sub_repo / "pkg.py"
    pkg_py.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(sub_repo), "add", "pkg.py"], check=True)
    subprocess.run(["git", "-C", str(sub_repo), "commit", "--no-gpg-sign", "-m", "init"], check=True)

    expensive_usage = TokenUsage(
        prompt_tokens=100_000,
        cached_tokens=0,
        candidate_tokens=50_000,
        thoughts_tokens=0,
        total_tokens=150_000,
    )
    container, _ = _make_test_container(
        tmp_path,
        token_usage=expensive_usage,
        max_task_usd=0.05,
    )
    runner = CliRunner()

    res_pause = runner.invoke(
        cli,
        ["exec", "Refactor fstools pkg command", "--domain", "fstools"],
        obj=container,
    )
    assert res_pause.exit_code == 0, res_pause.output
    assert "PAUSED_BUDGET" in res_pause.output

    tasks = asyncio.run(container.storage.list_tasks())
    assert len(tasks) == 1
    task_id = tasks[0].id

    res_resume = runner.invoke(
        cli,
        ["exec", "--resume", task_id, "--budget-top-up", "5.0"],
        obj=container,
    )
    assert res_resume.exit_code == 0, res_resume.output
    assert "Granted +$5.00 USD" in res_resume.output
    assert "COMPLETED" in res_resume.output

    # Modify sub-repo file and verify /diff shows it via worktree_manager.get_git_diff
    pkg_py.write_text("x = 2\n", encoding="utf-8")
    res_chat = runner.invoke(
        cli,
        ["chat", "--no-worktree", "--domain", "fstools", "--pipeline", "investigation_audit"],
        input="Inspect pkg.py diff\n/diff\n/exit\n",
        obj=container,
    )
    assert res_chat.exit_code == 0, res_chat.output
    assert "pkg.py" in res_chat.output


def test_chat_repl_and_exec_handle_api_key_error_gracefully(tmp_path: Path) -> None:
    import asyncio

    cfg = _make_isolated_config(tmp_path)

    async def _failing_turn_runner(_ctx: dict[str, Any]) -> StageTurnResult:
        raise ValueError(
            "A Gemini API key is required. Set it via GEMINI_API_KEY environment variable."
        )

    container = build_container(
        config=cfg,
        turn_runner=_failing_turn_runner,
        dump_provider=_fake_dump_provider,
        feed_fetcher=_fake_feed,
    )
    runner = CliRunner()

    # Non-interactive `exec` renders clean error panel + hint and exits 1 without Python traceback
    res_exec = runner.invoke(cli, ["exec", "test"], obj=container)
    assert res_exec.exit_code == 1
    assert "Traceback (most recent call last)" not in res_exec.output
    assert "GEMINI_API_KEY" in res_exec.output
    assert "Hint:" in res_exec.output
    assert "FAILED" in res_exec.output

    tasks = asyncio.run(container.storage.list_tasks())
    assert tasks and tasks[0].status == TaskStatus.FAILED
    assert tasks[0].error_message is not None and "GEMINI_API_KEY" in tasks[0].error_message

    # Interactive `chat` REPL stays alive after error so user can inspect /status or /exit
    res_chat = runner.invoke(
        cli,
        ["chat"],
        input="test\n/status\n/exit\n",
        obj=container,
    )
    assert res_chat.exit_code == 0, res_chat.output
    assert "Traceback (most recent call last)" not in res_chat.output
    assert "FAILED" in res_chat.output
    assert "Wintermute Status" in res_chat.output
    assert "Goodbye." in res_chat.output


def test_read_only_package_query_routes_to_investigation_audit(tmp_path: Path) -> None:
    container, _ = _make_test_container(tmp_path)
    route = container.single_agent_service.classify_request(
        "check what freeside packages can be updated"
    )
    assert route.domain == "packages"
    assert route.pipeline_template == "investigation_audit"

    mut_route = container.single_agent_service.classify_request(
        "update zlib package to latest version"
    )
    assert mut_route.domain == "packages"
    assert mut_route.pipeline_template == "package_lifecycle"


def test_cli_event_callback_renders_intent_tool_params_and_gate_error() -> None:
    from rich.console import Console

    from app.adapters.inbound.cli.main import _make_cli_event_callback

    console = Console(record=True, width=120)
    cb = _make_cli_event_callback(console)
    cb("AGENT_INTENT", {"kind": "thought", "summary": "Inspecting zlib manifest"})
    cb("TOOL_CALL_STARTED", {"tool": "view_file", "args": "packages/zlib/package.manifest:1-30"})
    cb("TOOL_CALL_FAILED", {"tool": "run_command", "args": "just pkg::verify", "error": "exit 1"})
    cb(
        "GATE_FAILED",
        {
            "stage": "static_verify",
            "attempt": 1,
            "validators": ["pkg::verify"],
            "error_summary": "missing field 'source'",
        },
    )

    rendered = console.export_text()
    assert "Inspecting zlib manifest" in rendered
    assert "view_file(packages/zlib/package.manifest:1-30)" in rendered
    assert "run_command(just pkg::verify): exit 1" in rendered
    assert "missing field 'source'" in rendered


def test_tech_lead_default_welcome_banner_and_fast_path_opt_in(
    tmp_path: Path,
) -> None:
    container, turns = _make_test_container(tmp_path)
    asyncio.run(container.initialize())

    # 1. Default routing always starts with Tech Lead (even for single-domain asks),
    # and plural package-group prompts leave target=None.
    group_route = container.single_agent_service.classify_request(
        "update the packages in the base, builder and linux package groups"
    )
    assert group_route.domain == "packages"
    assert group_route.target is None
    assert group_route.is_fast_path is False

    fp_prefix_route = container.single_agent_service.classify_request(
        "fast_path: Update guide doc"
    )
    assert fp_prefix_route.is_fast_path is True
    assert fp_prefix_route.domain == "docs"

    runner = CliRunner()
    # 2. Interactive `chat` starts in Tech Lead mode, shows welcome panel, supports /fast-path & /new
    res_chat = runner.invoke(
        cli,
        ["chat", "--domain", "docs", "--no-worktree"],
        input="Plan docs tidy\n/new\n/fast-path on\nUpdate guide doc\n/exit\n",
        obj=container,
    )
    assert res_chat.exit_code == 0, res_chat.output
    assert "Wintermute Chat" in res_chat.output
    assert "All messages share one Task" in res_chat.output
    assert "/fast-path" in res_chat.output
    assert "/new" in res_chat.output
    assert "Route: tech_lead" in res_chat.output
    assert "Started new session" in res_chat.output
    assert "Fast-path mode: ON" in res_chat.output
    assert "Route: fast_path" in res_chat.output
    assert turns[0]["role"] == AgentRole.TECH_LEAD

    turns.clear()
    # 3. `exec --fast-path` opts into fast-path (skips Tech Lead upfront)
    res_exec = runner.invoke(
        cli,
        ["exec", "--fast-path", "--domain", "docs", "--no-worktree", "Tidy docs index"],
        obj=container,
    )
    assert res_exec.exit_code == 0, res_exec.output
    assert "Route: fast_path" in res_exec.output
    assert all(t["role"] != AgentRole.TECH_LEAD for t in turns)


@pytest.mark.asyncio
async def test_session_as_task_conversational_tech_lead_and_fast_path_delegation(
    tmp_path: Path,
) -> None:
    cfg = _make_isolated_config(tmp_path)
    usage = TokenUsage(prompt_tokens=100, candidate_tokens=50, total_tokens=150)
    seen_prompts: list[str] = []

    async def _turn_runner(ctx: dict[str, Any]) -> StageTurnResult:
        role: AgentRole = ctx["role"]
        if role == AgentRole.TECH_LEAD:
            seen_prompts.append(ctx["prompt"])
            if len(seen_prompts) == 1:
                # Turn 1: conversational answer without dispatching any WorkUnit
                return StageTurnResult(
                    response_text="Let's focus on zlib first — confirm if ready.",
                    usage=usage,
                    files_modified=False,
                )
            # Turn 2: Tech Lead decides to delegate via fast_path=True
            for tool in ctx["tools"]:
                if getattr(tool, "__name__", "") == "dispatch_work_unit":
                    await tool(
                        domain="docs",
                        target="guide",
                        prompt="Update docs/guide.md",
                        fast_path=True,
                    )
                    break
            return StageTurnResult(
                response_text="Delegated to fast_path.",
                usage=usage,
                files_modified=False,
            )
        if role == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM", usage=usage, files_modified=False
            )
        return StageTurnResult(
            response_text="Done", usage=usage, files_modified=True
        )

    async def _cmd_runner(
        cmd: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[int, str, str]:
        return 0, "ok", ""

    container = build_container(
        config=cfg,
        turn_runner=_turn_runner,
        command_runner=_cmd_runner,
        dump_provider=_fake_dump_provider,
        feed_fetcher=_fake_feed,
    )
    await container.initialize()

    # Turn 1: Conversational Tech Lead reply completes cleanly with 0 WorkUnits
    events: list[tuple[str, dict[str, Any]]] = []
    res1 = await container.single_agent_service.execute_prompt(
        "Which packages should we inspect first?",
        use_worktree=False,
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )
    assert res1.completed is True
    assert res1.task.status == TaskStatus.COMPLETED
    assert res1.work_units == []
    assert "Let's focus on zlib first" in res1.summary

    # Turn 2 in SAME session Task: passes clean follow-up + Tech Lead delegates via fast_path=True
    res2 = await container.single_agent_service.execute_prompt(
        "Go ahead with docs guide",
        task_id=res1.task.id,
        use_worktree=False,
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )
    assert res2.task.id == res1.task.id
    assert res2.task.conversation_id == res1.task.conversation_id
    assert "User follow-up in this session:" in seen_prompts[1]
    assert "Which packages should we inspect first?" not in seen_prompts[1]
    assert res2.completed is True
    assert len(res2.work_units) == 1
    assert any(
        k == "route" and p.get("mode") == "fast_path" for k, p in events
    )


@pytest.mark.asyncio
async def test_tech_lead_escalation_executes_dispatched_recovery_units(
    tmp_path: Path,
) -> None:
    cfg = _make_isolated_config(tmp_path)
    usage = TokenUsage(prompt_tokens=100, candidate_tokens=50, total_tokens=150)
    turns: list[AgentRole] = []

    async def _turn_runner(ctx: dict[str, Any]) -> StageTurnResult:
        role: AgentRole = ctx["role"]
        turns.append(role)
        if role == AgentRole.TECH_LEAD:
            for tool in ctx["tools"]:
                if getattr(tool, "__name__", "") == "dispatch_work_unit":
                    await tool(
                        domain="packages",
                        target="zlib",
                        pipeline_template="investigation_audit",
                        prompt="Audit zlib after escalation",
                    )
                    break
            return StageTurnResult(
                response_text="Dispatched scoped recovery unit for zlib.",
                usage=usage,
                files_modified=False,
            )
        if role == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM", usage=usage, files_modified=False
            )
        return StageTurnResult(
            response_text="Done", usage=usage, files_modified=True
        )

    gate_calls = 0

    async def _cmd_runner(
        cmd: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[int, str, str]:
        nonlocal gate_calls
        gate_calls += 1
        return 1, "", "error: invalid target"

    container = build_container(
        config=cfg,
        turn_runner=_turn_runner,
        command_runner=_cmd_runner,
        dump_provider=_fake_dump_provider,
        feed_fetcher=_fake_feed,
    )
    await container.initialize()

    result = await container.single_agent_service.execute_prompt(
        "Update zlib",
        domain="packages",
        target="zlib",
        fast_path=True,
        use_worktree=False,
    )
    assert result.route.is_fast_path is True
    assert "tech_lead_escalation" in result.stage_history
    assert AgentRole.TECH_LEAD in turns
    assert result.completed is True
    assert result.task.status == TaskStatus.COMPLETED
    assert len(result.work_units) == 2


def test_chat_history_persistence_readline_and_retry(tmp_path: Path) -> None:
    import readline

    container, turns = _make_test_container(tmp_path)
    asyncio.run(container.initialize())
    runner = CliRunner()

    # Session 1 sends two prompts (plus duplicate to test dedup) and checks /history + /retry 1
    res1 = runner.invoke(
        cli,
        ["chat", "--domain", "docs", "--no-worktree"],
        input="First ask\nSecond ask\n/history\n/retry 1\n/exit\n",
        obj=container,
    )
    assert res1.exit_code == 0, res1.output
    assert "Sent Message History" in res1.output
    assert "1. First ask" in res1.output
    assert "2. Second ask" in res1.output
    assert "Retrying: First ask" in res1.output

    hist_file = tmp_path / ".wintermute" / "chat_history.jsonl"
    assert hist_file.is_file()

    # Session 2 loads prior history into readline (Up-arrow recall) and /history
    turns.clear()
    res2 = runner.invoke(
        cli,
        ["chat", "--domain", "docs", "--no-worktree"],
        input="/history\n/retry\n/exit\n",
        obj=container,
    )
    assert res2.exit_code == 0, res2.output
    assert "Sent Message History" in res2.output
    assert "1. First ask" in res2.output
    assert "2. Second ask" in res2.output
    assert "Retrying: First ask" in res2.output
    assert any("First ask" in t["prompt"] for t in turns)

    # Verify readline buffer was populated in exact order for Up-arrow recall
    rl_items = [
        readline.get_history_item(i)
        for i in range(1, readline.get_current_history_length() + 1)
    ]
    assert rl_items == ["First ask", "Second ask", "First ask"]


def test_resume_and_retry_work_unit_service_repl_and_cli(tmp_path: Path) -> None:
    from app.domain.models import Task, WorkUnit

    container, _ = _make_test_container(tmp_path)
    asyncio.run(container.initialize())

    task = Task(
        id="task_wu_test",
        title="Update fstools",
        prompt="Update fstools",
        status=TaskStatus.FAILED,
        mode="single",
        domain="fstools",
        pipeline_template="tdd_coding",
    )
    # Pause at stage 2 (validate_green in tdd_coding) with exhausted gate attempts
    wu = WorkUnit(
        id="wu_resume_01",
        task_id=task.id,
        title="fstools: cli",
        domain="fstools",
        target="cli",
        pipeline_template="tdd_coding",
        prompt="Fix cli help",
        current_stage_idx=2,
        current_stage_name="validate_green",
        gate_attempts={"validate_green": 3},
        status="ESCALATED",
        summary="Gate failed previously",
    )
    asyncio.run(container.storage.upsert_task(task))
    asyncio.run(container.storage.upsert_work_unit(wu))

    # 1. Resume from stage 2 (skips stage 0 write_tests & stage 1 implement)
    res_resume = asyncio.run(
        container.single_agent_service.resume_work_unit(
            wu.id,
            prompt="Re-run validation now",
            use_worktree=False,
        )
    )
    assert res_resume.completed is True
    assert res_resume.task.status == TaskStatus.COMPLETED
    assert res_resume.stage_history[0] == "validate_green"
    assert "write_tests" not in res_resume.stage_history
    stored_wu = asyncio.run(container.storage.get_work_unit(wu.id))
    assert stored_wu is not None
    assert stored_wu.status == "COMPLETED"
    assert "Re-run validation now" in stored_wu.prompt

    # 2. Retry via CLI `exec --work-unit <id> --from-start` resets to stage 0
    runner = CliRunner()
    res_cli = runner.invoke(
        cli,
        ["exec", "--work-unit", wu.id, "--from-start", "--no-worktree"],
        obj=container,
    )
    assert res_cli.exit_code == 0, res_cli.output
    assert "Route: work_unit_retry" in res_cli.output
    assert "write_tests" in res_cli.output

    # 3. REPL `/wu-resume` & `/wu-retry` pick up WorkUnits on the active session task
    wu2 = WorkUnit(
        id="wu_resume_02",
        task_id=task.id,
        title="fstools: retry me",
        domain="fstools",
        target="pkg",
        pipeline_template="standard_change",
        prompt="Tidy pkg",
        current_stage_idx=1,
        current_stage_name="validate_1",
        status="ESCALATED",
    )
    asyncio.run(container.storage.upsert_work_unit(wu2))
    res_repl = runner.invoke(
        cli,
        ["chat", "--resume", task.id, "--no-worktree"],
        input="/wu-resume check again\n/wu-retry fresh pass\n/exit\n",
        obj=container,
    )
    assert res_repl.exit_code == 0, res_repl.output
    assert f"Resuming WorkUnit {wu2.id}" in res_repl.output
    assert f"Retrying from stage 0 WorkUnit {wu2.id}" in res_repl.output
    assert "Route: work_unit_resume" in res_repl.output
    assert "Route: work_unit_retry" in res_repl.output


def test_chat_cost_command_shows_limits_and_remaining(tmp_path: Path) -> None:
    usage = TokenUsage(
        prompt_tokens=100_000,
        cached_tokens=0,
        candidate_tokens=50_000,
        thoughts_tokens=0,
        total_tokens=150_000,
    )
    container, _ = _make_test_container(
        tmp_path,
        token_usage=usage,
        max_task_usd=2.00,
    )
    asyncio.run(container.initialize())
    max_daily = container.config.cost.policy.max_daily_usd

    runner = CliRunner()
    # 1. Before any active task: `/cost` still prints configured task and daily limits + remaining
    res_idle = runner.invoke(
        cli,
        ["chat", "--no-worktree"],
        input="/cost\n/exit\n",
        obj=container,
    )
    assert res_idle.exit_code == 0, res_idle.output
    assert "Cost & Budget" in res_idle.output
    assert "No active task yet." in res_idle.output
    assert "$0.0000 / $2.00 USD ($2.0000 left)" in res_idle.output
    assert f"$0.0000 / ${max_daily:.2f} USD (${max_daily:.4f} left)" in res_idle.output

    # 2. After running a turn and granting a top-up: `/cost` shows task + daily spent, limits, left, and top-up
    run_res = asyncio.run(
        container.single_agent_service.execute_prompt(
            "Check packages status", domain="packages"
        )
    )
    asyncio.run(container.single_agent_service.grant_task_budget(run_res.task.id, 1.50))
    total_usd, _, _ = asyncio.run(
        container.single_agent_service.get_task_cost_breakdown(run_res.task.id)
    )
    daily_spent = asyncio.run(container.storage.get_daily_usd_spent())
    assert total_usd > 0.0

    task_left = max(0.0, 3.50 - total_usd)
    daily_left = max(0.0, max_daily - daily_spent)

    res_active = runner.invoke(
        cli,
        ["chat", "--resume", run_res.task.id, "--no-worktree"],
        input="/cost\n/exit\n",
        obj=container,
    )
    assert res_active.exit_code == 0, res_active.output
    assert "Task Budget:" in res_active.output
    assert (
        f"${total_usd:.4f} / $3.50 USD (${task_left:.4f} left, +$1.50 top-up)"
        in res_active.output
    )
    assert "Daily Budget:" in res_active.output
    assert (
        f"${daily_spent:.4f} / ${max_daily:.2f} USD (${daily_left:.4f} left)"
        in res_active.output
    )


def test_cross_process_session_management_list_continue_resume_rename_delete(
    tmp_path: Path,
) -> None:
    # Process 1 container: create two distinct persisted sessions
    container1, seen1 = _make_test_container(tmp_path)
    asyncio.run(container1.initialize())
    res_s1 = asyncio.run(
        container1.single_agent_service.execute_prompt(
            "Check zlib upgrade options",
            domain="packages",
            use_worktree=False,
        )
    )
    res_s2 = asyncio.run(
        container1.single_agent_service.execute_prompt(
            "Inspect docs index",
            domain="docs",
            use_worktree=False,
        )
    )
    assert len(seen1) >= 2

    # Create a dummy SDK session file for session 2 to verify cleanup on delete
    container1.config.workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = (
        container1.config.workspace.sessions_dir / f"{res_s2.task.conversation_id}.db"
    )
    sdk_file.write_text("dummy-sdk-session", encoding="utf-8")

    # Process 2 container: fresh AppContainer pointing at the same SQLite DB & sessions dir
    container2, seen2 = _make_test_container(tmp_path)
    asyncio.run(container2.initialize())

    runner = CliRunner()

    # 1. `sessions list` shows both persisted sessions ordered most-recently-updated first
    res_list = runner.invoke(cli, ["sessions", "list"], obj=container2)
    assert res_list.exit_code == 0, res_list.output
    assert "Stored Wintermute Sessions" in res_list.output
    assert res_s1.task.id in res_list.output
    assert res_s2.task.id in res_list.output

    # 2. `sessions rename` renames session 1 by prefix and updates its position
    s1_prefix = res_s1.task.id[:10]
    res_rename = runner.invoke(
        cli, ["sessions", "rename", s1_prefix, "Zlib Audit Session"], obj=container2
    )
    assert res_rename.exit_code == 0, res_rename.output
    assert "Zlib Audit Session" in res_rename.output

    # 3. `sessions show` displays transcript & session metadata from previous process
    res_show = runner.invoke(cli, ["sessions", "show", res_s1.task.id], obj=container2)
    assert res_show.exit_code == 0, res_show.output
    assert "Resumed Session" in res_show.output
    assert "Zlib Audit Session" in res_show.output
    assert "Check zlib upgrade options" in res_show.output

    # 4. `chat --continue` automatically resumes the most recently updated session across process exit
    res_continue = runner.invoke(
        cli,
        ["chat", "--continue", "--no-worktree"],
        input="What version was found?\n/rename Zlib Final\n/sessions\n/exit\n",
        obj=container2,
    )
    assert res_continue.exit_code == 0, res_continue.output
    assert "Zlib Audit Session" in res_continue.output
    assert "Renamed session" in res_continue.output
    assert "Zlib Final" in res_continue.output
    assert any("What version was found?" in str(t.get("prompt", "")) for t in seen2)
    assert any(t.get("role") == AgentRole.TECH_LEAD for t in seen2)

    # Verify follow-up turns persisted cleanly in SQLite task_messages
    msgs = asyncio.run(container2.storage.list_task_messages(res_s1.task.id))
    assert len(msgs) >= 4
    assert any(m.sender == "user" and "What version was found?" in m.content for m in msgs)

    # 5. REPL `/resume` switches between sessions & `sessions resume` enters chat resumed
    res_switch = runner.invoke(
        cli,
        ["chat", "--no-worktree"],
        input=f"/resume {res_s2.task.id[:10]}\n/status\n/exit\n",
        obj=container2,
    )
    assert res_switch.exit_code == 0, res_switch.output
    assert res_s2.task.id in res_switch.output
    assert "Inspect docs index" in res_switch.output

    # 6. `sessions delete` removes SQLite records + SDK session DB file on disk
    res_delete = runner.invoke(cli, ["sessions", "delete", res_s2.task.id], obj=container2)
    assert res_delete.exit_code == 0, res_delete.output
    assert f"Deleted session {res_s2.task.id}" in res_delete.output
    assert asyncio.run(container2.storage.get_task(res_s2.task.id)) is None
    assert asyncio.run(container2.storage.list_task_messages(res_s2.task.id)) == []
    assert not sdk_file.exists()


def test_budget_command_updates_and_persists_daily_limit(tmp_path: Path) -> None:
    usage = TokenUsage(
        prompt_tokens=100_000,
        cached_tokens=0,
        candidate_tokens=50_000,
        thoughts_tokens=0,
        total_tokens=150_000,
    )
    container1, _ = _make_test_container(
        tmp_path,
        token_usage=usage,
        max_task_usd=5.00,
    )
    asyncio.run(container1.initialize())
    runner = CliRunner()

    # 1. `/budget daily` works even with no active task and reflects in `/cost`
    res_idle = runner.invoke(
        cli,
        ["chat", "--no-worktree"],
        input="/budget daily 0.05\n/cost\n/budget daily +0.01\n/cost\n/exit\n",
        obj=container1,
    )
    assert res_idle.exit_code == 0, res_idle.output
    assert "Updated daily limit to $0.05 USD" in res_idle.output
    assert "$0.0000 / $0.05 USD ($0.0500 left)" in res_idle.output
    assert "Increased daily limit by +$0.01 to $0.06 USD" in res_idle.output
    assert "$0.0000 / $0.06 USD ($0.0600 left)" in res_idle.output

    # 2. Daily limit override persists across process restart (fresh container initialization)
    container2, _ = _make_test_container(tmp_path, token_usage=usage, max_task_usd=5.00)
    asyncio.run(container2.initialize())
    assert container2.config.cost.policy.max_daily_usd == pytest.approx(0.06)

    # 3. Task paused on daily budget can be unpaused & resumed via `/budget daily +<amount>`
    paused = asyncio.run(
        container2.single_agent_service.execute_prompt(
            "Check docs", domain="docs", use_worktree=False
        )
    )
    assert paused.task.status == TaskStatus.PAUSED_BUDGET

    res_resume = runner.invoke(
        cli,
        ["chat", "--resume", paused.task.id, "--no-worktree"],
        input="/budget daily +25.00\n/exit\n",
        obj=container2,
    )
    assert res_resume.exit_code == 0, res_resume.output
    assert "Resuming paused task..." in res_resume.output
    assert "COMPLETED" in res_resume.output
    assert container2.config.cost.policy.max_daily_usd == pytest.approx(25.06)



