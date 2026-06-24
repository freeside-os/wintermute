# wintermute

Simple ReAct agent
Agent generated with `agents-cli` version `0.5.0`

## Project Structure

```
wintermute/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   └── app_utils/             # App utilities and helpers
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

> 💡 **Tip:** Use [Gemini CLI](https://github.com/google-gemini/gemini-cli) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager - [Install](https://docs.astral.sh/uv/getting-started/installation/)
- **just**: Command runner - [Install](https://github.com/casey/just)

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
From the `wintermute` directory, build the image:
```bash
docker build -t wintermute .
```

### 2. Run the Container (Mounted FS & User Matched)
Mount the main `freeside` workspace directory into the container at the same absolute path `/home/dq/Code/freeside`. Pass the environment keys (e.g. `GEMINI_API_KEY`):

```bash
docker run -it --rm \
  --user "$(id -u):$(id -g)" \
  -v "/home/dq/Code/freeside:/home/dq/Code/freeside" \
  -w "/home/dq/Code/freeside/wintermute" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  wintermute uv run adk run app "Review package zlib"
```

*Note: Mounting the host passwd and group files (`-v /etc/passwd:/etc/passwd:ro` and `-v /etc/group:/etc/group:ro`) is recommended if the container needs to resolve host user/group names inside the environment.*
