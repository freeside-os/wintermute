---
name: fstools-dev
description: Develop and test fstools Python CLI commands (pkg, image, initrd, sandbox).
---

# Freeside `fstools` Development

Use this skill when adding or modifying subcommands under `fstools/src/fstools/`.

## Workflow
- Write `pytest` tests in `fstools/tests/` covering CLI flags, exit codes, and edge cases before implementation.
- Validate with `just_fstools_test` and `just_fstools_build`.
- Keep package and image helpers modular, typed, and free of host environment leakage.

