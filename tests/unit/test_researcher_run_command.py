"""Unit tests for Researcher run_command capability and mandatory approval policy."""

from pathlib import Path

import pytest

from app.adapters.outbound.antigravity_harness import AntigravityHarnessAdapter
from app.bootstrap.config import load_config
from app.domain.models import AgentRole


@pytest.mark.asyncio
async def test_researcher_run_command_allowed_only_with_approval(tmp_path: Path) -> None:
    from google.antigravity import policy, types

    repo_root = Path(__file__).resolve().parents[3]
    repo_cfg = load_config(workspace_root=repo_root)
    harness = AntigravityHarnessAdapter(config=repo_cfg)

    seen_prompts: list[str] = []

    async def _approve_cb(questions: list[dict[str, object]]) -> list[str]:
        seen_prompts.append(str(questions[0]["question"]))
        return ["Yes"]

    res_cfg = harness.build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=[],
        worktree_path=str(repo_root),
        interaction_callback=_approve_cb,
    )

    enabled = set(res_cfg.capabilities.enabled_tools or ())
    assert types.BuiltinTools.RUN_COMMAND in enabled
    assert types.BuiltinTools.EDIT_FILE not in enabled
    assert types.BuiltinTools.CREATE_FILE not in enabled

    # Researcher should not have block_raw_build_commands deny policy
    policy_names = {getattr(p, "name", "") for p in res_cfg.policies}
    assert "block_raw_build_commands" not in policy_names

    ask_policies = [
        p
        for p in res_cfg.policies
        if getattr(p, "tool", None) == "run_command"
        and getattr(p, "decision", None) == policy.Decision.ASK_USER
    ]
    assert len(ask_policies) == 1
    handler = ask_policies[0].ask_user
    assert handler is not None

    # Approval granted for arbitrary command (including pytest)
    call = types.ToolCall(name="run_command", args={"CommandLine": "pytest -q"})
    assert await handler(call, None) is True
    assert any("pytest -q" in q for q in seen_prompts)

    # Denial when user answers No
    async def _deny_cb(_questions: list[dict[str, object]]) -> list[str]:
        return ["No"]

    deny_cfg = harness.build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=[],
        worktree_path=str(repo_root),
        interaction_callback=_deny_cb,
    )
    deny_handler = next(
        p
        for p in deny_cfg.policies
        if getattr(p, "tool", None) == "run_command"
        and getattr(p, "decision", None) == policy.Decision.ASK_USER
    ).ask_user
    assert await deny_handler(call, None) is False

    # Denial when no interaction_callback is present (headless)
    headless_cfg = harness.build_sdk_agent_config(
        role=AgentRole.RESEARCHER,
        tools=[],
        worktree_path=str(repo_root),
        interaction_callback=None,
    )
    headless_handler = next(
        p
        for p in headless_cfg.policies
        if getattr(p, "tool", None) == "run_command"
        and getattr(p, "decision", None) == policy.Decision.ASK_USER
    ).ask_user
    assert await headless_handler(call, None) is False

