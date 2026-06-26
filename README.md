# wintermute

Wintermute is the AI-powered packaging agent for the Freeside OS distribution. It automates package creation, maintenance, security auditing, and build diagnostics using Google Gemini.

## Project Structure

```
wintermute/
├── app/                           # Core agent code
│   ├── agent.py                   # Main agent logic and tool registration
│   ├── app_utils/                 # Shared utilities (retry, path resolution)
│   ├── tools/                     # Agent tools (package I/O, dependency graph, compilation)
│   ├── agents/                    # Sub-agents (builder, reviewer, etc.)
│   └── workflows/                 # Orchestration workflows
├── tests/                         # Unit, integration, and eval tests
├── .env.example                   # Template for required environment variables
└── pyproject.toml                 # Project dependencies
```

> 💡 **Tip:** Use [Gemini CLI](https://github.com/google-gemini/gemini-cli) for AI-assisted development — project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager — [Install](https://docs.astral.sh/uv/getting-started/installation/)
- **just**: Command runner — [Install](https://github.com/casey/just)

## Environment Configuration

Wintermute uses [python-dotenv](https://pypi.org/project/python-dotenv/) and loads `.env` automatically on startup. **Both variables below are required** — the agent will raise an error at startup if either is missing.

1. Copy the example file:
   ```bash
   cp .env.example .env
   ```

2. Fill in the values:

   | Variable | Description |
   |---|---|
   | `GEMINI_API_KEY` | Your Google Gemini API key ([get one here](https://aistudio.google.com/apikey)) |
   | `WINTERMUTE_WORKSPACE_ROOT` | Absolute path to the root of your local Freeside workspace (the directory containing `packages/`) |

   Example `.env`:
   ```dotenv
   GEMINI_API_KEY=AIza...
   WINTERMUTE_WORKSPACE_ROOT=/home/yourname/Code/freeside
   ```

## Quick Start

Install dependencies:
```bash
uv sync
```

Run the agent in interactive CLI:
```bash
just wintermute run
```

Run the agent with a specific query:
```bash
just wintermute run "Review package zlib"
```

Start the Web UI interface:
```bash
just wintermute web
```

Run unit and integration tests:
```bash
just wintermute tests
```

## Commands

| Command | Description |
|---------|-------------|
| `just wintermute run` | Start interactive CLI session with the agent |
| `just wintermute run "<query>"` | Run the agent with a single user query |
| `just wintermute web` | Launch the ADK Web UI locally |
| `just wintermute tests` | Run all unit and integration tests via pytest |

## Development

Edit the agent logic directly in `app/agent.py`. The local session database, artifacts, Chroma DB memory, and server logs are centralized under the git-ignored `.adk/` directory at the root of `wintermute/`.

## 🐳 Running with Docker

You can run Wintermute inside a Docker container while sharing the host workspace filesystem. To prevent permission mismatches (e.g. files created inside the container being owned by `root` on the host), run the container using the host user's UID and GID:

### 1. Build the Docker Image
From the `wintermute` directory:
```bash
docker build -t wintermute .
```

### 2. Run the Container

Mount the Freeside workspace directory into the container and pass your environment variables:

```bash
docker run -it --rm \
  --user "$(id -u):$(id -g)" \
  -v "$WINTERMUTE_WORKSPACE_ROOT:$WINTERMUTE_WORKSPACE_ROOT" \
  -w "$WINTERMUTE_WORKSPACE_ROOT/wintermute" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -e WINTERMUTE_WORKSPACE_ROOT="$WINTERMUTE_WORKSPACE_ROOT" \
  wintermute uv run adk run app "Review package zlib"
```

*Note: Mounting the host passwd and group files (`-v /etc/passwd:/etc/passwd:ro` and `-v /etc/group:/etc/group:ro`) is recommended if the container needs to resolve host user/group names inside the environment.*
