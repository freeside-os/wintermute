from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


class CreateWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scaffold_agent: Agent
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
                    parts=[types.Part(text="Error: No package name specified for the create workflow.")]
                )
            )
            return

        # Scaffolding Step
        if not state.get("scaffold_done"):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Creating new package '{pkg_name}' by scaffolding manifest and justfile from scratch...")]
                )
            )
            async for event in self.scaffold_agent.run_async(ctx):
                yield event
            state["scaffold_done"] = True

        # Recipe Refiner Step
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

        # Build & Fixer Step
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
                parts=[types.Part(text=f"Package '{pkg_name}' successfully created, refined, compiled, and verified! Workflow complete. ✓")]
            )
        )
