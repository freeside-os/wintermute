"""Unit tests for WorkUnitPipelineEngine across all pluggable pipeline templates."""

from pathlib import Path

import pytest

from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter
from app.adapters.outbound.just_synthesizer import JustToolSynthesizer
from app.adapters.outbound.pricing_catalog import PricingCatalogAdapter
from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
from app.adapters.outbound.worktree_manager import WorktreeManagerAdapter
from app.application.cost_service import CostController
from app.application.knowledge_service import KnowledgeService
from app.application.pipeline import WorkUnitPipelineEngine
from app.application.ports import StageTurnResult
from app.bootstrap.config import load_config
from app.domain.cost import TokenUsage
from app.domain.models import AgentRole, Task, TaskStatus, WorkUnit


async def _setup_engine(tmp_path: Path, turn_runner, command_runner):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    (tmp_path / "straylight").mkdir(parents=True, exist_ok=True)
    (tmp_path / "packages" / "zlib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)

    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(db_path=cfg.workspace.db_path)
    await storage.initialize()

    pricing = PricingCatalogAdapter(config=cfg, storage=storage)
    cost = CostController(config=cfg, storage=storage, pricing_catalog=pricing)
    knowledge = KnowledgeService(config=cfg, storage=storage)
    worktree = WorktreeManagerAdapter(
        config=cfg, btrfs_detector=lambda _: False
    )

    fake_dump = {
        "recipes": {},
        "modules": {
            "pkg": {
                "recipes": {
                    "verify": {"name": "verify", "parameters": [{"name": "pkg_name"}], "doc": "verify"},
                    "resolve": {"name": "resolve", "parameters": [{"name": "pkg_name"}], "doc": "resolve"},
                    "build": {"name": "build", "parameters": [{"name": "pkg_name"}], "doc": "build"},
                    "logs": {"name": "logs", "parameters": [{"name": "pkg_name"}], "doc": "logs"},
                }
            },
            "straylight": {
                "recipes": {
                    "test": {"name": "test", "parameters": [], "doc": "test"},
                }
            },
            "git": {
                "recipes": {
                    "status": {"name": "status", "parameters": [], "doc": "status"},
                }
            },
        },
    }
    just_tools = JustToolSynthesizer(
        config=cfg,
        worktree=worktree,
        dump_provider=lambda _: fake_dump,
        command_runner=command_runner,
    )
    harness = AntigravityHarnessAdapter(config=cfg, turn_runner=turn_runner)

    engine = WorkUnitPipelineEngine(
        config=cfg,
        storage=storage,
        harness=harness,
        worktree=worktree,
        just_tools=just_tools,
        cost_controller=cost,
        knowledge=knowledge,
    )
    return cfg, storage, worktree, knowledge, engine


@pytest.mark.asyncio
async def test_tdd_coding_pipeline_skips_revalidation_when_reviewer_lgtm(tmp_path: Path):
    executed_validators: list[str] = []
    roles_seen: list[AgentRole] = []

    async def turn_runner(ctx: dict) -> StageTurnResult:
        roles_seen.append(ctx["role"])
        prompt = ctx["prompt"]
        # Reviewer makes 0 edits -> skip validate_refactor
        if ctx["role"] == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM",
                usage=TokenUsage(prompt_tokens=100, candidate_tokens=20, total_tokens=120),
                files_modified=False,
            )
        # Coder modifies file in worktree
        wt_file = Path(ctx["worktree_path"]) / "straylight" / "lib.rs"
        wt_file.parent.mkdir(parents=True, exist_ok=True)
        wt_file.write_text(f"// {prompt[:20]}\n", encoding="utf-8")
        return StageTurnResult(
            response_text="Updated files",
            usage=TokenUsage(prompt_tokens=200, candidate_tokens=50, total_tokens=250),
            files_modified=True,
        )

    async def cmd_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        executed_validators.append(cmd[1])
        return 0, "ok", ""

    _, storage, _, _, engine = await _setup_engine(tmp_path, turn_runner, cmd_runner)
    task = Task(id="t_tdd", title="Add flag", prompt="Implement flag", status=TaskStatus.RUNNING)
    await storage.upsert_task(task)
    wu = WorkUnit(
        id="wu_1",
        task_id=task.id,
        title="Add flag",
        domain="straylight",
        target="straylight",
        pipeline_template="tdd_coding",
        complexity="complex",
    )

    result = await engine.execute_work_unit(task, wu)
    assert result.completed is True
    assert result.escalated_to_tech_lead is False
    # validate_refactor was skipped because reviewer made 0 edits
    assert executed_validators == ["straylight::test"]
    assert roles_seen == [
        AgentRole.SENIOR_CODER,
        AgentRole.SENIOR_CODER,
        AgentRole.REVIEWER,
    ]
    assert "validate_refactor" not in result.stage_history


@pytest.mark.asyncio
async def test_package_lifecycle_troubleshoot_loop_and_knowledge_capture(tmp_path: Path):
    build_calls = 0
    roles_seen: list[AgentRole] = []

    async def turn_runner(ctx: dict) -> StageTurnResult:
        roles_seen.append(ctx["role"])
        if ctx["role"] == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM",
                usage=TokenUsage(prompt_tokens=150, candidate_tokens=30, total_tokens=180),
                files_modified=False,
            )
        wt_pkg = Path(ctx["worktree_path"]) / "packages" / "zlib" / "package.manifest"
        wt_pkg.parent.mkdir(parents=True, exist_ok=True)
        wt_pkg.write_text("[package]\nname='zlib'\n", encoding="utf-8")
        if "troubleshoot_build" in ctx["prompt"]:
            assert ctx["role"] == AgentRole.TROUBLESHOOTER
            assert "undefined reference to foo" in ctx["prompt"]
            assert "Extracted Package Build Logs (zlib):" in ctx["prompt"]
            assert "error: musl missing header <sys/cdefs.h>" in ctx["prompt"]
            return StageTurnResult(
                response_text="Added -lfoo to LDFLAGS",
                usage=TokenUsage(prompt_tokens=300, candidate_tokens=80, total_tokens=380),
                files_modified=True,
            )
        return StageTurnResult(
            response_text="Updated manifest",
            usage=TokenUsage(prompt_tokens=150, candidate_tokens=30, total_tokens=180),
            files_modified=True,
        )

    async def cmd_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        nonlocal build_calls
        recipe = cmd[1]
        if recipe == "pkg::build":
            build_calls += 1
            if build_calls == 1:
                return 1, "", "ld: undefined reference to foo"
        if recipe == "pkg::logs":
            return 0, "error: musl missing header <sys/cdefs.h>", ""
        return 0, "ok", ""

    _, storage, _, knowledge, engine = await _setup_engine(
        tmp_path, turn_runner, cmd_runner
    )
    task = Task(id="t_pkg", title="Fix zlib", prompt="Fix zlib build", status=TaskStatus.RUNNING)
    await storage.upsert_task(task)
    wu = WorkUnit(
        id="wu_pkg",
        task_id=task.id,
        title="Fix zlib",
        domain="packages",
        target="zlib",
        pipeline_template="package_lifecycle",
        complexity="simple",
    )

    result = await engine.execute_work_unit(task, wu)
    assert result.completed is True
    assert "troubleshoot_build" in result.stage_history
    assert AgentRole.TROUBLESHOOTER in roles_seen
    assert build_calls == 2

    notes = await knowledge.search("zlib", domain="packages")
    assert len(notes) >= 1


@pytest.mark.asyncio
async def test_investigation_audit_and_standard_change_and_escalation(tmp_path: Path):
    async def turn_runner(ctx: dict) -> StageTurnResult:
        if ctx["read_only"]:
            return StageTurnResult(
                response_text="Audit complete: 0 CVEs",
                usage=TokenUsage(prompt_tokens=100, candidate_tokens=40, total_tokens=140),
                files_modified=False,
            )
        return StageTurnResult(
            response_text="No edits made",
            usage=TokenUsage(prompt_tokens=50, candidate_tokens=10, total_tokens=60),
            files_modified=False,
        )

    async def cmd_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        return 0, "ok", ""

    _, storage, _, _, engine = await _setup_engine(tmp_path, turn_runner, cmd_runner)

    # 1. Read-only investigation_audit completes without file edits or commits
    audit_task = Task(id="t_audit", title="Audit", prompt="Audit packages", status=TaskStatus.RUNNING)
    await storage.upsert_task(audit_task)
    audit_wu = WorkUnit(
        id="wu_audit",
        task_id=audit_task.id,
        title="Audit",
        domain="packages",
        target="all",
        pipeline_template="investigation_audit",
    )
    audit_res = await engine.execute_work_unit(audit_task, audit_wu)
    assert audit_res.completed is True
    assert audit_res.stage_history == ["research", "synthesize"]

    # 2. standard_change on docs resolves coder stage to doc_writer and escalates if no edits made
    roles_seen: list[AgentRole] = []
    orig_runner = turn_runner

    async def tracking_runner(ctx: dict) -> StageTurnResult:
        roles_seen.append(ctx["role"])
        return await orig_runner(ctx)

    harness_orig = engine.harness
    engine.harness = AntigravityHarnessAdapter(
        config=engine.config, turn_runner=tracking_runner
    )
    std_task = Task(id="t_std", title="Docs", prompt="Update docs", status=TaskStatus.RUNNING)
    await storage.upsert_task(std_task)
    std_wu = WorkUnit(
        id="wu_std",
        task_id=std_task.id,
        title="Docs",
        domain="docs",
        target="guide",
        pipeline_template="standard_change",
    )
    std_res = await engine.execute_work_unit(std_task, std_wu)
    engine.harness = harness_orig
    assert AgentRole.DOC_WRITER in roles_seen
    assert std_res.completed is False
    assert std_res.escalated_to_tech_lead is True


@pytest.mark.asyncio
async def test_hybrid_skill_binding_and_stage_prompt_injection(tmp_path: Path):
    prompts_seen: list[str] = []

    async def turn_runner(ctx: dict) -> StageTurnResult:
        prompts_seen.append(ctx["prompt"])
        if ctx["role"] == AgentRole.REVIEWER:
            return StageTurnResult(
                response_text="LGTM",
                usage=TokenUsage(prompt_tokens=80, candidate_tokens=10, total_tokens=90),
                files_modified=False,
            )
        wt_pkg = Path(ctx["worktree_path"]) / "packages" / "zlib" / "package.manifest"
        wt_pkg.parent.mkdir(parents=True, exist_ok=True)
        wt_pkg.write_text("[package]\nname='zlib'\nversion='1.3.1'\n", encoding="utf-8")
        return StageTurnResult(
            response_text="Upgraded zlib to 1.3.1",
            usage=TokenUsage(prompt_tokens=120, candidate_tokens=20, total_tokens=140),
            files_modified=True,
        )

    async def cmd_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        return 0, "ok", ""

    _, storage, _, _, engine = await _setup_engine(tmp_path, turn_runner, cmd_runner)
    task = Task(id="t_upg", title="Upgrade zlib", prompt="Bump zlib", status=TaskStatus.RUNNING)
    await storage.upsert_task(task)
    wu = WorkUnit(
        id="wu_upg",
        task_id=task.id,
        title="Upgrade zlib",
        domain="packages",
        target="zlib",
        skills=["packages-upgrade"],
    )

    result = await engine.execute_work_unit(task, wu)
    assert result.completed is True
    assert wu.domain == "packages"
    assert wu.pipeline_template == "package_lifecycle"
    assert prompts_seen
    assert "### Skill: packages-upgrade" in prompts_seen[0]
    assert "just_pkg_checksum" in prompts_seen[0]

    reloaded = await storage.get_work_unit("wu_upg")
    assert reloaded is not None
    assert reloaded.skills == ["packages-upgrade"]
