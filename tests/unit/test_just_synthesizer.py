"""Unit tests for JustToolSynthesizer AST parsing, scoping, and build semaphores."""

import asyncio
import inspect
from pathlib import Path

import pytest

from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter
from app.adapters.outbound.just_synthesizer import JustToolSynthesizer
from app.adapters.outbound.worktree_manager import WorktreeManagerAdapter
from app.bootstrap.config import load_config
from app.domain.models import AgentRole

SAMPLE_JUST_DUMP = {
    "recipes": {},
    "modules": {
        "pkg": {
            "recipes": {
                "verify": {
                    "name": "verify",
                    "doc": "Verify package manifest and justfile",
                    "parameters": [{"name": "pkg_name", "default": None}],
                },
                "create": {
                    "name": "create",
                    "doc": "Create a new package skeleton",
                    "parameters": [
                        {"name": "pkg_name", "default": None},
                        {"name": "version", "default": ["string", "1.0.0"]},
                    ],
                },
                "build": {
                    "name": "build",
                    "doc": "Build a package inside systemd-nspawn",
                    "parameters": [{"name": "pkg_name", "default": None}],
                },
            }
        },
        "straylight": {
            "recipes": {
                "test": {
                    "name": "test",
                    "doc": "Run straylight tests",
                    "parameters": [],
                }
            }
        },
        "git": {
            "recipes": {
                "status": {
                    "name": "status",
                    "doc": "Show git status across repos",
                    "parameters": [],
                }
            }
        },
    },
}


def test_just_ast_synthesis_and_role_domain_scoping(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    synth = JustToolSynthesizer(
        config=cfg, dump_provider=lambda _: SAMPLE_JUST_DUMP
    )

    recipes = synth.discover_recipes()
    assert "pkg::create" in recipes
    assert recipes["pkg::verify"].read_only is True
    assert recipes["pkg::build"].read_only is False

    # Researcher only sees read-only tools
    researcher_names = {t.__name__ for t in synth.get_tools_for(AgentRole.RESEARCHER)}
    assert researcher_names == {"just_pkg_verify", "just_git_status"}

    # Junior coder on packages sees pkg::* + git::* (no straylight::test and no reload tools)
    coder_tools = synth.get_tools_for(AgentRole.JUNIOR_CODER, domain="packages")
    coder_map = {t.__name__: t for t in coder_tools}
    assert "just_pkg_create" in coder_map
    assert "just_pkg_build" in coder_map
    assert "just_git_status" in coder_map
    assert "reload_just_tools" not in coder_map
    assert "reload_wintermute_config" not in coder_map
    assert "just_straylight_test" not in coder_map

    # Check inspect.Signature generation
    sig = inspect.signature(coder_map["just_pkg_create"])
    assert list(sig.parameters.keys()) == ["pkg_name", "version"]
    assert sig.parameters["pkg_name"].default is inspect.Parameter.empty
    assert sig.parameters["version"].default == "1.0.0"

    # Tech lead sees read-only discovery tools + dispatch_work_unit + reload tools
    lead_tools = synth.get_tools_for(AgentRole.TECH_LEAD)
    lead_map = {t.__name__: t for t in lead_tools}
    lead_names = set(lead_map)
    assert "dispatch_work_unit" in lead_names
    assert "reload_just_tools" in lead_names
    assert "reload_wintermute_config" in lead_names
    assert "just_pkg_build" not in lead_names

    # Verify reload tools callable from Tech Lead update config & recipes
    assert "Reloaded" in lead_map["reload_just_tools"]()
    override_dir = tmp_path / ".wintermute"
    override_dir.mkdir(parents=True, exist_ok=True)
    (override_dir / "config.toml").write_text(
        "[concurrency.build_locks]\n\"pkg::build\" = 2\n",
        encoding="utf-8",
    )
    reload_msg = lead_map["reload_wintermute_config"]()
    assert "Reloaded config:" in reload_msg
    assert cfg.concurrency.build_locks["pkg::build"] == 2


@pytest.mark.asyncio
async def test_build_lock_semaphore_and_pre_build_materialization(tmp_path: Path):
    (tmp_path / "packages" / "zlib").mkdir(parents=True)
    (tmp_path / "fstools").mkdir(parents=True)
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    worktree = WorktreeManagerAdapter(config=cfg, btrfs_detector=lambda _: False)

    active = 0
    max_concurrent = 0

    async def slow_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.03)
        active -= 1
        return 0, f"built in {cwd}", ""

    synth = JustToolSynthesizer(
        config=cfg,
        worktree=worktree,
        dump_provider=lambda _: SAMPLE_JUST_DUMP,
        command_runner=slow_runner,
    )

    res1, res2 = await asyncio.gather(
        synth.run_validator("pkg::build", task_id="t1", domain="packages", target="zlib"),
        synth.run_validator("pkg::build", task_id="t2", domain="packages", target="zlib"),
    )
    assert res1[0] is True and res2[0] is True
    assert max_concurrent == 1

    wt1 = await worktree.ensure_worktree("t1")
    assert "packages" in wt1.materialized_sub_repos
    assert "fstools" in wt1.materialized_sub_repos


def test_agent_frontmatter_tool_allow_and_deny_scoping(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    override_agents = tmp_path / ".wintermute" / "agents"
    override_agents.mkdir(parents=True)
    (override_agents / "junior_coder.md").write_text(
        """---
description: Restricted package-verify junior coder
read_only: false
allowed_tools:
  - just_pkg_verify
  - just_git_*
  - search_memory
denied_tools:
  - just_pkg_build
  - run_command
---

Only verify manifests cleanly.
""",
        encoding="utf-8",
    )

    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    synth = JustToolSynthesizer(config=cfg, dump_provider=lambda _: SAMPLE_JUST_DUMP)
    junior_tools = synth.get_tools_for(AgentRole.JUNIOR_CODER, domain="packages")
    junior_names = {t.__name__ for t in junior_tools}
    assert junior_names == {"just_pkg_verify", "just_git_status"}
    assert "just_pkg_build" not in junior_names

    def _extra_disallowed() -> str:
        return "nope"

    def _extra_memory() -> str:
        return "ok"

    _extra_disallowed.__name__ = "run_command"
    _extra_memory.__name__ = "search_memory"

    harness = AntigravityHarnessAdapter(config=cfg)
    sdk_cfg = harness.build_sdk_agent_config(
        role=AgentRole.JUNIOR_CODER,
        tools=[*junior_tools, _extra_memory, _extra_disallowed],
        worktree_path=str(tmp_path),
    )
    if isinstance(sdk_cfg, dict):
        tool_names = set(sdk_cfg["tools"])
        assert "just_pkg_build" in sdk_cfg["denied_tools"]
        assert "run_command" in sdk_cfg["denied_tools"]
        sys_inst = sdk_cfg["system_instructions"]
    else:
        tool_names = {getattr(t, "__name__", str(t)) for t in (sdk_cfg.tools or ())}
        assert "just_pkg_build" in cfg.agents["junior_coder"].denied_tools
        assert "run_command" in cfg.agents["junior_coder"].denied_tools
        assert len(sdk_cfg.policies) >= 2
        sys_inst = sdk_cfg.system_instructions
    assert "run_command" not in tool_names
    assert "search_memory" in tool_names
    assert "just_pkg_verify" in tool_names
    assert sys_inst == "Only verify manifests cleanly."


@pytest.mark.asyncio
async def test_positional_args_preserved_and_verify_all_fallback(tmp_path: Path):
    dump_payload = {
        "recipes": {},
        "modules": {
            "pkg": {
                "recipes": {
                    "create": {
                        "name": "create",
                        "doc": "Create package",
                        "parameters": [
                            {"name": "name", "default": None},
                            {"name": "version", "default": ""},
                            {"name": "url", "default": ""},
                            {"name": "prompt", "default": ""},
                        ],
                    },
                    "verify": {
                        "name": "verify",
                        "doc": "Verify single package",
                        "parameters": [{"name": "target", "default": None}],
                    },
                    "build": {
                        "name": "build",
                        "doc": "Build package",
                        "parameters": [{"name": "pkg_name", "default": None}],
                    },
                    "verify-all": {
                        "name": "verify-all",
                        "doc": "Verify all packages",
                        "parameters": [],
                    },
                }
            }
        },
    }
    recorded_cmds: list[list[str]] = []

    async def fake_runner(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
        recorded_cmds.append(cmd)
        return 0, "ok", ""

    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    synth = JustToolSynthesizer(
        config=cfg,
        dump_provider=lambda _: dump_payload,
        command_runner=fake_runner,
    )
    tools = {t.__name__: t for t in synth.get_tools_for(AgentRole.SENIOR_CODER, domain="packages")}
    await tools["just_pkg_create"](name="htop", version="", url="", prompt="cli monitor")
    assert recorded_cmds[-1][1:] == ["pkg::create", "htop", "", "", "cli monitor"]

    # Parameter alias resolution: pkg_name <-> target
    await tools["just_pkg_verify"](pkg_name="zlib")
    assert recorded_cmds[-1][1:] == ["pkg::verify", "zlib"]

    await tools["just_pkg_build"](target="zlib")
    assert recorded_cmds[-1][1:] == ["pkg::build", "zlib"]

    ok, _ = await synth.run_validator("pkg::verify", task_id="t1", domain="packages", target=None)
    assert ok is True
    assert recorded_cmds[-1][1:] == ["pkg::verify-all"]


@pytest.mark.asyncio
async def test_sdk_policy_evaluation_and_hooks(tmp_path: Path):
    from google.antigravity import types
    from google.antigravity.hooks.hooks import HookContext
    from google.antigravity.hooks.policy import _PolicyDecideHook

    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    synth = JustToolSynthesizer(config=cfg, dump_provider=lambda _: SAMPLE_JUST_DUMP)
    harness = AntigravityHarnessAdapter(config=cfg)

    senior_tools = synth.get_tools_for(AgentRole.SENIOR_CODER, domain="packages")
    pre_written: list[str] = []
    asked_questions: list[list[dict]] = []

    async def _on_pre_write(path: str) -> None:
        pre_written.append(path)

    async def _on_interaction(qs: list[dict]) -> list[str]:
        asked_questions.append(qs)
        return ["Option A" for _ in qs]

    senior_cfg = harness.build_sdk_agent_config(
        role=AgentRole.SENIOR_CODER,
        tools=senior_tools,
        worktree_path=str(tmp_path),
        pre_write_callback=_on_pre_write,
        interaction_callback=_on_interaction,
    )
    assert (
        senior_cfg.models[0].endpoint.options.thinking_level
        == types.ThinkingLevel.MEDIUM
    )
    decide_hook = _PolicyDecideHook(senior_cfg.policies)
    ctx = HookContext()

    allow_just = await decide_hook.run(ctx, types.ToolCall(name="just_pkg_verify", args={"pkg_name": "zlib"}))
    assert allow_just.allow is True

    allow_builtin = await decide_hook.run(ctx, types.ToolCall(name="list_directory", args={"path": str(tmp_path)}))
    assert allow_builtin.allow is True

    deny_raw = await decide_hook.run(ctx, types.ToolCall(name="run_command", args={"CommandLine": "cargo test"}))
    assert deny_raw.allow is False

    deny_unlisted = await decide_hook.run(ctx, types.ToolCall(name="unlisted_custom_tool", args={}))
    assert deny_unlisted.allow is False

    # Read-only stage denies writes & shell commands even for coder roles
    ro_cfg = harness.build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=synth.get_tools_for(AgentRole.RESEARCHER, domain="packages"),
        worktree_path=str(tmp_path),
        read_only=True,
    )
    ro_hook = _PolicyDecideHook(ro_cfg.policies)
    assert (await ro_hook.run(ctx, types.ToolCall(name="edit_file", args={}))).allow is False
    assert (await ro_hook.run(ctx, types.ToolCall(name="create_file", args={}))).allow is False
    assert (await ro_hook.run(ctx, types.ToolCall(name="run_command", args={"CommandLine": "ls"}))).allow is False

    # Verify @hooks.pre_tool_call_decide and @hooks.on_interaction callbacks fire cleanly
    from google.antigravity.hooks.hooks import OnInteractionHook, PreToolCallDecideHook

    pre_hook = next(h for h in senior_cfg.hooks if isinstance(h, PreToolCallDecideHook))
    res_pre = await pre_hook.run(ctx, types.ToolCall(name="edit_file", args={"path": "packages/zlib/package.manifest"}))
    assert res_pre.allow is True
    assert pre_written == ["packages/zlib/package.manifest"]

    inter_hook = next(h for h in senior_cfg.hooks if isinstance(h, OnInteractionHook))
    spec = types.AskQuestionInteractionSpec(
        questions=[
            types.AskQuestionEntry(
                question="Proceed with static build?",
                options=[types.AskQuestionOption(id="a", text="Option A")],
            )
        ]
    )
    res_inter = await inter_hook.run(ctx, spec)
    assert res_inter.cancelled is False
    assert res_inter.responses[0].freeform_response == "Option A"
    assert len(asked_questions) == 1

