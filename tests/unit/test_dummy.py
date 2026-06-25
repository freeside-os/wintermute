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

def test_dependency_graph(tmpdir) -> None:
    import os
    import tomllib
    from app.tools.dependency import (
        DependencyGraph,
        build_dependency_tree,
        check_version_constraints,
    )

    workspace = str(tmpdir)
    packages_dir = os.path.join(workspace, "packages")
    os.makedirs(os.path.join(packages_dir, "zlib"))
    os.makedirs(os.path.join(packages_dir, "musl"))
    os.makedirs(os.path.join(packages_dir, "curl"))
    os.makedirs(os.path.join(packages_dir, "openssl"))

    with open(os.path.join(packages_dir, "zlib", "package.manifest"), "w") as f:
        f.write('[package]\nname="zlib"\nversion="1.3.2"\ndependencies=["musl"]')
    with open(os.path.join(packages_dir, "musl", "package.manifest"), "w") as f:
        f.write('[package]\nname="musl"\nversion="1.2.4"')
    with open(os.path.join(packages_dir, "curl", "package.manifest"), "w") as f:
        f.write('[package]\nname="curl"\nversion="8.7.1"\ndependencies=["musl", "openssl"]')
    with open(os.path.join(packages_dir, "openssl", "package.manifest"), "w") as f:
        f.write('[package]\nname="openssl"\nversion="3.3.1"')

    graph = DependencyGraph(workspace_root=workspace)

    # Assert nodes are successfully parsed from the workspace
    assert len(graph.nodes) > 0
    assert "zlib" in graph.nodes
    assert "curl" in graph.nodes

    # Check direct dependencies
    zlib_deps = graph.get_dependencies("zlib")
    assert "musl" in zlib_deps

    curl_deps = graph.get_dependencies("curl")
    assert "musl" in curl_deps
    assert "openssl" in curl_deps

    # Check dependents (reverse dependencies)
    musl_dependents = graph.get_dependents("musl")
    assert "zlib" in musl_dependents
    assert "curl" in musl_dependents

    # Check topological sort order
    order = graph.topological_sort()
    assert len(order) > 0
    # Dependencies must precede dependents in order
    assert order.index("musl") < order.index("zlib")
    assert order.index("openssl") < order.index("curl")

    # Check cycle detection (should be clean)
    cycles = graph.find_cycles()
    assert len(cycles) == 0

    # Check version constraints
    # zlib is version 1.3.2 in local workspace
    assert check_version_constraints("zlib", ">=1.0.0", workspace_root=workspace)
    assert check_version_constraints("zlib", "<2.0.0", workspace_root=workspace)
    assert check_version_constraints("zlib", "==1.3.2", workspace_root=workspace)
    assert not check_version_constraints("zlib", "<1.0.0", workspace_root=workspace)
    assert not check_version_constraints("zlib", ">1.3.2", workspace_root=workspace)

    # Check dependency tree representation
    tree = build_dependency_tree("zlib", workspace_root=workspace)
    assert tree["package"] == "zlib"
    assert any(d["package"] == "musl" for d in tree["dependencies"])



def test_query_security_feeds() -> None:
    import json
    import os

    from app.tools.feeds import query_security_feeds

    cache_file = os.path.expanduser("~/.cache/wintermute/security_feeds_cache.json")
    if os.path.exists(cache_file):
        try:
            os.remove(cache_file)
        except Exception:
            pass

    res = query_security_feeds(workspace_root="tests/test_data")
    assert res["status"] in ("success", "fallback")
    assert "cves" in res

    # Verify cache file was successfully created
    assert os.path.exists(cache_file)
    with open(cache_file) as f:
        cached = json.load(f)
    assert "timestamp" in cached
    assert cached["data"]["status"] == res["status"]

def test_fetch_source_checksum() -> None:
    from app.tools.package_io import fetch_source_checksum
    res = fetch_source_checksum("https://www.google.com/robots.txt")
    assert res["status"] == "success"
    assert "sha256" in res
    assert len(res["sha256"]) == 64

def test_package_io() -> None:
    import os

    from app.tools.package_io import read_package_file, write_package_file

    # Write a dummy package file
    pkg = "dummy_test_package_io"
    filename = "test_io.txt"
    content = "Hello Wintermute IO!"

    res = write_package_file(pkg, filename, content, workspace_root="tests/test_data")
    assert res["status"] == "success"

    res_read = read_package_file(pkg, filename, workspace_root="tests/test_data")
    assert res_read["status"] == "success"
    assert res_read["content"] == content

    # Clean up
    path = f"tests/test_data/packages/{pkg}/{filename}"
    if os.path.exists(path):
        os.remove(path)
    pkg_dir = f"tests/test_data/packages/{pkg}"
    if os.path.exists(pkg_dir):
        try:
            os.rmdir(pkg_dir)
        except Exception:
            pass

def test_apply_patch() -> None:
    import os

    from app.tools.package_io import apply_patch, write_package_file

    pkg = "dummy_test_patch"
    justfile_content = "build:\n\ttar -xf source.tar.gz\n\tcd src && make\n"

    # Setup dummy package justfile
    write_package_file(pkg, "package.justfile", justfile_content, workspace_root="tests/test_data")

    patch_content = "--- a/file.c\n+++ b/file.c\n@@ -1,1 +1,2 @@\n+patched\n"
    res = apply_patch(pkg, "src/file.c", patch_content, workspace_root="tests/test_data")
    assert res["status"] == "success"
    assert "patch_file" in res

    # Verify justfile was updated
    justfile_path = f"tests/test_data/packages/{pkg}/package.justfile"
    assert os.path.exists(justfile_path)
    with open(justfile_path, encoding="utf-8") as f:
        updated_content = f.read()

    assert "patch -p1 -d src < /workspace/packages/$PKG_NAME/patches/" in updated_content

    # Clean up
    patches_dir = f"tests/test_data/packages/{pkg}/patches"
    if os.path.exists(patches_dir):
        for f_name in os.listdir(patches_dir):
            os.remove(os.path.join(patches_dir, f_name))
        os.rmdir(patches_dir)
    os.remove(justfile_path)
    pkg_dir = f"tests/test_data/packages/{pkg}"
    if os.path.exists(pkg_dir):
        try:
            os.rmdir(pkg_dir)
        except Exception:
            pass

