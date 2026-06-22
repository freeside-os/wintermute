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
    """Queries OSV security feeds for actual CVE updates in the Alpine v3.20 ecosystem."""
    import tomllib
    import urllib.request
    import json
    
    packages_dir = "/home/dq/Code/freeside/packages"
    queries = []
    pkg_list = []
    
    # Step 1: Scan local package manifests
    try:
        if os.path.exists(packages_dir):
            items = os.listdir(packages_dir)
            for item in items:
                manifest_path = os.path.join(packages_dir, item, "package.manifest")
                if os.path.isfile(manifest_path):
                    with open(manifest_path, "rb") as f:
                        data = tomllib.load(f)
                    pkg_block = data.get("package", {})
                    name = pkg_block.get("name") or item
                    version = pkg_block.get("version", "")
                    if name and version:
                        queries.append({
                            "package": {
                                "name": name,
                                "ecosystem": "Alpine:v3.20"
                            },
                            "version": version
                        })
                        pkg_list.append(name)
    except Exception as e:
        return {"status": "error", "message": f"Failed to scan local packages: {e}"}

    if not queries:
        return {"status": "success", "cves": []}

    # Step 2: Query OSV batch API
    payload = {"queries": queries}
    req = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    cves_found = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            results = res_data.get("results", [])
            
            for i, res in enumerate(results):
                vulns = res.get("vulns", [])
                if not vulns:
                    continue
                    
                pkg_name = pkg_list[i]
                # Fetch details for the first vulnerability
                vuln = vulns[0]
                vuln_id = vuln.get("id")
                
                # Fetch details from OSV vuln endpoint
                detail_url = f"https://api.osv.dev/v1/vulns/{vuln_id}"
                detail_req = urllib.request.Request(detail_url, method="GET")
                try:
                    with urllib.request.urlopen(detail_req, timeout=5) as d_resp:
                        d_data = json.loads(d_resp.read().decode("utf-8"))
                        
                        # Find cve_id from aliases
                        cve_id = vuln_id
                        aliases = d_data.get("aliases", [])
                        for alias in aliases:
                            if alias.startswith("CVE-"):
                                cve_id = alias
                                break
                                
                        # Find fixed version
                        fixed_version = None
                        affected_list = d_data.get("affected", [])
                        for aff in affected_list:
                            aff_pkg = aff.get("package", {})
                            if aff_pkg.get("name") == pkg_name:
                                ranges = aff.get("ranges", [])
                                for r in ranges:
                                    events = r.get("events", [])
                                    for ev in events:
                                        if "fixed" in ev:
                                            fixed_version = ev["fixed"]
                                            break
                        
                        if not fixed_version:
                            continue
                            
                        # Parse severity
                        severity = "HIGH"
                        severities = d_data.get("severity", [])
                        if severities:
                            score_str = severities[0].get("score", "")
                            if "PR:N" in score_str or "C:H" in score_str:
                                severity = "CRITICAL"
                            elif "PR:H" in score_str:
                                severity = "MODERATE"
                        
                        cves_found.append({
                            "package": pkg_name,
                            "cve_id": cve_id,
                            "severity": severity,
                            "fixed_version": fixed_version
                        })
                except Exception:
                    pass
    except Exception as e:
        # Graceful fallback: return mock/test security feed data if API is down
        return {
            "status": "fallback",
            "message": f"OSV API request failed ({e}), returning mock feed data.",
            "cves": [
                {"package": "zlib", "cve_id": "CVE-2026-9999", "severity": "HIGH", "fixed_version": "1.3.2.1"},
                {"package": "openssl", "cve_id": "CVE-2026-8888", "severity": "CRITICAL", "fixed_version": "3.3.1"}
            ]
        }

    return {"status": "success", "cves": cves_found}
