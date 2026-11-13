"""Unit tests for token optimization primitives, stage isolation, and coordinator routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.bootstrap.config import load_config
from app.domain.cost import TokenUsage
from app.domain.models import AgentRole, KnowledgeEntry, Task, WorkUnit
from app.domain.text_compaction import compact_command_output, compact_diff_output


def test_compact_command_output_preserves_head_errors_and_tail():
    short = "all good\n"
    assert compact_command_output(short, max_chars=100) == short

    head = "HEADER: build started\n" + ("noise line\n" * 300)
    mid = "error: undefined reference to `foo_bar`\n" + ("more noise\n" * 600)
    tail = "FINAL_SUMMARY: exit code 2\n"
    big = head + mid + tail

    compacted = compact_command_output(big, max_chars=4_000)
    assert len(compacted) <= 4_000
    assert "HEADER: build started" in compacted
    assert "undefined reference to `foo_bar`" in compacted
    assert "FINAL_SUMMARY: exit code 2" in compacted


def test_compact_diff_output_keeps_headers_and_respects_budget():
    diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
        + ("+added line of code\n" * 1500)
    )
    compacted = compact_diff_output(diff, max_chars=3_000)
    assert len(compacted) <= 3_000
    assert "[Touched files summary]" in compacted
    assert "diff --git a/foo.py b/foo.py" in compacted


@pytest.mark.asyncio
async def test_just_synthesizer_compacts_output_and_scopes_escalate_tool(tmp_path: Path):
    from app.adapters.outbound.just_synthesizer import JustToolSynthesizer

    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=repo_root / "wintermute" / "wintermute.toml",
    )

    def _dump(_ws: Path) -> dict[str, Any]:
        return {
            "recipes": {
                "pkg::verify": {
                    "doc": "Verify package [read-only]",
                    "parameters": [],
                }
            }
        }

    async def _runner(_cmd: list[str], _cwd: Path, _env: dict[str, str]):
        noisy = ("info: checking\n" * 1500) + "error: bad checksum\n"
        return 1, noisy, ""

    synth = JustToolSynthesizer(config=cfg, dump_provider=_dump, command_runner=_runner)

    coord_tools = synth.get_tools_for(AgentRole.COORDINATOR, domain="packages")
    coord_names = [fn.__name__ for fn in coord_tools]
    assert coord_names == sorted(coord_names)
    assert "escalate_to_tech_lead" in coord_names
    assert "dispatch_work_unit" in coord_names

    lead_names = [fn.__name__ for fn in synth.get_tools_for(AgentRole.TECH_LEAD, domain="packages")]
    assert "escalate_to_tech_lead" not in lead_names
    assert "dispatch_work_unit" in lead_names

    verify_fn = next(fn for fn in coord_tools if fn.__name__ == "just_pkg_verify")
    output = await verify_fn()
    assert output.startswith("[FAILED] just pkg::verify")
    assert "error: bad checksum" in output
    assert len(output) <= 12_100


def test_harness_thinking_override_compaction_and_conversation_isolation(tmp_path: Path):
    from google.antigravity import types

    from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter

    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=repo_root / "wintermute" / "wintermute.toml",
    )
    harness = AntigravityHarnessAdapter(config=cfg)

    def just_b():
        return "b"

    def just_a():
        return "a"

    coord_cfg = harness.build_sdk_agent_config(
        role=AgentRole.COORDINATOR,
        tools=[just_b, just_a],
        worktree_path=str(tmp_path),
        conversation_id="a" * 32,
    )
    assert [t.__name__ for t in coord_cfg.tools] == ["just_a", "just_b"]
    assert coord_cfg.compaction_config.token_threshold == 60_000
    assert coord_cfg.conversation_id == "a" * 32
    assert coord_cfg.models[0].endpoint.options.thinking_level == types.ThinkingLevel.LOW

    # Stage turn with conversation_id=None must NOT inherit task.conversation_id
    coder_cfg = harness.build_sdk_agent_config(
        role=AgentRole.JUNIOR_CODER,
        tools=[],
        worktree_path=str(tmp_path),
        conversation_id=None,
    )
    assert getattr(coder_cfg, "conversation_id", None) is None
    assert coder_cfg.models[0].endpoint.options.thinking_level == types.ThinkingLevel.LOW

    assert not hasattr(cfg.models["argon"], "thinking_level")
    # Explicit thinking_level_override overrides role config
    esc_cfg = harness.build_sdk_agent_config(
        role=AgentRole.TECH_LEAD,
        tools=[],
        worktree_path=str(tmp_path),
        thinking_level_override="high",
    )
    assert esc_cfg.models[0].endpoint.options.thinking_level == types.ThinkingLevel.HIGH


@pytest.mark.asyncio
async def test_pipeline_isolates_stage_conversation_and_scales_prime_limit(tmp_path: Path):
    from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter
    from app.adapters.outbound.just_synthesizer import JustToolSynthesizer
    from app.adapters.outbound.pricing_catalog import PricingCatalogAdapter
    from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
    from app.application.cost_service import CostController
    from app.application.knowledge_service import KnowledgeService
    from app.application.pipeline import WorkUnitPipelineEngine
    from app.application.ports import StageTurnResult

    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=repo_root / "wintermute" / "wintermute.toml",
    )
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()
    knowledge = KnowledgeService(config=cfg, storage=storage)
    for idx in range(4):
        await knowledge.storage.upsert_knowledge(
            KnowledgeEntry(
                id=f"know_{idx}",
                domain="docs",
                target="guide",
                issue=f"Issue {idx} " + ("x" * 400),
                resolution=f"Fix {idx} " + ("y" * 400),
                outcome="success",
                capture_type="explicit",
            )
        )

    seen_conv_ids: list[str | None] = []
    seen_prompts: list[str] = []

    class _FakeWorktree:
        async def ensure_worktree(self, task_id: str, *, use_worktree: bool = True):
            from app.domain.models import WorktreeInfo

            return WorktreeInfo(
                task_id=task_id,
                root_path=str(tmp_path),
                branch_name="main",
                build_root=str(tmp_path),
                materialized_sub_repos=(),
            )

        async def get_latest_mtime(self, task_id: str, domain: str | None = None) -> float:
            return 1.0

        async def get_git_diff(self, task_id: str, sub_repos=None) -> str:
            return ""

        async def commit_changes(self, task_id: str, message: str, sub_repos=None) -> str:
            return "abc123"

    async def _turn_runner(ctx: dict[str, Any]) -> StageTurnResult:
        seen_conv_ids.append(ctx["conversation_id"])
        seen_prompts.append(ctx["prompt"])
        role: AgentRole = ctx["role"]
        return StageTurnResult(
            response_text="LGTM" if role == AgentRole.REVIEWER else "Done",
            usage=TokenUsage(prompt_tokens=50, candidate_tokens=20, total_tokens=70),
            files_modified=role != AgentRole.REVIEWER,
        )

    harness = AntigravityHarnessAdapter(config=cfg, turn_runner=_turn_runner)
    pricing = PricingCatalogAdapter(config=cfg, storage=storage, feed_fetcher=lambda _u: {})
    cost = CostController(config=cfg, storage=storage, pricing_catalog=pricing)

    def _dump(_ws: Path) -> dict[str, Any]:
        return {"recipes": {"git::status": {"doc": "Status [read-only]", "parameters": []}}}

    just = JustToolSynthesizer(
        config=cfg,
        dump_provider=_dump,
        command_runner=lambda _c, _w, _e: _ok_cmd(),
    )

    async def _ok_cmd():
        return 0, "ok", ""

    engine = WorkUnitPipelineEngine(
        config=cfg,
        storage=storage,
        harness=harness,
        worktree=_FakeWorktree(),
        just_tools=just,
        cost_controller=cost,
        knowledge=knowledge,
    )

    task = Task(
        id="task_1",
        title="Docs update",
        prompt="Update guide",
        conversation_id="c" * 32,
        domain="docs",
    )
    await storage.upsert_task(task)
    wu = WorkUnit(
        id="wu_1",
        task_id=task.id,
        title="Update guide",
        domain="docs",
        target="guide",
        pipeline_template="standard_change",
        complexity="simple",
        prompt="Update guide",
    )
    await storage.upsert_work_unit(wu)

    result = await engine.execute_work_unit(task, wu, use_worktree=False)
    assert result.completed is True
    assert seen_conv_ids and all(cid is None for cid in seen_conv_ids)
    assert task.conversation_id == "c" * 32
    # Simple complexity caps primed lessons to 2 entries and clips long strings
    lesson_lines = [ln for ln in seen_prompts[0].splitlines() if ln.startswith("- [docs/guide]")]
    assert len(lesson_lines) == 2
    assert all("…" in ln for ln in lesson_lines)


@pytest.mark.asyncio
async def test_coordinator_escalate_and_pipeline_escalation_thinking_levels(tmp_path: Path):
    from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
    from app.application.ports import StageTurnResult
    from app.bootstrap.container import build_container

    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=repo_root / "wintermute" / "wintermute.toml",
    )
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()
    usage = TokenUsage(prompt_tokens=100, candidate_tokens=50, total_tokens=150)

    recorded: list[dict[str, Any]] = []
    lead_calls = 0

    async def _turn_runner(ctx: dict[str, Any]) -> StageTurnResult:
        nonlocal lead_calls
        role: AgentRole = ctx["role"]
        recorded.append(
            {
                "role": role,
                "thinking": ctx["thinking_level_override"],
                "conv_id": ctx["conversation_id"],
            }
        )
        if role == AgentRole.COORDINATOR:
            for tool in ctx["tools"]:
                if getattr(tool, "__name__", "") == "escalate_to_tech_lead":
                    msg = await tool(brief="Gathered facts: update zlib", domain="packages", target="zlib")
                    return StageTurnResult(response_text=msg, usage=usage, files_modified=False)
        if role == AgentRole.TECH_LEAD:
            lead_calls += 1
            if lead_calls == 1:
                for tool in ctx["tools"]:
                    if getattr(tool, "__name__", "") == "dispatch_work_unit":
                        await tool(domain="packages", target="zlib", prompt="Fix zlib")
                        break
                return StageTurnResult(response_text="Dispatched zlib.", usage=usage, files_modified=False)
            return StageTurnResult(response_text="Manual intervention needed.", usage=usage, files_modified=False)
        if role == AgentRole.REVIEWER:
            return StageTurnResult(response_text="LGTM", usage=usage, files_modified=False)
        return StageTurnResult(response_text="Edited", usage=usage, files_modified=True)

    async def _fail_pkg_cmd(_cmd: list[str], _cwd: Path, _env: dict[str, str]):
        return 1, "", "error: build failed"

    def _dump(_ws: Path) -> dict[str, Any]:
        return {
            "recipes": {
                "pkg::verify": {"doc": "Verify [read-only]", "parameters": []},
                "pkg::resolve": {"doc": "Resolve [read-only]", "parameters": []},
                "pkg::build": {"doc": "Build", "parameters": []},
                "pkg::logs": {"doc": "Logs [read-only]", "parameters": []},
            }
        }

    container = build_container(
        config=cfg,
        turn_runner=_turn_runner,
        command_runner=_fail_pkg_cmd,
        dump_provider=_dump,
        feed_fetcher=lambda _u: {},
    )
    await container.initialize()

    res = await container.single_agent_service.execute_prompt(
        "Update zlib across packages", use_worktree=False
    )
    assert "coordinator_turn" in res.stage_history
    assert "tech_lead_turn" in res.stage_history
    assert "tech_lead_escalation" in res.stage_history

    coord_turn = next(r for r in recorded if r["role"] == AgentRole.COORDINATOR)
    assert coord_turn["conv_id"] == res.task.conversation_id
    assert coord_turn["thinking"] is None

    lead_turns = [r for r in recorded if r["role"] == AgentRole.TECH_LEAD]
    assert len(lead_turns) == 2
    # Decomposition uses tech_lead's configured medium thinking + isolated context
    assert lead_turns[0]["thinking"] is None
    assert lead_turns[0]["conv_id"] is None
    # Pipeline failure escalation overrides thinking_level to high
    assert lead_turns[1]["thinking"] == "high"
    assert lead_turns[1]["conv_id"] is None
