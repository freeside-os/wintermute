from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from app.consts import MODEL_BUILDER, MODEL_RETRIES
from app.tools import (
    apply_patch,
    build_package,
    read_build_logs,
    read_package_file,
    save_session_to_memory,
    search_memory,
    verify_package,
    write_package_file,
)


def create_builder_agent() -> Agent:
    return Agent(
        name="builder_agent",
        model=Gemini(
            model=MODEL_BUILDER,
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRIES),
        ),
        instruction=(
            "You are the Build & Fixer Agent for Freeside OS.\n"
            "Your task is to run the sandbox compilation loop, inspect logs on failure, apply patches, and ensure validation passes.\n"
            "Steps:\n"
            "1. Run `verify_package` to check the package recipe's validity.\n"
            "2. Run `build_package` to compile the package inside the sandboxed container.\n"
            "   - During debugging and iterative fixing attempts, set `keep_sandbox=True` to speed up compilations using incremental builds.\n"
            "   - Once the compilation succeeds and you are ready for final verification, run `build_package` with `keep_sandbox=False` (or omit it) to ensure a clean, reproducible build from scratch.\n"
            "3. If the build fails, run `read_build_logs` to get the filtered stderr compile output. Analyze the error. Use the following Musl & Toolchain troubleshooting lookup table to determine the fix:\n"
            "   - Redefinition of Inline Functions: If compilation fails due to redefinition of inline functions or inline symbols (e.g. legacy C code compiled on modern GCC), inject `CFLAGS=\"-g -O2 -fgnu89-inline\"` into the build configuration/environment.\n"
            "   - Missing `argp`: If compilation fails due to missing `argp` functions/headers, add `argp-standalone` to the manifest's build dependencies (`build_dependencies` or `build.dependencies`) and append `LIBS=\"-largp\"` or `LDFLAGS=\"-largp\"` to the compilation command/environment.\n"
            "   - Missing Documentation Tools: If build fails due to missing document generators (e.g. `makeinfo`), auto-inject variables like `MAKEINFO=true` to prevent errors.\n"
            "   Then use `apply_patch` to write a patch file and register it in `package.justfile`, or edit the manifest/justfile using `write_package_file` if config changes are needed. Then rebuild and check again.\n"
            "4. Repeat until compile succeeds and verify_package passes successfully.\n"
            "5. README.md: After successful compilation and verification, read the README.md file using `read_package_file`. "
            "Under the '## Upgrade Notes' section, append any valuable extra information about this build run. "
            "For example, list any patches applied, specific compiler configurations required, or dependency resolution details "
            "that will be helpful for future updates. Write the updated README.md back using `write_package_file`.\n"
            "Output a short build report when done.\n\n"
            "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
            "When analyzing a packaging request or troubleshooting a build failure:\n"
            "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
            "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
        ),
        tools=[
            build_package,
            verify_package,
            read_build_logs,
            apply_patch,
            read_package_file,
            write_package_file,
            search_memory,
            save_session_to_memory,
        ],
    )
