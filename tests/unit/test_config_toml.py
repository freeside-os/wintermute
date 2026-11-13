"""Unit tests for hierarchical TOML loading and pipeline template parsing."""

import os
from pathlib import Path

from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter
from app.bootstrap.config import load_config, parse_markdown_frontmatter
from app.domain.models import AgentRole, StageKind


def test_load_default_wintermute_toml():
    repo_root = Path(__file__).resolve().parents[3]
    cfg = load_config(workspace_root=repo_root)

    assert "tdd_coding" in cfg.pipeline.templates
    assert "package_lifecycle" in cfg.pipeline.templates
    assert "standard_change" in cfg.pipeline.templates
    assert "investigation_audit" in cfg.pipeline.templates

    assert cfg.pipeline.domain_defaults["packages"] == "package_lifecycle"
    assert cfg.pipeline.domain_defaults["straylight"] == "tdd_coding"
    assert cfg.pipeline.domain_defaults["docs"] == "standard_change"

    pkg_tpl = cfg.pipeline.templates["package_lifecycle"]
    assert pkg_tpl.max_retries_per_gate == 3
    stage_names = [s.name for s in pkg_tpl.stages]
    assert stage_names == [
        "scaffold_or_edit",
        "static_verify",
        "sandbox_build",
        "troubleshoot_build",
        "compliance_review",
        "reverify_after_review",
        "commit",
    ]
    assert pkg_tpl.stages[1].kind == StageKind.GATE
    assert pkg_tpl.stages[3].run_only_on_failure is True
    assert pkg_tpl.stages[6].capture_knowledge is True


def test_hierarchical_override_merges_and_saves(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    override_toml = tmp_path / ".wintermute" / "config.toml"
    override_toml.parent.mkdir(parents=True)
    override_toml.write_text(
        '[agents.junior_coder]\nmodel = "local_coder"\n[cost]\nmax_task_usd = 12.5\n',
        encoding="utf-8",
    )

    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=default_toml,
        override_toml_path=override_toml,
    )
    assert cfg.agents["junior_coder"].model == "local_coder"
    assert cfg.agents["senior_coder"].model == "hydrogen"
    assert cfg.cost.policy.max_task_usd == 12.5
    assert cfg.models["local_coder"].is_local is True

    cfg.save_override_dict({"cost": {"max_daily_usd": 40.0}})
    assert cfg.cost.policy.max_daily_usd == 40.0
    assert cfg.cost.policy.max_task_usd == 12.5


def test_agent_instructions_autoresolve_override_and_sdk_default(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[3]
    repo_cfg = load_config(workspace_root=repo_root)

    # 1. Auto-resolves wintermute/agents/<role>.md frontmatter + body when agent_file is omitted
    assert "coordinator" in repo_cfg.agents
    assert "troubleshooter" in repo_cfg.agents
    assert "doc_writer" in repo_cfg.agents
    assert "researcher" in repo_cfg.agents["coordinator"].allowed_subagents
    assert "escalate_to_tech_lead" in repo_cfg.agents["coordinator"].allowed_tools
    assert "dispatch_work_unit" in repo_cfg.agents["tech_lead"].allowed_tools
    assert repo_cfg.agents["tech_lead"].allowed_subagents == ()
    assert repo_cfg.agents["coordinator"].thinking_level == "low"
    assert repo_cfg.agents["tech_lead"].thinking_level == "medium"
    assert repo_cfg.agents["tech_lead"].compaction_token_threshold == 60_000
    assert repo_cfg.agents["researcher"].read_only is True
    assert "run_command" in repo_cfg.agents["researcher"].allowed_tools
    assert "run_command" not in repo_cfg.agents["researcher"].denied_tools
    assert "run_command" in repo_cfg.agents["doc_writer"].denied_tools
    troubleshooter_prompt = repo_cfg.resolve_agent_instructions("troubleshooter")
    assert troubleshooter_prompt is not None
    assert not troubleshooter_prompt.startswith("---")
    assert "Failure-Recovery" in troubleshooter_prompt

    doc_writer_prompt = repo_cfg.resolve_agent_instructions("doc_writer")
    assert doc_writer_prompt is not None
    assert "Technical Writer" in doc_writer_prompt

    assert repo_cfg.agents["troubleshooter"].skills == ("packages-troubleshooting", "cve-remediation")
    assert repo_cfg.agents["researcher"].skills == ("packages-audit",)

    # Verify agentskills.io strict frontmatter (only `name` + `description`)
    for skill_dir in sorted((repo_root / "wintermute" / "skills").iterdir()):
        if not skill_dir.is_dir():
            continue
        meta, body = parse_markdown_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        assert set(meta.keys()) == {"name", "description"}
        assert meta["name"] == skill_dir.name
        assert body

    # Skill resolution: role-declared skills vs explicit WorkUnit skills
    assert "packages-upgrade" in repo_cfg.skills
    assert "packages-troubleshooting" in repo_cfg.skills
    role_scoped = [
        s.name
        for s in repo_cfg.resolve_skills_for_stage(role="troubleshooter")
    ]
    assert role_scoped == ["packages-troubleshooting", "cve-remediation"]

    researcher_sdk = AntigravityHarnessAdapter(config=repo_cfg).build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=[],
        worktree_path=str(repo_root),
    )
    researcher_skills = (
        researcher_sdk["skills_paths"]
        if isinstance(researcher_sdk, dict)
        else list(researcher_sdk.skills_paths or ())
    )
    assert len(researcher_skills) == 1
    assert researcher_skills[0].endswith("/packages-audit")

    explicit_upgrade = [
        s.name
        for s in repo_cfg.resolve_skills_for_stage(
            role="troubleshooter",
            explicit_skills=["packages-upgrade"],
        )
    ]
    assert explicit_upgrade == ["packages-upgrade"]

    reviewer_prompt = repo_cfg.resolve_agent_instructions("reviewer")
    assert reviewer_prompt is not None
    assert not reviewer_prompt.startswith("---")
    assert "LGTM" in reviewer_prompt

    # 2. Local .wintermute/agents/<role>.md overrides wintermute/agents/<role>.md,
    #    and explicit agent_file overrides default <role>.md
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    (tmp_path / "wintermute" / "agents").mkdir(parents=True)
    (tmp_path / ".wintermute" / "agents").mkdir(parents=True)
    (tmp_path / "wintermute" / "agents" / "junior_coder.md").write_text(
        "Default junior coder", encoding="utf-8"
    )
    (tmp_path / ".wintermute" / "agents" / "junior_coder.md").write_text(
        "Override junior coder", encoding="utf-8"
    )
    (tmp_path / ".wintermute" / "agents" / "custom_senior.md").write_text(
        "Explicit custom senior", encoding="utf-8"
    )
    override_toml = tmp_path / ".wintermute" / "config.toml"
    override_toml.write_text(
        '[agents.senior_coder]\nagent_file = "custom_senior.md"\n',
        encoding="utf-8",
    )

    cfg = load_config(
        workspace_root=tmp_path,
        default_toml_path=default_toml,
        override_toml_path=override_toml,
    )
    assert cfg.resolve_agent_instructions("junior_coder") == "Override junior coder"
    assert cfg.resolve_agent_instructions("senior_coder") == "Explicit custom senior"

    # 3. Missing agent file -> returns None so SDK uses its built-in default
    assert cfg.resolve_agent_instructions("researcher") is None
    harness = AntigravityHarnessAdapter(config=cfg)
    sdk_cfg = harness.build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=[],
        worktree_path=str(tmp_path),
        read_only=True,
    )
    sys_inst = (
        sdk_cfg["system_instructions"]
        if isinstance(sdk_cfg, dict)
        else sdk_cfg.system_instructions
    )
    assert sys_inst is None


def test_sdk_capabilities_subagents_and_lightweight_openai(tmp_path: Path):
    from google.antigravity import types

    repo_root = Path(__file__).resolve().parents[3]
    repo_cfg = load_config(workspace_root=repo_root)
    harness = AntigravityHarnessAdapter(config=repo_cfg)

    # 1. coordinator enables navigation builtins + ask_question + researcher subagent
    async def _dummy_interact(_qs):
        return []

    coord_cfg = harness.build_sdk_agent_config(
        role=AgentRole.COORDINATOR,
        tools=[],
        worktree_path=str(repo_root),
        interaction_callback=_dummy_interact,
    )
    enabled = set(coord_cfg.capabilities.enabled_tools or ())
    assert types.BuiltinTools.LIST_DIR in enabled
    assert types.BuiltinTools.SEARCH_DIR in enabled
    assert types.BuiltinTools.FIND_FILE in enabled
    assert types.BuiltinTools.ASK_QUESTION in enabled
    assert types.BuiltinTools.INVOKE_SUBAGENT in enabled
    assert coord_cfg.capabilities.enable_subagents is True
    assert coord_cfg.capabilities.agent_behavior == types.AgentBehavior.INTERACTIVE
    subagent_names = {s.name for s in (coord_cfg.subagents or ())}
    assert subagent_names == {"researcher"}
    assert set(coord_cfg.capabilities.allowed_subagents or ()) == subagent_names
    assert coord_cfg.compaction_config.token_threshold == 60_000

    # tech_lead has zero SubagentConfig overhead, uses argon + compaction_config
    lead_cfg = harness.build_sdk_agent_config(
        role=AgentRole.TECH_LEAD,
        tools=[],
        worktree_path=str(repo_root),
        interaction_callback=_dummy_interact,
    )
    lead_enabled = set(lead_cfg.capabilities.enabled_tools or ())
    assert types.BuiltinTools.INVOKE_SUBAGENT not in lead_enabled
    assert lead_cfg.capabilities.enable_subagents is False
    assert lead_cfg.compaction_config.token_threshold == 60_000

    # 2. LocalOpenAIAgentConfig with lightweight=True preserves role-specific enabled_tools
    local_ro_cfg = harness.build_sdk_agent_config(
        role=AgentRole.SENIOR_CODER,
        tools=[],
        worktree_path=str(repo_root),
        read_only=True,
        model_override="local_coder",
    )
    ro_enabled = set(local_ro_cfg.capabilities.enabled_tools or ())
    assert types.BuiltinTools.LIST_DIR in ro_enabled
    assert types.BuiltinTools.EDIT_FILE not in ro_enabled
    assert types.BuiltinTools.CREATE_FILE not in ro_enabled


def test_dotenv_loads_gemini_api_key_without_clobbering_shell_env(
    tmp_path: Path, monkeypatch
):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    (tmp_path / ".env").write_text(
        'GEMINI_API_KEY="dotenv-key-123"\nWINTERMUTE_MAX_TASK_USD="9.75"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("WINTERMUTE_MAX_TASK_USD", raising=False)

    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    assert os.environ["GEMINI_API_KEY"] == "dotenv-key-123"
    assert cfg.cost.policy.max_task_usd == 9.75

    sdk_cfg = AntigravityHarnessAdapter(config=cfg).build_sdk_agent_config(
        role=AgentRole.SENIOR_CODER,
        tools=[],
        worktree_path=str(tmp_path),
    )
    if not isinstance(sdk_cfg, dict):
        assert sdk_cfg.api_key == "dotenv-key-123"

    # Existing shell env vars take precedence over .env values
    monkeypatch.setenv("GEMINI_API_KEY", "shell-override-key")
    cfg_reloaded = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    assert os.environ["GEMINI_API_KEY"] == "shell-override-key"
    sdk_cfg_reloaded = AntigravityHarnessAdapter(config=cfg_reloaded).build_sdk_agent_config(
        role=AgentRole.SENIOR_CODER,
        tools=[],
        worktree_path=str(tmp_path),
    )
    if not isinstance(sdk_cfg_reloaded, dict):
        assert sdk_cfg_reloaded.api_key == "shell-override-key"

