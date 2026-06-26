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

"""Unit test configuration: sets up a temporary workspace with fixture packages."""

import os
import shutil
import tempfile

import pytest


# Minimal package manifests required by test_dependency_graph assertions.
FIXTURE_PACKAGES = {
    "musl": """\
[package]
name = "musl"
version = "1.2.5"
description = "musl C library (libc) implementation for Freeside"
dependencies = []
group = "base"

[[sources]]
url = "https://musl.libc.org/releases/musl-1.2.5.tar.gz"
checksum = { algorithm = "sha256", value = "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4" }

[build]

[build.environment]
CONFIGURE_ARGS = "--prefix=/usr --syslibdir=/usr/lib"
""",
    "zlib": """\
[package]
name = "zlib"
version = "1.3.2"
description = "Compression library implementing the deflate compression method found in gzip and PKZIP"
group = "base"
dependencies = ["musl"]

[[sources]]
url = "https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.xz"
checksum = { algorithm = "sha256", value = "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3" }
""",
    "openssl": """\
[package]
name = "openssl"
version = "3.3.1"
description = "Secure Sockets Layer toolkit"
dependencies = ["musl"]
group = "base"

[[sources]]
url = "https://www.openssl.org/source/openssl-3.3.1.tar.gz"
checksum = { algorithm = "sha256", value = "777cd596284c883375a2a7a11bf5d2786fc5413255efab20c50d6ffe6d020b7e" }

[build]
dependencies = ["musl"]

[build.environment]
""",
    "curl": """\
[package]
name = "curl"
version = "8.8.0"
description = "Command line tool and library for transferring data with URLs"
dependencies = ["musl", "openssl"]
group = "base"

[[sources]]
url = "https://curl.se/download/curl-8.8.0.tar.xz"
checksum = { algorithm = "sha256", value = "0f58bb95fc330c8a46eeb3df5701b0d90c9d9bfcc42bd1cd08791d12551d4400" }

[build]

[build.environment]
CONFIGURE_ARGS = "--prefix=/usr --with-openssl --with-ca-bundle=/etc/ssl/certs/ca-certificates.crt"
dependencies = ["musl", "openssl"]
""",
}


@pytest.fixture(scope="session", autouse=True)
def temp_workspace(tmp_path_factory):
    """Creates a temporary workspace directory with fixture packages and sets WINTERMUTE_WORKSPACE_ROOT."""
    workspace = tmp_path_factory.mktemp("workspace", numbered=False)
    packages_dir = workspace / "packages"
    packages_dir.mkdir()

    for pkg_name, manifest_content in FIXTURE_PACKAGES.items():
        pkg_dir = packages_dir / pkg_name
        pkg_dir.mkdir()
        (pkg_dir / "package.manifest").write_text(manifest_content, encoding="utf-8")

    # Override the workspace root for the entire test session
    old_val = os.environ.get("WINTERMUTE_WORKSPACE_ROOT")
    os.environ["WINTERMUTE_WORKSPACE_ROOT"] = str(workspace)

    yield workspace

    # Restore original env
    if old_val is None:
        os.environ.pop("WINTERMUTE_WORKSPACE_ROOT", None)
    else:
        os.environ["WINTERMUTE_WORKSPACE_ROOT"] = old_val
