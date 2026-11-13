---
name: just-authoring
description: Author reusable justfile recipes and reload synthesized just_* tools dynamically.
---

# Freeside `just` Recipe Authoring

Use this skill when adding or updating developer/agent recipes under `just/*.just` or sub-repo `justfile`s.

## Guidelines
- Every recipe must include a `#` doc comment directly above its header so `just --dump --dump-format json` exports clear descriptions for agents.
- Keep recipes parameterized with sensible defaults and `set dotenv-load := false`.
- Once the `justfile` work unit completes, `tech_lead` calls `reload_just_tools()` so newly added `just_*` tools become immediately available.

