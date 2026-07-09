import os
import sys
import tomllib
import urllib.request

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.app_utils.paths import packages_root, workspace_root
from app.tools import (
    apply_patch,
    build_package,
    import_pkgbuild,
    scan_build_log,
    verify_package,
    query_security_feeds,
    list_workspace_packages,
)
from app.agents.upstream import get_latest_upstream_version
from app.agents.workflows.fix import inject_env_into_manifest
from app.services import memory_factory


async def check_packages(pkgs_to_check: list[str]):
    """Verifies package lint status, CVEs, and upstream updates."""
    feeds_res = query_security_feeds()
    cves = feeds_res.get("cves", [])

    for pkg in pkgs_to_check:
        yield ("status", f"Scanning package '{pkg}'...")

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
            cve_summary = ", ".join(
                [f"{c['cve_id']} (Severity: {c['severity']})" for c in pkg_cves]
            )

        yield (
            "result",
            {
                "package": pkg,
                "local_version": current_version,
                "upstream_version": upstream_version,
                "lint_status": lint_status,
                "cves": pkg_cves,
                "cve_summary": cve_summary,
            },
        )


async def import_package(pkg_name: str, url: str | None):
    """Imports package PKGBUILD, converts, and builds it."""
    from app.agents.workflows.import_pkg import ImportWorkflow

    if url:
        sys.path.append(os.path.join(workspace_root(), "packages"))
        try:
            pkg_dir = os.path.join(packages_root(), pkg_name)
            os.makedirs(pkg_dir, exist_ok=True)
            urllib.request.urlretrieve(url, os.path.join(pkg_dir, "PKGBUILD"))

            with open(os.path.join(pkg_dir, "PKGBUILD"), encoding="utf-8") as f:
                content = f.read()

            import fspack

            manifest, justfile, pkgver, source_url, checksum = (
                fspack.generate_freeside_package(content)
            )

            with open(
                os.path.join(pkg_dir, "package.manifest"), "w", encoding="utf-8"
            ) as f:
                f.write(manifest)
            with open(
                os.path.join(pkg_dir, "package.justfile"), "w", encoding="utf-8"
            ) as f:
                f.write(justfile)

            fspack.create_readme_file(
                pkg_dir, pkg_name, pkgver, url, source_url, checksum
            )
            yield ("success", f"Local conversion successful for custom URL: {url}")
        except Exception as e:
            yield ("error", f"Error converting PKGBUILD: {e}")
            return
    else:
        yield ("status", f"Importing PKGBUILD for package '{pkg_name}'...")
        res = import_pkgbuild(pkg_name)
        if res.get("status") != "success":
            yield (
                "error",
                f"Error importing package: {res.get('message', 'Unknown error')}",
            )
            return

    yield (
        "status",
        f"Routing '{pkg_name}' to refinement and sandbox build...",
    )
    workflow = ImportWorkflow(name="import_workflow")
    session_service = InMemorySessionService()
    initial_state = {"pkg_name": pkg_name, "import_done": True}
    await session_service.create_session(
        app_name="app",
        user_id="cli_user",
        session_id="s1",
        state=initial_state,
    )

    runner = Runner(
        agent=workflow,
        app_name="app",
        session_service=session_service,
        memory_service=memory_factory("memory://"),
    )

    async for event in runner.run_async(
        user_id="cli_user",
        session_id="s1",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Start workflow")]
        ),
    ):
        yield ("event", event)


async def create_package(pkg_name: str, group: str, version: str):
    """Scaffolds a new package skeleton, refines recipe, and compiles it."""
    from app.agents.workflows.create import CreateWorkflow

    yield ("status", f"Scaffolding skeleton for package '{pkg_name}' (Version: {version}, Group: {group})...")

    workflow = CreateWorkflow(name="create_workflow")

    session_service = InMemorySessionService()
    initial_state = {"pkg_name": pkg_name, "group": group, "version": version}
    await session_service.create_session(
        app_name="app",
        user_id="cli_user",
        session_id="s1",
        state=initial_state,
    )

    runner = Runner(
        agent=workflow,
        app_name="app",
        session_service=session_service,
        memory_service=memory_factory("memory://"),
    )

    async for event in runner.run_async(
        user_id="cli_user",
        session_id="s1",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Start workflow")]
        ),
    ):
        yield ("event", event)


async def fix_package(pkg_name: str, operator_suggestion: str | None = None):
    """Performs builds, signature log scanning, auto-patching, and agent-based fixing."""
    from app.agents.workflows.fix import FixWorkflow

    if not operator_suggestion:
        build_res = build_package(pkg_name)
        if build_res.get("status") == "success":
            yield (
                "success",
                f"Package '{pkg_name}' compiled successfully! No fixes needed. ✓",
            )
            return

        yield (
            "status",
            "Scanning build log for compiler/linker failure signatures...",
        )
        scan_res = scan_build_log(pkg_name)
        sig = (
            scan_res.get("signature")
            if scan_res.get("status") == "success"
            else None
        )
        proposal = (
            scan_res.get("proposal", {})
            if scan_res.get("status") == "success"
            else {}
        )

        if sig:
            yield (
                "info",
                f"Matched signature: {sig}\nProposal: {proposal.get('message')}",
            )

            if sig == "semantic_kb_match":
                operator_suggestion = f"Use the following Knowledge Base match to resolve the compilation issue:\n{proposal.get('message')}"
            else:
                if proposal.get("type") == "env_injection":
                    env = proposal.get("env", {})
                    inject_env_into_manifest(pkg_name, env)
                    yield ("info", f"Injected environment: {env}")
                elif proposal.get("type") == "patch":
                    apply_patch(
                        pkg_name,
                        proposal.get("target_file", "Makefile"),
                        proposal.get("patch_content", ""),
                    )
                    yield ("info", "Applied proposed patch.")

                yield ("status", "Rebuilding after auto-patch...")
                build_res = build_package(pkg_name)
                if build_res.get("status") == "success":
                    yield (
                        "success",
                        f"Package '{pkg_name}' built successfully after signature auto-patch! ✓",
                    )
                    return

    workflow = FixWorkflow(name="fix_workflow")
    state_vars = {"builder_done": False}
    if operator_suggestion:
        state_vars["operator_suggestion"] = operator_suggestion

    session_service = InMemorySessionService()
    initial_state = {"pkg_name": pkg_name}
    for k, v in state_vars.items():
        initial_state[k] = v

    await session_service.create_session(
        app_name="app",
        user_id="cli_user",
        session_id="s1",
        state=initial_state,
    )

    runner = Runner(
        agent=workflow,
        app_name="app",
        session_service=session_service,
        memory_service=memory_factory("memory://"),
    )

    async for event in runner.run_async(
        user_id="cli_user",
        session_id="s1",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Start workflow")]
        ),
    ):
        yield ("event", event)

    yield ("status", "Verifying build after workflow run...")
    build_res = build_package(pkg_name)
    if build_res.get("status") == "success":
        yield (
            "success",
            f"Package '{pkg_name}' successfully compiled and verified! ✓",
        )
    else:
        yield ("error", "compilation_failed")


async def upgrade_package(pkg_name: str, version: str):
    """Upgrades package version."""
    from app.agents.workflows.upgrade import UpgradeWorkflow

    workflow = UpgradeWorkflow(name="upgrade_workflow")

    state_vars = {
        "version": version,
        "upgrade_done": False,
        "refiner_done": False,
        "builder_done": False,
    }

    session_service = InMemorySessionService()
    initial_state = {"pkg_name": pkg_name}
    for k, v in state_vars.items():
        initial_state[k] = v

    await session_service.create_session(
        app_name="app",
        user_id="cli_user",
        session_id="s1",
        state=initial_state,
    )

    runner = Runner(
        agent=workflow,
        app_name="app",
        session_service=session_service,
        memory_service=memory_factory("memory://"),
    )

    async for event in runner.run_async(
        user_id="cli_user",
        session_id="s1",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Start workflow")]
        ),
    ):
        yield ("event", event)
