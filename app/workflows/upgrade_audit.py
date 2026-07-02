import os
import tomllib
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict


class UpgradeAuditWorkflow(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    audit_agent: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        target_pkg = state.get("pkg_name") or "all"
        target_group = state.get("group")

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Initiating upgrade audit flow for target: {target_pkg}...")]
            )
        )

        from app.tools import list_workspace_packages, query_security_feeds

        list_res = list_workspace_packages()
        workspace_pkgs = list_res.get("packages", [])

        # Filter packages
        from app.app_utils.paths import packages_root
        packages_dir = packages_root()
        target_pkgs = []

        for pkg in workspace_pkgs:
            manifest_path = os.path.join(packages_dir, pkg, "package.manifest")
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "rb") as f:
                        data = tomllib.load(f)
                    pkg_block = data.get("package", {})
                    name = pkg_block.get("name") or pkg
                    version = pkg_block.get("version", "")
                    group = pkg_block.get("group", "")

                    if target_pkg == "all" or target_pkg == name or target_pkg == group or target_group == group:
                        target_pkgs.append({"name": name, "version": version, "group": group})
                except Exception as e:
                    pass

        if not target_pkgs:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"No matching packages found for target: {target_pkg}. Aborting audit.")]
                )
            )
            return

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Found {len(target_pkgs)} matching package(s). Querying security feeds for CVEs...")]
            )
        )

        feeds_res = query_security_feeds()
        cves = feeds_res.get("cves", [])

        # Map CVEs to our target packages
        audit_data = []
        for tp in target_pkgs:
            pkg_name = tp["name"]
            pkg_cves = [cve for cve in cves if cve.get("package") == pkg_name]

            cve_summary = "None"
            if pkg_cves:
                cve_summary = ", ".join([f"{c['cve_id']} (Severity: {c['severity']})" for c in pkg_cves])

            audit_data.append(
                f"- Package: {pkg_name}\n"
                f"  - Current Version: {tp['version']}\n"
                f"  - Known CVEs: {cve_summary}\n"
            )

        audit_prompt = (
            "Please perform an upgrade audit for the following packages based on their current state:\n\n"
            + "\n".join(audit_data) +
            "\n\nSearch for their latest stable release versions, release dates, and any potential compatibility/dependency known issues when upgrading, and provide your final recommendation."
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text="Delegating to Audit Agent for research and report generation...")]
            )
        )

        # Append the audit instruction to the context events and run the audit agent
        ctx.session.events.append(
            Event(
                author="workflow",
                content=types.Content(
                    role="user",
                    parts=[types.Part(text=audit_prompt)]
                )
            )
        )

        async for event in self.audit_agent.run_async(ctx):
            yield event
