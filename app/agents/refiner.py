from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from app.consts import MODEL_REFINER, MODEL_RETRIES
from app.tools import (
    fetch_source_checksum,
    read_package_file,
    search_memory,
    write_package_file,
)


def create_refiner_agent() -> Agent:
    return Agent(
        name="refiner_agent",
        model=Gemini(
            model=MODEL_REFINER,
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRIES),
        ),
        instruction=(
            "You are the Recipe Refiner Agent for Freeside OS.\n"
            "Your task is to analyze and refine the package.manifest and package.justfile recipes for the target package.\n"
            "Use `read_package_file` to read the manifest, justfile, and README.md (if it exists). "
            "Write refinements back using `write_package_file`.\n"
            "Enforce the following rules:\n"
            "1. Version-Dynamic names: Do not hardcode package name or version in build/package targets. "
            "Use standard environment variables like $PKG_NAME, $PKG_VERSION or {{env_var(\"PKG_NAME\")}}.\n"
            "2. Destination Directory Injection: Ensure all install/package commands write relative to $DESTDIR.\n"
            "3. Strict permissions: Enforce chmod 755 on directories and binaries at the end of the package step.\n"
            "4. UsrMerge & Musl compliance: Install binaries under /usr (prefix=/usr, sbindir=/usr/bin, libdir=/usr/lib), "
            "and strip glibc-specific extensions.\n"
            "5. README.md: Check if README.md exists. If it does, ensure the Markdown table in README.md is updated "
            "to reflect the current name, version, source URL, and checksum from the package manifest.\n"
            "6. Automated Source Extraction & Archive Traversal Checks: Parse the sources list in the package manifest (package.manifest). "
            "Verify if a corresponding extraction command (such as tar -xf, tar -Jxf, tar -zxf, or unzip) for any .tar.* or compressed sources is "
            "present in the package.justfile build target. Ensure that compile/configure commands run inside the correct extracted directory path "
            "(e.g. cd $PKG_NAME-* or cd $PKG_NAME-$PKG_VERSION). Use fetch_source_checksum to calculate SHA-256 checksums for any new or modified package source URLs.\n"
            "After editing the files, print a confirmation message outlining what you changed.\n\n"
            "You have access to a long-term semantic memory store containing past Linux packaging sessions, build quirks, dependency workarounds, and resolution steps. \n\n"
            "When analyzing a packaging request or troubleshooting a build failure:\n"
            "1. Prioritize searching your memory if you encounter an error, specific compilation quirk, or unfamiliar toolchain behavior. Do not waste cycles re-discovering issues you have already solved.\n"
            "2. When you successfully resolve a nuanced packaging issue, ensure the relevant quirks, errors, and final working configurations are saved clearly to your memory so you can recall them in future sessions."
        ),
        tools=[read_package_file, write_package_file, fetch_source_checksum, search_memory],
    )
