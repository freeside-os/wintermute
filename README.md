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
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

---

## 🐳 Running with Docker

You can run Wintermute inside a Docker container while sharing the host workspace filesystem. To prevent permission mismatches (e.g. files created inside the container being owned by `root` on the host), run the container using the host user's UID and GID:

### 1. Build the Docker Image
From the `wintermute` directory, build the image:
```bash
docker build -t wintermute .
```

### 2. Run the Container (Mounted FS & User Matched)
Mount the main `freeside` workspace directory into the container at the same absolute path `/home/dq/Code/freeside`. Pass the host user credentials and environment keys (e.g. `GEMINI_API_KEY`):

```bash
docker run -it --rm \
  --user "$(id -u):$(id -g)" \
  -v "/home/dq/Code/freeside:/home/dq/Code/freeside" \
  -w "/home/dq/Code/freeside/wintermute" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  wintermute uv run adk run . --message "Review package zlib"
```

*Note: Mounting the host passwd and group files (`-v /etc/passwd:/etc/passwd:ro` and `-v /etc/group:/etc/group:ro`) is recommended if the container needs to resolve host user/group names inside the environment.*
