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

def test_dependency_graph() -> None:
    from app.tools.dependency import DependencyGraph, check_version_constraints, build_dependency_tree
    
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
