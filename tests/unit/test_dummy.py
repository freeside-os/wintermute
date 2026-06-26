def test_dependency_graph() -> None:
    from app.tools.dependency import (
        DependencyGraph,
        build_dependency_tree,
        check_version_constraints,
    )

    graph = DependencyGraph()

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
    assert check_version_constraints("zlib", ">=1.0.0")
    assert check_version_constraints("zlib", "<2.0.0")
    assert check_version_constraints("zlib", "==1.3.2")
    assert not check_version_constraints("zlib", "<1.0.0")
    assert not check_version_constraints("zlib", ">1.3.2")

    # Check dependency tree representation
    tree = build_dependency_tree("zlib")
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

    res = query_security_feeds()
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
    assert "sha256" in res
    assert len(res["sha256"]) == 64

def test_package_io() -> None:
    import os

    from app.tools.package_io import read_package_file, write_package_file

    # Write a dummy package file
    pkg = "dummy_test_package_io"
    filename = "test_io.txt"
    content = "Hello Wintermute IO!"

    res = write_package_file(pkg, filename, content)

    res_read = read_package_file(pkg, filename)
    assert res_read["status"] == "success"
    assert res_read["content"] == content

    # Clean up
    packages_root = os.path.join(os.environ.get("WINTERMUTE_WORKSPACE_ROOT", "/home/dq/Code/freeside"), "packages")
    path = os.path.join(packages_root, pkg, filename)
    if os.path.exists(path):
        os.remove(path)
    pkg_dir = os.path.join(packages_root, pkg)
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
    write_package_file(pkg, "package.justfile", justfile_content)

    patch_content = "--- a/file.c\n+++ b/file.c\n@@ -1,1 +1,2 @@\n+patched\n"
    res = apply_patch(pkg, "src/file.c", patch_content)
    assert "patch_file" in res

    # Verify justfile was updated
    packages_root = os.path.join(os.environ.get("WINTERMUTE_WORKSPACE_ROOT", "/home/dq/Code/freeside"), "packages")
    justfile_path = os.path.join(packages_root, pkg, "package.justfile")
    assert os.path.exists(justfile_path)
    with open(justfile_path, encoding="utf-8") as f:
        updated_content = f.read()

    assert "patch -p1 -d src < /workspace/packages/$PKG_NAME/patches/" in updated_content

    # Clean up
    patches_dir = os.path.join(packages_root, pkg, "patches")
    if os.path.exists(patches_dir):
        for f_name in os.listdir(patches_dir):
            os.remove(os.path.join(patches_dir, f_name))
        os.rmdir(patches_dir)
    if os.path.exists(justfile_path):
        os.remove(justfile_path)
    pkg_dir = os.path.join(packages_root, pkg)
    if os.path.exists(pkg_dir):
        try:
            os.rmdir(pkg_dir)
        except Exception:
            pass


