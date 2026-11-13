# Freeside Wintermute Agentic Platform targets
set dotenv-load := true
set positional-arguments

# Run wintermute interactive CLI
run *args:
    uv run python -m app.cli "$@"

# Run the wintermute Click CLI with arbitrary commands and arguments
cli *args:
    uv run python -m app.cli "$@"

# Start an interactive Wintermute REPL chat session
chat *args:
    uv run python -m app.cli chat "$@"

# Execute a single Wintermute prompt non-interactively
exec *args:
    uv run python -m app.cli exec "$@"

# List configured Wintermute stage-gate pipelines
pipelines *args:
    uv run python -m app.cli pipelines "$@"

# List configured model profiles and cached pricing
models *args:
    uv run python -m app.cli models "$@"

# Inspect tools scoped for a role and domain
tools *args:
    uv run python -m app.cli tools "$@"

# Search, list, add, or consolidate Wintermute hybrid memory
memory *args:
    uv run python -m app.cli memory "$@"

# Start the Wintermute web mission-control server
serve host="127.0.0.1" port="8420":
    uv run uvicorn app.adapters.inbound.web:app --host {{ host }} --port {{ port }}

# Run wintermute unit and integration tests
test:
    @if command -v uv >/dev/null 2>&1; then uv run pytest; else PYTHONPATH=".:.venv/lib/python3.15/site-packages" python3 -m pytest; fi

# Backward-compatible alias for tests
alias tests := test

# Run ruff static checks
lint:
    uv run ruff check app tests

# Clean wintermute test cache and temporary files
clean:
    @echo "=== [wintermute] Cleaning cache and temporary files ==="
    rm -rf .pytest_cache .ruff_cache .coverage __pycache__
