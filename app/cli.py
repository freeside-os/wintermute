"""Console script entrypoint for Wintermute CLI."""

from __future__ import annotations

from app.adapters.inbound.cli.main import cli

__all__ = ["cli"]

if __name__ == "__main__":
    cli()

