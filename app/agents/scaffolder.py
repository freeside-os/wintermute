from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.genai import types

from app.consts import MODEL_RETRIES, MODEL_SCAFFOLD
from app.tools import fetch_source_checksum, read_package_file, write_package_file


def fix_mixed_tools_callback(callback_context, llm_request):
    if llm_request.config and llm_request.config.tools:
        has_builtin = False
        has_custom = False
        for tool in llm_request.config.tools:
            if (
                getattr(tool, "google_search", None)
                or getattr(tool, "code_execution", None)
                or getattr(tool, "google_search_retrieval", None)
                or getattr(tool, "google_maps", None)
            ):
                has_builtin = True
            if getattr(tool, "function_declarations", None):
                has_custom = True
        if has_builtin and has_custom:
            if not llm_request.config.tool_config:
                llm_request.config.tool_config = types.ToolConfig()
            llm_request.config.tool_config.include_server_side_tool_invocations = True


def create_scaffold_agent() -> Agent:
    return Agent(
        name="scaffolder",
        model=Gemini(
            model=MODEL_SCAFFOLD,
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRIES),
        ),
        instruction=(
            "You are the Scaffolder Agent for Freeside OS.\n"
            "Your task is to generate a package recipe from scratch.\n"
            "Steps:\n"
            "1. Use `google_search` to search the web for the package details, including its description, upstream source website/URL, and latest stable release archive URL (typically .tar.gz, .tar.xz, etc.).\n"
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
        tools=[
            google_search,
            write_package_file,
            read_package_file,
            fetch_source_checksum,
        ],
        before_model_callback=fix_mixed_tools_callback,
    )
