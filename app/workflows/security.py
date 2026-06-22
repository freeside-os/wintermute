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


class SecurityWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    refiner_agent: Agent
    builder_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="Initiating autonomous security and vulnerability audit...")]
            )
        )

        from app.tools import list_workspace_packages, query_security_feeds
        feeds_res = query_security_feeds()
        cves = feeds_res.get("cves", [])

        list_res = list_workspace_packages()
        workspace_pkgs = set(list_res.get("packages", []))

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Retrieved {len(cves)} vulnerability feed entries. Checking workspace match...")]
            )
        )

        active_threats = []
        for cve in cves:
            pkg = cve.get("package")
            if pkg in workspace_pkgs:
                active_threats.append(cve)

        if not active_threats:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="No active package vulnerabilities found in the local workspace. Audit clean. ✓")]
                )
            )
            return

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Found {len(active_threats)} local packages with active CVEs. Starting auto-patching...")]
            )
        )

        success_reports = []
        fail_reports = []

        # We will loop through the vulnerabilities and attempt to upgrade/fix them
        for threat in active_threats:
            pkg = threat.get("package")
            cve_id = threat.get("cve_id")
            severity = threat.get("severity")
            fixed_version = threat.get("fixed_version")

            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Processing {cve_id} ({severity}) affecting package '{pkg}' -> upgrading to fixed version {fixed_version}...")]
                )
            )

            # Build a sub-state and delegate to UpgradeWorkflow
            # Wait, we can run UpgradeWorkflow directly inside this loop by sharing the context
            # and setting the target version/is_security_update dynamically in state.
            state["pkg_name"] = pkg
            state["version"] = fixed_version
            state["is_security_update"] = True

            # Reset workflow execution flags for this package upgrade run
            state["upgrade_done"] = False
            state["refiner_done"] = False
            state["builder_done"] = False
            state["pending_approval"] = False
            state["approved"] = False

            from app.workflows.upgrade import UpgradeWorkflow
            upgrade_wf = UpgradeWorkflow(
                name=f"upgrade_{pkg}",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )

            build_succeeded = False
            try:
                async for event in upgrade_wf.run_async(ctx):
                    yield event

                # Check if build completed successfully
                if state.get("builder_done") and not state.get("pending_approval"):
                    build_succeeded = True
            except Exception as e:
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=f"Error upgrading package {pkg}: {e}")]
                    )
                )

            if build_succeeded:
                success_reports.append(f"{pkg} upgraded to {fixed_version} (Fixes {cve_id})")
                self.send_notification(pkg, cve_id, fixed_version, "SUCCESS")
            else:
                fail_reports.append(f"{pkg} (Failed compiling/verifying version {fixed_version} to fix {cve_id})")
                self.send_notification(pkg, cve_id, fixed_version, "FAILED")

        # Compile final summary
        summary = ["### Security Audit Execution Summary:"]
        if success_reports:
            summary.append("\n**Successfully patched/upgraded:**")
            for rep in success_reports:
                summary.append(f"- ✓ {rep}")
        if fail_reports:
            summary.append("\n**Failed to patch (requires human operator intervention):**")
            for rep in fail_reports:
                summary.append(f"- ✗ {rep}")

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="\n".join(summary))]
            )
        )

    def send_notification(self, pkg: str, cve_id: str, version: str, status: str):
        """Sends a structured notification alert to a webhook (Slack/Discord/Google Chat) if configured."""
        import json
        import os
        import urllib.request

        webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL")
        if not webhook_url:
            print(f"[SECURITY NOTIFICATION] [{status}] Package {pkg} {cve_id} version {version}")
            return

        # Build Slack/Discord-compatible markdown payload
        status_emoji = "✅" if status == "SUCCESS" else "❌"
        message = (
            f"🛡️ **Wintermute Autonomous Patch Alert**\n"
            f"• **Package**: `{pkg}`\n"
            f"• **Vulnerability**: `{cve_id}`\n"
            f"• **Target Version**: `{version}`\n"
            f"• **Status**: {status_emoji} `{status}`"
        )

        payload = {"text": message}
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            print(f"[SECURITY NOTIFICATION ERROR] Failed to send webhook alert: {e}")
