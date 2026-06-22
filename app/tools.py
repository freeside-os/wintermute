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
import glob
import re
import urllib.request
import hashlib

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
        with open(latest_log, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "status": "success",
            "file": os.path.basename(latest_log),
            "content": content
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to read log file {latest_log}: {e}"}

def apply_patch(pkg_name: str, target_file: str, patch_content: str) -> dict:
    """Generates and writes a patch file to packages/<pkg_name>/patches/ and registers it in package.justfile.

    Args:
        pkg_name: Name of the package.
        target_file: The relative path of the file to be patched inside the source tree.
        patch_content: Unified diff patch content.

    Returns:
        A dictionary indicating success or failure.
    """
    patches_dir = f"/home/dq/Code/freeside/packages/{pkg_name}/patches"
    os.makedirs(patches_dir, exist_ok=True)
    
    existing = [f for f in os.listdir(patches_dir) if f.endswith(".patch")]
    index = len(existing) + 1
    clean_target = os.path.basename(target_file).replace(".", "_")
    patch_filename = f"{index:04d}-fix-{clean_target}.patch"
    patch_path = os.path.join(patches_dir, patch_filename)
    
    try:
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(patch_content)
            
        justfile_path = f"/home/dq/Code/freeside/packages/{pkg_name}/package.justfile"
        if not os.path.exists(justfile_path):
            return {"status": "error", "message": f"package.justfile not found at {justfile_path}"}
            
        with open(justfile_path, "r", encoding="utf-8") as f:
            justfile_content = f.read()
            
        # Determine source dir in justfile to target patch -d option
        cd_match = re.search(r"cd\s+([^\s&|;]+)", justfile_content)
        src_dir_in_justfile = cd_match.group(1) if cd_match else None
        
        if src_dir_in_justfile:
            patch_cmd = f"patch -p1 -d {src_dir_in_justfile} < /workspace/packages/$PKG_NAME/patches/{patch_filename}"
        else:
            patch_cmd = f"patch -p1 < /workspace/packages/$PKG_NAME/patches/{patch_filename}"
            
        lines = justfile_content.splitlines()
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if ("tar -xf" in line or "tar -Jxf" in line or "tar -zxf" in line) and not inserted:
                new_lines.append(f"\t{patch_cmd}")
                inserted = True
                
        if not inserted:
            new_lines = []
            for line in lines:
                new_lines.append(line)
                if line.startswith("build:") and not inserted:
                    new_lines.append(f"\t{patch_cmd}")
                    inserted = True
                    
        with open(justfile_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
            
        return {
            "status": "success",
            "patch_file": patch_filename,
            "message": f"Successfully created patch {patch_filename} and registered in package.justfile"
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

def upgrade_package_version(pkg_name: str, new_version: str) -> dict:
    """Bumps package version, updates URLs, downloads new sources to compute SHA256 checksums, and updates package.manifest.

    Args:
        pkg_name: Name of the package.
        new_version: The new version string to upgrade to.

    Returns:
        A dictionary indicating success or failure.
    """
    manifest_path = f"/home/dq/Code/freeside/packages/{pkg_name}/package.manifest"
    if not os.path.exists(manifest_path):
        return {"status": "error", "message": f"Manifest not found at {manifest_path}"}
    
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        ver_match = re.search(r'version\s*=\s*"([^"]+)"', content)
        if not ver_match:
            return {"status": "error", "message": "Could not find version field in manifest"}
        old_version = ver_match.group(1)
        
        content = content.replace(f'version = "{old_version}"', f'version = "{new_version}"')
        
        urls = re.findall(r'url\s*=\s*"([^"]+)"', content)
        for url in urls:
            if old_version in url:
                new_url = url.replace(old_version, new_version)
                content = content.replace(url, new_url)
                
                # Download to compute new hash
                tmp_file, _ = urllib.request.urlretrieve(new_url)
                h = hashlib.sha256()
                with open(tmp_file, "rb") as tf:
                    for chunk in iter(lambda: tf.read(65536), b""):
                        h.update(chunk)
                new_hash = h.hexdigest()
                os.remove(tmp_file)
                
                url_escaped = re.escape(new_url)
                pattern = rf'{url_escaped}.*?value\s*=\s*"([^"]+)"'
                val_match = re.search(pattern, content, re.DOTALL)
                if val_match:
                    old_val = val_match.group(1)
                    content = content.replace(f'value = "{old_val}"', f'value = "{new_hash}"', 1)
                    
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        return {
            "status": "success",
            "message": f"Successfully upgraded {pkg_name} from {old_version} to {new_version} and updated checksums."
        }
    except Exception as e:
        return {"status": "error", "message": f"Upgrade failed: {str(e)}"}

def read_package_file(pkg_name: str, filename: str) -> dict:
    """Reads a file within a package's directory.

    Args:
        pkg_name: Name of the package.
        filename: Name of the file (e.g. 'package.manifest' or 'package.justfile').

    Returns:
        A dictionary containing the status and file content.
    """
    path = f"/home/dq/Code/freeside/packages/{pkg_name}/{filename}"
    if not os.path.exists(path):
        return {"status": "error", "message": f"File {filename} not found for package {pkg_name} at {path}."}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"status": "success", "content": content}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def write_package_file(pkg_name: str, filename: str, content: str) -> dict:
    """Writes or overwrites a file within a package's directory.

    Args:
        pkg_name: Name of the package.
        filename: Name of the file to write (e.g. 'package.manifest' or 'package.justfile').
        content: The content to write to the file.

    Returns:
        A dictionary indicating success or failure.
    """
    path = f"/home/dq/Code/freeside/packages/{pkg_name}/{filename}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "message": f"Successfully wrote {filename}."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
