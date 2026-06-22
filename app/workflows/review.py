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

import tomllib
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


class ReviewWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    refiner_agent: Agent
    builder_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        pkg_name = state.get("pkg_name")

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Starting package review and compliance enforcement for '{pkg_name}'...")]
            )
        )

        # Resolve packages list
        from app.tools import list_workspace_packages
        list_res = list_workspace_packages()
        all_pkgs = set(list_res.get("packages", []))

        if pkg_name == "all":
            pkgs_to_review = list(all_pkgs)
        else:
            pkgs_to_review = [pkg_name]

        reject_reports = []
        pass_reports = []

        for pkg in pkgs_to_review:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Reviewing package [{pkg}]...")]
                )
            )

            # Check for critical dependency mismatch
            from app.tools import read_package_file
            manifest_res = read_package_file(pkg, "package.manifest")
            if manifest_res.get("status") == "error":
                reject_reports.append(f"Package [{pkg}]: Critical - package.manifest is missing or unreadable.")
                continue

            try:
                manifest_data = tomllib.loads(manifest_res.get("content", ""))
                pkg_block = manifest_data.get("package", {})
                build_block = manifest_data.get("build", {})

                deps = pkg_block.get("dependencies", []) + build_block.get("dependencies", [])
                missing_deps = [d for d in deps if d not in all_pkgs]

                if missing_deps:
                    reject_reports.append(f"Package [{pkg}]: REJECTED - Lists dependencies that do not exist in the workspace: {', '.join(missing_deps)}")
                    continue
            except Exception as e:
                reject_reports.append(f"Package [{pkg}]: Critical - Failed to parse package.manifest: {e}")
                continue

            # Check for minor issues
            readme_res = read_package_file(pkg, "README.md")
            has_readme = readme_res.get("status") == "success"

            from app.tools import verify_package
            verify_res = verify_package(pkg)
            is_valid = verify_res.get("status") == "success"

            minor_issues = []
            if not has_readme:
                minor_issues.append("Missing README.md")
            if not is_valid:
                minor_issues.append("Validation errors in manifest/justfile")

            if minor_issues:
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Package [{pkg}]: Minor issues found: {', '.join(minor_issues)}. Activating Recipe Refiner Agent to enforce guidance...")]
                    )
                )
                # Let refiner agent fix the files.
                state["pkg_name"] = pkg
                async for event in self.refiner_agent.run_async(ctx):
                    yield event

            # Check if it builds
            from app.tools import build_package
            build_res = build_package(pkg)
            if build_res.get("status") == "error":
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Package [{pkg}]: Sandbox build failed. Activating Build & Fixer Agent to diagnose and apply patches...")]
                    )
                )
                state["pkg_name"] = pkg
                async for event in self.builder_agent.run_async(ctx):
                    yield event

                # Re-verify build
                build_res = build_package(pkg)
                if build_res.get("status") == "error":
                    reject_reports.append(f"Package [{pkg}]: REJECTED - Sandbox build compilation failed and could not be auto-patched.")
                    continue

            pass_reports.append(f"Package [{pkg}]: PASSED (Enforced successfully with all minor auto-fixes applied)")

        # Final output for CI
        report_lines = []
        if reject_reports:
            report_lines.append("### CI Status: REJECT")
            for rep in reject_reports:
                report_lines.append(f"- ✗ {rep}")
        else:
            report_lines.append("### CI Status: PASS")

        if pass_reports:
            report_lines.append("\n### Passed Packages:")
            for rep in pass_reports:
                report_lines.append(f"- ✓ {rep}")

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="\n".join(report_lines))]
            )
        )
