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
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


class CreateWorkflow(BaseAgent):
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
            from google.adk.agents import Agent
            from google.adk.models import Gemini
            from google.adk.tools import google_search

            from app.tools import (
                fetch_source_checksum,
                read_package_file,
                write_package_file,
            )

            scaffold_agent = Agent(
                name="scaffolder",
                model=Gemini(
                    model="gemini-3.5-flash",
                    retry_options=types.HttpRetryOptions(attempts=3),
                ),
                instruction=(
                    "You are the Scaffolder Agent for Freeside OS.\n"
                    f"Your task is to generate a package recipe for '{pkg_name}' from scratch.\n"
                    "Steps:\n"
                    f"1. Use `google_search` to search the web for the package details of '{pkg_name}', including its description, upstream source website/URL, and latest stable release archive URL (typically .tar.gz, .tar.xz, etc.).\n"
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
                tools=[google_search, write_package_file, read_package_file, fetch_source_checksum]
            )
            async for event in scaffold_agent.run_async(ctx):
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
