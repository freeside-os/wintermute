# Coding Agent Guide

## Prerequisites

Ensure you have the following installed:
- **uv**: Python package manager
- **just**: Command runner

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `just wintermute run` or `just wintermute web` for interactive testing. Iterate based on feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run evaluations using `uv run adk eval app tests/eval/datasets/custom-dataset.json`, and make changes until satisfied.

### Phase 4: Verification
Run `just wintermute tests` and resolve any issues.

---

## Development Commands

| Command | Purpose |
|---------|---------|
| `just wintermute run` | Interactive local testing (CLI) |
| `just wintermute run "<query>"` | Run single query on the agent |
| `just wintermute web` | Interactive local testing (Web UI) |
| `just wintermute tests` | Run unit and integration tests |
| `uv run adk eval app <dataset>` | Run agent evaluations |
| `ruff check .` | Check code quality |

---

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run commands via Just**: Prefer running agent tests and interfaces using the `just wintermute` module.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
