import json
import logging
import os
import re
import warnings
from collections.abc import AsyncGenerator
from typing import Any

# Suppress experimental and deprecation warnings originating from google.adk submodules
warnings.filterwarnings("ignore", category=UserWarning, module=r"google\.adk\..*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"google\.adk\..*")

# Suppress warnings from google.genai logger (e.g. AFC warnings when mixing tool types)
logging.getLogger("google.genai").setLevel(logging.ERROR)

import google.auth  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from google.adk.agents import Agent, Context  # noqa: E402
from google.adk.apps import App  # noqa: E402
from google.adk.events import Event  # noqa: E402
from google.adk.models import Gemini  # noqa: E402
from google.adk.workflow import BaseNode  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import ConfigDict  # noqa: E402

# Load environment using python-dotenv
load_dotenv()

# Set GOOGLE_API_KEY/GEMINI_API_KEY mapping
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
elif os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]


# ------------------------------------------------------------------------------
# 1. Importer / Triage Agent
# ------------------------------------------------------------------------------
from app.agents import (  # noqa: E402
    create_audit_agent,
    create_builder_agent,
    create_refiner_agent,
    create_scaffold_agent,
    create_triage_agent,
)

triage_agent = create_triage_agent()
refiner_agent = create_refiner_agent()
builder_agent = create_builder_agent()
scaffold_agent = create_scaffold_agent()
audit_agent = create_audit_agent()

# ------------------------------------------------------------------------------
# 4. Master Workflow Coordinator
# ------------------------------------------------------------------------------
class Workflow(BaseNode):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    triage_agent: Agent
    refiner_agent: Agent
    builder_agent: Agent
    scaffold_agent: Agent
    audit_agent: Agent

    async def _run_impl(
        self,
        *,
        ctx: Context,
        node_input: Any,
    ) -> AsyncGenerator[Event, None]:
        ic = ctx.get_invocation_context()
        state = ic.session.state

        # Check if we are waiting for operator approval ( UpgradeWorkflow handles approval )
        if state.get("pending_approval"):
            from app.workflows.upgrade import UpgradeWorkflow
            wf = UpgradeWorkflow(
                name="upgrade_router",
                refiner_agent=self.refiner_agent,
                builder_agent=self.builder_agent
            )
            async for event in wf.run_async(ic):
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
            async for event in self.triage_agent.run_async(ic):
                yield event

            # Extract output and set variables in state
            triage_text = state.get("triage_output", "")
            if not triage_text:
                for ev in reversed(ic.session.events):
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
                for ev in ic.session.events:
                    if ev.author == "user" and ev.content and ev.content.parts:
                        user_query = ev.content.parts[0].text
                        break
                if "upgrade audit" in user_query.lower():
                    action = "upgrade_audit"
                    if "all" in user_query.lower():
                        pkg_name = "all"
                    else:
                        pkg_match = re.search(r"(?:upgrade audit)\s+(?:package\s+)?([a-zA-Z0-9_\-]+)", user_query, re.IGNORECASE)
                        if pkg_match:
                            pkg_name = pkg_match.group(1)
                        else:
                            pkg_name = state.get("pkg_name") or "all"
                elif "security" in user_query.lower() or "cve" in user_query.lower():
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

        # If package exists and action is create/import, override to review
        if action in ("create", "import") and pkg_name and pkg_name != "all":
            from app.tools import list_workspace_packages
            try:
                workspace_pkgs = set(list_workspace_packages().get("packages", []))
                if pkg_name in workspace_pkgs:
                    yield Event(
                        author=self.name,
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text=f"Package '{pkg_name}' already exists in workspace. Overriding action '{action}' to 'review'...")]
                        )
                    )
                    action = "review"
                    state["action"] = "review"
            except Exception:
                pass

        if action == "out_of_scope" or (not pkg_name and action not in ("security_audit", "upgrade_audit")):
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="I am the Wintermute packaging agent, designed to create, import, fix, upgrade, review, or audit packages for Freeside OS. This request seems out of scope. Please ask a package management or OS maintenance query.")]
                )
            )
            return

        # Route to appropriate workflow subclass
        if action == "upgrade_audit":
            from app.workflows.upgrade_audit import UpgradeAuditWorkflow
            workflow = UpgradeAuditWorkflow(
                name="upgrade_audit_workflow",
                audit_agent=self.audit_agent
            )
        elif action == "security_audit":
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

        async for event in workflow.run_async(ic):
            yield event

        # Print the final token totals
        input_tokens = state.get("total_input_tokens", 0)
        output_tokens = state.get("total_output_tokens", 0)
        print(f"Total tokens sent: {input_tokens}, received: {output_tokens}")

from google.adk.agents.context_cache_config import ContextCacheConfig  # noqa: E402
from google.adk.apps.app import EventsCompactionConfig  # noqa: E402
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer  # noqa: E402
from google.adk.plugins.reflect_retry_tool_plugin import (  # noqa: E402
    ReflectAndRetryToolPlugin,
)
from .plugins.logging import LoggingPlugin
from .plugins.tokens import TokenTrackingPlugin

from app.consts import (  # noqa: E402
    COMPACTION_INTERVAL,
    COMPACTION_OVERLAP,
    CONTEXT_CACHE_INTERVALS,
    CONTEXT_CACHE_MIN_TOKENS,
    CONTEXT_CACHE_TTL_SECONDS,
    MODEL_COMPACTION,
    TOOL_MAX_RETRIES,
)

# Instantiate root Workflow coordinator agent
root_agent = Workflow(
    name="root_agent",
    triage_agent=triage_agent,
    refiner_agent=refiner_agent,
    builder_agent=builder_agent,
    scaffold_agent=scaffold_agent,
    audit_agent=audit_agent
)

app = App(
    root_agent=root_agent,
    name="app",
    context_cache_config=ContextCacheConfig(
        min_tokens=CONTEXT_CACHE_MIN_TOKENS,
        ttl_seconds=CONTEXT_CACHE_TTL_SECONDS,
        cache_intervals=CONTEXT_CACHE_INTERVALS,
    ),
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=COMPACTION_INTERVAL,
        overlap_size=COMPACTION_OVERLAP,
        summarizer=LlmEventSummarizer(llm=Gemini(model=MODEL_COMPACTION)),
    ),
    plugins=[
        ReflectAndRetryToolPlugin(max_retries=TOOL_MAX_RETRIES),
        LoggingPlugin(),
        TokenTrackingPlugin(),
    ]
)
