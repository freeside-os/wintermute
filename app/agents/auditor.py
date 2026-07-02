from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.genai import types

from app.consts import MODEL_RETRIES, MODEL_SCAFFOLD
from app.agents.scaffolder import fix_mixed_tools_callback


def create_audit_agent() -> Agent:
    return Agent(
        name="audit_agent",
        model=Gemini(
            model=MODEL_SCAFFOLD,
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRIES),
        ),
        instruction=(
            "You are the Upgrade Auditor Agent for Freeside OS.\n"
            "Your task is to review a list of packages provided by the workflow and output a final upgrade audit report.\n"
            "For each package, the workflow will provide its current version and any known CVEs.\n"
            "Steps:\n"
            "1. Use `google_search` to find the latest stable version and its release date for each package.\n"
            "2. Also search for any potential compatibility or dependency known issues when upgrading from the current version to the latest version.\n"
            "3. Formulate a final recommendation for each package on whether or not it should be upgraded.\n"
            "4. Output a comprehensive Markdown report summarizing all the details clearly.\n"
            "Ensure the report contains:\n"
            "- Package name\n"
            "- Current version in use\n"
            "- Known CVEs (as provided)\n"
            "- Latest stable version and release date\n"
            "- Potential compatibility/dependency known issues\n"
            "- Upgrade recommendation (Yes/No with reasoning)"
        ),
        tools=[google_search],
        before_model_callback=fix_mixed_tools_callback,
    )
