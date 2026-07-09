import os
from google.adk.events import Event
from app.agents import create_builder_agent, create_refiner_agent, create_scaffold_agent
from app.tools import apply_patch, build_package, scan_build_log, verify_package

# Instantiate agent nodes using factory functions
scaffold_agent_node = create_scaffold_agent()
refiner_agent_node = create_refiner_agent()
builder_agent_node = create_builder_agent()


def compile_step(pkg_name: str) -> Event:
    """Runs sandbox compilation for a package."""
    res = build_package(pkg_name)
    if res.get("status") == "success":
        return Event(branch="SUCCESS", output=pkg_name)
    else:
        return Event(branch="FAILURE", output=pkg_name)


def auto_heal_step(pkg_name: str) -> Event:
    """Scans compile logs on failure and attempts to apply signature fixes."""
    scan_res = scan_build_log(pkg_name)
    if scan_res.get("status") == "success" and scan_res.get("signature"):
        from app.agents.workflows.fix import inject_env_into_manifest

        proposal = scan_res.get("proposal", {})

        if proposal.get("type") == "env_injection":
            inject_env_into_manifest(pkg_name, proposal.get("env", {}))
        elif proposal.get("type") == "patch":
            apply_patch(
                pkg_name,
                proposal.get("target_file", "Makefile"),
                proposal.get("patch_content", ""),
            )

        return Event(branch="REBUILD", output=pkg_name)

    return Event(branch="DELEGATE", output=pkg_name)


def rebuild_step(pkg_name: str) -> Event:
    """Rebuilds the package after a signature auto-patch."""
    res = build_package(pkg_name)
    if res.get("status") == "success":
        return Event(branch="SUCCESS", output=pkg_name)
    else:
        return Event(branch="FAILURE", output=pkg_name)


def verify_step(pkg_name: str) -> Event:
    """Runs final verification and builds the package."""
    verify_res = verify_package(pkg_name)
    build_res = build_package(pkg_name)
    if (
        verify_res.get("status") == "success"
        and build_res.get("status") == "success"
    ):
        msg = f"Package '{pkg_name}' successfully compiled and verified! ✓"
        return Event(branch="SUCCESS", output=pkg_name, content=msg)
    else:
        msg = f"Package '{pkg_name}' has verification/compilation issues. Please run 'fix {pkg_name}' to resolve."
        return Event(branch="FAILURE", output=pkg_name, content=msg)


def import_step(pkg_name: str) -> Event:
    """Imports PKGBUILD from Arch Linux."""
    from app.tools import import_pkgbuild

    import_pkgbuild(pkg_name)
    return Event(output=pkg_name)


def scaffold_check_step(pkg_name: str) -> Event:
    """Checks if manifest exists, if not triggers scaffolding fallback."""
    from app.app_utils.paths import packages_root

    manifest_path = os.path.join(packages_root(), pkg_name, "package.manifest")
    if not os.path.exists(manifest_path) or os.path.getsize(manifest_path) == 0:
        return Event(branch="SCAFFOLD", output=pkg_name)
    return Event(branch="SKIP", output=pkg_name)
