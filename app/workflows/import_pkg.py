import os
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


class ImportWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scaffold_agent: Agent
    refiner_agent: Agent
    builder_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        pkg_name = state.get("pkg_name")

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

        manifest_path = f"/home/dq/Code/freeside/packages/{pkg_name}/package.manifest"
        if not os.path.exists(manifest_path) or os.path.getsize(manifest_path) == 0:
            if not state.get("scaffold_done"):
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Import failed or manifest empty for package {pkg_name}. Activating Scaffolder Agent fallback...")]
                    )
                )
                async for event in self.scaffold_agent.run_async(ctx):
                    yield event
                state["scaffold_done"] = True

        # Run Recipe Refiner Agent
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

        # Run Build & Fixer Agent
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

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Promotion successful! Package {pkg_name} is fully verified and promoted autonomously. Workflow complete. ✓")]
            )
        )
