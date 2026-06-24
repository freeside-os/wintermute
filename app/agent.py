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

import json
import os
import re
from collections.abc import AsyncGenerator

import google.auth
from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App
from google.adk.events import Event
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.genai import types
from pydantic import ConfigDict

from . import services

# Try to find and load .env manually to avoid dependency issues
for env_path in [".env", "../.env", "../../.env", "app/.env"]:
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
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

from app.tools import (  # noqa: E402
    apply_patch,
    build_package,
    fetch_source_checksum,
    list_packages,
    list_workspace_packages,
    query_security_feeds,
    read_build_logs,
    read_package_file,
    save_session_to_memory,
    search_memory,
    verify_package,
    write_package_file,
)

# ------------------------------------------------------------------------------
# 1. Importer / Triage Agent
# ------------------------------------------------------------------------------
triage_agent = Agent(
    name="triage_agent",
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Importer/Triage Agent for Freeside OS.\n"
        "Analyze the user request and query security/package feeds to determine package classification and action.\n"
        "First, identify the target package name (or set to 'all' if the request is to review all packages).\n"
        "Determine if the request is a new package 'import' (from Arch Linux), a new package 'create' (from scratch/scaffold), a package build 'fix', a version 'upgrade', a 'review' of package quality/guidelines, or a 'security_audit'.\n"
        "Use `query_security_feeds` to check if there are high-severity CVEs for the package. "
        "If a high-severity CVE is found and the version is upgraded, classify the package upgrade as a security update (set is_security_update to true).\n"
        "Classify the package into the correct group: base, builder, system, linux, server, desktop, or extra.\n"
        "Use list_workspace_packages if you need to check existing packages.\n"
        "Provide your output in a clean JSON block in your response matching:\n"
        "{\n"
        '  "pkg_name": "package-name" or "all",\n'
        '  "action": "import" or "create" or "fix" or "upgrade" or "review" or "security_audit",\n'
        '  "version": "version-string" or null,\n'
        '  "group": "group-name" or null,\n'
        '  "is_security_update": true or false\n'
        "}\n\n"
        "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
        "When analyzing a packaging request or troubleshooting a build failure:\n"
        "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
        "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
    ),
    tools=[list_workspace_packages, list_packages, query_security_feeds, search_memory],
    output_key="triage_output"
)

# ------------------------------------------------------------------------------
# 2. Recipe Refiner Agent
# ------------------------------------------------------------------------------
refiner_agent = Agent(
    name="refiner_agent",
    model=Gemini(
        model="gemini-3.5-flash",
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
        "6. Automated Source Extraction & Archive Traversal Checks: Parse the sources list in the package manifest (package.manifest). "
        "Verify if a corresponding extraction command (such as tar -xf, tar -Jxf, tar -zxf, or unzip) for any .tar.* or compressed sources is "
        "present in the package.justfile build target. Ensure that compile/configure commands run inside the correct extracted directory path "
        "(e.g. cd $PKG_NAME-* or cd $PKG_NAME-$PKG_VERSION). Use fetch_source_checksum to calculate SHA-256 checksums for any new or modified package source URLs.\n"
        "After editing the files, print a confirmation message outlining what you changed.\n\n"
        "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
        "When analyzing a packaging request or troubleshooting a build failure:\n"
        "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
        "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
    ),
    tools=[read_package_file, write_package_file, fetch_source_checksum, search_memory]
)

# ------------------------------------------------------------------------------
# 3. Build & Fixer Agent
# ------------------------------------------------------------------------------
builder_agent = Agent(
    name="builder_agent",
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Build & Fixer Agent for Freeside OS.\n"
        "Your task is to run the sandbox compilation loop, inspect logs on failure, apply patches, and ensure validation passes.\n"
        "Steps:\n"
        "1. Run `verify_package` to check the package recipe's validity.\n"
        "2. Run `build_package` to compile the package inside the sandboxed container.\n"
        "   - During debugging and iterative fixing attempts, set `keep_sandbox=True` to speed up compilations using incremental builds.\n"
        "   - Once the compilation succeeds and you are ready for final verification, run `build_package` with `keep_sandbox=False` (or omit it) to ensure a clean, reproducible build from scratch.\n"
        "3. If the build fails, run `read_build_logs` to get the filtered stderr compile output. Analyze the error. Use the following Musl & Toolchain troubleshooting lookup table to determine the fix:\n"
        "   - Redefinition of Inline Functions: If compilation fails due to redefinition of inline functions or inline symbols (e.g. legacy C code compiled on modern GCC), inject `CFLAGS=\"-g -O2 -fgnu89-inline\"` into the build configuration/environment.\n"
        "   - Missing `argp`: If compilation fails due to missing `argp` functions/headers, add `argp-standalone` to the manifest's build dependencies (`build_dependencies` or `build.dependencies`) and append `LIBS=\"-largp\"` or `LDFLAGS=\"-largp\"` to the compilation command/environment.\n"
        "   - Missing Documentation Tools: If build fails due to missing document generators (e.g. `makeinfo`), auto-inject variables like `MAKEINFO=true` to prevent errors.\n"
        "   Then use `apply_patch` to write a patch file and register it in `package.justfile`, or edit the manifest/justfile using `write_package_file` if config changes are needed. Then rebuild and check again.\n"
        "4. Repeat until compile succeeds and verify_package passes successfully.\n"
        "5. README.md: After successful compilation and verification, read the README.md file using `read_package_file`. "
        "Under the '## Upgrade Notes' section, append any valuable extra information about this build run. "
        "For example, list any patches applied, specific compiler configurations required, or dependency resolution details "
        "that will be helpful for future updates. Write the updated README.md back using `write_package_file`.\n"
        "Output a short build report when done.\n\n"
        "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
        "When analyzing a packaging request or troubleshooting a build failure:\n"
        "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
        "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
    ),
    tools=[build_package, verify_package, read_build_logs, apply_patch, read_package_file, write_package_file, search_memory, save_session_to_memory]
)

def fix_mixed_tools_callback(callback_context, llm_request):
    if llm_request.config and llm_request.config.tools:
        has_builtin = False
        has_custom = False
        for tool in llm_request.config.tools:
            if getattr(tool, 'google_search', None) or getattr(tool, 'code_execution', None) or getattr(tool, 'google_search_retrieval', None) or getattr(tool, 'google_maps', None):
                has_builtin = True
            if getattr(tool, 'function_declarations', None):
                has_custom = True
        if has_builtin and has_custom:
            if not llm_request.config.tool_config:
                llm_request.config.tool_config = types.ToolConfig()
            llm_request.config.tool_config.include_server_side_tool_invocations = True

# ------------------------------------------------------------------------------
# 3.5. Scaffolder Agent
# ------------------------------------------------------------------------------
scaffold_agent = Agent(
    name="scaffolder",
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Scaffolder Agent for Freeside OS.\n"
        "Your task is to generate a package recipe from scratch.\n"
        "Steps:\n"
        "1. Use `google_search` to search the web for the package details, including its description, upstream source website/URL, and latest stable release archive URL (typically .tar.gz, .tar.xz, etc.).\n"
        "2. Once you find a suitable release archive URL, use the `fetch_source_checksum` tool to download the package and calculate its SHA-256 checksum.\n"
        "3. Generate a valid Freeside schema `package.manifest` and a basic `package.justfile` for the package, and write them using `write_package_file`.\n\n"
        "Requirements for package.manifest:\n"
        "- Must be in TOML format.\n"
        "- Root keys: `[package]`, `[build]`, and `[build.environment]` if env variables are needed.\n"
        "- Under `[package]`, include: `name` (must be the package name), `version`, `description`, and `group`.\n"
        "- Under `[build]`, include: `sources` as an array of tables containing `url` and `hash = { algo = \"sha256\", value = \"...\" }`.\n\n"
        "Requirements for package.justfile:\n"
        "- Must be a valid justfile format with a `build:` target and a `package:` target.\n"
        "- Under the `build:` target, extract the source archive (e.g. `tar -xf ...`), `cd` into the extracted folder (use a version-dynamic directory like `cd $PKG_NAME-*` or `cd $PKG_NAME-$PKG_VERSION`), run configure and make (or other build commands).\n"
        "- Under the `package:` target, copy built files/binaries to `$DESTDIR` (e.g. `make DESTDIR=\"$DESTDIR\" install` or manually copy files relative to `$DESTDIR` under `/usr/bin`, `/usr/lib`, etc.). Make sure to enforce chmod 755 on directories and binaries.\n\n"
        "Once you have written `package.manifest` and `package.justfile` successfully, print a confirmation report."
    ),
    tools=[google_search, write_package_file, read_package_file, fetch_source_checksum],
    before_model_callback=fix_mixed_tools_callback
)

# ------------------------------------------------------------------------------
# 4. Master Workflow Coordinator
# ------------------------------------------------------------------------------
class Workflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    triage_agent: Agent
    refiner_agent: Agent
    builder_agent: Agent
    scaffold_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        # Check if we are waiting for operator approval ( UpgradeWorkflow handles approval )
        if state.get("pending_approval"):
            from app.workflows.upgrade import UpgradeWorkflow
            wf = UpgradeWorkflow(
                name="upgrade_router",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
            async for event in wf.run_async(ctx):
                yield event
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
                pkg_match = re.search(r"(?:package|import|create|upgrade|fix)\s+([a-zA-Z0-9_\-]+)", triage_text, re.IGNORECASE)
                if pkg_match:
                    pkg_name = pkg_match.group(1)

            if not pkg_name:
                user_query = ""
                for ev in ctx.session.events:
                    if ev.author == "user" and ev.content and ev.content.parts:
                        user_query = ev.content.parts[0].text
                        break
                if "security" in user_query.lower() or "cve" in user_query.lower():
                    action = "security_audit"
                    pkg_name = "all"
                elif "review" in user_query.lower() or "check" in user_query.lower() or "enforce" in user_query.lower():
                    action = "review"
                    if "all" in user_query.lower():
                        pkg_name = "all"
                    else:
                        pkg_match = re.search(r"(?:review|check|validate|enforce)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
                        if pkg_match:
                            pkg_name = pkg_match.group(1)
                elif "create" in user_query.lower() or "scaffold" in user_query.lower() or "scratch" in user_query.lower():
                    action = "create"
                    pkg_match = re.search(r"(?:create|scaffold|scratch|new)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
                    if pkg_match:
                        pkg_name = pkg_match.group(1)
                elif "fix" in user_query.lower() or "patch" in user_query.lower() or "debug" in user_query.lower():
                    action = "fix"
                    pkg_match = re.search(r"(?:fix|patch|debug|repair)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
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

            if not action and ("security" in triage_text.lower() or "cve" in triage_text.lower()):
                action = "security_audit"
                pkg_name = "all"

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

        if not pkg_name and action != "security_audit":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="No target package name identified. I am the Wintermute packaging agent, designed to create, import, fix, upgrade, review, or audit packages for Freeside OS. Please specify a package name to proceed.")]
                )
            )
            return

        # Route to appropriate workflow subclass
        if action == "security_audit":
            from app.workflows.security import SecurityWorkflow
            workflow = SecurityWorkflow(
                name="security_workflow",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
        elif action == "review":
            from app.workflows.review import ReviewWorkflow
            workflow = ReviewWorkflow(
                name="review_workflow",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
        elif action == "upgrade":
            from app.workflows.upgrade import UpgradeWorkflow
            workflow = UpgradeWorkflow(
                name="upgrade_workflow",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
        elif action == "create":
            from app.workflows.create import CreateWorkflow
            workflow = CreateWorkflow(
                name="create_workflow",
                scaffold_agent=self.scaffold_agent,
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
        elif action == "fix":
            from app.workflows.fix import FixWorkflow
            workflow = FixWorkflow(
                name="fix_workflow",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
        else:
            from app.workflows.import_pkg import ImportWorkflow
            workflow = ImportWorkflow(
                name="import_workflow",
                scaffold_agent=self.scaffold_agent,
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )

        async for event in workflow.run_async(ctx):
            yield event

# Instantiate root Workflow coordinator agent
root_agent = Workflow(
    name="root_agent",
    triage_agent=triage_agent,
    refiner_agent=refiner_agent,
    builder_agent=builder_agent,
    scaffold_agent=scaffold_agent
)

app = App(
    root_agent=root_agent,
    name="app",
)
