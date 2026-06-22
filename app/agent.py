# ruff: noqa
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
import re
import json
from typing import AsyncGenerator
from pydantic import ConfigDict

import google.auth
from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

# Try to find and load .env manually to avoid dependency issues
for env_path in [".env", "../.env", "../../.env", "app/.env"]:
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"\'')
                        os.environ[k] = v
        except Exception:
            pass

# Set GOOGLE_API_KEY if GEMINI_API_KEY is present
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

# Initialize credentials and default location for Vertex AI
has_vertex_creds = False
try:
    _, project_id = google.auth.default()
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    has_vertex_creds = True
except Exception:
    pass

if os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
elif has_vertex_creds:
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

# Import tools
from app.tools import (
    import_pkgbuild,
    verify_package,
    build_package,
    read_build_logs,
    apply_patch,
    list_packages,
    list_workspace_packages,
    query_security_feeds,
    upgrade_package_version,
    read_package_file,
    write_package_file,
)

# ------------------------------------------------------------------------------
# 1. Importer / Triage Agent
# ------------------------------------------------------------------------------
triage_agent = Agent(
    name="triage_agent",
    model=Gemini(
        model="gemini-3-flash-preview",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Importer/Triage Agent for Freeside OS.\n"
        "Analyze the user request and query security/package feeds to determine package classification and action.\n"
        "First, identify the target package name (or set to 'all' if the request is to review all packages).\n"
        "Determine if the request is a new package 'import', a version 'upgrade', or a 'review' of package quality/guidelines.\n"
        "Use `query_security_feeds` to check if there are high-severity CVEs for the package. "
        "If a high-severity CVE is found and the version is upgraded, classify the package upgrade as a security update (set is_security_update to true).\n"
        "Classify the package into the correct group: base, builder, system, server, or desktop.\n"
        "Use list_workspace_packages if you need to check existing packages.\n"
        "Provide your output in a clean JSON block in your response matching:\n"
        "{\n"
        '  "pkg_name": "package-name" or "all",\n'
        '  "action": "import" or "upgrade" or "review",\n'
        '  "version": "version-string" or null,\n'
        '  "group": "group-name" or null,\n'
        '  "is_security_update": true or false\n'
        "}"
    ),
    tools=[list_workspace_packages, list_packages, query_security_feeds],
    output_key="triage_output"
)

# ------------------------------------------------------------------------------
# 2. Recipe Refiner Agent
# ------------------------------------------------------------------------------
refiner_agent = Agent(
    name="refiner_agent",
    model=Gemini(
        model="gemini-3-flash-preview",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Recipe Refiner Agent for Freeside OS.\n"
        "Your task is to analyze and refine the package.manifest and package.justfile recipes for the target package.\n"
        "Use `read_package_file` to read the manifest, justfile, and README.md (if it exists). "
        "Write refinements back using `write_package_file`.\n"
        "Enforce the following rules:\n"
        "1. Version-Dynamic names: Do not hardcode package name or version in build/package targets. "
        "Use standard environment variables like $PKG_NAME, $PKG_VERSION or {{env_var(\"PKG_NAME\")}}.\n"
        "2. Destination Directory Injection: Ensure all install/package commands write relative to $DESTDIR.\n"
        "3. Strict permissions: Enforce chmod 755 on directories and binaries at the end of the package step.\n"
        "4. UsrMerge & Musl compliance: Install binaries under /usr (prefix=/usr, sbindir=/usr/bin, libdir=/usr/lib), "
        "and strip glibc-specific extensions.\n"
        "5. README.md: Check if README.md exists. If it does, ensure the Markdown table in README.md is updated "
        "to reflect the current name, version, source URL, and checksum from the package manifest.\n"
        "After editing the files, print a confirmation message outlining what you changed."
    ),
    tools=[read_package_file, write_package_file]
)

# ------------------------------------------------------------------------------
# 3. Build & Fixer Agent
# ------------------------------------------------------------------------------
builder_agent = Agent(
    name="builder_agent",
    model=Gemini(
        model="gemini-3-flash-preview",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Build & Fixer Agent for Freeside OS.\n"
        "Your task is to run the sandbox compilation loop, inspect logs on failure, apply patches, and ensure validation passes.\n"
        "Steps:\n"
        "1. Run `verify_package` to check the package recipe's validity.\n"
        "2. Run `build_package` to compile the package inside the sandboxed container container.\n"
        "3. If the build fails, run `read_build_logs` to get the stderr compile output. "
        "Analyze the error (e.g. missing dependencies, Musl header incompatibilities) and use `apply_patch` to "
        "write a patch file and register it in package.justfile. Then rebuild and check again.\n"
        "4. Repeat until compile succeeds and verify_package passes successfully.\n"
        "5. README.md: After successful compilation and verification, read the README.md file using `read_package_file`. "
        "Under the '## Upgrade Notes' section, append any valuable extra information about this build run. "
        "For example, list any patches applied, specific compiler configurations required, or dependency resolution details "
        "that will be helpful for future updates. Write the updated README.md back using `write_package_file`.\n"
        "Output a short build report when done."
    ),
    tools=[build_package, verify_package, read_build_logs, apply_patch, read_package_file, write_package_file]
)

# ------------------------------------------------------------------------------
# 4. Master Workflow Coordinator
# ------------------------------------------------------------------------------
class Workflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    triage_agent: Agent
    refiner_agent: Agent
    builder_agent: Agent
    
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        
        # Check if we are waiting for operator approval
        if state.get("pending_approval"):
            # Get the operator's decision from the latest user message
            last_event = ctx.session.events[-1]
            user_msg = ""
            if last_event.author == "user" and last_event.content and last_event.content.parts:
                user_msg = last_event.content.parts[0].text.lower()
                
            if "yes" in user_msg or "approve" in user_msg:
                pkg_name = state.get("pkg_name")
                version = state.get("version")
                state["pending_approval"] = False
                state["approved"] = True
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Operator approved! Promoting package {pkg_name} version {version} to base/system distribution channels. Workflow complete. ✓")]
                    )
                )
                return
            else:
                pkg_name = state.get("pkg_name")
                state["pending_approval"] = False
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Upgrade aborted by operator. Package {pkg_name} was not promoted.")]
                    )
                )
                return
        
        # Step 1: Run Triage Agent if not already done
        if not state.get("triage_done"):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Starting Wintermute Packaging Workflow Coordinator. Activating Importer/Triage Agent...")]
                )
            )
            
            # Execute triage agent
            async for event in self.triage_agent.run_async(ctx):
                yield event
                
            # Extract output and set variables in state
            triage_text = state.get("triage_output", "")
            if not triage_text:
                for ev in reversed(ctx.session.events):
                    if ev.author == self.triage_agent.name and ev.content and ev.content.parts:
                        triage_text = ev.content.parts[0].text
                        break
            
            pkg_name = None
            action = "import"
            version = None
            is_security_update = False
            group = "extra"
            
            try:
                json_match = re.search(r"\{.*\}", triage_text, re.DOTALL)
                if json_match:
                    triage_data = json.loads(json_match.group(0))
                    pkg_name = triage_data.get("pkg_name") or triage_data.get("package")
                    action = triage_data.get("action", "import").lower()
                    version = triage_data.get("version")
                    is_security_update = triage_data.get("is_security_update", False)
                    group = triage_data.get("group", "extra")
            except Exception:
                pass
                
            if not pkg_name:
                pkg_match = re.search(r"(?:package|import|upgrade)\s+([a-zA-Z0-9_\-]+)", triage_text, re.IGNORECASE)
                if pkg_match:
                    pkg_name = pkg_match.group(1)
            
            if not pkg_name:
                user_query = ""
                for ev in ctx.session.events:
                    if ev.author == "user" and ev.content and ev.content.parts:
                        user_query = ev.content.parts[0].text
                        break
                if "review" in user_query.lower() or "check" in user_query.lower() or "enforce" in user_query.lower():
                    action = "review"
                    if "all" in user_query.lower():
                        pkg_name = "all"
                    else:
                        pkg_match = re.search(r"(?:review|check|validate|enforce)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
                        if pkg_match:
                            pkg_name = pkg_match.group(1)
                else:
                    pkg_match = re.search(r"(?:import|build|upgrade)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
                    if pkg_match:
                        pkg_name = pkg_match.group(1)
                        if "upgrade" in user_query.lower():
                            action = "upgrade"
                        ver_match = re.search(r"to\s+(?:version\s+)?([0-9\.]+)", user_query, re.IGNORECASE)
                        if ver_match:
                            version = ver_match.group(1)
            
            state["pkg_name"] = pkg_name
            state["action"] = action
            state["version"] = version
            state["is_security_update"] = is_security_update
            state["group"] = group
            state["triage_done"] = True
            
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=
                        f"Triage Complete:\n"
                        f"- Package: {pkg_name}\n"
                        f"- Action: {action}\n"
                        f"- Version: {version}\n"
                        f"- Group: {group}\n"
                        f"- Security Update: {is_security_update}"
                    )]
                )
            )
            
        pkg_name = state.get("pkg_name")
        action = state.get("action", "import")
        version = state.get("version")
        is_security_update = state.get("is_security_update", False)
        group = state.get("group", "extra")
        
        if not pkg_name:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Error: Could not determine target package name. Workflow aborted.")]
                )
            )
            return

        # Step 2: Perform the review workflow if requested
        if action == "review":
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
                    
                import tomllib
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
            return

        # Step 3: Perform the package acquisition (Import or Upgrade)
        if action == "upgrade":
            if not state.get("upgrade_done"):
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Running package version upgrade to {version}...")]
                    )
                )
                from app.tools import upgrade_package_version
                res = upgrade_package_version(pkg_name, version)
                state["upgrade_result"] = res
                state["upgrade_done"] = True
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Upgrade tool result: {res}")]
                    )
                )
        else:
            if not state.get("import_done"):
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Importing package {pkg_name} PKGBUILD from Arch Linux...")]
                    )
                )
                from app.tools import import_pkgbuild
                res = import_pkgbuild(pkg_name)
                state["import_result"] = res
                state["import_done"] = True
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Import tool result: {res}")]
                    )
                )

        # Step 4: Run Recipe Refiner Agent
        if not state.get("refiner_done"):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Activating Recipe Refiner Agent to check and adapt manifest & justfile...")]
                )
            )
            async for event in self.refiner_agent.run_async(ctx):
                yield event
            state["refiner_done"] = True

        # Step 5: Run Build & Fixer Agent
        if not state.get("builder_done"):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Activating Build & Fixer Agent to compile package inside sandbox...")]
                )
            )
            async for event in self.builder_agent.run_async(ctx):
                yield event
            state["builder_done"] = True

        # Step 6: Operator approval or autonomous promotion
        if action == "upgrade" and not is_security_update:
            state["pending_approval"] = True
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=
                        f"The package '{pkg_name}' has been successfully upgraded to version {version} and successfully compiled/verified in the container sandbox.\n"
                        f"Operator confirmation required for promotion. Do you approve promoting this package? (Reply with 'yes' or 'no')"
                    )]
                )
            )
        else:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Promotion successful! Package {pkg_name} is fully verified and promoted autonomously. Workflow complete. ✓")]
                )
            )

# Instantiate root Workflow coordinator agent
root_agent = Workflow(
    name="root_agent",
    triage_agent=triage_agent,
    refiner_agent=refiner_agent,
    builder_agent=builder_agent
)

app = App(
    root_agent=root_agent,
    name="app",
)
