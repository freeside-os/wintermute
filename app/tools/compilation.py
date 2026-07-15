import glob
import os
import re
import subprocess

from app.app_utils.paths import workspace_root as get_workspace_root
from app.tools.pattern_storage import get_active_patterns


def verify_package(pkg_name: str) -> dict:
    """Verifies the package recipe and manifest validity.

    Args:
        pkg_name: Name of the package to verify.

    Returns:
        A dictionary containing the verification status and output.
    """
    packages_dir = os.path.join(get_workspace_root(), "packages")
    try:
        res = subprocess.run(
            ["python3", "fspack.py", "verify", pkg_name],
            cwd=packages_dir,
            capture_output=True,
            text=True
        )
        return {
            "status": "success" if res.returncode == 0 else "error",
            "stdout": res.stdout,
            "stderr": res.stderr
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

def build_package(pkg_name: str, keep_sandbox: bool = False) -> dict:
    """Builds a package inside the systemd-nspawn sandboxed container core.

    Args:
        pkg_name: Name of the package to build.
        keep_sandbox: If True, preserves the build sandbox directory for subsequent incremental compilation.

    Returns:
        A dictionary containing the build status, stdout, and stderr.
    """
    env = os.environ.copy()
    workspace = get_workspace_root()
    env.setdefault("STRAYLIGHT_PACKAGES_ROOT", os.path.join(workspace, "packages"))
    env.setdefault("STRAYLIGHT_BUILDER_ROOT", os.path.join(workspace, "build"))
    env.setdefault("STRAYLIGHT_BUILDER_OUTPUT_ROOT", os.path.join(workspace, "build", "packages"))
    straylight_bin = os.path.join(workspace, "build", "straylight")
    try:
        cmd = ["sudo", "-E", straylight_bin, "build", "--pkg", pkg_name]
        if keep_sandbox:
            cmd.append("--keep-sandbox")
        res = subprocess.run(
            cmd,
            cwd=get_workspace_root(),
            env=env,
            capture_output=True,
            text=True
        )
        return {
            "status": "success" if res.returncode == 0 else "error",
            "stdout": res.stdout,
            "stderr": res.stderr
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

def read_build_logs(pkg_name: str) -> dict:
    """Reads stdout and stderr log from the most recent compile attempt of a package.

    Args:
        pkg_name: Name of the package to read logs for.

    Returns:
        A dictionary with the status, log file name, and log content.
    """
    log_dir = os.path.join(get_workspace_root(), "build")
    log_pattern = os.path.join(log_dir, f"{pkg_name}-*.log")
    log_files = glob.glob(log_pattern)
    if not log_files:
        return {"status": "error", "message": f"No build logs found for package {pkg_name}."}

    log_files.sort(key=os.path.getmtime, reverse=True)
    latest_log = log_files[0]
    try:
        with open(latest_log, encoding="utf-8") as f:
            content = f.read()
        filtered_content = parse_compiler_errors(content)
        return {
            "status": "success",
            "file": os.path.basename(latest_log),
            "content": filtered_content
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to read log file {latest_log}: {e}"}

def parse_compiler_errors(log_content: str) -> str:
    """Extracts lines containing compiler or linker errors from the build log content."""
    lines = log_content.splitlines()
    if len(lines) <= 250:
        return log_content

    error_patterns = get_active_patterns()
    matched_lines = []
    for i, line in enumerate(lines):
        for pattern in error_patterns:
            if re.search(pattern, line):
                # Grab context lines around the error for better diagnostics
                start = max(0, i - 3)
                end = min(len(lines), i + 4)
                matched_lines.append(f"--- Context around Line {i+1} ---")
                for j in range(start, end):
                    prefix = ">> " if j == i else "   "
                    matched_lines.append(f"{prefix}Line {j+1}: {lines[j]}")
                matched_lines.append("")
                break
    if not matched_lines:
        # If no explicit errors are found, return the last 100 lines as a fallback
        fallback_lines = lines[-100:]
        return "No specific error patterns matched. Showing last 100 lines of log:\n" + "\n".join(fallback_lines)
    return "\n".join(matched_lines)


def scan_build_log(pkg_name: str) -> dict:
    """Retrieves build logs for a package and scans for known compile/link failure signatures.

    Proposes the exact patch or env injection depending on the match.

    Args:
        pkg_name: Name of the package to scan.

    Returns:
        A dictionary with the scan status, signature name, and proposed fix.
    """
    res = read_build_logs(pkg_name)
    if res.get("status") != "success":
        return {
            "status": "error",
            "message": f"Could not retrieve build logs: {res.get('message', 'Unknown error')}"
        }

    content = res.get("content", "")
    content_lower = content.lower()

    # Define signatures and proposals
    # 1. Redefined inline functions
    if "redefinition of" in content_lower and "inline" in content_lower:
        return {
            "status": "success",
            "signature": "redefined_inline",
            "proposal": {
                "type": "env_injection",
                "env": {
                    "CFLAGS": "-fcommon"
                },
                "message": "Detected redefinition of inline functions. Proposing CFLAGS='-fcommon' injection."
            }
        }

    # 2. Missing argp-standalone
    if "cannot find -largp" in content_lower or "argp.h" in content_lower or "largp" in content_lower or "argp-standalone" in content_lower:
        return {
            "status": "success",
            "signature": "missing_argp",
            "proposal": {
                "type": "env_injection",
                "env": {
                    "LDFLAGS": "-largp"
                },
                "message": "Detected missing argp-standalone link/header failure. Proposing LDFLAGS='-largp' injection."
            }
        }

    # 3. Missing doc tools like makeinfo
    if "makeinfo: command not found" in content_lower or "makeinfo: not found" in content_lower or "makeinfo" in content_lower:
        return {
            "status": "success",
            "signature": "missing_makeinfo",
            "proposal": {
                "type": "env_injection",
                "env": {
                    "MAKEINFO": "true"
                },
                "message": "Detected missing makeinfo doc tool. Proposing MAKEINFO='true' environment injection."
            }
        }

    try:
        from app.memory_service import PersistentGeminiMemoryService
        import asyncio
        chroma_path = os.path.join(get_workspace_root(), ".adk", "chroma_memory")
        memory_service = PersistentGeminiMemoryService(path=chroma_path)

        async def do_query():
            return await memory_service.search_memory(
                query=content[-1000:],
                limit=1
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = pool.submit(asyncio.run, do_query()).result()
        else:
            results = asyncio.run(do_query())

        if results:
            match = results[0]
            workaround_text = ""
            if match.content and match.content.parts:
                workaround_text = " ".join([p.text for p in match.content.parts if p.text])
            return {
                "status": "success",
                "signature": "semantic_kb_match",
                "proposal": {
                    "type": "semantic_note",
                    "message": workaround_text
                }
            }
    except Exception:
        pass

    return {
        "status": "success",
        "signature": None,
        "proposal": None
    }


