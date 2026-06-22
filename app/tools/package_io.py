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

import hashlib
import os
import re
import urllib.request


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

        with open(justfile_path, encoding="utf-8") as f:
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
        with open(manifest_path, encoding="utf-8") as f:
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
        return {"status": "error", "message": f"Upgrade failed: {e!s}"}

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
        with open(path, encoding="utf-8") as f:
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

def fetch_source_checksum(url: str) -> dict:
    """Downloads the source package locally to calculate its SHA-256 checksum.

    Args:
        url: The URL of the source package to download.

    Returns:
        A dictionary with the success status and the computed sha256 hex digest, or error details.
    """
    try:
        tmp_file, _ = urllib.request.urlretrieve(url)
        h = hashlib.sha256()
        with open(tmp_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        os.remove(tmp_file)
        return {"status": "success", "sha256": h.hexdigest()}
    except Exception as e:
        return {"status": "error", "message": str(e)}

