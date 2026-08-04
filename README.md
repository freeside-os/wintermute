# Wintermute Agentic Platform

Wintermute is the autonomous AI-powered development platform and harness for Freeside OS. Built using Google Gemini and the Google ADK, it automates development, build diagnostics, security auditing, QEMU boot testing, and release workflows across `packages`, `straylight`, `installer`, `simstim`, `freesidectl`, and `fstools`.

---

## Project Structure

```text
wintermute/
├── app/                           # Core agent platform code
│   ├── cli.py                     # Click CLI entry point & unified manifest runner
│   ├── engine.py                  # WintermuteEngine runtime
│   ├── config.py                  # WorkspaceConfig sub-repo path resolver
│   ├── types.py                   # DomainKind, WorkflowKind, and ExecutionSpec types
│   ├── provider.py                # DomainProvider protocol & fallback
│   ├── registry.py                # RegistryLibrary for namespaced tools & skills
│   ├── registry_domains.py        # DomainRegistry for autodiscovery & validation
│   ├── manifests/                 # K8s-style manifest parser (kind: Workflow / EventHandler)
│   ├── parsers/                   # Compiler diagnostics (RustDiagnosticParser)
│   ├── domains/                   # Domain plugins (packages, straylight, installer, etc.)
│   ├── tools/                     # System tools & container execution wrappers
│   └── agents/                    # Sub-agents and ADK workflows
├── .wintermute/                   # Workflows and event handler manifests
├── tests/                         # Test suite
└── pyproject.toml                 # Project dependencies
```

---

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager
- **just**: Command runner

---

## Environment Configuration

Copy the example configuration:
```bash
cp .env.example .env
```
And set the required variables:
- `GEMINI_API_KEY`: Your Google Gemini API key.
- `FREESIDE_WORKSPACE_ROOT`: Path to the Freeside workspace containing `packages/`, `straylight/`, `installer/`, etc. (defaults to `~/Code/freeside`).

---

## Command Reference

### 1. Unified Manifest Runner (`wintermute run`)
Executes declarative Kubernetes-style YAML manifest specifications (`kind: Workflow` or `kind: EventHandler`):

```bash
# Run a workflow manifest:
just wintermute run .wintermute/workflows/straylight-check.yaml
```

### 2. Interactive CLI Menu
Start the interactive selection prompt:
```bash
just wintermute cli
```

### 3. Check Package Status
Verifies package lint status, queries OSV CVE feeds, and looks up latest upstream versions:
```bash
just wintermute cli check <package-name>
# Check all workspace packages:
just wintermute cli check --all
```

### 4. Component Domain Commands
Run component domain subcommands directly:
```bash
# Fix a package build:
just wintermute cli fix <package-name>

# Import an Arch Linux package:
just wintermute cli import <package-name>

# Upgrade package version:
just wintermute cli upgrade <package-name>
```

---

## Running Tests

Run the complete pytest suite:
```bash
just wintermute tests
```
