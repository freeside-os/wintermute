import sys
from dotenv import load_dotenv
load_dotenv()
import click

from app.use_cases import (
    check_packages,
    import_package,
    create_package,
    fix_package,
    upgrade_package,
)
from app.agents.upstream import get_latest_upstream_version, pkgbuild_exists_on_arch
from app.tools import list_workspace_packages


# ------------------------------------------------------------------------------
# Click CLI Definitions
# ------------------------------------------------------------------------------

def run_use_case(async_gen) -> tuple[str, list]:
    """Orchestrates use case async generator execution, formatting and printing to click."""
    import asyncio
    status_res = "success"
    yielded = []

    async def _run():
        nonlocal status_res
        async for msg_type, content in async_gen:
            yielded.append((msg_type, content))
            if msg_type == "status":
                click.echo(click.style(f"⚙ {content}", fg="cyan"))
            elif msg_type == "success":
                click.echo(click.style(f"✓ {content}", fg="green", bold=True))
                status_res = "success"
            elif msg_type == "error":
                click.echo(click.style(f"✗ {content}", fg="red", bold=True))
                status_res = content
            elif msg_type == "warning":
                click.echo(click.style(f"⚠ {content}", fg="yellow"))
            elif msg_type == "info":
                click.echo(content)
            elif msg_type == "result":
                pass
            elif msg_type == "event":
                event = content
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            author = event.author if event.author else "workflow"
                            if author not in ("user", "root_agent"):
                                click.echo(
                                    click.style(f"[{author}] ", fg="cyan", bold=True)
                                    + part.text
                                )
                            else:
                                click.echo(part.text)

                for fc in event.get_function_calls():
                    args_str = (
                        ", ".join(f"{k}={v}" for k, v in fc.args.items())
                        if fc.args
                        else ""
                    )
                    click.echo(
                        click.style(
                            f"  🔧 Calling tool '{fc.name}' ({args_str})...",
                            fg="yellow",
                        )
                    )

                for fr in event.get_function_responses():
                    status = "success"
                    response_val = fr.response
                    if isinstance(response_val, dict):
                        status = response_val.get("status", "completed")
                        if status == "error":
                            click.echo(
                                click.style(
                                    f"  ✗ Tool '{fr.name}' failed: {response_val.get('message', 'Unknown error')}",
                                    fg="red",
                                    bold=True,
                                )
                            )
                            continue
                    click.echo(
                        click.style(
                            f"  ✓ Tool '{fr.name}' completed with status: {status}",
                            fg="green",
                        )
                    )

    try:
        import nest_asyncio

        nest_asyncio.apply()
    except Exception:
        pass

    asyncio.run(_run())
    return status_res, yielded


def run_interactive_menu(ctx):
    """Enters an interactive prompt menu to select operation and input arguments."""
    import questionary

    click.echo("=== Wintermute Interactive Package Manager ===")

    choices = [
        "Check Package status (verify lint, CVEs, upstream updates)",
        "Import PKGBUILD from Arch Linux or custom URL",
        "Create new package skeleton from scratch",
        "Fix package sandboxed compilation",
        "Upgrade package version",
        "Exit"
    ]

    action = questionary.select(
        "Select an operation to perform:",
        choices=choices
    ).ask()

    if not action or action == "Exit":
        click.echo("Exiting Wintermute.")
        sys.exit(0)

    # Resolve commands from the CLI group to invoke them properly
    if action.startswith("Check"):
        pkg = questionary.text("Enter package name (leave blank to check all):").ask()
        if pkg is None:
            sys.exit(0)
        pkg_val = pkg.strip()
        if not pkg_val:
            ctx.invoke(check, pkg_name=None, all_pkgs=True)
        else:
            ctx.invoke(check, pkg_name=pkg_val, all_pkgs=False)

    elif action.startswith("Import"):
        pkg = questionary.text("Enter package name to import (required):").ask()
        if pkg is None:
            sys.exit(0)
        pkg_val = pkg.strip()
        if not pkg_val:
            click.echo("Error: package name is required.", err=True)
            sys.exit(1)
        url = questionary.text("Enter custom URL for PKGBUILD (optional):").ask()
        if url is None:
            sys.exit(0)
        url_val = url.strip()
        ctx.invoke(import_cmd, pkg_name=pkg_val, url=url_val if url_val else None)

    elif action.startswith("Create"):
        pkg = questionary.text("Enter package name to create (required):").ask()
        if pkg is None:
            sys.exit(0)
        pkg_val = pkg.strip()
        if not pkg_val:
            click.echo("Error: package name is required.", err=True)
            sys.exit(1)
        version = questionary.text("Enter package version (default: 1.0.0):").ask()
        if version is None:
            sys.exit(0)
        version_val = version.strip() if version.strip() else "1.0.0"
        group = questionary.text("Enter package group (default: extra):").ask()
        if group is None:
            sys.exit(0)
        group_val = group.strip() if group.strip() else "extra"
        ctx.invoke(create, pkg_name=pkg_val, group=group_val, version=version_val)

    elif action.startswith("Fix"):
        pkg = questionary.text("Enter package name to fix (required):").ask()
        if pkg is None:
            sys.exit(0)
        pkg_val = pkg.strip()
        if not pkg_val:
            click.echo("Error: package name is required.", err=True)
            sys.exit(1)
        ctx.invoke(fix, pkg_name=pkg_val)

    elif action.startswith("Upgrade"):
        pkg = questionary.text("Enter package name to upgrade (required):").ask()
        if pkg is None:
            sys.exit(0)
        pkg_val = pkg.strip()
        if not pkg_val:
            click.echo("Error: package name is required.", err=True)
            sys.exit(1)
        version = questionary.text("Enter target version (optional, leave empty to fetch latest):").ask()
        if version is None:
            sys.exit(0)
        version_val = version.strip()
        ctx.invoke(upgrade, pkg_name=pkg_val, version=version_val if version_val else None)


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Wintermute Package Manager CLI."""
    if ctx.invoked_subcommand is None:
        run_interactive_menu(ctx)


@cli.command()
@click.argument("pkg_name", required=False)
@click.option("--all", "all_pkgs", is_flag=True, help="Check all packages.")
def check(pkg_name, all_pkgs):
    """Verifies package lint status, CVEs, and upstream updates."""
    if not pkg_name and not all_pkgs:
        click.echo("Error: Please specify <pkg_name> or pass the --all flag.", err=True)
        sys.exit(1)

    pkgs_to_check = []
    if all_pkgs:
        list_res = list_workspace_packages()
        pkgs_to_check = list_res.get("packages", [])
    else:
        pkgs_to_check = [pkg_name]

    click.echo(f"Scanning {len(pkgs_to_check)} package(s)...")
    click.echo("-" * 80)
    _, yielded = run_use_case(check_packages(pkgs_to_check))

    any_unhealthy = False
    for msg_type, content in yielded:
        if msg_type == "result":
            res = content
            is_unhealthy = (
                res["local_version"] != res["upstream_version"]
                or res["lint_status"] != "SUCCESS"
                or len(res["cves"]) > 0
            )
            if is_unhealthy:
                any_unhealthy = True

            if not all_pkgs or is_unhealthy:
                click.echo(f"Package: {res['package']}")
                click.echo(f"  - Local Version: {res['local_version']}")
                click.echo(f"  - Upstream version: {res['upstream_version']}")
                click.echo(f"  - Lint Verification: {res['lint_status']}")
                click.echo(f"  - Security CVEs: {res['cve_summary']}")
                click.echo("-" * 80)

    if all_pkgs and not any_unhealthy:
        click.echo("All packages are healthy (up-to-date, lint clean, and no known CVEs).")


@cli.command()
@click.argument("pkg_name")
@click.option("--url", default=None, help="Custom URL for PKGBUILD.")
def import_cmd(pkg_name, url):
    """Imports package PKGBUILD, converts, and builds it."""
    if not url:
        if not pkgbuild_exists_on_arch(pkg_name):
            click.echo(
                f"Error: PKGBUILD for '{pkg_name}' not found on Arch Linux packaging repository, and no custom --url was provided.",
                err=True,
            )
            sys.exit(1)

    run_use_case(import_package(pkg_name, url))


@cli.command()
@click.argument("pkg_name")
@click.option("--group", default="extra", help="Package group.")
@click.option("--version", default="1.0.0", help="Package version.")
def create(pkg_name, group, version):
    """Scaffolds a new package skeleton, refines recipe, and compiles it."""
    run_use_case(create_package(pkg_name, group, version))


@cli.command()
@click.argument("pkg_name")
def fix(pkg_name):
    """Performs builds, signature log scanning, auto-patching, and agent-based fixing."""
    status, _ = run_use_case(fix_package(pkg_name))
    if status == "compilation_failed":
        while True:
            click.echo(f"\nCompilation continues to fail for '{pkg_name}'.")
            suggestion = click.prompt(
                "Enter a suggestion to fix the build, or type 'abort' to stop"
            )
            if suggestion.strip().lower() == "abort":
                click.echo("Fix workflow aborted.")
                sys.exit(1)

            click.echo(
                f"Retrying build with operator suggestion: '{suggestion}'..."
            )
            status, _ = run_use_case(fix_package(pkg_name, suggestion))
            if status != "compilation_failed":
                break


@cli.command()
@click.argument("pkg_name")
@click.argument("version", required=False)
def upgrade(pkg_name, version):
    """Upgrades package version, with confirmation if version is omitted."""
    if not version:
        click.echo("Looking up the latest version upstream...")
        version = get_latest_upstream_version(pkg_name)
        if version == "Unknown":
            click.echo(
                f"Error: Could not retrieve latest upstream version for {pkg_name}.",
                err=True,
            )
            sys.exit(1)

        if not click.confirm(
            f"Latest upstream version for '{pkg_name}' is {version}. Do you want to upgrade?"
        ):
            click.echo("Upgrade aborted.")
            return

    click.echo(f"Upgrading package '{pkg_name}' to version {version}...")
    run_use_case(upgrade_package(pkg_name, version))


if __name__ == "__main__":
    cli()
