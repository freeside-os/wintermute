import asyncio
import json
import os
import re
import sys
import tomllib
import urllib.request

import click

from app.agents import (
    create_builder_agent,
    create_refiner_agent,
    create_scaffold_agent,
)
from app.app_utils.paths import packages_root, workspace_root
from app.tools import (
    apply_patch,
    build_package,
    import_pkgbuild,
    list_workspace_packages,
    query_security_feeds,
    read_package_file,
    scan_build_log,
    verify_package,
    write_package_file,
)

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def pkgbuild_exists_on_arch(pkg_name: str) -> bool:
    """Checks if a PKGBUILD for the given package exists on the Arch raw repository."""
    url = f"https://gitlab.archlinux.org/archlinux/packaging/packages/{pkg_name}/-/raw/main/PKGBUILD"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        try:
            req_get = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req_get, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

def get_arch_version(pkg_name: str) -> str | None:
    """Queries the Arch Linux packages API to get the latest version (fast)."""
    url = f"https://archlinux.org/packages/search/json/?name={pkg_name}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            for r in results:
                if r.get("pkgname") == pkg_name:
                    return r.get("pkgver")
    except Exception:
        pass
    return None

def get_latest_upstream_version(pkg_name: str) -> str:
    """Finds the latest stable version of a package upstream."""
    arch_ver = get_arch_version(pkg_name)
    if arch_ver:
        return arch_ver

    # Fallback to Gemini with search grounding
    from google import genai
    from google.genai import types

    if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

    try:
        client = genai.Client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                f"What is the latest stable upstream version of the package '{pkg_name}'? "
                "Respond with ONLY the version string (e.g. '1.3.1') and nothing else."
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0
            )
        )
        version_text = response.text.strip()
        match = re.search(r"([0-9]+(?:\.[0-9]+)+[a-z]?)", version_text)
        if match:
            return match.group(1)
        return version_text
    except Exception:
        return "Unknown"

def inject_env_into_manifest(pkg_name: str, env_vars: dict[str, str]) -> bool:
    """Updates/adds environment variables in the package.manifest file under [build.environment]."""
    res = read_package_file(pkg_name, "package.manifest")
    if res.get("status") != "success":
        return False
    content = res.get("content", "")

    has_build_env = "[build.environment]" in content or "[build.env]" in content

    if has_build_env:
        lines = content.splitlines()
        new_lines = []
        in_build_env = False
        variables_to_inject = env_vars.copy()

        for line in lines:
            if line.strip() in ("[build.environment]", "[build.env]"):
                in_build_env = True
                new_lines.append(line)
                continue

            if in_build_env and line.strip().startswith("[") and not line.strip().startswith("[build.environment") and not line.strip().startswith("[build.env"):
                for k, v in list(variables_to_inject.items()):
                    new_lines.append(f'{k} = "{v}"')
                    variables_to_inject.pop(k)
                in_build_env = False

            if in_build_env:
                match = re.match(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*)', line)
                if match:
                    key = match.group(1)
                    if key in variables_to_inject:
                        val = variables_to_inject.pop(key)
                        new_lines.append(f'{key} = "{val}"')
                        continue

            new_lines.append(line)

        for k, v in variables_to_inject.items():
            new_lines.append(f'{k} = "{v}"')

        content = "\n".join(new_lines) + "\n"
    else:
        content = content.rstrip() + "\n\n[build.environment]\n"
        for k, v in env_vars.items():
            content += f'{k} = "{v}"\n'

    write_res = write_package_file(pkg_name, "package.manifest", content)
    return write_res.get("status") == "success"

def run_workflow_sync(workflow_node, pkg_name: str, state_vars: dict) -> dict:
    """Synchronously executes a workflow agent node using InMemorySessionService."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    async def _run():
        session_service = InMemorySessionService()
        
        # Prepare initial state with package name and custom state vars
        initial_state = {"pkg_name": pkg_name}
        for k, v in state_vars.items():
            initial_state[k] = v

        await session_service.create_session(
            app_name="app",
            user_id="cli_user",
            session_id="s1",
            state=initial_state
        )

        from app.services import memory_factory
        runner = Runner(
            agent=workflow_node,
            app_name="app",
            session_service=session_service,
            memory_service=memory_factory("memory://")
        )

        async for event in runner.run_async(
            user_id="cli_user",
            session_id="s1",
            new_message=types.Content(role="user", parts=[types.Part.from_text(text="Start workflow")]),
        ):
            # Print text content (thoughts / progress / answers) from agents
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        author = event.author if event.author else "workflow"
                        if author not in ("user", "root_agent"):
                            click.echo(click.style(f"[{author}] ", fg="cyan", bold=True) + part.text)
                        else:
                            click.echo(part.text)
            
            # Print function calls (tools being triggered)
            for fc in event.get_function_calls():
                args_str = ", ".join(f"{k}={v}" for k, v in fc.args.items()) if fc.args else ""
                click.echo(click.style(f"  🔧 Calling tool '{fc.name}' ({args_str})...", fg="yellow"))

            # Print function responses (tool completions)
            for fr in event.get_function_responses():
                status = "success"
                response_val = fr.response
                if isinstance(response_val, dict):
                    status = response_val.get("status", "completed")
                    if status == "error":
                        click.echo(click.style(f"  ✗ Tool '{fr.name}' failed: {response_val.get('message', 'Unknown error')}", fg="red", bold=True))
                        continue
                click.echo(click.style(f"  ✓ Tool '{fr.name}' completed with status: {status}", fg="green"))

        updated_session = await session_service.get_session(app_name="app", user_id="cli_user", session_id="s1")
        return updated_session.state

    try:
        import nest_asyncio
        nest_asyncio.apply()
    except Exception:
        pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, _run()).result()
    else:
        return asyncio.run(_run())

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
    if not pkg_name and not all_pkgs:
        click.echo("Error: Please specify <pkg_name> or pass the --all flag.", err=True)
        sys.exit(1)

    pkgs_to_check = []
    if all_pkgs:
        list_res = list_workspace_packages()
        pkgs_to_check = list_res.get("packages", [])
    else:
        pkgs_to_check = [pkg_name]

    feeds_res = query_security_feeds()
    cves = feeds_res.get("cves", [])

    click.echo(f"Scanning {len(pkgs_to_check)} package(s)...")
    click.echo("-" * 80)

    displayed_count = 0

    for pkg in pkgs_to_check:
        manifest_path = os.path.join(packages_root(), pkg, "package.manifest")
        current_version = "Unknown"
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "rb") as f:
                    data = tomllib.load(f)
                current_version = data.get("package", {}).get("version", "Unknown")
            except Exception:
                pass

        # Lint / Verify status
        verify_res = verify_package(pkg)
        lint_status = "SUCCESS" if verify_res.get("status") == "success" else "ERROR"

        # Upstream update version
        upstream_version = get_latest_upstream_version(pkg)

        # Map CVEs
        pkg_cves = [c for c in cves if c.get("package") == pkg]
        cve_summary = "None"
        if pkg_cves:
            cve_summary = ", ".join([f"{c['cve_id']} (Severity: {c['severity']})" for c in pkg_cves])

        # Filter criteria: only show packages with a new version, lint issues, or CVEs
        has_new_version = (upstream_version != "Unknown" and current_version != upstream_version)
        lint_failed = (lint_status != "SUCCESS")
        has_cves = len(pkg_cves) > 0

        # If a specific package name was requested, we always show it.
        # Otherwise, we filter using the criteria.
        if not all_pkgs or has_new_version or lint_failed or has_cves:
            click.echo(f"Package: {pkg}")
            click.echo(f"  - Local Version: {current_version}")
            click.echo(f"  - Upstream version: {upstream_version}")
            click.echo(f"  - Lint Verification: {lint_status}")
            click.echo(f"  - Security CVEs: {cve_summary}")
            click.echo("-" * 80)
            displayed_count += 1

    if all_pkgs and displayed_count == 0:
        click.echo("All packages are healthy (up-to-date, lint clean, and no known CVEs).")

@cli.command()
@click.argument("pkg_name")
@click.option("--url", default=None, help="Custom URL for PKGBUILD.")
def import_cmd(pkg_name, url):
    """Imports package PKGBUILD, converts, and builds it."""
    if not url:
        if not pkgbuild_exists_on_arch(pkg_name):
            click.echo(f"Error: PKGBUILD for '{pkg_name}' not found on Arch Linux packaging repository, and no custom --url was provided.", err=True)
            sys.exit(1)

    click.echo(f"Importing PKGBUILD for package '{pkg_name}'...")

    if url:
        sys.path.append(os.path.join(workspace_root(), "packages"))
        try:
            pkg_dir = os.path.join(packages_root(), pkg_name)
            os.makedirs(pkg_dir, exist_ok=True)
            urllib.request.urlretrieve(url, os.path.join(pkg_dir, "PKGBUILD"))

            with open(os.path.join(pkg_dir, "PKGBUILD"), encoding="utf-8") as f:
                content = f.read()

            import fspack
            manifest, justfile, pkgver, source_url, checksum = fspack.generate_freeside_package(content)

            with open(os.path.join(pkg_dir, "package.manifest"), "w", encoding="utf-8") as f:
                f.write(manifest)
            with open(os.path.join(pkg_dir, "package.justfile"), "w", encoding="utf-8") as f:
                f.write(justfile)

            fspack.create_readme_file(pkg_dir, pkg_name, pkgver, url, source_url, checksum)
            click.echo(f"Local conversion successful for custom URL: {url}")
        except Exception as e:
            click.echo(f"Error converting PKGBUILD: {e}", err=True)
            sys.exit(1)
    else:
        res = import_pkgbuild(pkg_name)
        if res.get("status") != "success":
            click.echo(f"Error importing package: {res.get('message', 'Unknown error')}", err=True)
            sys.exit(1)

    from app.workflows.import_pkg import ImportWorkflow
    scaffold_agent = create_scaffold_agent()
    refiner_agent = create_refiner_agent()
    builder_agent = create_builder_agent()

    workflow = ImportWorkflow(
        name="import_workflow",
        scaffold_agent=scaffold_agent,
        refiner_agent=refiner_agent,
        builder_agent=builder_agent
    )

    click.echo(f"Routing '{pkg_name}' to refinement and sandbox build...")
    state_vars = {"import_done": True}
    run_workflow_sync(workflow, pkg_name, state_vars)

@cli.command()
@click.argument("pkg_name")
@click.option("--group", default="extra", help="Package group.")
@click.option("--version", default="1.0.0", help="Package version.")
def create(pkg_name, group, version):
    """Scaffolds a new package skeleton, refines recipe, and compiles it."""
    from app.workflows.create import CreateWorkflow
    scaffold_agent = create_scaffold_agent()
    refiner_agent = create_refiner_agent()
    builder_agent = create_builder_agent()

    workflow = CreateWorkflow(
        name="create_workflow",
        scaffold_agent=scaffold_agent,
        refiner_agent=refiner_agent,
        builder_agent=builder_agent
    )

    click.echo(f"Scaffolding skeleton for package '{pkg_name}' (Version: {version}, Group: {group})...")
    state_vars = {
        "group": group,
        "version": version
    }
    run_workflow_sync(workflow, pkg_name, state_vars)

@cli.command()
@click.argument("pkg_name")
def fix(pkg_name):
    """Performs builds, signature log scanning, auto-patching, and agent-based fixing."""
    click.echo(f"Building package '{pkg_name}' inside sandbox...")
    build_res = build_package(pkg_name)
    if build_res.get("status") == "success":
        click.echo(f"Package '{pkg_name}' compiled successfully! No fixes needed. ✓")
        return

    click.echo("Scanning build log for compiler/linker failure signatures...")
    scan_res = scan_build_log(pkg_name)
    sig = scan_res.get("signature") if scan_res.get("status") == "success" else None
    proposal = scan_res.get("proposal", {}) if scan_res.get("status") == "success" else {}

    kb_suggestion = None
    if sig:
        click.echo(f"Matched signature: {sig}")
        click.echo(proposal.get("message"))

        if sig == "semantic_kb_match":
            kb_suggestion = f"Use the following Knowledge Base match to resolve the compilation issue:\n{proposal.get('message')}"
        else:
            if proposal.get("type") == "env_injection":
                env = proposal.get("env", {})
                inject_env_into_manifest(pkg_name, env)
                click.echo(f"Injected environment: {env}")
            elif proposal.get("type") == "patch":
                apply_patch(pkg_name, proposal.get("target_file", "Makefile"), proposal.get("patch_content", ""))
                click.echo("Applied proposed patch.")

            click.echo("Rebuilding after auto-patch...")
            build_res = build_package(pkg_name)
            if build_res.get("status") == "success":
                click.echo(f"Package '{pkg_name}' built successfully after signature auto-patch! ✓")
                return

    from app.workflows.fix import FixWorkflow
    refiner_agent = create_refiner_agent()
    builder_agent = create_builder_agent()

    workflow = FixWorkflow(
        name="fix_workflow",
        refiner_agent=refiner_agent,
        builder_agent=builder_agent
    )

    state_vars = {"builder_done": False}
    if kb_suggestion:
        state_vars["operator_suggestion"] = kb_suggestion
        click.echo("Activating Build & Fixer Agent to diagnose and repair using Knowledge Base match...")
    else:
        click.echo("Activating Build & Fixer Agent to diagnose and repair...")

    run_workflow_sync(workflow, pkg_name, state_vars)

    build_res = build_package(pkg_name)
    if build_res.get("status") == "success":
        click.echo(f"Package '{pkg_name}' successfully compiled and verified! ✓")
        return

    while True:
        click.echo(f"\nCompilation continues to fail for '{pkg_name}'.")
        suggestion = click.prompt("Enter a suggestion to fix the build, or type 'abort' to stop")
        if suggestion.strip().lower() == "abort":
            click.echo("Fix workflow aborted.")
            sys.exit(1)

        click.echo(f"Retrying build with operator suggestion: '{suggestion}'...")
        state_vars = {
            "builder_done": False,
            "operator_suggestion": suggestion
        }
        run_workflow_sync(workflow, pkg_name, state_vars)

        build_res = build_package(pkg_name)
        if build_res.get("status") == "success":
            click.echo(f"Package '{pkg_name}' successfully compiled and verified! ✓")
            return

@cli.command()
@click.argument("pkg_name")
@click.argument("version", required=False)
def upgrade(pkg_name, version):
    """Upgrades package version, with confirmation if version is omitted."""
    if not version:
        click.echo("Looking up the latest version upstream...")
        version = get_latest_upstream_version(pkg_name)
        if version == "Unknown":
            click.echo(f"Error: Could not retrieve latest upstream version for {pkg_name}.", err=True)
            sys.exit(1)

        if not click.confirm(f"Latest upstream version for '{pkg_name}' is {version}. Do you want to upgrade?"):
            click.echo("Upgrade aborted.")
            return

    from app.workflows.upgrade import UpgradeWorkflow
    refiner_agent = create_refiner_agent()
    builder_agent = create_builder_agent()

    workflow = UpgradeWorkflow(
        name="upgrade_workflow",
        refiner_agent=refiner_agent,
        builder_agent=builder_agent
    )

    state_vars = {
        "version": version,
        "upgrade_done": False,
        "refiner_done": False,
        "builder_done": False
    }

    click.echo(f"Upgrading package '{pkg_name}' to version {version}...")
    run_workflow_sync(workflow, pkg_name, state_vars)

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    cli()
