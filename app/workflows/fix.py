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

from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


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

        # Run Build & Fixer Agent to diagnose build failures, apply patches, and update README.md
        if not state.get("builder_done"):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Activating Build & Fixer Agent to compile, debug, and patch package inside sandbox...")]
                )
            )
            async for event in self.builder_agent.run_async(ctx):
                yield event
            state["builder_done"] = True

        from app.tools import verify_package, build_package
        verify_res = verify_package(pkg_name)
        build_res = build_package(pkg_name)

        if verify_res.get("status") == "success" and build_res.get("status") == "success":
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Package '{pkg_name}' has been successfully compiled and verified! Workflow complete. ✓")]
                )
            )
        else:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Fix process completed, but package '{pkg_name}' still has verification/compilation issues. Please review build logs.")]
                )
            )
