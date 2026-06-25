import glob
import os
import re
import subprocess


def verify_package(pkg_name: str, workspace_root: str = "/home/dq/Code/freeside") -> dict:
    """Verifies the package recipe and manifest validity.

    Args:
        pkg_name: Name of the package to verify.

    Returns:
        A dictionary containing the verification status and output.
    """
    packages_dir = f"{workspace_root}/packages"
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

def build_package(pkg_name: str, keep_sandbox: bool = False, workspace_root: str = "/home/dq/Code/freeside") -> dict:
    """Builds a package inside the systemd-nspawn sandboxed container core.

    Args:
        pkg_name: Name of the package to build.
        keep_sandbox: If True, preserves the build sandbox directory for subsequent incremental compilation.

    Returns:
        A dictionary containing the build status, stdout, and stderr.
    """
    env = os.environ.copy()
    env["STRAYLIGHT_PACKAGES_ROOT"] = f"{workspace_root}/packages"
    env["STRAYLIGHT_BUILDER_ROOT"] = f"{workspace_root}/build"
    env["STRAYLIGHT_BUILDER_OUTPUT_ROOT"] = f"{workspace_root}/build/packages"
    try:
        cmd = ["sudo", "-E", f"{workspace_root}/build/straylight", "build", "--pkg", pkg_name]
        if keep_sandbox:
            cmd.append("--keep-sandbox")
        res = subprocess.run(
            cmd,
            cwd=workspace_root,
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

def read_build_logs(pkg_name: str, workspace_root: str = "/home/dq/Code/freeside") -> dict:
    """Reads stdout and stderr log from the most recent compile attempt of a package.

    Args:
        pkg_name: Name of the package to read logs for.

    Returns:
        A dictionary with the status, log file name, and log content.
    """
    log_dir = f"{workspace_root}/build"
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

    error_patterns = [
        r"(?i)\berror\b",
        r"(?i)fatal error",
        r"(?i)\bfailed\b",
        r"(?i)undefined reference",
        r"(?i)ld returned",
        r"(?i)cannot find -l",
        r"(?i)collect2:"
    ]
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

