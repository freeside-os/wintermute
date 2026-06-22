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

from typing import AsyncGenerator
from pydantic import ConfigDict

from google.adk.agents import BaseAgent, Agent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types


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

        # Check if we are waiting for operator approval
        if state.get("pending_approval"):
            last_event = ctx.session.events[-1]
            user_msg = ""
            if last_event.author == "user" and last_event.content and last_event.content.parts:
                user_msg = last_event.content.parts[0].text.lower()
                
            if "yes" in user_msg or "approve" in user_msg:
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
                state["pending_approval"] = False
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Upgrade aborted by operator. Package {pkg_name} was not promoted.")]
                    )
                )
                return

        # Perform upgrade step
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

        # Operator approval or autonomous promotion
        if not is_security_update:
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
                    parts=[types.Part(text=f"Promotion successful! Package {pkg_name} is fully verified and promoted autonomously (Security Upgrade). Workflow complete. ✓")]
                )
            )
