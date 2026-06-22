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

import os
import subprocess

def import_pkgbuild(pkg_name: str) -> dict:
    """Converts an Arch Linux PKGBUILD to Freeside format.

    Args:
        pkg_name: The name of the package on Arch GitLab to convert.

    Returns:
        A dictionary containing the status of the command and its output.
    """
    packages_dir = "/home/dq/Code/freeside/packages"
    try:
        res = subprocess.run(
            ["python3", "fspack.py", "convert", pkg_name],
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

def list_workspace_packages() -> dict:
    """Lists all existing packages in the packages directory.

    Returns:
        A dictionary containing a list of package names.
    """
    packages_dir = "/home/dq/Code/freeside/packages"
    try:
        items = os.listdir(packages_dir)
        pkgs = []
        for item in items:
            manifest_path = os.path.join(packages_dir, item, "package.manifest")
            if os.path.isfile(manifest_path):
                pkgs.append(item)
        return {"status": "success", "packages": sorted(pkgs)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def list_packages() -> dict:
    """Lists all existing packages in the packages directory.

    Returns:
        A dictionary containing a list of package names.
    """
    return list_workspace_packages()

def query_security_feeds() -> dict:
    """Queries security feeds for mock CVE updates.

    Returns:
        A dictionary containing mock CVE update entries.
    """
    return {
        "status": "success",
        "cves": [
            {"package": "zlib", "cve_id": "CVE-2026-9999", "severity": "HIGH", "fixed_version": "1.3.1.1"},
            {"package": "openssl", "cve_id": "CVE-2026-8888", "severity": "CRITICAL", "fixed_version": "3.3.1"}
        ]
    }
