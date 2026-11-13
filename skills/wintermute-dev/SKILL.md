---
name: wintermute-dev
description: Extend and improve Wintermute itself by authoring skills, agent role files, pipeline templates, and tools.
---

# Wintermute Self-Improvement & Extension

Use this skill when modifying Wintermute (`wintermute/`), adding new skills, tuning agent roles, defining pipeline templates, or implementing new tools.

## 1. Skills (`wintermute/skills/<name>/SKILL.md` or `.wintermute/skills/`)
- Follow the `agentskills.io` standard: include YAML frontmatter with `name` (matching the folder name) and `description` only.
- Keep instructions concise and procedural; place domain-specific steps in skills rather than bloating base agent role files.

## 2. Agent Definitions (`wintermute/agents/<role>.md` & `wintermute/wintermute.toml`)
- Define each agent's persona, capabilities, skills, and tool policy in `wintermute/agents/<role>.md` (`.wintermute/agents/` overrides `wintermute/agents/`) using YAML frontmatter plus markdown instructions:
  - `name`: Role identifier matching `<role>`.
  - `description`: Role summary used for orchestrator routing.
  - `read_only`: Boolean disabling file edits, shell commands, and mutating `just_*` tools when `true`.
  - `skills`: List of skill names exposed to this role.
  - `allowed_subagents`: List of delegatable roles (for orchestrators such as `tech_lead`).
  - `allowed_tools`: Exact names, glob patterns (`"just_pkg_*"`), and selectors (`"@domain"`, `"@read_only"`).
  - `denied_tools`: Explicit names or glob patterns blocked from this agent (deny outranks allow).
- Keep runtime model selection and resource budgets (`model`, `max_model_calls`, `max_tool_calls`, `max_total_tokens`, optional `agent_file`) under `[agents.<role>]` in `wintermute/wintermute.toml`.
- Register new roles in `AgentRole` (`wintermute/app/domain/models.py`), `[agents.<role>]` in `wintermute/wintermute.toml`, and `tech_lead.md`'s `allowed_subagents` when delegatable.

## 3. Pipeline Templates (`[pipeline.templates.<name>]`)
- Compose workflows from `agent`, `gate`, and `commit` stages in `wintermute/wintermute.toml` using `require_file_edits`, `pass_git_diff`, `skip_next_gate_if_unmodified`, `run_only_on_failure`, `attach_build_logs`, `on_fail_goto`, and `on_complete_goto`.
- Map domain pipeline and test defaults under `[pipeline.domain_defaults]` and `[pipeline.domain_test_targets]`.

## 4. Implementing New Tools
- **Tier 1 (`just_*` synthesis, preferred for CLI/OS workflows):** Implement reusable CLI subcommands (e.g., in `fstools`), expose a documented recipe in `just/*.just`, and let `tech_lead` call `reload_just_tools()` once the work unit lands.
- **Tier 2 (Hexagonal core extensions):** Keep external I/O behind `typing.Protocol` ports in `wintermute/app/application/ports.py`, implement logic in `application/`, and add adapters in `adapters/outbound/`.
- Write `pytest` unit tests under `wintermute/tests/unit/` and verify with `just_wintermute_test`. Only `tech_lead` invokes `reload_just_tools()` and `reload_wintermute_config()` after work units complete.

