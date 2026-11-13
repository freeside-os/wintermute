# Wintermute Agentic Platform

Wintermute is the autonomous AI engineering platform and orchestrator for Freeside OS, built on the **Google Antigravity SDK** (`google-antigravity`) using a hexagonal (ports-and-adapters) architecture.

---

## Architecture & Project Structure

```text
wintermute/
├── agents/                        # Role definitions (YAML frontmatter + instructions)
├── skills/                        # Domain & workflow skills (*/SKILL.md)
├── app/
│   ├── domain/                    # Core entities, WorkUnit pipelines, token cost math
│   ├── application/               # Stage-gate pipeline engine, cost controller, knowledge service, single-agent routing, ports
│   ├── adapters/
│   │   ├── inbound/cli/           # Click CLI & Rich REPL (`chat`, `exec`, `tools`, `pipelines`, `models`, `memory`)
│   │   └── outbound/              # Antigravity SDK harness, just AST synthesizer, SQLite FTS5/vec0, worktrees, pricing
│   ├── bootstrap/                 # Hierarchical TOML loader, DI container, agent/skill frontmatter resolvers
│   └── cli.py                     # CLI console entrypoint (`wintermute`)
├── tests/
│   ├── unit/                      # Unit test suite
│   └── integration/               # Single-agent & CLI integration tests
├── wintermute.toml                # Pipelines, domain defaults, concurrency locks, budgets & model profiles
├── pyproject.toml                 # Dependencies & tooling config
└── justfile                       # Task runner recipes (`just wintermute ...`)
```

---

## Agent Definitions (`agents/*.md`) & Runtime Config

- **Agent Definitions ([agents/](agents)):** Each role (`coordinator`, `tech_lead`, `senior_coder`, `junior_coder`, `troubleshooter`, `researcher`, `reviewer`, `doc_writer`) is defined in `wintermute/agents/<role>.md` with YAML frontmatter controlling `name`, `description`, `read_only`, `thinking_level`, `compaction_token_threshold`, `skills`, `allowed_subagents`, `allowed_tools` (`@domain`, `@read_only`, exact names, globs), and `denied_tools`, followed by the role's markdown system instructions (auto-resolved; omitted if absent so the SDK default applies). Local overrides can be placed in `.wintermute/agents/<role>.md`.
- **Runtime Config ([wintermute.toml](wintermute.toml)):** Owns workspace paths, pluggable stage-gate pipelines (`tdd_coding`, `package_lifecycle`, `standard_change`, `investigation_audit`), domain defaults (`[pipeline.domain_defaults]`, `[pipeline.domain_test_targets]`), build lock semaphores, cost budgets, model profiles, and per-role runtime settings (`model`, `thinking_level`, `compaction_token_threshold`, `max_model_calls`, `max_tool_calls`, `max_total_tokens`). Repository-local overrides go in `.wintermute/config.toml`.

---

## Hybrid Skill System (`skills/*/SKILL.md`)

Wintermute follows the **`agentskills.io`** specification (`name` + `description` frontmatter) and keeps skill scoping simple:
1. **Role-Scoped SDK Discovery:** Each agent receives only the skill directories listed in `skills` inside `agents/<role>.md` via `skills_paths`.
2. **Explicit WorkUnit Binding:** A `WorkUnit` can specify `skills` directly (e.g. via `--skill` or `dispatch_work_unit`) to pre-inject those skill instructions into its stage prompts.

---

## Authentication (Google Antigravity SDK)

Wintermute relies on the **Google Antigravity SDK** (`google-antigravity`):
- **Google AI Studio (default `gemini` provider):** Copy [../.env-example](../.env-example) to `.env` at the Freeside workspace root and set `GEMINI_API_KEY` (or export it in your shell). Wintermute loads `.env`, `.wintermute/.env`, and `wintermute/.env` automatically via `python-dotenv` without overriding variables already set in your shell.
- **Vertex AI (`provider = "vertex"` or `vertex = true`):**
  - *Standard Mode (ADC):* Run `gcloud auth application-default login` and set `project` / `location` on the model profile in [wintermute.toml](wintermute.toml).
  - *Express Mode:* Set `vertex = true` and `api_key_env` on the model profile.
- **Local Models (zero credentials):** Configure `provider = "litert"` (`LiteRTAgentConfig`) or `provider = "openai_compat"` (`LocalOpenAIAgentConfig`, e.g. `local_coder` on `http://localhost:8000/v1`) in [wintermute.toml](wintermute.toml).

---

## Commands & Testing

From the Freeside workspace root:

```bash
# Inspect pipelines, models, tools, or memory
just wintermute pipelines
just wintermute models
just wintermute tools --role senior_coder --domain packages
just wintermute memory list

# Run interactive REPL or one-shot prompt execution
just wintermute chat
just wintermute exec "Patch CVE in zlib" --domain packages --target zlib

# Run unit & integration tests
just wintermute test

# Run ruff static checks
just wintermute lint

# Clean caches
just wintermute clean
```
