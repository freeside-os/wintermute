from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from app.consts import MODEL_RETRIES, MODEL_TRIAGE
from app.tools import (
    list_packages,
    list_workspace_packages,
    query_security_feeds,
    search_memory,
)


def create_triage_agent() -> Agent:
    return Agent(
        name="triage_agent",
        model=Gemini(
            model=MODEL_TRIAGE,
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRIES),
        ),
        instruction=(
            "You are the Importer/Triage Agent for Freeside OS.\n"
            "Analyze the user request and query security/package feeds to determine package classification and action.\n"
            "First, identify the target package name (or set to 'all' if the request is to review all packages).\n"
            "Determine if the request is a new package 'import' (from Arch Linux), a new package 'create' (from scratch/scaffold), a package build 'fix', a version 'upgrade', a 'review' of package quality/guidelines, a 'security_audit', or 'out_of_scope' for general queries unrelated to Freeside packaging.\n"
            "Use `query_security_feeds` to check if there are high-severity CVEs for the package. "
            "If a high-severity CVE is found and the version is upgraded, classify the package upgrade as a security update (set is_security_update to true).\n"
            "Classify the package into the correct group: base, builder, system, linux, server, desktop, or extra.\n"
            "Use list_workspace_packages if you need to check existing packages.\n"
            "Provide your output in a clean JSON block in your response matching:\n"
            "{\n"
            '  "pkg_name": "package-name" or "all" or null,\n'
            '  "action": "import" or "create" or "fix" or "upgrade" or "review" or "security_audit" or "out_of_scope",\n'
            '  "version": "version-string" or null,\n'
            '  "group": "group-name" or null,\n'
            '  "is_security_update": true or false\n'
            "}\n\n"
            "If the prompt is general conversation, general knowledge, or unrelated to package management/Freeside OS, set action to 'out_of_scope' and pkg_name to null.\n\n"
            "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
            "When analyzing a packaging request or troubleshooting a build failure:\n"
            "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
            "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
        ),
        tools=[list_workspace_packages, list_packages, query_security_feeds, search_memory],
        output_key="triage_output",
    )
