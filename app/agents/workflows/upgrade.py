from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict

from app.tools import apply_patch, build_package, scan_build_log, verify_package
from app.agents.workflows.fix import inject_env_into_manifest


class UpgradeWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    refiner_agent: Agent
    builder_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        pkg_name = state.get("pkg_name")
        version = state.get("version")
        is_security_update = state.get("is_security_update", False)



        # 1. Perform upgrade step
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

        # 2. Recipe Refiner Step
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

        # 3. Compilation & Self-Healing Step
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Compiling package '{pkg_name}' inside container sandbox...")]
            )
        )
        build_res = build_package(pkg_name)
        if build_res.get("status") != "success":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Build failed. Scanning logs for failure signatures...")]
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
                        parts=[types.Part(text=f"Matched failure signature: {sig}\nApplying: {proposal.get('message')}")]
                    )
                )
                if proposal.get("type") == "env_injection":
                    inject_env_into_manifest(pkg_name, proposal.get("env", {}))
                elif proposal.get("type") == "patch":
                    apply_patch(pkg_name, proposal.get("target_file", "Makefile"), proposal.get("patch_content", ""))

                # Rebuild
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Rebuilding after signature fix...")]
                    )
                )
                build_res = build_package(pkg_name)

        if build_res.get("status") != "success":
            # Call builder agent to fix
            if not state.get("builder_done"):
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Build still failing. Activating Build & Fixer Agent to compile and patch package inside sandbox...")]
                    )
                )
                async for event in self.builder_agent.run_async(ctx):
                    yield event
                state["builder_done"] = True

        # Verify build
        verify_res = verify_package(pkg_name)
        build_res = build_package(pkg_name)

        if verify_res.get("status") != "success" or build_res.get("status") != "success":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Package '{pkg_name}' upgraded, but has verification/compilation issues. Upgrade verification failed.")]
                )
            )
            return

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Promotion successful! Package {pkg_name} is fully verified and promoted. Workflow complete. ✓")]
            )
        )
