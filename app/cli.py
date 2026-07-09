import sys
import click

from app.use_cases import (
    check_packages,
    import_package,
    create_package,
    fix_package,
    upgrade_package,
)


# ------------------------------------------------------------------------------
# Click CLI Definitions
# ------------------------------------------------------------------------------

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
    check_packages(pkg_name, all_pkgs)


@cli.command()
@click.argument("pkg_name")
@click.option("--url", default=None, help="Custom URL for PKGBUILD.")
def import_cmd(pkg_name, url):
    """Imports package PKGBUILD, converts, and builds it."""
    import_package(pkg_name, url)


@cli.command()
@click.argument("pkg_name")
@click.option("--group", default="extra", help="Package group.")
@click.option("--version", default="1.0.0", help="Package version.")
def create(pkg_name, group, version):
    """Scaffolds a new package skeleton, refines recipe, and compiles it."""
    create_package(pkg_name, group, version)


@cli.command()
@click.argument("pkg_name")
def fix(pkg_name):
    """Performs builds, signature log scanning, auto-patching, and agent-based fixing."""
    fix_package(pkg_name)


@cli.command()
@click.argument("pkg_name")
@click.argument("version", required=False)
def upgrade(pkg_name, version):
    """Upgrades package version, with confirmation if version is omitted."""
    upgrade_package(pkg_name, version)


if __name__ == "__main__":
    cli()
