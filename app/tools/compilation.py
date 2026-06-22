# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import os
import re
import subprocess


def verify_package(pkg_name: str) -> dict:
    """Verifies the package recipe and manifest validity.

    Args:
        pkg_name: Name of the package to verify.

    Returns:
        A dictionary containing the verification status and output.
    """
    packages_dir = "/home/dq/Code/freeside/packages"
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

def build_package(pkg_name: str) -> dict:
    """Builds a package inside the systemd-nspawn sandboxed container core.

    Args:
        pkg_name: Name of the package to build.

    Returns:
        A dictionary containing the build status, stdout, and stderr.
    """
    env = os.environ.copy()
    env["STRAYLIGHT_PACKAGES_ROOT"] = "/home/dq/Code/freeside/packages"
    env["STRAYLIGHT_BUILDER_ROOT"] = "/home/dq/Code/freeside/build"
    env["STRAYLIGHT_BUILDER_OUTPUT_ROOT"] = "/home/dq/Code/freeside/build/packages"
    try:
        res = subprocess.run(
            ["sudo", "-E", "/home/dq/Code/freeside/build/straylight", "build", "--pkg", pkg_name],
            cwd="/home/dq/Code/freeside",
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
    log_dir = "/home/dq/Code/freeside/build"
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
    error_patterns = [
        r"(?i)\berror\b",
        r"(?i)fatal error",
        r"(?i)undefined reference",
        r"(?i)ld returned",
        r"(?i)cannot find -l",
        r"(?i)collect2:"
    ]
    matched_lines = []
    for i, line in enumerate(lines):
        for pattern in error_patterns:
            if re.search(pattern, line):
                matched_lines.append(f"Line {i+1}: {line}")
                break
    if not matched_lines:
        # If no explicit errors are found, return the last 100 lines as a fallback
        fallback_lines = lines[-100:]
        return "No specific error patterns matched. Showing last 100 lines of log:\n" + "\n".join(fallback_lines)
    return "\n".join(matched_lines)

