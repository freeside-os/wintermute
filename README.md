# Wintermute Packaging Agent

Wintermute is the AI-powered packaging agent for the Freeside OS distribution. It automates package creation, maintenance, security auditing, and build diagnostics using Google Gemini and the Google ADK.

## Project Structure

```
wintermute/
├── app/                           # Core agent code
│   ├── cli.py                     # Click-based CLI entry point (input validation, CLI output)
│   ├── use_cases.py               # Orchestration use cases (async generators yielding progress)
│   ├── services.py                # Services (memory factories)
│   ├── consts.py                  # Model and configuration constants
│   ├── tools/                     # Core system tools (sandbox build, patch, log scanning)
│   └── agents/                    # Sub-agents and workflows
│       ├── __init__.py            # Agent factories (scaffold, refiner, builder)
│       ├── nodes.py               # Reusable ADK Graph Nodes
│       ├── upstream.py            # Upstream package version resolver
│       └── workflows/             # Package workflows
│           ├── create.py          # Create / scaffold package workflow
│           ├── import_pkg.py      # Arch PKGBUILD converter workflow
│           ├── fix.py             # Build fixer workflow
│           ├── upgrade.py         # Version upgrade workflow
│           └── review.py          # Validation review workflow
├── tests/                         # Unit and integration tests
├── .env.example                   # Template for required environment variables
└── pyproject.toml                 # Project dependencies
```

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager
- **just**: Command runner

## Environment Configuration

Copy the example configuration:
```bash
cp .env.example .env
```
And fill in the required variables:
- `GEMINI_API_KEY`: Your Google Gemini API key.
- `WINTERMUTE_WORKSPACE_ROOT`: Absolute path to the Freeside workspace containing `packages/`.

---

## CLI Command Reference

Execute Wintermute commands using the Click CLI:

### 1. Interactive Menu
Start the interactive questionary-based menu:
```bash
just wintermute cli
```

### 2. Check Package Status
Verifies package lint status, queries OSV CVE feeds, and looks up the latest upstream version:
```bash
just wintermute cli check <package-name>
# Or check all workspace packages:
just wintermute cli check --all
```

### 3. Create Package
Scaffolds a new package skeleton, refines the recipe manifests, and compiles it inside the Musl sandbox:
```bash
just wintermute cli create <package-name> --group <group-name> --version <version>
```

### 4. Fix Package Build
Performs builds, scans failure log signatures, applies automatic environment or source patches, and activates the Fixer workflow if builds continue to fail:
```bash
just wintermute cli fix <package-name>
```

### 5. Import Package
Queries Arch Linux gitlab packaging, downloads, converts to Freeside structure, and builds the package:
```bash
just wintermute cli import <package-name>
# Or import using a custom URL to a PKGBUILD:
just wintermute cli import <package-name> --url <url>
```

### 6. Upgrade Package
Upgrades a package to a new version, automatically downloading and recalculating hashes and rebuilding inside the sandbox:
```bash
just wintermute cli upgrade <package-name> <version>
# Or omit version to search upstream and ask for interactive confirmation:
just wintermute cli upgrade <package-name>
```

---

## Agent Workflows: Graph vs. Dynamic

Wintermute organizes its agent execution pipelines into two types of ADK workflows depending on the nature of the task:

### 1. Declarative Graph Workflows
*   **Workflows**: `CreateWorkflow` and `ImportWorkflow`
*   **Class**: Inherit from `google.adk.workflow.Workflow`
*   **Why**: These workflows represent deterministic, strictly linear pipelines (e.g., `Import -> Scaffold Check -> Compile -> Auto-Heal -> Rebuild -> Verify`). In ADK, declaring transitions as static edges (`self.edges = [...]`) allows the framework to compile, optimize, and visualize the path.

### 2. Imperative Dynamic Workflows
*   **Workflows**: `FixWorkflow`, `UpgradeWorkflow`, and `ReviewWorkflow`
*   **Class**: Inherit from `google.adk.agents.BaseAgent` (overriding `_run_async_impl`)
*   **Why**: These workflows involve complex conditional branching, state-dependent loops, and interactive operator suggestion checks. Since the next step is determined dynamically during execution based on the build results of the previous step, an imperative Python generator (`async def _run_async_impl`) is utilized to yield events dynamically.

---

## Running Tests

Run the complete pytest suite:
```bash
just wintermute tests
```
