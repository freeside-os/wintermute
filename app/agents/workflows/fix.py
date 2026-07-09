import re
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict

from app.tools import (
    apply_patch,
    build_package,
    read_package_file,
    scan_build_log,
    verify_package,
    write_package_file,
)


def inject_env_into_manifest(pkg_name: str, env_vars: dict[str, str]) -> bool:
    """Updates/adds environment variables in the package.manifest file under [build.environment]."""
    res = read_package_file(pkg_name, "package.manifest")
    if res.get("status") != "success":
        return False
    content = res.get("content", "")

    has_build_env = "[build.environment]" in content or "[build.env]" in content

    if has_build_env:
        lines = content.splitlines()
        new_lines = []
        in_build_env = False
        variables_to_inject = env_vars.copy()

        for line in lines:
            if line.strip() in ("[build.environment]", "[build.env]"):
                in_build_env = True
                new_lines.append(line)
                continue

            if in_build_env and line.strip().startswith("[") and not line.strip().startswith("[build.environment") and not line.strip().startswith("[build.env"):
                for k, v in list(variables_to_inject.items()):
                    new_lines.append(f'{k} = "{v}"')
                    variables_to_inject.pop(k)
                in_build_env = False

            if in_build_env:
                match = re.match(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*)', line)
                if match:
                    key = match.group(1)
                    if key in variables_to_inject:
                        val = variables_to_inject.pop(key)
                        new_lines.append(f'{key} = "{val}"')
                        continue

            new_lines.append(line)

        for k, v in variables_to_inject.items():
            new_lines.append(f'{k} = "{v}"')

        content = "\n".join(new_lines) + "\n"
    else:
        content = content.rstrip() + "\n\n[build.environment]\n"
        for k, v in env_vars.items():
            content += f'{k} = "{v}"\n'

    write_res = write_package_file(pkg_name, "package.manifest", content)
    return write_res.get("status") == "success"


class FixWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    refiner_agent: Agent
    builder_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        pkg_name = state.get("pkg_name")

        if not pkg_name:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Error: No package name specified for the fix workflow.")]
                )
            )
            return

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Starting autonomous package build fix process for '{pkg_name}'...")]
            )
        )

        # Step 1: Perform sandbox build
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Performing sandbox build of package '{pkg_name}'...")]
            )
        )
        build_res = build_package(pkg_name)
        if build_res.get("status") == "success":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Package '{pkg_name}' compiled successfully! ✓")]
                )
            )
            return

        # Step 2: Build failed, scan logs for signatures
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="Build failed. Scanning logs for compiler/linker failure signatures...")]
            )
        )
        scan_res = scan_build_log(pkg_name)
        if scan_res.get("status") == "success" and scan_res.get("signature"):
            sig = scan_res.get("signature")
            proposal = scan_res.get("proposal", {})
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Failure signature matched: {sig}\nProposing: {proposal.get('message')}")]
                )
            )

            # Apply signature patch/env
            if proposal.get("type") == "env_injection":
                env = proposal.get("env", {})
                inject_env_into_manifest(pkg_name, env)
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Injected environment variable to package.manifest: {env}")]
                    )
                )
            elif proposal.get("type") == "patch":
                apply_patch(pkg_name, proposal.get("target_file", "Makefile"), proposal.get("patch_content", ""))
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Applied signature patch.")]
                    )
                )

            # Rebuild
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Rebuilding after signature patch...")]
                )
            )
            build_res = build_package(pkg_name)
            if build_res.get("status") == "success":
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Package '{pkg_name}' compiled successfully after signature auto-patch! ✓")]
                    )
                )
                return

        # Step 3: Run Build & Fixer Agent if build still fails or no signature matched
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="Activating Build & Fixer Agent to diagnose, debug, and repair package inside sandbox...")]
            )
        )

        # Inject operator suggestion into history if present in state
        if state.get("operator_suggestion"):
            ctx.session.events.append(
                Event(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[types.Part(text=f"Operator suggestion to fix the build: {state.get('operator_suggestion')}")]
                    )
                )
            )
            # Clear it so we don't duplicate on next runs
            state["operator_suggestion"] = None

        async for event in self.builder_agent.run_async(ctx):
            yield event

        # Re-check build
        verify_res = verify_package(pkg_name)
        build_res = build_package(pkg_name)

        if verify_res.get("status") == "success" and build_res.get("status") == "success":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Package '{pkg_name}' has been successfully compiled and verified! ✓")]
                )
            )
        else:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Fix process completed, but package '{pkg_name}' still has verification/compilation issues.")]
                )
            )
