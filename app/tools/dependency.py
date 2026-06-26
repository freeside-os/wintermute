import os
import re
import tomllib

from app.app_utils.paths import packages_root as get_packages_root
from app.app_utils.paths import workspace_root as get_workspace_root


class DependencyGraph:
    """Represents a package dependency graph across the workspace."""

    def __init__(self, workspace_root: str | None = None):
        self.workspace_root = workspace_root or get_workspace_root()
        self.packages_dir = os.path.join(self.workspace_root, "packages")
        self.nodes = {}  # pkg_name -> manifest_data dict
        self.edges = {}  # pkg_name -> list of dependencies
        self.reverse_graph = {}  # pkg_name -> list of packages depending on it
        self.load_graph()

    def load_graph(self) -> None:
        """Loads the package dependency graph from the workspace."""
        if not os.path.exists(self.packages_dir):
            return

        try:
            items = os.listdir(self.packages_dir)
        except Exception:
            return

        # Step 1: Read all package manifests
        for item in items:
            manifest_path = os.path.join(self.packages_dir, item, "package.manifest")
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "rb") as f:
                        data = tomllib.load(f)

                    pkg_block = data.get("package", {})
                    name = pkg_block.get("name") or item
                    version = pkg_block.get("version", "")

                    # Extract dependencies
                    deps = set()
                    # 1. Package block dependencies
                    if isinstance(pkg_block.get("dependencies"), list):
                        deps.update(pkg_block["dependencies"])
                    # 2. Build block dependencies
                    build_block = data.get("build", {})
                    if isinstance(build_block.get("dependencies"), list):
                        deps.update(build_block["dependencies"])
                    # 3. Build environment block dependencies (precautionary)
                    build_env = build_block.get("environment", {})
                    if isinstance(build_env.get("dependencies"), list):
                        deps.update(build_env["dependencies"])

                    deps_list = sorted(deps)
                    self.nodes[name] = {
                        "version": version,
                        "group": pkg_block.get("group", "extra"),
                        "dependencies": deps_list,
                        "description": pkg_block.get("description", "")
                    }
                    self.edges[name] = deps_list
                except Exception:
                    pass

        # Step 2: Initialize reverse graph
        for pkg in self.nodes:
            self.reverse_graph[pkg] = []

        # Step 3: Populate reverse graph
        for pkg, deps in self.edges.items():
            for dep in deps:
                if dep in self.reverse_graph:
                    self.reverse_graph[dep].append(pkg)

    def get_dependencies(self, pkg_name: str) -> list[str]:
        """Gets direct dependencies for a package.

        Args:
            pkg_name: The name of the package.

        Returns:
            A list of dependency package names.
        """
        return self.edges.get(pkg_name, [])

    def get_dependents(self, pkg_name: str) -> list[str]:
        """Gets packages that depend on the given package (reverse dependencies).

        Args:
            pkg_name: The name of the package.

        Returns:
            A list of package names that depend on this package.
        """
        return sorted(self.reverse_graph.get(pkg_name, []))

    def find_cycles(self) -> list[list[str]]:
        """Detects dependency cycles in the graph using DFS path tracking.

        Returns:
            A list of cycles, each cycle being a list of package names.
        """
        visited = {}  # pkg -> state (0=unvisited, 1=visiting, 2=visited)
        cycles = []

        for node in self.nodes:
            visited[node] = 0

        def dfs(node: str, path: list[str]) -> None:
            visited[node] = 1
            path.append(node)

            for neighbor in self.get_dependencies(node):
                # We only track dependencies that actually exist in our workspace nodes
                if neighbor not in visited:
                    continue

                if visited[neighbor] == 1:
                    # Cycle detected: extract sub-path from neighbor to node
                    idx = path.index(neighbor)
                    cycles.append([*path[idx:], neighbor])
                elif visited[neighbor] == 0:
                    dfs(neighbor, path)

            path.pop()
            visited[node] = 2

        for node in self.nodes:
            if visited[node] == 0:
                dfs(node, [])

        return cycles

    def topological_sort(self) -> list[str]:
        """Returns packages in topological build order (dependencies before dependents).

        Returns:
            A list of package names. If a cycle is present, returns a partial sort.
        """
        visited = set()
        temp_visited = set()
        order = []

        def visit(node: str) -> None:
            if node in visited:
                return
            if node in temp_visited:
                # Cycle detected during sort - skip back-edge to avoid infinite recursion
                return
            temp_visited.add(node)
            for neighbor in self.get_dependencies(node):
                if neighbor in self.nodes:  # Only visit packages present in workspace
                    visit(neighbor)
            temp_visited.remove(node)
            visited.add(node)
            order.append(node)

        for node in self.nodes:
            if node not in visited:
                visit(node)

        return order


def _parse_version(ver_str: str) -> tuple:
    """Helper to split version strings into numeric/string parts for comparison."""
    parts = []
    for part in re.split(r"(\d+)", ver_str):
        if part.isdigit():
            parts.append(int(part))
        elif part:
            parts.append(part)
    return tuple(parts)


def check_version_constraints(pkg_name: str, constraint: str, workspace_root: str | None = None) -> bool:
    """Helper tool to check version constraints.

    Args:
        pkg_name: Name of the package.
        constraint: Version constraint expression (e.g. '>=1.3.0', '<2.0').
        workspace_root: Path to the workspace root.

    Returns:
        True if the current package version satisfies the constraints, False otherwise.
    """
    manifest_path = os.path.join(workspace_root or get_workspace_root(), "packages", pkg_name, "package.manifest")
    if not os.path.exists(manifest_path):
        return False

    try:
        with open(manifest_path, "rb") as f:
            data = tomllib.load(f)
        current_version = data.get("package", {}).get("version", "")
    except Exception:
        return False

    if not current_version or not constraint:
        return False

    # Extract comparison operator and comparison version
    match = re.match(r"\s*(>=|<=|>|<|==|=)?\s*([0-9A-Za-z\.\-\_]+)", constraint)
    if not match:
        return False

    op, target_version = match.groups()
    op = op or "=="

    curr_parsed = _parse_version(current_version)
    targ_parsed = _parse_version(target_version)

    if op == "==" or op == "=":
        return curr_parsed == targ_parsed
    elif op == ">=":
        return curr_parsed >= targ_parsed
    elif op == "<=":
        return curr_parsed <= targ_parsed
    elif op == ">":
        return curr_parsed > targ_parsed
    elif op == "<":
        return curr_parsed < targ_parsed

    return False


def build_dependency_tree(pkg_name: str, workspace_root: str | None = None) -> dict:
    """Helper tool to build dependency trees.

    Args:
        pkg_name: Name of the package.
        workspace_root: Path to the workspace root.

    Returns:
        A dictionary representation of the dependency tree.
    """
    graph = DependencyGraph(workspace_root=workspace_root or get_workspace_root())
    visited = set()

    def walk(node: str) -> dict:
        if node in visited:
            return {"package": node, "dependencies": [], "cycle": True}
        visited.add(node)

        deps = graph.get_dependencies(node)
        dep_trees = []
        for dep in deps:
            if dep in graph.nodes:
                dep_trees.append(walk(dep))
            else:
                dep_trees.append({"package": dep, "dependencies": [], "external": True})

        visited.remove(node)
        return {
            "package": node,
            "version": graph.nodes.get(node, {}).get("version", "unknown"),
            "dependencies": dep_trees
        }

    if pkg_name not in graph.nodes:
        return {"package": pkg_name, "error": "Package not found in workspace"}

    return walk(pkg_name)
